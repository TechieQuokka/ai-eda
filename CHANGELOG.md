# Changelog

All notable changes to ai-eda. Versions follow [Semantic Versioning](https://semver.org/):
MAJOR for changes that break existing design files or the CLI's JSON, MINOR for new
features that keep them working, PATCH for fixes. Each release is tagged `vX.Y.Z`.

## [1.1.1] - 2026-10-21

### Fixed
- `calculator`: the division sign is a CGRAM glyph. ROM code 0xFD is a division sign only on
  A00-ROM LCD modules (not on A02 ones), and the viewer drew it as a box.

## [1.1.0] - 2026-10-21

### Added
- Example `calculator`: a battery-powered pocket calculator meant to be ordered and
  hand-soldered (Pico, 20 keys in a 5 x 4 matrix, 16x2 LCD, 2xAA with a 5 V boost
  module, low-battery mark); 14 tests with firmware in the loop; DRC-clean 112 x 126 mm
  board (`DESIGN_DECISIONS.md` §27).
- Parts: `battery` (pack on wire pads), `slide_switch` (SPDT), `pololu_u1v11f5` (5 V boost
  module, behavioural model), and the 1N5817 Schottky model for `diode`.
- `pcb.joined_pads` in part definitions (set on `push_button`): pads sharing a number are
  joined inside the part, so the router connects the one nearest the net and KiCad counts
  the rest as connected.
- `<choice:P.T>` in part templates: one choice parameter can set several elements.

### Fixed
- `pins_init.h` switches the reset pull-down off for inputs declared without a pull, as
  the simulation assumes.
- Tracks routed closer to the board edge than KiCad's copper-to-edge clearance.
- Tracks routed 2 um inside the clearance next to round pads (the router now keeps a 10 um
  margin).
- LCD screens with CGRAM characters crashed the PNG rendering (control characters in the
  SVG).

## [1.0.0] - 2026-10-21

First versioned release: everything built so far (`DESIGN_DECISIONS.md` §10-§26).

### Design and simulation
- Design files in strict YAML 1.2 (`circuit.yaml`, `test.yaml`, `board.yaml`,
  `firmware.yaml`), validated with pydantic; every error as JSON with file, line, field,
  and rule. ERC, parameter ranges, and hardening against malformed or extreme input.
- ngspice analyses: operating point, transient, AC, and DC sweeps of any part quantity;
  typed measures (`max`, `avg`, `at`, `cross`, `period`, `width`, `duty`, `db`, ...),
  events during a run, measure time ranges, transient operating points with a settling
  search.
- Parts: sources, R/C/L, diodes, LEDs, NPN transistors, LDR, button, potentiometer,
  generic op-amp; vendor SPICE model import (`eda model inspect/import`).

### Boards and schematics
- netlistsvg schematic, KiCad schematic export (placed, wired, netlist-checked).
- KiCad boards: connectivity-aware placement, Freerouting, ground pours, DRC as JSON,
  Gerbers/drill/BOM, PCB house limits (`fab: jlcpcb`), assembly drawing, shopping BOM.
- `placement_overlap`: overlapping hand placements are reported before routing.

### Firmware
- Raspberry Pi Pico (RP2040): pin roles, generated `pins.h` / `pins_init.h`, pico-sdk
  builds with compiler messages as JSON.
- Firmware in the loop: the compiled firmware runs in rp2040js in lock-step with ngspice
  (GPIO, PWM, ADC, UART in and out, I2C devices), with fault and halt detection.
- I2C bus health (`i2c_bus`, `i2c_nack`) and `mcu_pin_overvoltage` in firmware tests.

### Displays
- 4-digit 7-segment LED display (`seven_segment_4`), decoded from every segment LED's
  current as the eye averages it.
- 16x2 character LCD (`lcd_1602`, HD44780): the controller decodes the firmware's bus with
  its timing, busy times, power-on reset, CGRAM, and contrast.
- 128x64 OLED (`oled_ssd1306`, SSD1306 on I2C): commands, addressing modes, panel mapping,
  charge pump; text read back from the pixels, lit-pixel regions.
- `{ display: REF, match: REGEX, at: TIME }` and `region` expectations; display frames
  in `out/display/` and in the viewer's Playback.

### Viewer and tools
- `eda watch` live viewer: schematic, verdicts, waveforms, board, iteration log, Playback.
- `eda value` standard values, `eda part`, `eda log`, iteration log with semantic diffs.

### Examples
- `led_demo`, `rc_filter`, `opamp_amp`, `tl072_amp`, `pico_io`, `led_blink`,
  `led_blink_bjt`, `nightlight`, `water_alarm`, `seg_counter`, `lcd_message`, `oled_meter`.

[1.0.0]: https://github.com/TechieQuokka/ai-eda/releases/tag/v1.0.0
