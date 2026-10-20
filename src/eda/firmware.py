"""Hardware-firmware integration (level 1, static): pin roles, pins.h, cross-checks.

The schematic decides which GPIO a net reaches; `design/firmware.yaml` says how firmware
uses that net. From both we check the pin can do the job, find conflicts, generate
`firmware/pins.h` (+ an init helper), and scan firmware sources for pin misuse.
"""

import re
from collections import defaultdict
from pathlib import Path

from pydantic import JsonValue

from eda.loader import SourceMap
from eda.model import Design, FirmwareSignal, ResolvedCircuit, ResolvedFirmware
from eda.result import Diagnostics, Location
from eda.schema.firmware import FirmwareFile

GENERATED_HEADER = "pins.h"
GENERATED_INIT = "pins_init.h"
_MACRO_USE = re.compile(r"\bPIN_[A-Z0-9_]+\b")
_RAW_GPIO = re.compile(
    r"\b(gpio_[a-z_]+|adc_gpio_init|pwm_gpio_to_slice_num|pwm_gpio_to_channel)\s*\(\s*(\d+)\s*[,)]"
)
_SOURCE_SUFFIXES = (".c", ".h", ".cpp", ".hpp")


def resolve(
    raw: FirmwareFile, src: SourceMap, circuit: ResolvedCircuit, diag: Diagnostics
) -> ResolvedFirmware | None:
    part = circuit.parts.get(raw.mcu)
    if part is None or part.spec.mcu is None:
        what = "no part" if part is None else f"{part.spec.id}, which is not a microcontroller"
        diag.error("invalid_mcu", f"mcu: {raw.mcu!r} is {what}", src.at("mcu"))
        return None
    mcu = part.spec.mcu
    ok = True

    signals: dict[str, FirmwareSignal] = {}
    for net, spec in raw.signals.items():
        at = src.at("signals", net)
        if net not in circuit.nets:
            diag.error("unknown_net", f"signals.{net}: no net named {net!r} in the circuit", at)
            ok = False
            continue
        gpios = [pin for ref, pin in circuit.nets[net] if ref == raw.mcu and pin in mcu.gpio]
        if len(gpios) != 1:
            problem = "does not reach a GPIO of" if not gpios else "reaches several GPIOs of"
            diag.error("signal_pin", f"net {net!r} {problem} {raw.mcu}", at, gpios=gpios)
            ok = False
            continue
        pin = gpios[0]
        if spec.function is not None and spec.function not in mcu.gpio[pin].functions:
            where = [p for p, g in mcu.gpio.items() if spec.function in g.functions]
            hint = f"it is available on {', '.join(where)}" if where else "no pin has it"
            diag.error(
                "pin_function",
                f"{spec.function} is not available on {pin} (net {net}); {hint}",
                src.at("signals", net, "function"),
                pin=pin,
                available=where,
            )
            ok = False
        if spec.mode != "output" and spec.initial != "low":
            diag.warning("initial_ignored", f"signals.{net}.initial only applies to outputs", at)
        signals[net] = FirmwareSignal(
            net=net,
            pin=pin,
            gpio=mcu.gpio[pin].number,
            mode=spec.mode,
            function=spec.function,
            pull=spec.pull,
            initial=spec.initial,
            description=spec.description,
        )

    # The same peripheral signal on two pins is a conflict; two PWM outputs on one
    # slice channel always show the same waveform.
    by_function: dict[str, list[str]] = defaultdict(list)
    for signal in signals.values():
        if signal.function is not None:
            by_function[signal.function].append(signal.net)
    for function, nets in by_function.items():
        if len(nets) > 1:
            rule = "pwm_shared" if function.startswith("PWM") else "pin_conflict"
            message = f"{function} is assigned to nets {', '.join(nets)}"
            if rule == "pwm_shared":
                diag.warning(
                    rule,
                    message + " (same PWM slice channel: identical output)",
                    src.at("signals", nets[1]),
                )
            else:
                diag.error(rule, message, src.at("signals", nets[1]), nets=nets)
                ok = False

    used = {s.pin for s in signals.values()}
    for net, members in circuit.nets.items():
        for ref, pin in members:
            if ref == raw.mcu and pin in mcu.gpio and pin not in used:
                diag.warning(
                    "undeclared_gpio",
                    f"{raw.mcu}.{pin} is on net {net!r} but firmware.yaml does not say how it "
                    "is used; add it under signals",
                    src.at("signals"),
                    net=net,
                )

    _check_levels(raw.mcu, mcu.vio, signals, circuit, diag)
    if not ok:
        return None
    return ResolvedFirmware(
        mcu=raw.mcu,
        family=mcu.family,
        signals=signals,
        sources=tuple(raw.sources),
        source=src,
    )


def _check_levels(
    mcu_ref: str,
    vio: float,
    signals: dict[str, FirmwareSignal],
    circuit: ResolvedCircuit,
    diag: Diagnostics,
) -> None:
    """A source wired straight to a GPIO net must stay within the I/O voltage."""
    for signal in signals.values():
        for ref, pin in circuit.nets[signal.net]:
            part = circuit.parts.get(ref)
            if part is None or part.spec.category != "source" or pin != part.spec.pins[0]:
                continue
            peak = _source_peak(part.values)
            if peak is not None and peak > vio + 0.3:
                diag.error(
                    "gpio_overvoltage",
                    f"{ref} drives {peak:g} V onto {mcu_ref}.{signal.pin} (net {signal.net}); "
                    f"the GPIO tolerates {vio:g} V (+0.3 V)",
                    circuit.source.at("parts", ref),
                    volts=peak,
                )


def _source_peak(values: dict[str, float | str]) -> float | None:
    def num(key: str) -> float:
        value = values.get(key, 0.0)
        return value if isinstance(value, float) else 0.0

    if "voltage" in values:
        return abs(num("voltage"))
    if "high" in values:
        return max(abs(num("high")), abs(num("low")))
    if "amplitude" in values:
        return abs(num("offset")) + abs(num("amplitude"))
    return None


# --- generated files ---------------------------------------------------------------------


def pins_header(design: Design, firmware: ResolvedFirmware) -> str:
    lines = [
        "/* Generated by `eda fw` from design/circuit.yaml and design/firmware.yaml.",
        " * Do not edit: change the schematic or firmware.yaml and run `eda fw` again. */",
        "#ifndef EDA_PINS_H",
        "#define EDA_PINS_H",
        "",
        f"/* MCU: {firmware.mcu} ({firmware.family}) in project {design.project.name} */",
        "",
    ]
    width = max((len(s.macro) for s in firmware.signals.values()), default=8)
    for signal in sorted(firmware.signals.values(), key=lambda s: s.gpio):
        role = signal.function or signal.mode or ""
        extra = f", pull-{signal.pull}" if signal.pull != "none" else ""
        if signal.mode == "output":
            extra += f", starts {signal.initial}"
        note = f"  /* {signal.pin}: {role}{extra}"
        note += f" - {signal.description} */" if signal.description else " */"
        number = f"{signal.gpio}u"
        lines.append(f"#define {signal.macro:<{width}} {number:<3}{note}")
    lines += ["", "#endif /* EDA_PINS_H */", ""]
    return "\n".join(lines)


def init_header(firmware: ResolvedFirmware) -> str:
    """pico-sdk initialisation for every declared pin (RP2040)."""
    body: list[str] = []
    includes = {"hardware/gpio.h"}
    for signal in sorted(firmware.signals.values(), key=lambda s: s.gpio):
        macro = signal.macro
        function = signal.function or ""
        if signal.mode is not None:
            body.append(f"    gpio_init({macro});")
            direction = "GPIO_OUT" if signal.mode == "output" else "GPIO_IN"
            body.append(f"    gpio_set_dir({macro}, {direction});")
            if signal.mode == "output":
                level = "1" if signal.initial == "high" else "0"
                body.append(f"    gpio_put({macro}, {level});")
        elif function.startswith("ADC"):
            includes.add("hardware/adc.h")
            body.append(f"    adc_gpio_init({macro});")
        else:
            kind = function.split("_")[0].rstrip("0123456789")
            sdk = {"SPI": "SPI", "UART": "UART", "I2C": "I2C", "PWM": "PWM", "PIO": "PIO0"}
            name = sdk.get(kind, kind)
            if kind == "PIO":
                name = function  # PIO0 / PIO1
            body.append(f"    gpio_set_function({macro}, GPIO_FUNC_{name});")
        if signal.pull == "up":
            body.append(f"    gpio_pull_up({macro});")
        elif signal.pull == "down":
            body.append(f"    gpio_pull_down({macro});")
    lines = [
        "/* Generated by `eda fw`: pin setup for the pico-sdk. Do not edit. */",
        "#ifndef EDA_PINS_INIT_H",
        "#define EDA_PINS_INIT_H",
        "",
        *(f'#include "{inc}"' for inc in sorted(includes)),
        '#include "pins.h"',
        "",
        "/* Configure every pin declared in design/firmware.yaml. Call once at startup;",
        " * peripherals themselves (uart_init, adc_init, ...) are still up to the firmware. */",
        "static inline void pins_init(void) {",
        *body,
        "}",
        "",
        "#endif /* EDA_PINS_INIT_H */",
        "",
    ]
    return "\n".join(lines)


def generate(design: Design, diag: Diagnostics) -> dict[str, JsonValue]:
    firmware = design.firmware
    if firmware is None:
        diag.error("no_firmware", "no design/firmware.yaml; it declares the MCU and pin roles")
        return {}
    paths = design.paths
    out_dir = paths.root / firmware.sources[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    header = out_dir / GENERATED_HEADER
    init = out_dir / GENERATED_INIT
    header.write_text(pins_header(design, firmware), encoding="utf-8")
    init.write_text(init_header(firmware), encoding="utf-8")
    scanned = scan_sources(design, firmware, diag)
    return {
        "mcu": firmware.mcu,
        "family": firmware.family,
        "generated": [paths.rel(header), paths.rel(init)],
        "pins": [
            {
                "net": s.net,
                "pin": s.pin,
                "gpio": s.gpio,
                "macro": s.macro,
                "role": s.function or s.mode,
                "pull": s.pull,
                "initial": s.initial if s.mode == "output" else None,
            }
            for s in sorted(firmware.signals.values(), key=lambda s: s.gpio)
        ],
        "sources_scanned": [str(f) for f in scanned],
    }


def scan_sources(design: Design, firmware: ResolvedFirmware, diag: Diagnostics) -> list[str]:
    """Undefined PIN_ macros are errors; hard-coded GPIO numbers in SDK calls are warnings."""
    paths = design.paths
    defined = {s.macro for s in firmware.signals.values()}
    by_number = {s.gpio: s.macro for s in firmware.signals.values()}
    scanned: list[str] = []
    for directory in firmware.sources:
        base = paths.root / directory
        if not base.is_dir():
            continue
        for file in sorted(base.rglob("*")):
            if file.suffix not in _SOURCE_SUFFIXES or file.name in (
                GENERATED_HEADER,
                GENERATED_INIT,
            ):
                continue
            shown = paths.rel(file)
            scanned.append(shown)
            _scan_file(file, shown, defined, by_number, diag)
    return scanned


def _scan_file(
    file: Path, shown: str, defined: set[str], by_number: dict[int, str], diag: Diagnostics
) -> None:
    for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), start=1):
        code = line.split("//", 1)[0]
        for match in _MACRO_USE.finditer(code):
            name = match.group(0)
            if name not in defined and not name.startswith("PIN_INIT"):
                diag.error(
                    "undefined_pin",
                    f"{name} is not a pin of this design; defined: {', '.join(sorted(defined))}",
                    Location(file=shown, line=number, column=match.start() + 1),
                )
        for match in _RAW_GPIO.finditer(code):
            gpio = int(match.group(2))
            macro = by_number.get(gpio)
            hint = f"use {macro}" if macro else "this GPIO is not declared in firmware.yaml"
            diag.warning(
                "hardcoded_gpio",
                f"{match.group(1)}({gpio}, ...) hard-codes a GPIO number; {hint}",
                Location(file=shown, line=number, column=match.start() + 1),
            )
