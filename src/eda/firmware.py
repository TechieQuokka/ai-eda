"""Hardware-firmware integration: pin roles, pins.h, cross-checks, and the firmware build.

The schematic decides which GPIO a net reaches; `design/firmware.yaml` says how firmware
uses that net. From both we check the pin can do the job, find conflicts, generate
`firmware/pins.h` (+ an init helper), and scan firmware sources for pin misuse.
`build` compiles the firmware with the pico-sdk and maps compiler output to issues.
"""

import re
from collections import defaultdict
from pathlib import Path

from pydantic import JsonValue

from eda.adapters import picosdk
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
_EXECUTABLE = re.compile(r"add_executable\(\s*([A-Za-z0-9_.+-]+)")
BUILD_DIR = "fw-build"
_RULES = {
    ("compile", "error"): "fw_compile_error",
    ("compile", "warning"): "fw_compile_warning",
    ("link", "error"): "fw_link_error",
    ("cmake", "error"): "fw_cmake_error",
    ("cmake", "warning"): "fw_cmake_warning",
    ("build", "error"): "fw_build_failed",
}


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
        if spec.mode == "output" and spec.pull != "none":
            diag.warning(
                "pull_on_output",
                f"signals.{net}.pull: a pull resistor on a driven output only wastes current",
                at,
            )
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
        elif signal.mode == "input":
            # The RP2040 resets with the pull-down on: an input declared without a pull
            # must have it switched off, as the simulation assumes.
            body.append(f"    gpio_disable_pulls({macro});")
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
    if not scanned:
        diag.warning(
            "no_firmware_sources",
            f"no C/C++ files in {', '.join(firmware.sources)}; write the firmware there "
            "(it includes pins.h and calls pins_init())",
            firmware.source.at("sources"),
        )
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


# --- build -------------------------------------------------------------------------------


def build(design: Design, diag: Diagnostics) -> dict[str, JsonValue]:
    """Compile the firmware (CMakeLists.txt in the first source dir) into out/fw-build/."""
    firmware = design.firmware
    if firmware is None:
        diag.error("no_firmware", "no design/firmware.yaml; it declares the MCU and pin roles")
        return {}
    paths = design.paths
    source = paths.root / firmware.sources[0]
    cmakelists = source / "CMakeLists.txt"
    if not cmakelists.is_file():
        diag.error(
            "fw_no_cmakelists",
            f"{paths.rel(cmakelists)} not found; the firmware build needs a pico-sdk project",
        )
        return {}
    match = _EXECUTABLE.search(cmakelists.read_text(encoding="utf-8"))
    target = match.group(1) if match else design.project.name
    mcu = design.circuit.parts[firmware.mcu].spec.mcu
    board = (mcu.sdk_board if mcu else None) or "pico"
    build_dir = paths.out_dir / BUILD_DIR
    try:
        sdk = picosdk.sdk_path()
        output = picosdk.build(source, build_dir, target, board)
        sizes = picosdk.size(output.elf) if output.elf else None
    except picosdk.PicoSdkError as exc:
        diag.fail_tool("fw_toolchain", str(exc))
        return {}
    (paths.out_dir / "fw-build.log").write_text(output.log, encoding="utf-8")
    hidden = sum(not _report(m, paths.root, build_dir, diag) for m in output.messages)
    data: dict[str, JsonValue] = {
        "target": target,
        "sdk_warnings_hidden": hidden,
        "board": board,
        "sdk": picosdk.sdk_version(sdk),
        "log": paths.rel(paths.out_dir / "fw-build.log"),
        "built": output.ok,
    }
    if output.elf is not None and sizes is not None:
        uf2 = output.elf.with_suffix(".uf2")
        data["elf"] = paths.rel(output.elf)
        data["uf2"] = paths.rel(uf2) if uf2.is_file() else None
        flash = sizes["text"] + sizes["data"]
        ram = sizes["data"] + sizes["bss"]
        data["size"] = {"flash": flash, "ram": ram, **sizes}
        if mcu is not None and mcu.flash_bytes and mcu.ram_bytes:
            data["size_percent"] = {
                "flash": round(100 * flash / mcu.flash_bytes, 1),
                "ram": round(100 * ram / mcu.ram_bytes, 1),
            }
            for what, used, total in (
                ("flash", flash, mcu.flash_bytes),
                ("ram", ram, mcu.ram_bytes),
            ):
                if used > total:
                    diag.error(
                        "fw_too_big", f"firmware uses {used} B of {what}; {total} B available"
                    )
    return data


def _report(message: picosdk.Message, root: Path, build_dir: Path, diag: Diagnostics) -> bool:
    """Add one build message as an issue. Warnings from outside the project (the SDK's own
    CMake and sources) are not actionable and are only counted; returns False for those."""
    rule = _RULES[(message.stage, message.kind)]
    shown: str | None = None
    if message.file is not None:
        file = message.file if message.file.is_absolute() else build_dir / message.file
        inside = file.resolve().is_relative_to(root.resolve())
        if message.kind == "warning" and not inside:
            return False
        shown = file.relative_to(root).as_posix() if inside else file.as_posix()
    at = Location(file=shown, line=message.line, column=message.column)
    if message.kind == "error":
        diag.error(rule, message.message, at)
    else:
        diag.warning(rule, message.message, at)
    return True
