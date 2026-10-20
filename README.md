<div align="center">

# ai-eda

**An electronics design toolchain that an AI agent drives through plain files and a CLI.**

You describe the goal. The agent writes the circuit as YAML, simulates it, reads the verdict, fixes the design, and repeats until every requirement passes. You watch it happen in a live browser view.

![Python](https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white)
![ngspice](https://img.shields.io/badge/simulator-ngspice%2047-555)
![pyright](https://img.shields.io/badge/types-pyright%20strict-2563eb)
![tests](https://img.shields.io/badge/tests-89%20passing-15803d)
![license](https://img.shields.io/badge/license-free%20for%20learning%20%7C%20paid%20commercial-b45309)

</div>

<p align="center">
  <img src="docs/images/viewer.png" alt="Live viewer showing a schematic, test verdicts, and interactive waveforms" width="820">
</p>

---

## Why

Most EDA tools are built around a mouse and a GUI, which an AI agent cannot use well. ai-eda works the other way around:

- **Text is the source of truth.** Circuits, tests, and parts are human-readable YAML, diffable and versioned with git.
- **Every action is one command** that prints one JSON object, with errors that point at a file, line, and field.
- **Pass/fail is explicit.** Requirements live in `test.yaml`; simulation produces a verdict, not just a plot.
- **The agent can see.** Schematics and waveforms are rendered to SVG and PNG so the agent can inspect what it built.
- **The CLI never designs.** It validates, simulates, and renders. The design decisions belong to the agent and to you.

## A design in two files

```yaml
# design/circuit.yaml
parts:
  V1: { type: dc_voltage_source, voltage: 5V }
  R1: { type: resistor, resistance: 316ohm }
  D1: { type: led, color: red }
nets:
  VCC: [V1.+, R1.1]
  N1:  [R1.2, D1.anode]
  GND: [D1.cathode, V1.-]
```

```yaml
# design/test.yaml
tests:
  led_current:
    analysis: { type: op }
    expect:
      - { measure: current(D1), min: 9.5mA, max: 10.5mA }
      - { measure: voltage(N1), max: 3V }
```

Values always carry a unit (`4.7kohm`, `100nF`, `5ms`), and prefixes are case-sensitive (`m` = milli, `M` = mega), so the common SPICE pitfall of `1M` meaning one *milli*-ohm cannot happen.

## The loop

```console
$ eda run projects/led_demo -m "Start from 470 ohm"
```
```json
{
  "ok": false,
  "errors": [{
    "rule": "expect",
    "message": "led_current: current(D1) = 6.752mA, expected 9.5mA..10.5mA (too low by 2.748mA)",
    "file": "design/test.yaml", "line": 6, "path": "tests.led_current.expect[0]"
  }]
}
```
```console
$ eda value 315ohm -s E96          # nearest real resistor value → 316Ω (+0.32 %)
$ eda run projects/led_demo -m "Fix: (5 V - 1.85 V) / 10 mA ≈ 315 ohm → E96 316 ohm"
```
```json
{ "ok": true, ... "current(D1)": "9.969mA" }
```

Every run is written to the project's `iterations.jsonl` with the note, the changed files, and a semantic diff such as `R1.resistance: 470Ω → 316Ω`.

<table>
<tr>
<td align="center"><img src="docs/images/led_demo_schematic.png" alt="LED schematic" height="300"><br><sub>Auto-laid-out schematic with net labels</sub></td>
<td align="center"><img src="docs/images/rc_filter_step_response.png" alt="RC step response" height="300"><br><sub>Transient waveforms with the test bounds</sub></td>
</tr>
</table>

## Features

| Area | What you get |
|---|---|
| **Design files** | Strict YAML 1.2 (no `no` → `false` surprises, duplicate keys are errors), validated with pydantic; every error has file, line, column, and path |
| **ERC** | Unconnected pins, single-pin nets, missing ground, nets with no DC path, shorts between nets, shorted sources, names that collide in case-insensitive SPICE |
| **Simulation** | ngspice batch runs: operating point (`op`) and transient (`tran`); measures `voltage(N)`, `voltage(A, B)`, `current(REF)` with `max min avg rms pp initial final` |
| **Parts** | One YAML file per part type: pins, typed parameters, SPICE template, schematic symbol. Built in: DC and pulse sources, R, C, L, diodes (1N4148, 1N4007), LEDs in five colors |
| **Rendering** | Schematic auto-layout via netlistsvg with a custom skin and net labels; waveform SVGs; PNG copies via CairoSVG for the agent to look at |
| **Live viewer** | `eda watch` re-runs on every save and pushes updates to a local page: schematic, verdicts, operating point, zoomable waveforms, iteration log |
| **Helpers** | `eda value` for E3 … E192 standard values, `eda part list/show`, `eda log` notes |

## Quick start

Requirements: Python 3.13 with [uv](https://docs.astral.sh/uv/), [ngspice](https://ngspice.sourceforge.io/) on `PATH`, Node.js (for schematic layout), and the system cairo library (present on most Linux desktops).

```bash
git clone https://github.com/TechieQuokka/ai-eda.git
cd ai-eda
uv sync                              # Python dependencies
npm --prefix tools/netlistsvg ci     # schematic auto-layout engine

uv run eda run projects/led_demo     # check → simulate → render
uv run eda watch projects/rc_filter  # live viewer at http://127.0.0.1:8765/
```

| Command | Purpose |
|---|---|
| `eda check [PROJECT]` | Validate files, run ERC, compile tests |
| `eda sim [PROJECT] [-t TEST]` | Simulate and judge every expectation |
| `eda render [PROJECT]` | Schematic and waveform SVG/PNG |
| `eda run [PROJECT] [-m NOTE]` | All three in one call, logged with an optional note |
| `eda watch [PROJECT]` | Re-run on change and serve the live viewer |
| `eda value TARGET [-s E24]` | Nearest standard component values |
| `eda part list` / `eda part show ID` | Inspect the part database |
| `eda log [PROJECT] -m TEXT` | Add a note to the iteration log |

Exit codes: `0` all checks pass, `1` the design has errors, `2` a tool or the environment failed.

## Using it with an AI agent

The repository's [`CLAUDE.md`](CLAUDE.md) is the working contract for an agent such as Claude Code: the design loop, how to read the JSON errors, and how to record each iteration. Give the agent a goal ("drive a red LED at 10 mA from 5 V"), start `eda watch` for yourself, and watch the iterations arrive.

## Roadmap

| Milestone | Scope | Status |
|---|---|---|
| **M0** | Text design → ngspice → JSON verdict → schematic and waveforms → live viewer | ✅ Done |
| **M1** | Vendor SPICE models (`.lib` subcircuits), op-amps, AC analysis | Next |
| **M2** | KiCad board generation, `kicad-cli` DRC and Gerber export, auto-routing | Planned |
| **M3** | Firmware integration: generated `pins.h`, schematic/firmware cross-checks | Planned |
| **M4** | MCU emulation co-simulated with the analog circuit | Planned |

Background and decisions: [`PROJECT_SUMMARY.md`](PROJECT_SUMMARY.md), [`DESIGN_DECISIONS.md`](DESIGN_DECISIONS.md).

## Development

```bash
uv run ruff format --check && uv run ruff check   # style and lint
uv run pyright                                    # strict static typing
uv run pytest                                     # 89 tests
```

---

## License

Copyright © 2026 TechieQuokka. All rights reserved except as granted below.

ai-eda is **source-available under a dual licensing model**. The full terms are in [`LICENSE`](LICENSE); this section is a summary.

### 🎓 Free for learning and non-commercial use

You may use, copy, and modify ai-eda **free of charge** for:

- personal learning and self-study,
- teaching, coursework, and academic research,
- hobby projects and other non-commercial experimentation.

Keep this license notice in any copy or derivative you share.

### 💼 Commercial use requires a paid license

A separate **commercial license (paid)** is required for any commercial use, including but not limited to:

- using ai-eda, or designs and outputs produced with it, in work done for profit or for a client,
- integrating ai-eda into a commercial product, service, or internal business tool,
- offering ai-eda or a derivative as a hosted or paid service.

To obtain a commercial license, please [open an issue](https://github.com/TechieQuokka/ai-eda/issues) with the title **"Commercial license inquiry"** or contact the maintainer through the GitHub profile [@TechieQuokka](https://github.com/TechieQuokka). Pricing depends on the size of the organization and the intended use.

If you are unsure whether your use counts as commercial, ask before using it.

### Third-party components

ai-eda relies on, but does not relicense, the following projects. Each keeps its own license:

| Component | License | How it is used |
|---|---|---|
| [netlistsvg](https://github.com/nturley/netlistsvg) | MIT, © 2016 Neil Turley | Schematic layout (installed via npm). `src/eda/render/skin.svg` is a modified copy of its analog skin and stays under the MIT license |
| [ngspice](https://ngspice.sourceforge.io/) | BSD-3-Clause (mostly) | External simulator, not bundled |
| [pydantic](https://github.com/pydantic/pydantic), [ruamel.yaml](https://sourceforge.net/projects/ruamel-yaml/), [watchfiles](https://github.com/samuelcolvin/watchfiles) | MIT | Python dependencies |
| [CairoSVG](https://github.com/Kozea/CairoSVG) | LGPL-3.0-or-later | Python dependency (dynamically imported, unmodified) |

Built-in SPICE model parameters for the 1N4148 and 1N4007 follow widely published manufacturer-style models; the LED models are fitted by this project.

### Disclaimer

The software is provided "as is", without warranty of any kind. Simulation results are approximations; verify real hardware against datasheets and measurements before relying on it.
