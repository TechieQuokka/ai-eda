# AI-Native EDA — Design Decisions

> Companion to `PROJECT_SUMMARY.md`. Records what was discussed and decided on 2026-10-20.
> Status: M0–M3 complete (2026-10-20). M0: §10–§11. M1: §12. M2: §13. M3: §14.

---

## 1. Usage Model

The human gives an abstract goal inside Claude Code. Claude edits design files, runs the `eda` CLI to check / simulate / render, iterates until all criteria pass, and shows the results.

```
Human:  "Drive three LEDs from 5V with adjustable brightness"
   ↓
Claude: write design YAML → eda check → eda sim → eda render → fix → repeat
   ↓
Human:  watches schematic · waveforms · board · verdicts, makes the final call
```

Three equally important pillars:

| Pillar | Handled by |
|---|---|
| Circuit / firmware design | Claude writes YAML and C; `eda check` runs ERC and pin-conflict checks |
| Simulation | `eda sim` runs ngspice; results become a JSON verdict plus waveform data |
| Visualization | Schematic and PCB rendered via KiCad (`kicad-cli` SVG export, 3D render); waveforms as interactive charts |

Because the human is the observer, visualization quality is a first-class requirement, raised above the "simple is fine" bar in `PROJECT_SUMMARY.md` §6. Schematic auto-layout is the hard part; candidates are KiCad output and an auto-layout tool such as netlistsvg.

## 2. Settled Decisions

| # | Topic | Decision | Rationale |
|---|---|---|---|
| 1 | Implementation language | **Python** | The ecosystem favors Python: KiCad automation API and file tools, SKiDL / atopile, numpy / matplotlib. Rust was considered for type safety; that concern is covered by strict static typing (see §3). Heavy compute can move to Rust later via PyO3. |
| 2 | Design file format | **YAML** | Readable, supports comments, handles deep nesting, easy for an LLM to edit. Pitfalls (indentation, implicit coercion such as `no` → false) are blocked by a strict parser plus pydantic validation. |
| 3 | Fork tscircuit | **No** | It is TypeScript/TSX and does not fit decision #1. atopile and SKiDL (Python) are studied as references. tscircuit features (e.g. autorouter) may be invoked as an external tool later. |
| — | Result viewing | **Local browser viewer** | `eda watch` auto-runs check / sim / render on file change and pushes updates to a local page. |
| — | Repository layout | **Monorepo** | CLI source and design projects live in one repository, so the tool can be fixed immediately when design work exposes a gap. |

## 3. Type-Safety Strategy

| Layer | Tool | Purpose |
|---|---|---|
| Static | pyright (strict) | Missing or wrong type hints are errors |
| Runtime | pydantic v2 | Design files validated on load; errors report file, line, field, and reason as JSON |
| Lint / test | ruff, pytest | Bundled into one command that must pass before work is considered done |

Exact library versions are chosen at setup time by checking the latest releases.

## 4. Local Viewer (draft)

```
eda watch  ─ detect design file change → run check / sim / render
   └─ local server (http://localhost:<port>)
        ├─ schematic (SVG)
        ├─ waveforms (interactive, zoomable chart)
        ├─ board (PCB SVG / 3D render)
        └─ verdicts + iteration log (what changed, what failed, how it was fixed)
```

Server in Python; page is lightweight static HTML. Libraries to be selected at implementation time.

## 5. Project Skeleton (created 2026-10-20)

```
ai-eda/
 ├─ PROJECT_SUMMARY.md
 ├─ DESIGN_DECISIONS.md
 ├─ CLAUDE.md              # dev rules + design work loop
 ├─ pyproject.toml         # uv, Python 3.13, pyright strict, ruff, pytest
 ├─ src/eda/
 │   ├─ cli.py             # entry: eda check | sim | render | watch
 │   ├─ result.py          # unified JSON result / error format
 │   ├─ schema/            # pydantic models (circuit, test, part)
 │   ├─ loader.py          # strict YAML → models, line-numbered errors
 │   ├─ netlist/           # models → SPICE netlist
 │   ├─ adapters/          # ngspice.py, kicad.py (thin subprocess wrappers)
 │   ├─ verify/            # test-criteria evaluation, ERC
 │   ├─ render/            # schematic SVG, waveform data
 │   └─ viewer/            # local server + static HTML
 ├─ parts/                 # built-in part DB (one YAML per part, vendor models)
 ├─ projects/              # design projects (layout from PROJECT_SUMMARY.md §9)
 │   └─ led_demo/
 │       ├─ project.yaml
 │       ├─ design/circuit.yaml
 │       ├─ design/test.yaml
 │       └─ out/           # generated artifacts (git-ignored)
 ├─ tests/                 # tests for the CLI itself
 └─ eval/                  # tool evaluations (git-ignored)
```

Changes from `PROJECT_SUMMARY.md` §9:
- Design files use `.yaml` instead of `.eda`.
- Built-in parts live in the root `parts/`; project-specific parts live in each project's own `parts/`.

Git is initialized, per core principle 1 ("text is the source of truth, version-controlled with git"). `result.py`, `loader.py`, and the subpackage contents are written in M0.

## 6. `CLAUDE.md` Contents (written 2026-10-20)

1. **Working rules**: English for artifacts, Korean for conversation; explicit approval before substantive work; one question at a time; respect context; stay inside the project directory; use the latest library versions.
2. **Tool development rules**: type hints required; `pyright --strict`, `ruff`, and `pytest` must all pass; external tools are called only through `adapters/`.
3. **Design work loop**: edit YAML → `eda check` → `eda sim` → `eda render` → on failure read the JSON errors and fix. The CLI never makes design decisions.
4. **Iteration log**: each loop records what changed, what failed, and how it was fixed (shown in the viewer).

## 7. Deferred Decisions

Not needed for M0; revisit at the listed milestone.

| # | Topic | Default leaning | Revisit at |
|---|---|---|---|
| 6 | Board complexity | 2-layer (decided in M2, §13); multi-layer later if needed | — |

## 8. Research Snapshot (2026-10)

| Project | Latest seen | Notes |
|---|---|---|
| tscircuit | 0.0.2962 (npm, 2026-10-19) | TSX framework with ngspice (WASM) simulation, local autorouter, KiCad / DSN / netlist export. Pre-1.0 with daily builds. `tsci` requires the Bun runtime. |
| atopile | 0.15.9 (2026-09) | Core rewrite in 0.14 (2026-01). Emits a netlist and updates the KiCad layout; place-and-route stays in KiCad. No built-in simulation found. |
| SKiDL | 2.0.1 (2024-12, third-party source) | Python netlist generation; slow-moving. |
| KiCad / kicad-cli | 10.0.6 (10.0 released 2026-03) | Graphical DRC rule editor, design blocks, variants. A headless `kicad-cli api-server` is mentioned in post-10 development discussion; unconfirmed whether 10.0 includes it. |
| ngspice | 46 (distro-confirmed); 47 reported (2026-08) | Official page not yet confirmed. |

Confirmed releases: ngspice 47 (SourceForge `ng-spice-rework/47`), KiCad 10.0.7 (PPA `kicad/kicad-10.0-releases`).

### Installed Environment (2026-10-20)

| Tool | Version | Install method |
|---|---|---|
| Python | 3.13.12 | Pre-installed; kept as is (`requires-python = ">=3.13,<3.14"`) |
| Node / uv | 22.22.2 / 0.12.13 | Pre-installed; kept as is |
| Python deps | pydantic 2.14.1, ruamel.yaml 0.19.1; dev: pyright 1.1.414, ruff 0.17.1, pytest 9.1.1 | `uv sync` (pinned in `pyproject.toml`) |
| ngspice | 47 | Built from source in `build/ngspice-47/` (git-ignored): two configure runs, `release/` (CLI, `--without-x --enable-openmp --with-readline=yes`) and `shared/` (`--with-ngshared`), installed to `/usr/local` |
| libngspice | 47 (`/usr/local/lib`, takes precedence) | From the `shared/` build. Ubuntu's `libngspice0` 42 is also present as a KiCad dependency |
| KiCad / kicad-cli | 10.0.7 | `ppa:kicad/kicad-10.0-releases`, `apt-get install --no-install-recommends kicad kicad-symbols kicad-footprints` |
| KiCad 3D models | — | Not installed (`kicad-packages3d` is several GB and the PPA is slow); install when 3D rendering is needed (M2) |

Notes: Ubuntu 24.04's own repositories only offer ngspice 42 and KiCad 7.0.11, hence the source build and PPA. Bun is not installed; the aborted tscircuit evaluation install sits in `eval/tscircuit/`.

Sources:
- https://github.com/tscircuit/tscircuit-standalone/releases
- https://tscircuit.com/
- https://pypi.org/project/atopile/
- https://atopile.io/enterprise
- https://data.safetycli.com/packages/pypi/skidl/
- https://www.kicad.org/blog/2026/03/Version-10.0.0-Released/
- https://forum.kicad.info/t/post-v10-new-features-and-development-news/66611
- https://en.wikipedia.org/wiki/Ngspice

## 9. Next Steps

1. ~~Create the project skeleton and `CLAUDE.md`, initialize git.~~ Done.
2. ~~Install ngspice and KiCad (kicad-cli); Python dependencies via uv.~~ Done.
3. ~~Agree on the M0 schemas and the unified JSON result format.~~ Done (§10).
4. ~~Implement M0.~~ Done.
5. ~~Run the "LED current to 10 mA" scenario and note friction points.~~ Done (§11).
6. Fix the friction points in §11, then start M1.

## 10. M0 Specification (decided 2026-10-20)

The user approved the recommended defaults and delegated implementation-level choices; they are recorded here.

### 10.1 `circuit.yaml` — net-centric

```yaml
parts:
  V1: { type: dc_voltage_source, voltage: 5V }
  R1: { type: resistor, resistance: 330ohm }
  D1: { type: led, color: red }
nets:
  VCC: [V1.+, R1.1]
  N1:  [R1.2, D1.anode]
  GND: [D1.cathode, V1.-]
```

- `parts`: reference → `{type, <params>...}`. Params are flat keys checked against the part definition.
- `nets`: net name → list of `REF.PIN`. A net named `GND` is required and is the simulation ground (SPICE node `0`).
- References and net names match `[A-Za-z_][A-Za-z0-9_]*` and must be unique ignoring case (SPICE is case-insensitive).
- Every pin connects to exactly one net; every net has at least two pins; every net needs a DC path to `GND`.

### 10.2 Values

- Format: number + optional SI prefix + **mandatory unit**: `4.7kohm`, `100nF`, `10mA`, `1Mohm`, `5ms`.
- Prefixes are case-sensitive: `p n u µ m k M G` (`m` = milli, `M` = mega).
- Units are checked per field: `ohm`/`Ω`, `F`, `H`, `V`, `A`, `s`, `Hz`.
- Values are converted to SI floats on load and written to SPICE in exponent form (`4.7e+03`), avoiding SPICE's own prefix rules (`M` = milli there).

### 10.3 `test.yaml`

```yaml
tests:
  led_current:
    analysis: { type: op }
    expect:
      - { measure: current(D1), min: 9.5mA, max: 10.5mA }
      - { measure: voltage(N1), max: 3V }
  step_response:
    analysis: { type: tran, step: 10us, stop: 5ms }
    expect:
      - { measure: final(voltage(OUT)), min: 4.9V }
```

- Probes: `voltage(NET)`, `voltage(NET_A, NET_B)` (difference), `current(REF)`.
- Current sign: positive when flowing into the part's first pin and through the part (`R.1 → R.2`, `anode → cathode`, into `+` for sources; a source delivering power reads negative).
- Reductions: `max`, `min`, `avg`, `rms`, `pp` (peak-to-peak), `initial`, `final`. Required for `tran`; optional for `op`.
- Each expectation needs `min`, `max`, or both (inclusive).

### 10.4 Part definitions (`parts/<id>.yaml`)

One YAML file per part type: pins, params (`unit` or `choices`, optional `default`), SPICE template, current probe, schematic symbol, `dc_path` (false for capacitors). M0 parts: `dc_voltage_source`, `pulse_voltage_source`, `resistor`, `capacitor`, `inductor`, `diode`, `led`. LED colors use Shockley models fitted to typical forward voltage at 10 mA (red 1.85 V, yellow 2.0 V, green 2.1 V, blue/white 3.0 V). Project-local `parts/` may add types; duplicates are errors.

### 10.5 Results and exit codes

Every command prints one JSON object to stdout and writes it to `out/<command>.json`:

```json
{"ok": false, "command": "sim", "project": "led_demo",
 "errors": [{"rule": "expect", "message": "current(D1) = 9.31mA, expected 9.5mA..10.5mA",
             "file": "design/test.yaml", "line": 5, "column": 9,
             "path": "tests.led_current.expect[0]", "details": {"value": 0.00931}}],
 "warnings": [], "data": {}}
```

Exit codes: `0` all passed, `1` design errors (fix the files), `2` tool or environment failure.

### 10.6 Commands

| Command | Does |
|---|---|
| `eda check [PROJECT]` | Load, schema and value validation, ERC, test compilation |
| `eda sim [PROJECT] [--test NAME]` | `check`, then one ngspice batch run per test, verdicts |
| `eda render [PROJECT]` | Schematic SVG (netlistsvg auto-layout) + waveform SVGs from the last sim; PNG copies when a rasterizer is available |
| `eda run [PROJECT]` | `check` → `sim` → `render` in one call |
| `eda part list` / `eda part show ID` | Inspect the part database |
| `eda log [PROJECT] -m TEXT` | Append a note (intent / fix) to the iteration log |
| `eda value TARGET [-s E24]` | Nearest standard values (E3 … E192) for a target, e.g. `315ohm` → `316Ω` in E96 |
| `eda watch [PROJECT]` | Re-run `run` on file change; serve the local viewer |

`check`, `sim`, `render`, and `run` accept `-m TEXT` to attach a note to that run's log entry.

### 10.7 Engines and generated files

- ngspice: `ngspice -b -n` with a `.control` block, `.option savecurrents`, ASCII raw output, parsed in Python.
- netlistsvg 1.0.2, pinned in `tools/netlistsvg/` and run with Node, using our skin `src/eda/render/skin.svg` (its analog skin plus +/− marks on voltage sources and values on diodes/LEDs). Net names are added afterwards next to each net's longest wire; GND and supply nets keep their symbols.
- Rasterizing for AI inspection: CairoSVG 2.9.1 in-process (needs the system cairo library), then `rsvg-convert`, then headless Chrome/Chromium.
- Generated: `out/check.json`, `out/sim.json`, `out/render.json`, `out/sim/<test>.{cir,raw,log,json}`, `out/schematic.svg`, `out/waves/<test>.svg`.
- Iteration log: `iterations.jsonl` in the project root (tracked by git, not regenerated). Each command appends its outcome, the changed design files, a semantic diff (`R1.resistance: 470Ω → 316Ω`, added/removed parts, net membership, test changes), and an optional `-m` note. Times are local with UTC offset. `eda log` adds standalone notes.

### 10.8 Viewer

`eda watch` runs a stdlib HTTP server (`127.0.0.1`), pushes updates with Server-Sent Events, and watches files with watchfiles. The page shows the schematic, verdict table, interactive waveforms (zoom and hover), and the iteration log. No external CDN.

## 11. M0 Result (2026-10-20)

### Scenario: "make the LED current 10 mA"

`projects/led_demo/` started from R1 = 470 Ω with the test `current(D1)` in 9.5–10.5 mA.

| Iteration | Change | `eda run` result |
|---|---|---|
| 1 | — | fail: `current(D1) = 6.752mA, expected 9.5mA..10.5mA (too low by 2.748mA)` at `design/test.yaml:6`; operating point showed V(N1) = 1.83 V |
| 2 | R1 = (5 V − 1.85 V) / 10 mA ≈ 315 Ω → E96 316 Ω | pass: 9.969 mA, V(N1) = 1.85 V |

The JSON error and the operating-point table gave everything needed for the fix; no tool change was required mid-loop. The commits `6cbf099` → `bd15ba7` and `projects/led_demo/iterations.jsonl` record the loop.

### What exists

- Commands: `check`, `sim`, `render`, `run`, `part list|show`, `log`, `watch` (§10.6).
- 7 built-in parts; ERC (unconnected pins, single-pin nets, missing ground, no DC path, shorts, case collisions).
- ngspice 47 batch runs (`op`, `tran`) with seven reductions; netlistsvg schematics; SVG waveforms; PNG copies via headless Chrome.
- Live viewer: schematic, verdicts, operating point, interactive waveforms (zoom, pan, hover), iteration log, errors with file:line. `?snapshot` disables the live connection for headless screenshots.
- Examples: `projects/led_demo/` (op), `projects/rc_filter/` (tran + op).
- 79 tests; ruff, pyright strict, pytest all pass.

### Friction points and fixes

| # | Friction found in the scenario | Fix (2026-10-20) |
|---|---|---|
| 1 | Schematic did not show net names such as `N1`, `OUT` | Net labels drawn next to each net's longest wire (`render/schematic.py: label_nets`) |
| 2 | No +/− on voltage sources; LED color not shown | Own netlistsvg skin `src/eda/render/skin.svg` |
| 3 | The "why" of a change was only logged by a separate `eda log` call | `-m` on `check/sim/render/run`; every summarized run also logs a semantic diff of the design |
| 4 | PNG via headless Chrome took about 1 s per image | CairoSVG in-process (about 0.03 s per image); Chrome stays as fallback |
| 5 | No help picking standard values | `eda value 315ohm -s E96` → 316 Ω (+0.32 %), with neighbors below/above |
| 6 | Log timestamps were UTC | Local time with offset (`2026-10-20T18:21:53+09:00`) |

Remaining known limits: netlistsvg places parts automatically with no hints (fine for small circuits; KiCad-based rendering is planned for M2), and waveform bound labels can overlap traces near the right edge.

## 12. M1 Specification and Result (2026-10-20)

Goal (from §12 of `PROJECT_SUMMARY.md`): real ICs simulate with pass/fail tests.

### 12.1 Decisions

| Topic | Decision | Why |
|---|---|---|
| Vendor model files | Imported into `parts/vendor/` (git-ignored) with `eda model import`; never committed | The repository is public. Vendor models carry their own copyright and often forbid redistribution (e.g. Microchip's license limits use to its products) |
| Built-in op-amp | Own macromodel `parts/models/eda_opamp.lib` (part `opamp`): dominant pole from `aol`/`gbw`, slew limit via `tanh`, soft rail clamp, `rin`, `rout`, `iq` | License-clean default that works on a fresh clone; vendor models when a specific chip matters |
| PSpice models | `spice.compat: ps` on a part runs ngspice with `-D ngbehavior=ps`; `eda model import` detects the mode by loading the model both ways | TI TLV9002/OPA171 need it; TL072 does not |
| Currents | Every current is measured through a 0 V sense source: `current(R1)` flows into the first pin; multi-pin parts (`pin_currents: true`) expose `current(U1.out)` per pin. `.option savecurrents` is no longer used | `savecurrents` reports no AC currents for devices inside subcircuits, which made `write all` fail; sense sources work in every analysis with one sign convention |
| AC analysis | `analysis: { type: ac, start, stop, points_per_decade }`; sources take an `ac` magnitude; an AC test without an AC source is an error | Frequency response is the core op-amp requirement |
| Measures | Nested functions: reductions (`max min initial final avg rms pp`), `at(trace, X)`, `cross(trace, LEVEL)`, AC transforms `db mag phase`; type-checked per analysis with the result unit | One grammar for gain, bandwidth, phase, timing (`cross` in tran gives rise/fall times) |
| Units | Added `dB`, `deg`, `V/s` (written `V/us`), and the `T` prefix | Op-amp parameters and AC bounds |
| YAML commas | A measure with a comma inside `{ ... }` must be quoted; the loader detects the split and says so | Found while writing the first AC test |

### 12.2 Commands added

| Command | Does |
|---|---|
| `eda model inspect FILE` | Lists `.subckt` names, nodes, default params, and the pin-description comments around them |
| `eda model import FILE --id ID [--subckt NAME] [--opamp IN+,IN-,V+,V-,OUT]` | Copies the model into `parts/vendor/models/`, writes `parts/vendor/<id>.yaml`, detects PSpice mode, and checks that the part loads |

### 12.3 Result

| Project | Part | Tests |
|---|---|---|
| `projects/opamp_amp` | built-in `opamp` (GBW 1 MHz), gain 11 | op: offset 1.3 mV, Iq 1 mA; tran: ±5.5 V; ac: 20.83 dB at 1 kHz, −3 dB at 90.3 kHz (theory 91 kHz), −0.64° at 1 kHz — all pass |
| `projects/tl072_amp` | TI TL072 vendor model (imported) | op: offset 0.12 mV; tran: ±11 V; ac: 20.83 dB, −3 dB at 320.7 kHz — all pass |
| (local check) | TI TLV9002 vendor model, PSpice mode | Iq 60.4 µA (datasheet 60 µA), −3 dB at 95.2 kHz for gain 11 (GBW 1 MHz) — all pass |

`tl072_amp` needs the model imported first:

```bash
uv run eda model import <path>/TL072.301 --id tl072 --opamp 1,2,3,4,5
```

(ngspice's source tarball ships TI's file as `examples/optran/models/TL072.301`.)

### 12.4 Findings

1. **Vendor macromodels do not model everything.** The 1989 TL072 model draws 14 mA from ±15 V through a dummy resistor (`RP 3 4 2.143k`), versus 1.4 mA in the datasheet, so a supply-current requirement cannot be tested with it. TLV9002's newer model matches its datasheet. Tests should only check what the model claims to model (TI lists those in each file's header).
2. ~~**TLV6001 does not reach an operating point** in ngspice without `.option optran`~~ — corrected in §18: ngspice 47 already falls back to a 10 µs transient operating point, which for TLV6001 is wrong (unsettled).
3. **Quoting**: measures with commas need quotes in flow mappings; the error message now says how.
4. **Schematics of op-amp circuits** with split supplies are readable but crowded (netlistsvg places supply sources freely). KiCad-based rendering remains the plan for M2.

## 13. M2 Specification and Result (2026-10-20)

Goal (`PROJECT_SUMMARY.md` §12): a board is produced from design files with DRC results as JSON.

### 13.1 Decision #4 — PCB source of truth

**Our YAML is the source of truth; KiCad files are build outputs** (decided with the user). Everything under `out/pcb/` is regenerated on each run, so the agent only ever edits `circuit.yaml` and `board.yaml`.

### 13.2 `design/board.yaml`

```yaml
board:                 # all optional
  width: 50mm          # with height; omit both to fit the board to the parts
  height: 30mm
  margin: 3mm          # around parts when fitting
  spacing: 3mm         # gap between auto-placed parts
rules:
  clearance: 0.2mm
  track_width: 0.4mm
  via_diameter: 0.8mm
  via_drill: 0.4mm
placement:             # parts not listed are packed automatically
  V1: { x: 5mm, y: 6mm, rotation: 90deg, side: top }
routing:
  passes: 20
```

- Footprints: each part type has a default `pcb: { footprint, pads, nc }` (KiCad `Library:Name`, pin → pad number, pads meant to stay open). A part instance can override with `footprint:` and `pads:` in `circuit.yaml` (e.g. one channel of a dual op-amp in DIP-8).
- Sources become 2-pin headers: on a board, a supply or signal source is the connector where it enters.
- `eda check` validates the board: the footprint exists, every pin has a pad, every pad exists, placements name real parts; IC pads left unconnected produce a `unused_pads` warning.
- Length unit `m` was added (`5mm`, `0.25mm`).

### 13.3 Toolchain

| Step | Tool | Notes |
|---|---|---|
| Board creation, placement, nets, rules, outline | KiCad 10 `pcbnew` Python API, run under KiCad's Python (`/usr/bin/python3`, 3.12) by `src/eda/adapters/pcbnew_script.py` | Separate interpreter from eda's 3.13; JSON in/out over a pipe |
| Autorouting | Freerouting **2.1.0** jar on the existing Java 21, via Specctra DSN/SES | 2.2+ jars need Java 25; the 2.5.0 native CLI hangs (GraalVM serialization error). Pinned with SHA-256 in `tools/freerouting/fetch.sh`; analytics disabled, settings kept in `tools/freerouting/data/` |
| Stub cleanup | pcbnew connectivity | Track ends left dangling by the router are removed after import |
| DRC | `kicad-cli pcb drc --format json` | Each violation becomes an `Issue` (`drc_<type>`), pointing at the part's placement or definition; positions in mm |
| Fabrication | `kicad-cli pcb export gerbers/drill/pos` + our BOM | Two-layer layer set only; zipped as `out/pcb/<name>-fab.zip` |
| Images | `kicad-cli pcb export svg` (+ PNG) and `kicad-cli pcb render` (3D, no 3D models installed) | Shown in the viewer's Board panel |

**Determinism.** Freerouting gives different tracks on repeated runs of the same `.dsn`; the generated `.dsn` itself is deterministic. Sessions are therefore cached by the hash of the `.dsn` (plus passes and router version) in `out/pcb/route-cache/`, so the same design always yields the same board on a machine. A fresh clone routes once and then stays stable.

### 13.4 Result

| Project | Board | Routing | DRC |
|---|---|---|---|
| `led_demo` | 28 × 22 mm, V1 placed by hand, rest packed | fully routed, 0 vias | 0 errors, 0 warnings |
| `opamp_amp` | 36 × 26 mm, SOIC-8 + THT parts | fully routed | 0 errors, 0 warnings |
| `tl072_amp` | 39 × 31 mm, DIP-8 TL072 (channel A) | fully routed, 0 vias | 0 errors, 0 warnings; `unused_pads` warning for channel B (pads 5–7) |

`eda pcb` takes about 6 s for these boards (most of it Freerouting's JVM start). `eda run` includes `pcb` whenever `design/board.yaml` exists.

### 13.5 Not done in M2 / findings

1. ~~**KiCad schematic (`.kicad_sch`) generation** is not done~~ (done in §19); the schematic stays netlistsvg. The board is generated straight from the netlist, which is what fabrication needs.
2. ~~**Placement is naive**~~ (done in §17) (shelf packing in definition order). Connectivity-aware placement would shorten tracks; `placement:` covers hand placement meanwhile.
3. ~~**No copper pours / ground plane** yet.~~ Done in §16.
4. **3D render has no component bodies** because `kicad-packages3d` (several GB) is not installed.
5. Dual-channel parts: an imported single-channel model on a dual package leaves the other channel floating; the `unused_pads` warning says so, but tying it off is still a manual design step.

## 14. M3 Specification and Result (2026-10-20)

Goal (`PROJECT_SUMMARY.md` §12): changing a pin in the schematic updates `pins.h` and flags conflicts.

### 14.1 Decision #5 — target MCU

**RP2040, as the Raspberry Pi Pico module** (decided with the user): cheap, free toolchain (pico-sdk + GCC), a simple and regular pin-function table, and a 40-pin DIP module that is easy to put on a two-layer board. Correction to the earlier rationale: Renode's RP2040 support comes from a community plugin, not upstream; it will be verified at M4.

### 14.2 Model

- **Part `raspberry_pi_pico`**: 33 pins (GP0–GP22, GP26–GP28, GND, 3V3, 3V3_EN, VSYS, VBUS, RUN, ADC_VREF), an `mcu:` facet with each GPIO's functions generated from the RP2040 datasheet's function-select table (SPI, UART, I2C, PWM slice/channel, ADC0–2, PIO0/1), and the Pico footprint (`GND` owns eight pads). All pins except GND are `optional_pins`, so unused GPIOs are not ERC errors.
- **The schematic decides which GPIO a net reaches; `design/firmware.yaml` says how firmware uses that net**:

```yaml
mcu: U1
signals:                      # net name → role; macro is PIN_<NET>
  LED:     { mode: output, initial: low }
  BUTTON:  { mode: input, pull: up }
  POT:     { function: ADC0 }
  UART_TX: { function: UART0_TX }
```

- **Simulation follows the firmware**: the MCU becomes a 3.3 V source (3V3 pin behind 1 Ω) with each used GPIO driven high/low through 50 Ω, pulled up/down through 50 kΩ, or high-impedance; UART TX and SPI CSN idle high. A test can override levels: `pins: { LED: high }`.
- New parts for this: `push_button` (state open/pressed), `potentiometer` (resistance, position %), `connector_1x02/03/04` (off-board headers). Templates may span several lines; `<choice:P>` maps a choice to a SPICE value.

### 14.3 Checks (`eda check`, `eda fw`)

| Rule | When |
|---|---|
| `pin_function` | The function is not on that pin; the message lists the pins that have it |
| `pin_conflict` | One peripheral signal (e.g. UART0_TX) on two nets |
| `pwm_shared` (warning) | Two nets on the same PWM slice channel |
| `signal_pin` | A declared net reaches no GPIO (or several) of the MCU |
| `undeclared_gpio` (warning) | A GPIO is wired but firmware.yaml does not say how it is used |
| `gpio_overvoltage` | A source above VIO + 0.3 V drives a GPIO net |
| `undefined_pin` | Firmware source uses a `PIN_` macro the design does not define (file:line) |
| `hardcoded_gpio` (warning) | `gpio_put(15, …)`-style literals; suggests the macro |
| `unknown_signal` | A test overrides a pin that is not a firmware signal |

### 14.4 Generated files

`eda fw` (and `eda run` when firmware.yaml exists) writes `firmware/pins.h` (`#define PIN_LED 15u` with role comments) and `firmware/pins_init.h` (a pico-sdk `pins_init()` that sets direction, pulls, initial levels, and peripheral functions). The viewer shows the pin table.

### 14.5 Result

`projects/pico_io` (Pico, LED, button, potentiometer, UART header, 5 V on VSYS):

- Simulation: LED 6.1 mA with `LED: high`, 0 A with `LED: low`; BUTTON 3.3 V via the internal pull-up; POT 1.65 V at mid-scale; UART TX idles at 3.3 V — all pass.
- Firmware: `pins.h` / `pins_init.h` generated; `firmware/main.c` uses only `PIN_` macros and passes the scan.
- Board: fully routed, 0 DRC errors (Pico placed by hand, the rest packed).
- Done criterion, as tests (`tests/test_m3.py`): moving `LED` from GP15 to GP16 in `circuit.yaml` turns `#define PIN_LED 15u` into `16u`; moving `UART_TX` to GP2 fails with "UART0_TX is not available on GP2; it is on GP0, GP12, GP16, GP28".

### 14.6 Not done / findings

1. **The firmware is not compiled.** pico-sdk and `arm-none-eabi-gcc` are not installed (the SDK with submodules is a large download, the compiler needs `sudo apt`); `CMakeLists.txt` is ready for when they are. This is the first step of M4.
2. **Static pin model only**: no timing, PWM duty, or ADC conversion. That is M4's co-simulation.
3. **Auto-placement around a 51 mm module is poor**; hand placement of the MCU (as in `pico_io/design/board.yaml`) is needed for a sensible board.
4. Another flow-mapping comma trap (`description: LED, active high`) hit while writing firmware.yaml; the loader's hint now covers any split value, not just measures.
5. Pico's internal nets (GP23–GP25, GP29, the on-board LED) are not modelled.

## 15. M4 Specification and Result (2026-10-20)

Goal (`PROJECT_SUMMARY.md` §12): the firmware runs against the simulated circuit.

### 15.1 Firmware build (`eda fw --build`)

- **Toolchain**: Ubuntu's `gcc-arm-none-eabi` 13.2 + newlib (system packages) and pico-sdk **2.3.1** in `tools/pico-sdk/` (git-ignored, `tools/fetch-pico-sdk.sh`; `PICO_SDK_PATH` overrides). The SDK builds its own `picotool` into the build directory on the first build (~20 s); later builds are incremental.
- **Adapter** `adapters/picosdk.py`: configures `firmware/CMakeLists.txt` into `out/fw-build/` with `PICO_BOARD` from the MCU part (`mcu.sdk_board`), builds the `add_executable` target, and parses the log into messages: gcc `file:line:col: error|warning:`, ld `file:line:(section): undefined reference`, and `CMake Error|Warning at file:line`.
- **Issues**: `fw_compile_error`, `fw_compile_warning`, `fw_link_error`, `fw_cmake_error`, `fw_cmake_warning`, `fw_build_failed` (no parsable message), `fw_too_big` (flash/RAM over `mcu.flash_bytes`/`ram_bytes`), `fw_toolchain` (missing SDK/compiler; exit code 2). Warnings from files outside the project (the SDK's own CMake and sources) are counted in `sdk_warnings_hidden`, not reported.
- **Warnings on every build**: the objects of the project's own sources are deleted before each build, so their warnings are reported every time, not only when the file changed; SDK objects stay cached.
- `eda run` builds when the SDK is present and warns `fw_build_skipped` otherwise. The example firmware is compiled with `-Wall -Wextra`.
- Result: `pico_io` builds cleanly: 21 KB flash (1.0 %), 1.2 KB RAM.

### 15.2 Emulator: rp2040js

Verified before choosing (HANDOFF §4.2 criteria: runs the pico-sdk image unmodified, exposes GPIO/ADC/UART to an external process, headless and scriptable):

| Candidate | Finding |
|---|---|
| **rp2040js 1.4.1** (Wokwi, MIT, Node) | Runs pico-sdk UF2 images (its CI builds pico-sdk examples); GPIO pin states with listeners, `setInputValue`, ADC `channelValues`, UART `onByte`, PWM drives pins; the clock skips ahead while the core waits (WFE), so `sleep_ms` is cheap. **Chosen.** |
| Renode | No RP2040 platform upstream (only a community plugin); needs Mono/.NET. |
| QEMU | No RP2040 machine upstream (only Cortex-A Raspberry Pi boards). |

- Pinned in `tools/rp2040/` (`package.json` + lock, like netlistsvg). rp2040js needs the RP2040 boot ROM: `tools/rp2040/fetch-bootrom.sh` downloads revision B2 from `raspberrypi/pico-bootrom-rp2040` (SHA-256 pinned) into the git-ignored `tools/rp2040/dl/` (part of it has its own license; not redistributed).
- `adapters/rp2040_driver.mjs` runs the core instruction by instruction to a target time and answers line-delimited JSON (`init`, `run`); `adapters/rp2040.py` is the Python side. Only core 0 is emulated (rp2040js limitation).
- Speed: 2.2 s of `pico_io` firmware time in ~30 ms of emulator time; the whole co-simulated test takes ~1 s, dominated by ngspice.

### 15.3 Co-simulation (level 2)

- **ngspice in-process**: `adapters/ngspice_shared.py` drives libngspice through ctypes. A transient run is halted at each window boundary (`ngSpice_SetBkpt` makes a time point land exactly there, `stop when time >= t` halts on it), sources are changed with `alter`, and the run `resume`s. The result is written as the usual ASCII raw file, so measures, waveforms, and the viewer work unchanged.
- **Pin model**: every firmware-driven pin (GPIO `mode:` and PWM) is a Thevenin source (voltage source + resistor) named per pin. States map to high/low behind `drive_ohm`, pull-up/down behind `pull_ohm`, high impedance as 100 MΩ. Within a window the states are averaged as Norton equivalents (mean conductance, mean current), so a PWM output becomes its average and an edge inside a window becomes one intermediate step. UART/SPI/I2C pins keep their static model; UART TX bytes come from the emulated peripheral.
- **Loop** (window = the test's `step`, ngspice max step = window/5): read inputs at the circuit time (digital through RP2040 Schmitt thresholds VIH 2.0 V / VIL 0.8 V scaled to `vio`; ADC as `round(V / vio × 4095)`), run the emulator to the end of the window, apply the averaged pin sources, run ngspice to the same time.
- **Mode changes stop the window**: inputs lag by one window, which broke the first real run: `pins_init()` enables BUTTON's pull-up and the firmware reads it microseconds later, still seeing the reset pull-down (0 V, "pressed"), so the first blink was 100 ms instead of 500 ms. Now the emulator stops right after any change other than a high↔low output toggle; the circuit settles on the new state for window/100 before the firmware continues.
- **Tests**: `firmware: true` (tran only, needs firmware.yaml, no `pins:` overrides); `parts:` per-test parameters (reusable by any test); `period(trace, LEVEL)` measure; serial expectations `{ serial: NET, match: REGEX, min, max }` with rules `serial_needs_firmware`, `serial_net`, `invalid_match`. Errors: `firmware_not_built` (build failed), `cosim_failed` / `fw_toolchain` (tool errors, exit 2).
- `eda sim` builds the firmware before co-simulated tests; `eda run` reuses the image built in its fw step.

### 15.4 Result

`projects/pico_io` (done criterion of HANDOFF §4.4):

| Test | Result |
|---|---|
| `firmware_blink` (button released, pot at 50 %) | LED 6.1 mA, off at 700 ms, blink period 1000 ms; UART `pot=2047` |
| `firmware_button` (`SW1: pressed`, `RV1: 25%`) | blink period 200 ms (the firmware reads the button through the circuit); UART `pot=1024`, 12 lines |

- Co-simulation found a real firmware bug: `main.c` called `uart_init` but never `stdio_init_all`, so `printf` went nowhere. Fixed.
- `tests/test_m4.py`: log parsing, missing SDK, compile error → fix → warning, Norton averaging, `period`, test rules, and firmware-in-the-loop (changing the blink delay in `main.c` makes `period` fail at 500 ms).

### 15.5 Not done / findings

1. Inputs still see the circuit one window late for output→input paths (e.g. a GPIO looped back to another through an RC); choose `step` well below the timing under test.
2. Only core 0; no USB, no PIO timing beyond rp2040js's model; the ADC reference is taken as `vio`.
3. `ngSpice_nospiceinit()` crashes libngspice 47 when called before `ngSpice_Init` as documented, so the shared-library runs do read a user `.spiceinit` (batch runs use `-n`).
4. ~~Serial input to the firmware (UART RX) is not modelled yet.~~ Done in §15.6.
5. The co-simulated netlist adds two elements per firmware pin; a design with dozens of PWM pins changing every window will be slower (each change is two `alter`s).

### 15.6 Stimulus: events during a run (2026-10-20)

Tests could only hold inputs fixed for a whole run, so firmware behaviour over time (reacting to a press, parsing a command) could not be tested.

- **`events:`** in a `tran` test, each at a time `at`:
  - `part` + `set`: change part parameters mid-run (`{ at: 1.1s, part: SW1, set: { state: pressed } }`). Implemented with ngspice `alter` on the element values; `netlist.spice.alter_commands` re-renders the part's template with the new values and accepts only lines whose R/C/L value changed (values may be `{...}` expressions, evaluated by a small AST evaluator: numbers, + − × ÷, max/min/abs). Anything else (a source voltage, a model change) is `event_unsupported` at check time.
  - `serial` + `send`: text into a firmware UART RX (`UARTn_RX` net). The emulator queues the bytes and feeds them one character time apart at the baud rate the firmware set (8N1; 115200 before it sets one), so the 32-byte RX FIFO does not overflow.
- Tests with events but no firmware run through the same stepped runner (`cosim.run` without an emulator); ngspice lands exactly on each event time.
- **Measure time ranges**: `from:` / `to:` on a tran expectation evaluate it over part of the run; the label shows the range (`period(current(D1), 3mA) [1.6s..end]`).
- Rules: `events_need_tran`, `event_time` (outside the run), `event_unsupported`, `serial_needs_firmware`, `serial_net` (not a UART RX net), `window_needs_tran`, `invalid_window`.
- Waveform plots mark each event with a labelled vertical line; the viewer lists a test's events.

**Example firmware** (`pico_io/firmware/main.c`) now polls the UART every millisecond: `rate <ms>` sets the normal toggle interval and answers `rate=<ms>`, anything else answers `error: <line>`. New tests:

| Test | Result |
|---|---|
| `firmware_press` (SW1 pressed at 1.1 s) | period 1.001 s before, 200.3 ms after; BUTTON 3.3 V → 6.6 µV |
| `firmware_command` (`rate 100` at 300 ms, `speed up` at 400 ms) | period 200.3 ms from 700 ms; `rate=100`; one `error: speed up` |

The 1 ms polling loop makes the firmware wake 1000 times per simulated second; all nine `pico_io` tests take about 5 s.

## 16. Copper pours (2026-10-20)

Carry-over from §13.5: boards had no ground plane.

- **`board.yaml`**: `pours: [{ net: GND, layer: bottom }]`; `layer` is `top`, `bottom`, or `both`; `clearance` (default 0.3 mm) and `min_width` (0.25 mm) are optional. One pour per layer. Rules: `pour_net` (the net has no pads on the board), `pour_overlap`.
- **Flow**: route first, then pour. The router sees the same `.dsn` as before (route cache unaffected) and connects every net with tracks; the fill is added afterwards by the `pour` command of `pcbnew_script.py`: a zone over the board outline inset 0.5 mm, thermal reliefs on pads (gap and spoke width at least 0.5 mm), islands not connected to a pad removed, filled with `ZONE_FILLER`. Gerbers and the board images include it; `data.pcb.pours` lists net, layer, and filled area.
- **DRC**: `starved_thermal` (a pad's thermal spokes reach only a fill island cut off by tracks) is reported as a **warning**: connectivity is already guaranteed by the tracks and checked as `unconnected` items, so it is a soldering/quality note, not a broken board.
- Examples: `opamp_amp`, `tl072_amp`, and `pico_io` now have a bottom GND pour (861, 980, and 4257 mm² of copper). The first two pass DRC with 0 errors and 0 warnings; `pico_io` has one `starved_thermal` warning at D1's cathode, whose pad sits in a strip enclosed by bottom tracks. `led_demo` stays without a pour.

## 17. Connectivity-aware placement (2026-10-20)

Carry-over from §13.5 / §14.6: auto-placement packed parts by size, so connected parts ended up far apart (long tracks, a via on `pico_io`, a fill island under D1).

- **Algorithm** (`pcbnew_script._connected_place`): greedy and deterministic. Hand-placed parts are fixed first. Then, repeatedly, the unplaced part with the most connections to placed parts (ties: larger, then by reference) is tried at every 0.5 mm grid position and every 90° rotation that does not overlap another part's courtyard plus `spacing`. The cost is the growth of each of its nets' bounding box (half-perimeter wire length) plus 0.5 × the growth of the layout's own half-perimeter, which keeps the board compact. With a fixed `width`/`height` the search stays inside the board minus `margin`; with nothing placed the first part starts at the origin.
- **Courtyards**: part sizes now come from the footprint courtyard (plus pads) instead of the full bounding box, which for the Pico includes a fabrication drawing 20 mm beyond the module and left an empty strip on the board.
- `board.placer: shelf` keeps the old packer.
- Errors: a part that fits nowhere on a fixed-size board fails the build with `no room for <ref> on the board; enlarge it`.

Result (same rules and router; DRC 0 errors on all):

| Board | Track length shelf → connected | Board size (mm) |
|---|---|---|
| `led_demo` | 37.9 → 26.5 mm | 18.4 × 27.8 → 18.6 × 21.7 |
| `opamp_amp` | 167.2 → 127.9 mm | 34.7 × 28.8 → 36.8 × 26.7 |
| `tl072_amp` | 187.3 → 130.1 mm | 34.7 × 34.0 → 38.0 × 35.0 |
| `pico_io` | 585.2 → 369.4 mm, 1 → 0 vias | 93.7 × 54.0 → 72.5 × 42.3; the `starved_thermal` warning is gone |

Not done: no improvement pass after the greedy one (swaps, annealing), no rotation other than 90° steps, no awareness of board edges for connectors.

`data.pcb.routing.hpwl_mm` (added 2026-10-21) reports the placement's half-perimeter wire length over all nets. It does not depend on the router, so it is what the placement test compares: Freerouting sometimes leaves one connection open on the shelf layout of `opamp_amp` (HPWL shelf 163.5 mm, connected 125.6 mm).

## 18. Transient operating points (optran) (2026-10-20)

Carry-over from §12.4, which turned out to be misdiagnosed.

**Finding.** ngspice 47 enables `optran 1 1 1 100n 10u 0` at start-up: when Newton iteration, gmin stepping, and source stepping all fail, it computes the operating point by a 10 µs transient from power-up ("Note: Transient op finished successfully"). So TLV6001 *does* get an operating point — but in a gain −1000 amplifier (ngspice's own `examples/optran` circuit) the 10 µs result is OUT = 0.58 V, while longer runs settle at 3.38 V (100 µs: 2.04 V, 1 ms: 3.376 V, 10 ms – 1 s: 3.377 V). The only trace was three "stepping failed" warnings: a silently wrong operating point.

**What eda does now**

- `adapters/ngspice.classify_log` recognises the transient operating point, drops the stepping noise, and keeps real warnings; "Transient op failed" is an error. The same applies to the shared-library (co-simulation) runs.
- **Settling search** (`simulate._settle`): when a test's operating point came from a transient, the operating point is recomputed with 10x longer runs (from the part's request or 10 µs, up to 1 s) until two runs agree on every net voltage within 1 mV + 0.1 %; the test is then simulated again with the shorter of the two. Results are cached per circuit within a run. Warning `op_via_transient` reports the time and that it is settled; `op_not_settled` says the voltages were still changing at 1 s.
- **Parts can ask for it**: `spice: { optran: { step: 100ns, stop: 1ms } }` (or `eda model import ... --optran 1ms`) emits `optran 0 0 0 <step> <stop> 0` — straight to the transient operating point, no stepping — and starts the settling search there. Co-simulated tests use the part's setting without the search.

TLV6001 example (not committed; vendor model): the bias test now reads OUT = 3.376 V with "settled: a 10ms run gives the same net voltages within 1 mV", AC gain 59.96 dB. `tests/test_optran.py` runs it when ngspice's example models are present.

## 19. KiCad schematic export (2026-10-21)

Carry-over from §13.5. The netlistsvg drawing stays the quick view; `eda render` now also writes `out/kicad/<name>.kicad_sch`, a KiCad 10 schematic (format `20260306`) a person can open, edit, or print.

- **S-expressions**: `eda/sexpr.py` reads and writes KiCad's format (bare atoms vs quoted strings preserved; the 2.4 MB `Device.kicad_sym` round-trips exactly).
- **Symbols**: parts name a library symbol, `kicad: { symbol: Device:R }`; the symbol pin numbers default to the part's `pcb.pads`, which already follow KiCad numbering (LED 1 = K, Pico pin numbers = module pins); op-amps map `in+ in- v+ v- out` to `Simulation_SPICE:OPAMP` (built-in `opamp`, and vendor imports with `--opamp`, also when imported earlier). Sources use `Simulation_SPICE` VDC/VSIN/VPULSE. Symbols are copied into the file's `lib_symbols` (derived `extends` symbols flattened). Parts without a symbol get a generated rectangle with their pins.
- **Drawing style**: no wires between parts. Every pin gets a 2.54 mm stub and a local net label; GND gets ground symbols; pins on no net get no-connect flags; stacked pins (the Pico's seven hidden GND pins at one spot) are drawn once. Reference/value fields sit where the library symbol puts them. Parts flow left to right in `circuit.yaml` order, rows wrapping on A4 → A3 → A2 → A1 as needed. UUIDs are derived from the project and item names, so the file is stable across runs.
- **PWR_FLAG**: nets a source drives get a PWR_FLAG (KiCad's ERC otherwise reports undriven power pins), except nets where a symbol pin already is a power output (the Pico's GND pins).
- **Verification** (when `kicad-cli` is installed): the schematic's netlist is exported by KiCad and compared with the design's nets member by member; a difference is `kicad_sch_mismatch` (a generator bug, exit 2). KiCad's ERC runs and its findings become `kicad_erc_<type>` warnings, except library-table notices (absent in a command-line run) and pin conflicts inside one symbol (the Pico's GND/AGND outputs). The schematic is plotted to SVG/PNG and shown in the viewer under the netlistsvg drawing.

Result: all five example projects export, their KiCad netlists match the designs exactly, and KiCad's ERC reports 0 errors and 0 warnings.

Known limits: label-based drawing (readable, not hand-drawn); references without a number (`VCC`, `RF`) are valid for eda but KiCad's netlist exporter warns about annotation; vendor symbols other than op-amps are boxes.
