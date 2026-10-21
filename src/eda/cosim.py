"""Firmware-in-the-loop co-simulation (level 2): the RP2040 emulator and ngspice in lock-step.

Time advances in windows of the test's `step` (ngspice lands exactly on each boundary).
At each window boundary:

1. the circuit's node voltages at the MCU pins become the firmware's inputs: digital
   levels through Schmitt thresholds, ADC codes (12 bit against the I/O voltage);
2. the emulator runs the firmware to the end of the window and reports every pin state
   change (output high/low, input with pull-up/pull-down, high impedance);
3. each pin's states over the window become one Thevenin source (time-weighted average
   of conductance and current, so PWM shows up as its average), and ngspice simulates
   the window.

When the firmware changes a pin's mode (enables a pull-up, turns a pin into an output,
...) the emulator stops right there and the circuit settles on the new state for
window/100 before the firmware continues, so code that configures a pin and reads it
right away sees the circuit's answer.

Pins configured as GPIO (`mode:`) or PWM are driven by the firmware; other peripheral
pins (UART, SPI, I2C) keep their static model from firmware.yaml, and UART output is
captured as text.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

from eda.adapters import ngspice_shared, rp2040
from eda.display import Screen
from eda.display.hd44780 import Hd44780Device
from eda.model import GROUND, CompiledTest, Design, ResolvedCircuit, Tran
from eda.netlist.spice import (
    alter_commands,
    build_netlist,
    fw_driver,
    needs_pspice_mode,
    optran_command,
    voltage_vector,
)
from eda.schema.part import Hd44780DisplaySpec, McuSpec

HIZ_OHM = 1e8
# RP2040 input thresholds at IOVDD = 3.3 V: VIH 2.0 V, VIL 0.8 V (datasheet §5.5.3).
VIH_FRACTION = 2.0 / 3.3
VIL_FRACTION = 0.8 / 3.3
ADC_MAX = 4095
MAX_STEP_PER_WINDOW = 5  # ngspice steps at least five times per window
SETTLE_PER_WINDOW = 100  # after a pin mode change the circuit runs window/100 first


class CosimError(RuntimeError):
    pass


@dataclass
class CosimResult:
    windows: int
    uart: dict[int, str]  # UART index → everything the firmware sent
    halted: int | None
    halted_at: float | None = None  # s
    faults: list[tuple[float, str]] = field(default_factory=lambda: list[tuple[float, str]]())
    warnings: list[str] = field(default_factory=lambda: list[str]())
    op_via_transient: bool = False
    screens: dict[str, Screen] = field(default_factory=lambda: dict[str, Screen]())
    display_issues: list[tuple[str, str]] = field(default_factory=lambda: list[tuple[str, str]]())


@dataclass(frozen=True)
class _Pin:
    net: str
    gpio: int
    source: str  # Thevenin voltage source element
    resistor: str


def run(design: Design, test: CompiledTest, image: Path | None, raw_path: Path) -> CosimResult:
    """Run a stepped tran test (`firmware: true` and/or `events:`) and write its raw file.

    `image` is the firmware UF2; it is needed only when the test runs the firmware."""
    analysis = test.analysis
    assert isinstance(analysis, Tran)
    circuit = test.circuit(design.circuit)
    mcu_ref, drives, pins, adc, uarts = _firmware_pins(design) if test.firmware else _NO_MCU
    netlist = build_netlist(
        f"{design.project.name} / {test.name} (stepped)",
        circuit,
        analysis,
        raw_path,
        {mcu_ref: drives} if mcu_ref else None,
        batch=False,
    )
    raw_path.with_suffix(".cir").write_text(netlist, encoding="utf-8")
    window = analysis.step
    settle = window / SETTLE_PER_WINDOW
    start = analysis.start or 0.0
    command = (
        f"tran {window:.12g} {analysis.stop:.12g} {start:.12g} {window / MAX_STEP_PER_WINDOW:.12g}"
    )
    events = list(test.events)
    state = {ref: part for ref, part in circuit.parts.items()}
    levels: dict[int, bool] = {}
    uart: dict[int, str] = {}
    send: dict[int, str] = {}
    halted: int | None = None
    halted_at: float | None = None
    faults: list[tuple[float, str]] = []
    count = 0

    try:
        with ExitStack() as stack:
            sim = stack.enter_context(ngspice_shared.session(netlist, needs_pspice_mode(circuit)))
            emulator: rp2040.Emulator | None = None
            if test.firmware:
                assert image is not None
                log = raw_path.with_suffix(".emulator.log")
                emulator = stack.enter_context(
                    rp2040.Emulator(image, [p.gpio for p in pins.values()], uarts, log)
                )
            mcu = design.circuit.parts[mcu_ref].spec.mcu if mcu_ref else None
            thevenin = _Thevenin(mcu) if mcu else None
            devices = _devices(circuit, pins, mcu.vio) if mcu and emulator else []
            sampled = sorted({net for device in devices for net in device.nets})
            applied: dict[str, tuple[float, float]] = {}

            def apply(pin: _Pin, volts: float, ohms: float) -> None:
                if applied.get(pin.source) != (volts, ohms):
                    sim.command(f"alter {pin.source} dc = {volts:.12g}")
                    sim.command(f"alter {pin.resistor} resistance = {ohms:.12g}")
                    applied[pin.source] = (volts, ohms)

            def advance(until: float) -> float:
                if until < analysis.stop * (1 - 1e-9):
                    return sim.run_until(until)
                sim.command("delete all")
                sim.command("resume")  # run to the end of the analysis
                return analysis.stop

            def fire_due(now: float) -> None:
                while events and events[0].time <= now * (1 + 1e-9) + 1e-15:
                    event = events.pop(0)
                    if event.part is not None:
                        old = state[event.part]
                        new = replace(
                            old,
                            values={**old.values, **{k: v for k, (v, _) in event.values.items()}},
                        )
                        for alter in alter_commands(old, new, circuit):
                            sim.command(alter)
                        state[event.part] = new
                    elif event.uart is not None:
                        send[event.uart] = send.get(event.uart, "") + event.text

            if thevenin is not None:
                for pin in pins.values():
                    apply(pin, *thevenin.state("pulldown"))  # GPIO reset state
            first = events[0].time if events and events[0].time > 0 else settle
            optran = optran_command(circuit)
            if optran:
                sim.command(optran)
            now = sim.start(command, min(settle, first))
            fire_due(now)
            while now < analysis.stop * (1 - 1e-9):
                target = min(now + window, analysis.stop)
                if events:
                    target = min(target, max(events[0].time, now))
                if emulator is None or thevenin is None or mcu is None:
                    count += 1
                    now = advance(target)
                    fire_due(now)
                    continue
                inputs = {
                    p.gpio: _level(levels, p.gpio, _volts(sim, p.net), mcu.vio)
                    for p in pins.values()
                }
                codes = {ch: _adc_code(_volts(sim, net), mcu.vio) for ch, net in adc.items()}
                result = emulator.run(target, inputs, codes, break_on_mode=True, send=send)
                send = {}
                for index, text in result.uart.items():
                    uart[index] = uart.get(index, "") + text
                if halted is None and result.halted is not None:
                    halted, halted_at = result.halted, result.halted_at
                faults += result.faults[: 10 - len(faults)]
                for pin in pins.values():
                    segments = result.pins.get(pin.gpio, [])
                    apply(pin, *thevenin.average(segments, result.end, inputs[pin.gpio]))
                count += 1
                window_start = now
                if result.end > now:
                    now = advance(result.end)
                if devices:
                    volts = {net: _volts(sim, net) for net in sampled}
                    gpio = {p.net: result.pins.get(p.gpio, []) for p in pins.values()}
                    for device in devices:
                        device.window(window_start, now, gpio, volts)
                if result.early:
                    # A pin changed mode: let the circuit settle on the new state before the
                    # firmware reads its inputs again.
                    for pin in pins.values():
                        segments = result.pins.get(pin.gpio, [])
                        if segments:
                            apply(pin, *thevenin.state(segments[-1][1], inputs[pin.gpio]))
                    now = advance(now + settle)
                fire_due(now)
            sim.write_raw(raw_path)
            warnings = sim.warnings()
            via_transient = sim.op_via_transient
            screens: dict[str, Screen] = {d.ref: d.screen(analysis.stop) for d in devices}
            issues = [issue for device in devices for issue in device.issues()]
    except (ngspice_shared.SharedSpiceError, rp2040.EmulatorError, ValueError) as exc:
        raise CosimError(str(exc)) from exc
    return CosimResult(
        windows=count,
        uart=uart,
        halted=halted,
        halted_at=halted_at,
        faults=faults,
        warnings=warnings,
        op_via_transient=via_transient,
        screens=screens,
        display_issues=issues,
    )


type _McuSetup = tuple[str | None, dict[str, str], dict[str, _Pin], dict[int, str], list[int]]
_NO_MCU: _McuSetup = (None, {}, {}, {}, [])


def _firmware_pins(design: Design) -> _McuSetup:
    """(MCU ref, pin drives, firmware-driven pins, ADC channel → net, UART TX indexes)."""
    firmware = design.firmware
    assert firmware is not None
    part = design.circuit.parts[firmware.mcu]
    connected = [p for p in part.spec.pins if design.circuit.net_of(part.ref, p) is not None]
    drives = firmware.drives()
    pins: dict[str, _Pin] = {}
    adc: dict[int, str] = {}
    uarts: list[int] = []
    for signal in firmware.signals.values():
        function = signal.function or ""
        if signal.mode is not None or function.startswith("PWM"):
            drives[signal.pin] = "fw"
            source, resistor = fw_driver(part, signal.pin, connected)
            pins[signal.pin] = _Pin(signal.net, signal.gpio, source, resistor)
        elif function.startswith("ADC"):
            adc[int(function.removeprefix("ADC"))] = signal.net
        elif function.startswith("UART") and function.endswith("_TX"):
            uarts.append(int(function[4]))
    return firmware.mcu, drives, pins, adc, uarts


def _devices(circuit: ResolvedCircuit, pins: dict[str, _Pin], vio: float) -> list[Hd44780Device]:
    """The display controllers the firmware writes to, fed after every window."""
    driven = {p.net for p in pins.values()}
    return [
        Hd44780Device(part, circuit, vio, driven)
        for part in circuit.parts.values()
        if isinstance(part.spec.display, Hd44780DisplaySpec)
    ]


def _volts(sim: ngspice_shared.Session, net: str) -> float:
    if net == GROUND:
        return 0.0
    vector = voltage_vector(net)
    assert vector is not None
    return sim.last(vector)


def _level(levels: dict[int, bool], gpio: int, volts: float, vio: float) -> bool:
    """Schmitt-trigger input: keeps its level between VIL and VIH."""
    previous = levels.get(gpio, volts > vio / 2)
    if volts >= VIH_FRACTION * vio:
        level = True
    elif volts <= VIL_FRACTION * vio:
        level = False
    else:
        level = previous
    levels[gpio] = level
    return level


def _adc_code(volts: float, vref: float) -> int:
    return max(0, min(ADC_MAX, round(volts / vref * ADC_MAX)))


class _Thevenin:
    """Pin state → (open-circuit volts, source ohms) for one MCU."""

    def __init__(self, mcu: McuSpec) -> None:
        self.vio = mcu.vio
        self.drive = mcu.drive_ohm
        self.pull = mcu.pull_ohm

    def state(self, name: str, level: bool = False) -> tuple[float, float]:
        if name == "high":
            return self.vio, self.drive
        if name == "low":
            return 0.0, self.drive
        if name == "pullup":
            return self.vio, self.pull
        if name == "pulldown":
            return 0.0, self.pull
        if name == "keeper":  # bus keeper: weakly holds the last level
            return (self.vio if level else 0.0), self.pull
        return 0.0, HIZ_OHM

    def average(self, segments: rp2040.Segments, end: float, level: bool) -> tuple[float, float]:
        """Norton average over the window: G = mean(1/R), I = mean(V/R) → (I/G, 1/G)."""
        if not segments:
            return self.state("hiz")
        if len(segments) == 1 or end <= segments[0][0]:
            return self.state(segments[-1][1], level)
        span = end - segments[0][0]
        conductance = current = 0.0
        for i, (start, name) in enumerate(segments):
            stop = segments[i + 1][0] if i + 1 < len(segments) else end
            volts, ohms = self.state(name, level)
            weight = max(0.0, stop - start) / span
            conductance += weight / ohms
            current += weight * volts / ohms
        return current / conductance, 1 / conductance
