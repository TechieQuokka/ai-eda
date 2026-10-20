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

from dataclasses import dataclass, field
from pathlib import Path

from eda.adapters import ngspice_shared, rp2040
from eda.model import GROUND, CompiledTest, Design, Tran
from eda.netlist.spice import build_netlist, fw_driver, needs_pspice_mode, voltage_vector
from eda.schema.part import McuSpec

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
    warnings: list[str] = field(default_factory=lambda: list[str]())


@dataclass(frozen=True)
class _Pin:
    net: str
    gpio: int
    source: str  # Thevenin voltage source element
    resistor: str


def run(design: Design, test: CompiledTest, image: Path, raw_path: Path) -> CosimResult:
    """Co-simulate `test` (a tran test with `firmware: true`) and write its raw file."""
    firmware = design.firmware
    analysis = test.analysis
    assert firmware is not None and isinstance(analysis, Tran)
    part = design.circuit.parts[firmware.mcu]
    mcu = part.spec.mcu
    assert mcu is not None

    connected = [p for p in part.spec.pins if design.circuit.net_of(part.ref, p) is not None]
    drives = firmware.drives()
    pins: dict[str, _Pin] = {}
    adc: dict[int, str] = {}  # ADC channel → net
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

    netlist = build_netlist(
        f"{design.project.name} / {test.name} (co-simulation)",
        test.circuit(design.circuit),
        analysis,
        raw_path,
        {firmware.mcu: drives},
        batch=False,
    )
    raw_path.with_suffix(".cir").write_text(netlist, encoding="utf-8")
    window = analysis.step
    tmax = window / MAX_STEP_PER_WINDOW
    settle = window / SETTLE_PER_WINDOW
    start = analysis.start or 0.0
    command = f"tran {window:.12g} {analysis.stop:.12g} {start:.12g} {tmax:.12g}"
    thevenin = _Thevenin(mcu)
    levels: dict[int, bool] = {}
    uart: dict[int, str] = {}
    halted: int | None = None
    count = 0
    log = raw_path.with_suffix(".emulator.log")

    try:
        with (
            ngspice_shared.session(netlist, needs_pspice_mode(design.circuit)) as sim,
            rp2040.Emulator(image, [p.gpio for p in pins.values()], uarts, log) as emulator,
        ):
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

            for pin in pins.values():
                apply(pin, *thevenin.state("pulldown"))  # GPIO reset state
            now = sim.start(command, settle)
            while now < analysis.stop * (1 - 1e-9):
                inputs = {
                    p.gpio: _level(levels, p.gpio, _volts(sim, p.net), mcu.vio)
                    for p in pins.values()
                }
                codes = {ch: _adc_code(_volts(sim, net), mcu.vio) for ch, net in adc.items()}
                target = min(now + window, analysis.stop)
                result = emulator.run(target, inputs, codes, break_on_mode=True)
                for index, text in result.uart.items():
                    uart[index] = uart.get(index, "") + text
                halted = result.halted if halted is None else halted
                for pin in pins.values():
                    segments = result.pins.get(pin.gpio, [])
                    apply(pin, *thevenin.average(segments, result.end, inputs[pin.gpio]))
                count += 1
                if result.end > now:
                    now = advance(result.end)
                if result.early:
                    # A pin changed mode: let the circuit settle on the new state before the
                    # firmware reads its inputs again.
                    for pin in pins.values():
                        segments = result.pins.get(pin.gpio, [])
                        if segments:
                            apply(pin, *thevenin.state(segments[-1][1], inputs[pin.gpio]))
                    now = advance(now + settle)
            sim.write_raw(raw_path)
            warnings = sim.warnings()
    except (ngspice_shared.SharedSpiceError, rp2040.EmulatorError) as exc:
        raise CosimError(str(exc)) from exc
    return CosimResult(windows=count, uart=uart, halted=halted, warnings=warnings)


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
