# CLAUDE.md

Rules for Claude Code working in this repository. **Resuming work? Read `HANDOFF.md` first.** Background: `PROJECT_SUMMARY.md` (vision, architecture, roadmap) and `DESIGN_DECISIONS.md` (settled decisions).

## 1. Working Rules

1. **Language**: all artifacts (code, docs, comments, commit messages) in English; conversation with the user in Korean.
2. **Approval**: never do substantive work (creating/modifying/deleting files, design decisions, project code) without the user's explicit approval. Propose first, then wait. Read-only lookups and basic environment setup are fine.
3. **One question at a time.**
4. **Context**: read `PROJECT_SUMMARY.md`, `DESIGN_DECISIONS.md`, and the earlier conversation before acting.
5. **Directory scope**: stay inside this project directory unless the user explicitly allows another path.
6. **Versions**: newly added dependencies use their latest release (verify online). Already-installed toolchains (Python 3.13, Node 22, uv) are kept as they are.

## 2. Tool Development Rules

- Python 3.13, managed with uv. Run everything through `uv run`.
- Type hints everywhere; `pyright` runs in strict mode.
- Design files are parsed with ruamel.yaml (YAML 1.2, no implicit coercion) and validated with pydantic v2. Errors report file, line, field, and reason as JSON.
- External tools (ngspice, kicad-cli, ...) are called only through `src/eda/adapters/`.
- The CLI never makes design decisions; it reads, validates, simulates, renders, and exports.
- Work is done only when all of these pass:

```sh
uv run ruff format --check
uv run ruff check
uv run pyright
uv run pytest
```

## 3. Design Work Loop

For a design task in `projects/<name>/` (schema: `DESIGN_DECISIONS.md` §10; examples: `projects/led_demo/`, `projects/rc_filter/`):

1. Record the goal: `uv run eda log projects/<name> -m "Goal: ..."`.
2. Edit the design YAML (`design/circuit.yaml`, `design/test.yaml`). Every requirement becomes an expectation in `test.yaml`.
3. `uv run eda run projects/<name> -m "<why this change>"` — check → sim → render (→ pcb when `design/board.yaml` exists) in one call; the note and a semantic diff of the design go into the iteration log. Exit code 0 = all pass, 1 = design errors, 2 = tool failure.
4. On failure, read `errors` in the JSON: each has `rule`, `message`, `file`, `line`, `path`, `details`. Use `data.sim.tests.<name>.operating_point` for node voltages and currents.
5. Fix the design files and repeat from step 3 with a `-m "Fix: ..."` note. Pick real component values with `uv run eda value <target> -s E24|E96`.
6. Look at `out/schematic.png`, `out/waves/<test>.png`, and (with a board) `out/board.png` before reporting; when everything passes, report to the user. `eda render` also writes a KiCad schematic, `out/kicad/<name>.kicad_sch`, placed and wired, checked against the design's nets; `out/kicad/<name>.png` shows the drawing (cropped), `.svg` the full sheet.

Other commands: `eda check`, `eda sim [-t TEST]`, `eda render`, `eda pcb`, `eda part list`, `eda part show ID`, `eda value TARGET [-s SERIES]`.

**Trying inputs** (`eda try`): runs the circuit and its firmware with inputs given on the command line, without editing `test.yaml`; nothing is judged or logged. `--keys "9*8-2="` (keys mapped in `design/inputs.yaml`, from its `start`), `--at TIME`, `--event "150ms SW1.state=pressed"`, `--send "300ms UART_RX rate 100\n"`, `--set RV1.position=25%`, `--from TEST` (start from a tran test), `--stop`, `--step`, `--measure "max(current(D1))"` (value reported). Results: `data.try` (`displays`, `firmware.serial`, `expectations` values, `events`), files in `out/try/` and `out/display/try.*`. Use it to explore; turn what matters into a test.

The CLI never makes design decisions; it only reports.

## 4. Iteration Log and Viewer

- `iterations.jsonl` in each project (tracked by git) records every `eda` run (changed files, errors) and every `eda log` note (intent and fix).
- `uv run eda watch projects/<name>` re-runs on file changes and serves the live viewer at http://127.0.0.1:8765/ (schematic, verdicts, interactive waveforms, iteration log). The user watches it; start it when they want to observe.

## 5. Writing Tests (`design/test.yaml`)

- Analyses: `{ type: op }`, `{ type: tran, step, stop }`, `{ type: ac, start, stop, points_per_decade }`, `{ type: dc, sweep: PART.param, start, stop, step }` (an operating point per value of one part quantity: a source's `voltage`, a resistance, an LDR's `light`, a pot's `position`; ≤ 10,000 points). AC needs a source with an `ac:` magnitude.
- Probes: `voltage(NET)`, `voltage(A, B)`, `current(R1)` (into the first pin), `current(U1.out)` (per pin of multi-pin parts).
- Functions: `max min initial final avg rms pp`, `at(trace, 1ms | 1kHz | 5V)`, `cross(trace, LEVEL)` (time / frequency / swept value of the first crossing; dc: e.g. `"cross(current(D1), 1mA)"` in lx when sweeping light), `period(trace, LEVEL)` (tran: mean time between rising crossings), `width(trace, LEVEL)` / `gap(trace, LEVEL)` (tran: mean time above / below LEVEL per pulse, whole pulses only), `duty(trace, LEVEL)` (tran: % of the time above LEVEL), and for AC `db mag phase`. Examples: `"at(db(voltage(OUT)), 1kHz)"`, `"cross(db(voltage(OUT)), 17.8dB)"`, `"period(current(D1), 3mA)"`, `"width(current(D1), 3mA)"`.
- Per-test part parameters: `parts: { SW1: { state: pressed }, RV1: { position: 25% } }`.
- Events during a `tran` test: `events: [{ at: 1.1s, part: SW1, set: { state: pressed } }, { at: 300ms, serial: UART_RX, send: "rate 100\n" }]`. Part events can change R/C/L values (switches, pots, resistors, an LDR's `light`) and a DC source's `voltage`; serial events need `firmware: true`. Event times are marked on the waveform plots. `every: 50ms` (optional `until: 1s`, default the end of the run) repeats an event, e.g. a remote's command line: `{ at: 100ms, serial: UART_RX, send: "D,100,0\n", every: 50ms, until: 700ms }`.
- Key presses: `{ at: 150ms, keys: "12+34=" }` presses one key after another (each a press and a release event; optional `hold` / `gap` per event). Keys are mapped in `design/inputs.yaml`: `keys: { "1": K1, "+": KADD, menu: { part: RV1, press: { position: 90% }, release: { position: 10% } } }` (a bare part name is a button: `state` pressed, then open; a key longer than one character is written `{menu}` in a key string), plus `hold`, `gap` (default 40 ms each) and, for `eda try`, `start`, `settle`, `step`. Rules: `keys_need_inputs`, `unknown_key`, `key_part`, `event_time` (released after the run ends).
- Time range of a tran measure: `{ measure: "period(current(D1), 3mA)", from: 1.6s, to: 2s, ... }` (either bound may be left out).
- Display expectations: `{ display: DS1, match: '^  1\.2$', at: 1.25s }` searches the text on a display part's screen (rows joined by newlines; `at` defaults to the end of the run); with `min`/`max` the value is the number in the capture group, or the count of matches. See §9.
- **Quote any measure that contains a comma** inside `{ ... }`.
- Units: `V A ohm F H s Hz dB deg V/us % lx`, prefixes `p n u m k M G T` (case-sensitive).
- Limits: tran ≤ 1e6 points (stop/step), ac ≤ 1e5 frequencies, firmware tests ≤ 1e5 steps (`too_many_points`). A `singular_matrix` error means the circuit has no unique solution (e.g. a `dc_loop` of sources and inductors): fix the circuit, the numbers are not judged.

## 6. Parts and Vendor Models

- Built-in parts: `parts/<id>.yaml` (one file per type, file name = id); project-specific parts in `projects/<name>/parts/`. Fields: pins, params (`unit` or `choices`, optional `default`; quantities may set `positive: true` and inclusive `min` / `max`), `spice` template (`include`, `compat`, `pin_currents`), netlistsvg `symbol`, `dc_path`. List them with `eda part list` / `eda part show ID`.
- `opamp` is a generic macromodel (aol, gbw, slew, rin, rout, iq, swing) and is the default choice. For a specific chip, import the vendor model:
  1. `uv run eda model inspect FILE` — find the `.subckt` and read its pin comments.
  2. `uv run eda model import FILE --id <id> [--subckt NAME] [--opamp IN+,IN-,V+,V-,OUT]`.
- Imported models go to `parts/vendor/`, which is git-ignored: **never commit vendor model files** (their licenses usually forbid redistribution).
- Model files with a `.control` / `.exec` block (also in files they include) are refused as `unsafe_model`: ngspice would run its commands, `shell` among them. Part templates hold element lines only.
- Only test what a model claims to model (vendor files list it in their header); e.g. the old TL072 model's supply current is not realistic.
- `op_via_transient` warning: the model's operating point did not converge directly, so ngspice found it by a transient run from power-up; `eda` lengthens that run until the net voltages stop changing and reports the time. Put it on the part (`--optran 1ms` at import, or `spice: { optran: { stop: 1ms } }`) to skip the search. `op_not_settled` means even 1 s was not enough: do not trust that operating point.

- Board-related part fields: `pcb: { footprint, pads, nc, value, joined_pads }`; `joined_pads: true` (e.g. `push_button`) means pads sharing a number are joined inside the part (only one of each is routed).
- Power parts: `battery` (pack off the board on wire pads; `voltage`, `rint`; negative = reversed), `slide_switch` (SPDT, `position: "1"|"3"`, 100 mA), `pololu_u1v11f5` (5 V boost module; check its input current against 1.2 A yourself), `pololu_s7v8f5` (5 V buck-boost, 2.7-11.8 V in, output current limited along Pololu's curve), `diode` with `model: 1N5817` (Schottky; give it the DO-41 footprint) or `1N5822` (3 A Schottky, DO-201AD).
- Motion and radio parts (`projects/rc_car`, `projects/rc_remote`): `dc_motor` (wire pads; `stall_current`, `no_load_current`, `time_constant`, `shaft: free|stalled`), `drv8833_carrier` (Pololu #2130 in a DIP-16 footprint; bridges averaged per PWM window; check peaks against 2 A per bridge yourself), `servo_sg90` (header; `load: idle|moving|stalled`), `hc05` (Bluetooth serial module; `link: connected|waiting` sets STATE; the radio is not simulated: received bytes are `serial` events, sent bytes `serial` expectations; pairing: `docs/hc05_pairing.md`), `joystick_2axis` (KY-023 module; `x`, `y` in %, `button`; feed it from 3V3 for the Pico's ADC).
- KiCad schematic symbols: each part's `kicad: { symbol: Library:Name, pins: {...} }` (pins default to the `pcb.pads` numbers); parts without one get a generated box. `kicad_sch_mismatch` (exit 2) means the exported schematic does not connect like the design; `kicad_erc_<type>` warnings are KiCad's own ERC; `data.kicad.labelled_nets` lists nets the router could not wire (joined by labels instead).

## 7. Boards (`design/board.yaml`)

- Our YAML is the source of truth; never edit files under `out/pcb/` (they are regenerated).
- `board.yaml` sets outline, rules, optional hand placement, and routing passes (`DESIGN_DECISIONS.md` §13.2). An empty `board: {}` is enough to get a board.
- Parts without a hand placement are placed next to the parts they connect to (`board.placer: connected`, the default; `shelf` packs by size). Hand-place only what has a mechanical reason (connectors at the edge, a module's orientation); with a fixed `width`/`height`, a `no_room` error (`no room for X`) means the board is too small. `placement_overlap`: two hand-placed parts' courtyards overlap (a placement's x/y is the footprint origin, usually pin 1, not its centre).
- Footprints come from each part's `pcb:` default; override per part with `footprint:` (KiCad `Library:Name`) and `pads:` (pin → pad) in `circuit.yaml`. Sources become 2-pin headers.
- `uv run eda pcb projects/<name>` builds, autoroutes (Freerouting), runs DRC, and exports Gerbers/drill/BOM. DRC issues come back as `drc_<type>` errors pointing at the part; `unrouted` means the router gave up: move parts apart (placement), enlarge the board, or raise `routing.passes`.
- Read `unused_pads` warnings: unused IC channels usually need tying off. Tie them on the board with `board_pads:` on the part in `circuit.yaml` (pad → net, board only; a net not in the circuit needs two or more pads), e.g. the spare channel of a dual op-amp as a follower: `board_pads: { "5": GND, "6": UB_OUT, "7": UB_OUT }`. Rules: `invalid_pads`, `board_net`.
- The BOM (`out/pcb/fab/<name>-bom.csv`) is a shopping list grouped by item. Give each part to buy an `mpn:` (part number) in `circuit.yaml` when its value alone does not name it; the generic `opamp` must have one (`bom_generic_part` warning).
- Ordering and hand soldering: `docs/ordering.md`.
- `out/assembly.svg` (`.png`) is the soldering guide: outline, parts, references, values, true size.
- PCB house: `fab: jlcpcb` checks rules, pours, size, and footprint holes against the house's limits (`fab_limit` errors); `fab: { base: jlcpcb, min_track: 0.15mm }` overrides, and limits without `base` describe another house.
- Track widths: `rules: { track_width: 0.25mm, track_widths: { VBAT: 1mm, MOT_A: 1mm } }` gives nets that carry current wider tracks (a net class each in KiCad and the router). About 1 mm per amp for short tracks on 1 oz copper; the GND return needs it too unless a pour carries it. Rule: `board_net` (net with no pads).
- Ground plane: `pours: [{ net: GND, layer: bottom }]` (`top`, `bottom`, or `both`; optional `clearance`, `min_width`) fills the board with copper on that net after routing; `data.pcb.pours` reports the filled area. A `drc_starved_thermal` warning means a pad's thermal relief reaches only a small fill island (it is still connected by its tracks); moving parts or pouring both layers usually clears it.
- Freerouting must be installed once: `tools/freerouting/fetch.sh`.

## 8. Firmware (`design/firmware.yaml`, `firmware/`)

- The MCU is the Raspberry Pi Pico (`raspberry_pi_pico`, RP2040). Wire GPIOs in `circuit.yaml` with meaningful net names: the net name becomes the firmware macro `PIN_<NET>`.
- `firmware.yaml` declares each GPIO net's role: `mode: input|output` (+ `pull`, `initial`) or `function:` (e.g. `UART0_TX`, `ADC0`, `PWM7_B`, `I2C0_SDA`). `eda part show raspberry_pi_pico` lists which functions each pin has.
- `uv run eda fw projects/<name>` validates roles against the pinout, finds conflicts, writes `firmware/pins.h` and `firmware/pins_init.h` (never edit those by hand), and scans firmware sources.
- Firmware code uses only `PIN_` macros and `pins_init()`; hard-coded GPIO numbers are warnings, unknown macros are errors.
- `uv run eda fw projects/<name> --build` also compiles `firmware/` with the pico-sdk into `out/fw-build/` (`eda run` does it whenever the SDK is installed). Compiler and linker messages come back as `fw_compile_error` / `fw_compile_warning` / `fw_link_error` with file and line; `data.build.size` gives flash/RAM use. SDK setup: `tools/fetch-pico-sdk.sh` plus `gcc-arm-none-eabi`.
- Simulation follows firmware.yaml (outputs drive, pulls pull, others are high-Z). Test each output state with `pins: { LED: high }` in a test.
- **Firmware in the loop**: a `tran` test with `firmware: true` runs the compiled firmware in the RP2040 emulator against the circuit (`DESIGN_DECISIONS.md` §15). The firmware drives GPIO/PWM pins, reads inputs and ADC channels from node voltages, and its UART output is captured. Check it with `period(...)`/`at(...)` measures and serial expectations: `{ serial: UART_TX, match: 'pot=(\d+)', min: 1900, max: 2200 }` (number in the last match; without a group, the number of matches). The output is in `data.sim.tests.<name>.firmware.serial` and `out/sim/<test>.uart.txt`. Values are exchanged every `step`, so pick `step` well below the timing you test; `pins:` overrides do not apply.
- Firmware faults: `firmware_fault` (unmapped memory access, a HardFault on the chip) fails the test; `firmware_halted` warns that the firmware stopped (main returned, panic). UART output is paced at the baud rate, as on the chip.
- Emulator setup: `npm --prefix tools/rp2040 ci` and `tools/rp2040/fetch-bootrom.sh`; co-simulation also needs libngspice.

## 9. Displays

- Display parts carry a `display:` section; the simulation decodes what each one shows. Every op/tran test that contains one writes `out/display/<test>.<ref>.png` (last frame) and `.json` (frames over the run), and reports `data.sim.tests.<name>.displays.<ref>` (`text`, `changes`). The viewer shows the screens and replays them in Playback.
- `seven_segment_4` (CC56-12, 4 digits, common cathode, multiplexed): every segment LED is measured. A segment reads as lit when its mean current over the last 20 ms reaches 100 uA **and** 30 % of the brightest segment's (dimmer ones are ghosting). Text is one character per digit (`8.` with its decimal point, `?` for a shape that is no character). Works in op tests (static drive) and tran tests (multiplexed by firmware: use `step` ≤ 1/10 of a digit's on-time).
- `lcd_1602` (16x2 character LCD, HD44780, 5 V, RW to GND): in a `firmware: true` test the co-simulation feeds the controller the bus levels with the emulator's exact GPIO timing (expectations on it need firmware: `display_needs_firmware`). Text is the visible rows, trailing spaces stripped; CGRAM characters read as `\x00`-`\x07`; a display that is off, unpowered, or below the contrast threshold reads as empty rows. Warnings say what went wrong on the bus: `display_busy` (a write while the controller was busy is ignored, e.g. no 1.52 ms wait after clear), `display_timing` (E pulse < 230 ns, setup/hold), `display_logic_level`, `display_unpowered`, `display_supply`, `display_contrast` (VDD-V0 outside 3.0-4.8 V), `display_read` (RW high), `display_bus` (a bus pin not on a firmware GPIO net). Drive E, RS, D4-D7 straight from GPIOs.
- `oled_ssd1306` (0.96" 128x64 module, I2C, pins GND VCC SCL SDA, own 3.3 V regulator and 4.7 kohm pull-ups; `pullups: none`, `address: "0x3D"`): in a firmware test it is an I2C device on the bus its SDA/SCL nets reach (firmware.yaml `I2Cn_SDA` / `I2Cn_SCL`). It acknowledges its address only while SDA and SCL idle above 2.64 V; bytes take nine SCL periods at the firmware's rate. Text drawn in the 5x8 font (`font5x8.py`; the example's `firmware/font5x8.h` is the same table) is read back from the pixels at any position; `{ display: OLED1, region: [x0, y0, x1, y1], min, max }` counts lit pixels. Warnings: `display_dark` (display off, or charge pump not enabled with `8D 14`), `display_layout` (COM pins not `DA 12`: rows interleave), `display_scroll` (not drawn), `display_bus` (not on an I2C bus, or never written), `display_unpowered` / `display_supply`.
- I2C in firmware tests: `i2c_bus` (a device was addressed but SDA/SCL idle too low: missing pull-ups; nothing answers, as on the board), `i2c_nack` (no device at that address). Any firmware pin above the MCU's I/O supply + 0.3 V is `mcu_pin_overvoltage` (RP2040 GPIOs are not 5 V tolerant).
- Examples: `projects/seg_counter/` (stopwatch: segments straight from GPIOs, digit cathodes through 2N3904s), `projects/lcd_message/` (4-bit LCD driver in `firmware/lcd.c`, text from the UART), `projects/oled_meter/` (SSD1306 driver in `firmware/oled.c`: text, a bar graph of the pot on ADC0). `projects/calculator/` (battery-powered: key matrix scanned by switching row direction, calculator engine in `firmware/calc.c`, CGRAM glyphs, low-battery mark, a board to order).
