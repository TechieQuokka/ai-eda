# AI-Native EDA — Design Decisions

> Companion to `PROJECT_SUMMARY.md`. Records what was discussed and decided on 2026-10-20.
> Status: M0 complete (2026-10-20). See §10 for the M0 specification and §11 for results.

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
| 4 | PCB source of truth | Our format is the source; KiCad files are build outputs | M2 |
| 5 | Primary target MCU | Undecided | M3 |
| 6 | Board complexity | Start with 2-layer | M2 |

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
