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

import re
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path

from eda.adapters import ngspice_shared, rp2040
from eda.display import Screen
from eda.display.hd44780 import Hd44780Device
from eda.display.ssd1306 import Ssd1306Device
from eda.model import GROUND, CompiledTest, Design, ResolvedCircuit, Tran
from eda.netlist.spice import (
    alter_commands,
    build_netlist,
    fw_driver,
    needs_pspice_mode,
    optran_command,
    voltage_vector,
)
from eda.schema.part import Hd44780DisplaySpec, McuSpec, Ssd1306DisplaySpec
from eda.units import format_quantity

HIZ_OHM = 1e8
# RP2040 input thresholds at IOVDD = 3.3 V: VIH 2.0 V, VIL 0.8 V (datasheet §5.5.3).
VIH_FRACTION = 2.0 / 3.3
VIL_FRACTION = 0.8 / 3.3
ADC_MAX = 4095
MAX_STEP_PER_WINDOW = 5  # ngspice steps at least five times per window
SETTLE_PER_WINDOW = 100  # after a pin mode change the circuit runs window/100 first


class CosimError(RuntimeError):
    pass


def _saved_vectors(netlist: str, circuit: ResolvedCircuit) -> list[str]:
    """v() of every net and i() of every top-level voltage source (the pin sense sources
    among them)."""
    nets = [v for v in (voltage_vector(net) for net in circuit.nets) if v is not None]
    sources = [
        f"i({line.split()[0].lower()})"
        for line in netlist.splitlines()
        if line[:1] in "Vv" and len(line.split()) > 1
    ]
    return nets + sources


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
    setup = _firmware_pins(design) if test.firmware else _McuSetup()
    mcu_ref, drives, pins, adc, uarts = setup.mcu, setup.drives, setup.pins, setup.adc, setup.uarts
    mcu = design.circuit.parts[mcu_ref].spec.mcu if mcu_ref else None
    devices = _devices(circuit, setup) if mcu else []
    i2c_devices: dict[int, list[int]] = {}
    for device in devices:
        if isinstance(device, Ssd1306Device) and device.bus is not None:
            i2c_devices.setdefault(device.bus, []).append(device.address)
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
                    rp2040.Emulator(image, [p.gpio for p in pins.values()], uarts, log, i2c_devices)
                )
            thevenin = _Thevenin(mcu) if mcu else None
            sampled = sorted({net for device in devices for net in device.nets})
            bus = _BusWatch(setup, devices, mcu.vio) if mcu else None
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
            # Only the circuit's nets and source currents (measures name nothing else),
            # not the nodes inside subcircuits.
            sim.save(_saved_vectors(netlist, circuit))
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
                assert bus is not None
                healthy = bus.healthy({net: _volts(sim, net) for net in bus.nets})
                result = emulator.run(
                    target, inputs, codes, break_on_mode=True, send=send, i2c_ok=healthy
                )
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
                bus.transfers(result.i2c)
                bus.check_pins({p.net: _volts(sim, p.net) for p in pins.values()}, now)
                if devices:
                    volts = {net: _volts(sim, net) for net in sampled}
                    gpio = {p.net: result.pins.get(p.gpio, []) for p in pins.values()}
                    for device in devices:
                        device.window(window_start, now, gpio, volts, result.i2c)
                if result.early:
                    # A pin changed mode: let the circuit settle on the new state before the
                    # firmware reads its inputs again.
                    for pin in pins.values():
                        segments = result.pins.get(pin.gpio, [])
                        if segments:
                            apply(pin, *thevenin.state(segments[-1][1], inputs[pin.gpio]))
                    now = advance(now + settle)
                fire_due(now)
            # One point per window: the firmware's pins change only at window boundaries,
            # and ngspice's own points (about ten per window after each halt) would make
            # long runs gigabytes.
            sim.write_raw(raw_path, grid=True)
            warnings = sim.warnings()
            via_transient = sim.op_via_transient
            screens: dict[str, Screen] = {d.ref: d.screen(analysis.stop) for d in devices}
            issues = [issue for device in devices for issue in device.issues()]
            issues += bus.issues() if bus else []
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


@dataclass
class _McuSetup:
    mcu: str | None = None  # part reference
    drives: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    pins: dict[str, _Pin] = field(default_factory=lambda: dict[str, _Pin]())  # firmware-driven
    adc: dict[int, str] = field(default_factory=lambda: dict[int, str]())  # channel → net
    uarts: list[int] = field(default_factory=lambda: list[int]())  # UART TX indexes
    i2c: dict[int, dict[str, str]] = field(  # bus → {"sda": net, "scl": net}
        default_factory=lambda: dict[int, dict[str, str]]()
    )


def _firmware_pins(design: Design) -> _McuSetup:
    """What the firmware drives and reads. GPIO (`mode:`), PWM, and I2C pins follow the
    emulated pin states (I2C pins only pull: the transfers themselves are not drawn as
    waveforms); ADC channels and UART TX are exchanged as values and text."""
    firmware = design.firmware
    assert firmware is not None
    part = design.circuit.parts[firmware.mcu]
    connected = [p for p in part.spec.pins if design.circuit.net_of(part.ref, p) is not None]
    setup = _McuSetup(mcu=firmware.mcu, drives=firmware.drives())
    for signal in firmware.signals.values():
        function = signal.function or ""
        i2c = re.fullmatch(r"I2C(\d)_(SDA|SCL)", function)
        if signal.mode is not None or function.startswith("PWM") or i2c:
            setup.drives[signal.pin] = "fw"
            source, resistor = fw_driver(part, signal.pin, connected)
            setup.pins[signal.pin] = _Pin(signal.net, signal.gpio, source, resistor)
        if i2c:
            setup.i2c.setdefault(int(i2c[1]), {})[i2c[2].lower()] = signal.net
        elif function.startswith("ADC"):
            setup.adc[int(function.removeprefix("ADC"))] = signal.net
        elif function.startswith("UART") and function.endswith("_TX"):
            setup.uarts.append(int(function[4]))
    return setup


type _Device = Hd44780Device | Ssd1306Device


def _devices(circuit: ResolvedCircuit, setup: _McuSetup) -> list[_Device]:
    """The display controllers the firmware writes to, fed after every window."""
    assert setup.mcu is not None
    mcu = circuit.parts[setup.mcu].spec.mcu
    assert mcu is not None
    vio = mcu.vio
    driven = {p.net for p in setup.pins.values()}
    found: list[_Device] = []
    for part in circuit.parts.values():
        spec = part.spec.display
        if isinstance(spec, Hd44780DisplaySpec):
            found.append(Hd44780Device(part, circuit, vio, driven))
        elif isinstance(spec, Ssd1306DisplaySpec):
            sda = circuit.net_of(part.ref, spec.pins["sda"])
            scl = circuit.net_of(part.ref, spec.pins["scl"])
            bus = next(
                (
                    n
                    for n, nets in setup.i2c.items()
                    if nets.get("sda") == sda and nets.get("scl") == scl
                ),
                None,
            )
            found.append(Ssd1306Device(part, circuit, bus))
    return found


OVERVOLTAGE_V = 0.3  # above the I/O supply: the pin's protection diode conducts


class _BusWatch:
    """I2C bus health and MCU pin voltages during a co-simulation."""

    def __init__(self, setup: _McuSetup, devices: list[_Device], vio: float) -> None:
        self.setup = setup
        self.vio = vio
        self.addresses: dict[int, dict[int, str]] = {}  # bus → address → device
        self.threshold: dict[int, float] = {}
        for device in devices:
            if isinstance(device, Ssd1306Device) and device.bus is not None:
                self.addresses.setdefault(device.bus, {})[device.address] = device.ref
                self.threshold[device.bus] = max(self.threshold.get(device.bus, 0.0), device.vih)
        self.nets = sorted({net for nets in setup.i2c.values() for net in nets.values()})
        self.idle: dict[int, tuple[float, float]] = {}
        self.notes: dict[str, str] = {}

    def healthy(self, volts: dict[str, float]) -> dict[int, bool]:
        """Per bus: SDA and SCL idle high enough for the MCU and every device on it."""
        found: dict[int, bool] = {}
        for bus, nets in self.setup.i2c.items():
            sda, scl = (volts.get(nets.get(line, ""), 0.0) for line in ("sda", "scl"))
            self.idle[bus] = (sda, scl)
            need = max(VIH_FRACTION * self.vio, self.threshold.get(bus, 0.0))
            found[bus] = min(sda, scl) >= need
        return found

    def transfers(self, transfers: tuple[rp2040.I2cTransfer, ...]) -> None:
        for transfer in transfers:
            if transfer.acked:
                continue
            here = self.addresses.get(transfer.bus, {})
            sda, scl = self.idle.get(transfer.bus, (0.0, 0.0))
            when = format_quantity(transfer.time, "s")
            if transfer.address in here:
                need = max(VIH_FRACTION * self.vio, self.threshold.get(transfer.bus, 0.0))
                self.notes.setdefault(
                    "i2c_bus",
                    f"at {when} the firmware addressed {here[transfer.address]} "
                    f"(0x{transfer.address:02X}) on I2C{transfer.bus}, but the bus idles at "
                    f"SDA {format_quantity(sda, 'V')} / SCL {format_quantity(scl, 'V')} "
                    f"(needs {format_quantity(need, 'V')}): pull-ups missing or too weak, so "
                    "nothing answers",
                )
            else:
                there = ", ".join(f"{ref} at 0x{a:02X}" for a, ref in here.items()) or "none"
                self.notes.setdefault(
                    "i2c_nack",
                    f"at {when} the firmware addressed 0x{transfer.address:02X} on "
                    f"I2C{transfer.bus}; no device answers there (devices: {there})",
                )

    def check_pins(self, volts: dict[str, float], time: float) -> None:
        for net, value in volts.items():
            if value > self.vio + OVERVOLTAGE_V:
                self.notes.setdefault(
                    f"mcu_pin_overvoltage:{net}",
                    f"{net} reached {format_quantity(value, 'V')} at "
                    f"{format_quantity(time, 's')}, above the MCU's I/O supply "
                    f"{format_quantity(self.vio, 'V')} + {OVERVOLTAGE_V} V (its GPIOs are "
                    "not 5 V tolerant)",
                )

    def issues(self) -> list[tuple[str, str]]:
        return [(rule.split(":")[0], message) for rule, message in self.notes.items()]


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
