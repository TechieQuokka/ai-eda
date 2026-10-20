# AI-Native EDA — Project Summary (working title)

> Status: M0 and M1 complete (2026-10-20). Settled decisions and the M0 specification are in `DESIGN_DECISIONS.md`.
> This document is the reference for all upcoming work. Facts about third-party projects are from memory and should be re-verified before relying on them.

---

## 1. Vision

A desktop-local, free, unrestricted EDA toolchain that an AI agent (Claude Code) operates entirely through **files and CLI**. The human supplies the idea and observes the process and results; the AI does the design, verification, and iteration.

Scope of "design" covers the full chain:

1. Circuit (schematic) design
2. Simulation and verification
3. PCB design
4. Firmware that runs on the board
5. Virtual end-to-end run of the finished product on a PC

## 2. Motivation

- Tinkercad Circuits: too few parts, apparent bugs, limited for real projects.
- Alternatives are paid or restricted.
- Existing EDA tools are GUI-centric and hard for AI agents to operate. This gap is the differentiator.
- Target is **real parts** (low-cost and high-end chips, discretes, ICs), not Arduino boards. Arduino is just one possible MCU target, not the center.

## 3. Constraints and Decisions Already Made

| Topic | Decision |
|---|---|
| Users | Personal use only, single user |
| Licensing | Not a concern (no redistribution). GPL code may be copied/forked, vendor SPICE models may be stored in the repo |
| Human role | Observer: gives ideas, watches process and results, makes final judgment |
| AI role | Writes and edits all design files, runs CLI, reads results, fixes failures |
| Interface | Files + CLI first. GUI is optional (read-only viewer at most) |
| Performance | Not a primary concern at this stage |
| Strategy | Do not reinvent engines; build the AI-friendly layer on top of existing ones |

## 4. Core Principles

1. **Text is the source of truth.** All design data is human-readable, diffable text. Version-controlled with git.
2. **Files are the design; the CLI only reads and executes.** The CLI never designs. It validates, simulates, renders, and exports.
3. **Every action is one CLI command** with machine-readable output (JSON, with precise error locations).
4. **The AI must be able to "see" results**: waveforms, schematics, and PCB layouts exported as numeric summaries plus images (SVG/PNG).
5. **Deterministic**: same input produces same output, so verification loops are reliable.
6. **Pass/fail by explicit criteria**: tests declare acceptance conditions in a file; simulation yields an automatic verdict.
7. **Errors must be actionable**, e.g. `{"rule":"clearance","net":"VCC","at":[12.3,4.5],"min":0.2,"actual":0.1}` rather than "DRC failed".

## 5. Roles

| Actor | Responsibility |
|---|---|
| Human | Idea, observation, final decision |
| Claude Code | Edit design files, run CLI, interpret results, iterate |
| `eda` CLI | Read files, validate, simulate, render, export. No design decisions |

## 6. Build vs. Reuse

Rule of thumb: **build the AI-friendly interface; reuse the physics and standards.**

### Build (core assets — "brain and glue")

| Area | Notes |
|---|---|
| Design file format / schema and its validator | The heart of the "AI-native" identity |
| CLI orchestrator | `check`, `sim`, `pcb`, `fw`, `render`, `watch` |
| Part database and part model structure | One part, multiple facets (see §8) |
| Verification rules | ERC, test-file pass/fail evaluation |
| Unified result/error format (JSON) | Translates the differing outputs of external tools |
| Schematic SVG renderer | netlistsvg auto-layout in M0; KiCad-based rendering later. Visual quality is a first-class requirement (`DESIGN_DECISIONS.md` §1) |
| Read-only viewer and `watch` mode | Lightweight |
| Cross-domain sync logic | e.g. MCU pin assignment in schematic generates firmware `pins.h` |

### Reuse (the "muscle")

| Area | Tool |
|---|---|
| Analog / mixed-signal simulation | ngspice |
| PCB file format, DRC, Gerber export | KiCad file format + `kicad-cli` |
| Auto-routing | Freerouting (or similar) |
| MCU emulation | avr8js (AVR), Renode / QEMU (ARM Cortex-M and others) |
| Firmware compilation | gcc-arm, PlatformIO, etc. |
| Waveform plotting | matplotlib or equivalent |
| Part SPICE models | Manufacturer-published models (TI, Analog Devices, onsemi, ...) |
| Footprints / symbols | KiCad official libraries |

Because licensing is not a concern, external code may be forked or embedded where convenient. Still, keep external tools behind a thin adapter so they can be swapped later.

## 7. Architecture

```
eda (CLI orchestrator)
 ├─ part    part DB (symbol + spice + footprint + pin functions + datasheet)
 ├─ check   ERC / DRC → structured error list (JSON)
 ├─ sim     SPICE simulation → waveform summary JSON + PNG, pass/fail verdict
 ├─ pcb     placement, routing, Gerber/BOM export
 ├─ fw      pin header generation, pin conflict / connectivity checks
 ├─ render  schematic / board images for AI inspection
 └─ watch   re-run check/sim/render on file change
```

Execution flow:

```
Idea (human)
   ↓
Claude Code writes design files
   ↓
eda check      structure / rule validation   → fail → fix
   ↓
eda sim        test criteria verdict         → fail → fix
   ↓
eda pcb check  DRC                           → fail → fix
   ↓
All pass → report results to human
```

External engines are invoked as subprocesses (ngspice, kicad-cli, compilers) through thin wrappers; their outputs are normalized into one JSON schema.

## 8. Part Model

A part is a single record with several facets:

```
Part
 ├─ symbol      schematic symbol
 ├─ spice       simulation model (vendor .lib/.mod, or parametric built-in)
 ├─ footprint   PCB pad geometry
 ├─ firmware    pin multiplexing / peripheral info (MCUs)
 ├─ 3D model    (optional)
 └─ meta        datasheet URL, price, supplier
```

Registration tiers:

1. **Parameter-only parts** (resistors, capacitors, LEDs, diodes, BJTs, MOSFETs, ideal op-amps): add a data entry, no code.
2. **SPICE-model parts** (real op-amps, regulators, etc.): point to a vendor `.lib` and subcircuit name.
3. **Behavioral parts** (sensors, LCDs, shift registers, servos, ultrasonic modules, MCU peripherals): small behavior code written once, then parameterized.

Example entry:

```json
{
  "id": "lm358",
  "name": "LM358 Dual Op-Amp",
  "category": "opamp",
  "pins": ["OUT1","IN1-","IN1+","GND","IN2+","IN2-","OUT2","VCC"],
  "spice": { "file": "lm358.lib", "subckt": "LM358" },
  "package": "DIP-8",
  "footprint": "Package_DIP:DIP-8_W7.62mm",
  "symbol": "opamp-dual.svg",
  "datasheet": "https://..."
}
```

## 9. Design File Layout

```
my-project/
 ├─ project.yaml
 ├─ design/
 │   ├─ circuit.yaml    parts, nets, values
 │   ├─ board.yaml      placement, layers, rules (M2)
 │   └─ test.yaml       simulation scenarios + acceptance criteria
 ├─ firmware/           ordinary C / Rust source (M3)
 ├─ parts/              project-specific parts and vendor models
 ├─ iterations.jsonl    iteration log (tracked)
 └─ out/                generated artifacts (git-ignored)
```

The syntax is YAML; see `DESIGN_DECISIONS.md` §10 for the M0 schema and `projects/led_demo/` for a working example. The AI working rules live in the repository-level `CLAUDE.md`.

## 10. Hardware–Firmware Integration Levels

| Level | Description | Difficulty |
|---|---|---|
| 1 | Static checks: pin conflicts, voltage-level mismatch, missing pull-ups, firmware pins vs. schematic connectivity | Easy |
| 2 | MCU emulator drives SPICE sources (mixed simulation) | Medium |
| 3 | Accurate on-chip peripherals (ADC, timers, etc.) | Hard |

Start with Level 1, aim for Level 2. Generated artifacts such as `pins.h` and peripheral init templates (similar to STM32CubeMX) keep schematic and firmware in sync.

## 11. Decisions

| # | Decision | Status |
|---|---|---|
| 1 | Implementation language | **Python** (strict typing + pydantic) — decided |
| 2 | Design file format | **YAML** (strict parsing) — decided |
| 3 | Fork tscircuit or build from scratch | **Build**; tscircuit not forked — decided |
| 4 | PCB source of truth | Open; leaning: our format is the source, KiCad files are build outputs (M2) |
| 5 | Primary target MCU | Open (M3) |
| 6 | Board complexity | Open; start with 2-layer (M2) |

Rationale for each is in `DESIGN_DECISIONS.md` §2 and §7.

## 12. Roadmap

| Milestone | Scope | Done when |
|---|---|---|
| **M0** ✅ | Vertical slice: source + resistor + LED defined in text → `sim` → JSON verdict → `render` SVG | Claude Code, given "make LED current 10 mA," edits the file, runs check/sim, and confirms pass on its own (done 2026-10-20) |
| **M1** ✅ | Part DB, import vendor SPICE models, waveform output, ERC, AC analysis | Real ICs (e.g. an op-amp circuit) simulate with pass/fail tests (done 2026-10-20: TI TL072 / TLV9002) |
| **M2** | KiCad format generation, `kicad-cli` DRC and Gerber export, auto-routing | A board is produced from design files with DRC results as JSON |
| **M3** | Firmware integration: pin header generation, static cross-checks | Changing a pin in the schematic updates `pins.h` and flags conflicts |
| **M4** | MCU emulation + SPICE mixed simulation | Firmware runs against the simulated circuit |
| **Later** | Decide whether to build a custom PCB editor | Only if KiCad-based flow proves limiting |

## 13. Observability for the Human

Because the human only watches, provide:

- `eda watch`: auto re-run check/sim/render on every file change.
- Read-only browser viewer: schematic, board, waveforms updating live.
- Iteration log: per loop, what changed, which check failed, how it was fixed.

## 14. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Overall scope (EDA is huge) | Close M0 first; expand by repeating the same pattern |
| External tool installation and version drift | Document versions; consider Docker later; keep adapters thin |
| SPICE convergence problems on real circuits | Rely on ngspice options; make failure output explicit so the AI can adjust |
| MCU support is per-chip | Limit initial targets to one or two chips |
| PCB layout quality via automation | Declare placement rules in files; use external router; iterate with DRC feedback |
| Similar projects already exist | Check tscircuit, atopile, SKiDL, `kicad-cli` current status before building; reuse where it fits |

## 15. Related Existing Projects (to study)

- **tscircuit** — TypeScript/TSX circuit and PCB definition; closest in direction.
- **atopile** — text language for circuits, exports to KiCad.
- **SKiDL** — Python netlist generation for KiCad.
- **kicad-cli** — command-line DRC, Gerber export, and more.
- **wokwi-elements / avr8js** — TypeScript part components and AVR emulator.
- **SimulIDE** — component-folder architecture for parts.
- **Falstad Circuit Simulator** — compact analog solver implementation.
- **ngspice**, **KiCad**, **Renode**, **QEMU**, **Freerouting** — engines to reuse.

## 16. Immediate Next Steps

1. Start M2: KiCad schematic/board generation from the design files, `kicad-cli` ERC/DRC and Gerber export, auto-routing (decide open item #4 first).
2. Carry-overs from M1 (`DESIGN_DECISIONS.md` §12.4): `optran` for hard-to-converge vendor models.
