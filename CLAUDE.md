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
6. Look at `out/schematic.png`, `out/waves/<test>.png`, and (with a board) `out/board.png` before reporting; when everything passes, report to the user.

Other commands: `eda check`, `eda sim [-t TEST]`, `eda render`, `eda pcb`, `eda part list`, `eda part show ID`, `eda value TARGET [-s SERIES]`.
The CLI never makes design decisions; it only reports.

## 4. Iteration Log and Viewer

- `iterations.jsonl` in each project (tracked by git) records every `eda` run (changed files, errors) and every `eda log` note (intent and fix).
- `uv run eda watch projects/<name>` re-runs on file changes and serves the live viewer at http://127.0.0.1:8765/ (schematic, verdicts, interactive waveforms, iteration log). The user watches it; start it when they want to observe.

## 5. Writing Tests (`design/test.yaml`)

- Analyses: `{ type: op }`, `{ type: tran, step, stop }`, `{ type: ac, start, stop, points_per_decade }`. AC needs a source with an `ac:` magnitude.
- Probes: `voltage(NET)`, `voltage(A, B)`, `current(R1)` (into the first pin), `current(U1.out)` (per pin of multi-pin parts).
- Functions: `max min initial final avg rms pp`, `at(trace, 1ms | 1kHz)`, `cross(trace, LEVEL)` (time / frequency of the first crossing), and for AC `db mag phase`. Examples: `"at(db(voltage(OUT)), 1kHz)"`, `"cross(db(voltage(OUT)), 17.8dB)"`.
- **Quote any measure that contains a comma** inside `{ ... }`.
- Units: `V A ohm F H s Hz dB deg V/us`, prefixes `p n u m k M G T` (case-sensitive).

## 6. Parts and Vendor Models

- Built-in parts: `parts/<id>.yaml` (one file per type, file name = id); project-specific parts in `projects/<name>/parts/`. Fields: pins, params (`unit` or `choices`, optional `default`), `spice` template (`include`, `compat`, `pin_currents`), netlistsvg `symbol`, `dc_path`. List them with `eda part list` / `eda part show ID`.
- `opamp` is a generic macromodel (aol, gbw, slew, rin, rout, iq, swing) and is the default choice. For a specific chip, import the vendor model:
  1. `uv run eda model inspect FILE` — find the `.subckt` and read its pin comments.
  2. `uv run eda model import FILE --id <id> [--subckt NAME] [--opamp IN+,IN-,V+,V-,OUT]`.
- Imported models go to `parts/vendor/`, which is git-ignored: **never commit vendor model files** (their licenses usually forbid redistribution).
- Only test what a model claims to model (vendor files list it in their header); e.g. the old TL072 model's supply current is not realistic.

## 7. Boards (`design/board.yaml`)

- Our YAML is the source of truth; never edit files under `out/pcb/` (they are regenerated).
- `board.yaml` sets outline, rules, optional hand placement, and routing passes (`DESIGN_DECISIONS.md` §13.2). An empty `board: {}` is enough to get a board.
- Footprints come from each part's `pcb:` default; override per part with `footprint:` (KiCad `Library:Name`) and `pads:` (pin → pad) in `circuit.yaml`. Sources become 2-pin headers.
- `uv run eda pcb projects/<name>` builds, autoroutes (Freerouting), runs DRC, and exports Gerbers/drill/BOM. DRC issues come back as `drc_<type>` errors pointing at the part; `unrouted` means the router gave up: move parts apart (placement), enlarge the board, or raise `routing.passes`.
- Read `unused_pads` warnings: unused IC channels usually need tying off.
- Freerouting must be installed once: `tools/freerouting/fetch.sh`.

## 8. Firmware (`design/firmware.yaml`, `firmware/`)

- The MCU is the Raspberry Pi Pico (`raspberry_pi_pico`, RP2040). Wire GPIOs in `circuit.yaml` with meaningful net names: the net name becomes the firmware macro `PIN_<NET>`.
- `firmware.yaml` declares each GPIO net's role: `mode: input|output` (+ `pull`, `initial`) or `function:` (e.g. `UART0_TX`, `ADC0`, `PWM7_B`, `I2C0_SDA`). `eda part show raspberry_pi_pico` lists which functions each pin has.
- `uv run eda fw projects/<name>` validates roles against the pinout, finds conflicts, writes `firmware/pins.h` and `firmware/pins_init.h` (never edit those by hand), and scans firmware sources.
- Firmware code uses only `PIN_` macros and `pins_init()`; hard-coded GPIO numbers are warnings, unknown macros are errors.
- `uv run eda fw projects/<name> --build` also compiles `firmware/` with the pico-sdk into `out/fw-build/` (`eda run` does it whenever the SDK is installed). Compiler and linker messages come back as `fw_compile_error` / `fw_compile_warning` / `fw_link_error` with file and line; `data.build.size` gives flash/RAM use. SDK setup: `tools/fetch-pico-sdk.sh` plus `gcc-arm-none-eabi`.
- Simulation follows firmware.yaml (outputs drive, pulls pull, others are high-Z). Test each output state with `pins: { LED: high }` in a test.
