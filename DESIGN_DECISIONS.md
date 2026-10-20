# AI-Native EDA — Design Decisions

> Companion to `PROJECT_SUMMARY.md`. Records what was discussed and decided on 2026-10-20.
> Status: decisions settled, implementation not started. Project skeleton and `CLAUDE.md` are proposed but not yet created.

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

## 5. Proposed Project Skeleton (not yet created)

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

Also proposed: initialize git, per core principle 1 ("text is the source of truth, version-controlled with git").

## 6. Proposed `CLAUDE.md` Contents (not yet written)

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

Local environment: Python 3.13.12, Node 22.22.2, uv 0.12.13 present; ngspice, kicad-cli, and Bun not installed. An aborted tscircuit evaluation install sits in `eval/tscircuit/`.

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

1. Approve and create the project skeleton and `CLAUDE.md` (§5–§6), and initialize git.
2. Install the environment: latest ngspice and KiCad (kicad-cli); Python dependencies via uv.
3. Implement M0: `circuit.yaml` → netlist → ngspice → JSON result → test verdict → SVG render → viewer.
4. Run the "LED current to 10 mA" scenario and note friction points.
