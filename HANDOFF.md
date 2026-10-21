# Handoff — where the work stopped and how to continue

> Written 2026-10-20 after M0–M3; updated after M4 and on 2026-10-21 (stimulus, pours, placement, optran, KiCad schematic, wired schematic, bad-input hardening, playback viewer, NPN/LDR parts and new example designs). Read this first, then
> `CLAUDE.md` (working rules and design loop), then `DESIGN_DECISIONS.md` §10–§15 for detail.

## 1. Status

| Milestone | State | Record |
|---|---|---|
| M0 Vertical slice: YAML → ngspice → JSON verdict → schematic → live viewer | ✅ done | `DESIGN_DECISIONS.md` §10–§11 |
| M1 Vendor SPICE models, op-amps, AC analysis | ✅ done | §12 |
| M2 Boards: KiCad layout, Freerouting, DRC JSON, Gerbers | ✅ done | §13 |
| M3 Firmware integration for the Raspberry Pi Pico (pins.h, cross-checks) | ✅ done | §14 |
| M4 Firmware build (`eda fw --build`), RP2040 emulation co-simulated with ngspice | ✅ done | §15 |

- All six open decisions from `PROJECT_SUMMARY.md` §11 are settled (Python, YAML, no tscircuit fork, YAML is the PCB source of truth, RP2040/Pico, 2-layer boards).
- Repository: https://github.com/TechieQuokka/ai-eda (public, `master`); the user approves each push (`git status` shows what is local).
- Quality gates: `ruff format --check`, `ruff check`, `pyright` (strict), and `pytest` (249 tests) all pass; all eight example projects pass `eda run`.
- Bad-input hardening (`DESIGN_DECISIONS.md` §21): malformed and extreme input answers in JSON with a rule instead of crashing, hanging, or passing; regression tests in `tests/test_bad_input.py`.
- Checking the examples: `eda run`/`eda render` append to each project's `iterations.jsonl`; for a status check that should leave no trace, call `eda.pipeline.render(Path("projects/<name>"))` from `uv run python` instead.

## 2. Resuming on this machine

Everything below is already installed here; on a fresh machine see §3.

```bash
cd ai-eda
uv sync                                   # Python deps (pinned in pyproject.toml)
uv run ruff format --check && uv run ruff check && uv run pyright && uv run pytest
uv run eda run projects/pico_io           # check → fw (+ build) → sim (+ firmware in the loop) → render → pcb
uv run eda watch projects/pico_io         # live viewer at http://127.0.0.1:8765/
```

Example projects: `led_demo` (op, board), `rc_filter` (tran + op), `opamp_amp` (generic op-amp: op/tran/ac, board), `tl072_amp` (TI TL072 vendor model, board), `pico_io` (RP2040: firmware, sim, board), `led_blink` (Pico firmware blinks an LED 1 s on / 1 s off), `led_blink_bjt` (two-2N3904 astable multivibrator, the cheap blinker), `nightlight` (LDR + Darlington; a `sunset` test dims the light with events).

## 3. Environment

| Tool | Version here | How it was installed | Needed for |
|---|---|---|---|
| Python / uv | 3.13.12 / 0.12.13 | pre-installed; keep as is | everything |
| ngspice | 47 (CLI + libngspice) | built from source into `/usr/local` (`build/ngspice-47/`, git-ignored) | `sim` |
| Node.js | 22 | pre-installed; `npm --prefix tools/netlistsvg ci` | schematic layout |
| KiCad / kicad-cli | 10.0.7 | `ppa:kicad/kicad-10.0-releases`, `--no-install-recommends kicad kicad-symbols kicad-footprints` | `pcb` (also its Python `pcbnew` under `/usr/bin/python3` 3.12) |
| Java | 21 | pre-installed; keep as is | Freerouting |
| Freerouting | 2.1.0 jar | `tools/freerouting/fetch.sh` (SHA-256 pinned; 2.2+ needs Java 25) | `pcb` autorouting |
| cairo | system library | pre-installed | PNG output (CairoSVG) |
| arm-none-eabi-gcc + newlib | 13.2.1 | `sudo apt install gcc-arm-none-eabi libnewlib-arm-none-eabi` (user ran it) | `eda fw --build` |
| CMake | 3.28 | pre-installed | firmware build |
| pico-sdk | 2.3.1 | `tools/fetch-pico-sdk.sh` → `tools/pico-sdk/` (git-ignored, 436 MB) | firmware build |
| rp2040js | 1.4.1 | `npm --prefix tools/rp2040 ci` | co-simulation |
| RP2040 boot ROM | B2 | `tools/rp2040/fetch-bootrom.sh` → `tools/rp2040/dl/` (git-ignored) | co-simulation |
| libngspice | 47 (shared) | built with ngspice into `/usr/local/lib` | co-simulation |

Local-only, git-ignored state worth knowing about:

- `parts/vendor/` holds the imported TI models (`tl072`, `tlv9002`, `tlv6001`). `projects/tl072_amp` needs `tl072`: `uv run eda model import build/ngspice-47/examples/optran/models/TL072.301 --id tl072 --opamp 1,2,3,4,5`. **Never commit vendor models** (public repo; vendor licenses).
- `build/` (278 MB: ngspice source/build), `tools/freerouting/dl/` (64 MB jar), `tools/freerouting/data/` (router settings/logs, kept inside the repo on purpose), every project's `out/` (regenerated, includes the route cache).
- KiCad 3D models (`kicad-packages3d`, several GB) are not installed, so 3D renders show bare boards.

## 4. Next

M4 is finished (`DESIGN_DECISIONS.md` §15): `pico_io` blinks its LED in the emulator, the `tran` tests see the LED current toggle with a 1 s period (200 ms with the button held), and the pot reading reaches the firmware through the ADC (`pot=2047` / `pot=1024` on the UART). Candidates for what comes next, none started or agreed:

1. ~~Firmware stimulus~~: done (`events:`, §15.6).
2. ~~Ground plane / copper pours~~: done (`pours:`, §16).
3. Tighter output→input timing (inputs currently lag one window except after pin mode changes), when a design needs it.
4. ~~Connectivity-aware placement~~: done (§17).
5. ~~optran~~: done (§18).
6. ~~KiCad schematic export~~: done (§19); placed and wired like a hand-drawn schematic (§20).
7. ~~Bad-input testing~~: done (§21).
8. ~~Replay a simulation on the schematic~~: done (Playback card, §22).
9. **DC sweep analysis** (not started): sweep a source or a part parameter (e.g. an LDR's light) and measure where an output crosses a level; `nightlight` finds its switching point only in steps (9–7 lx) through events today.
10. More example designs the user suggested: PWM dimming from a pot (Pico ADC → PWM, check with `duty()`), a button that toggles an LED, an op-amp low-pass filter.

Working style the user set (2026-10-21): every task must be doable without the GUI (the CLI's JSON is the source of truth); the user checks GUI layout themselves, Claude verifies logic only.

## 5. Known issues and carry-overs

| Area | Issue | Where noted |
|---|---|---|
| Boards | Auto-placement is greedy (one part at a time, no later improvement pass); rotations are 90° steps | §17 |
| Schematic | KiCad export placement is heuristic (greedy + refinement + variants); fan-out rows only for the left/right sides of the first IC; unroutable pins fall back to net labels | §20 |
| Boards | Freerouting is nondeterministic (handled by a route cache keyed on the `.dsn` hash); one unexplained 10-min hang (now a 180 s timeout) | §13.3, adapters/freerouting.py |
| Simulation | Transient operating points are checked for settling only in batch tests, not co-simulated ones | §18 |
| Simulation | Old vendor macromodels (TL072 1989) do not model supply current | §12.4 |
| Firmware | Co-simulation exchanges values once per `step`; output→input paths lag one window | §15.5 |
| Firmware | Only core 0 emulated; ADC reference = `vio` | §15.5 |
| Simulation | libngspice reads a user `.spiceinit` (`ngSpice_nospiceinit` crashes ngspice 47) | §15.5 |
| YAML | Values with commas inside `{ ... }` must be quoted; the loader explains it when it happens | §12.1, §14.6 |
| Design | A pulse `width` longer than its `period` is not flagged (needs a cross-parameter rule) | §21 |
| Simulation | No DC sweep: thresholds are found with stepped events in a `tran` test | §22 |
| Simulation | Waveform JSON is large (4–8 MB per firmware test; full float precision) | — |
| Schematic | netlistsvg layout is automatic only; crowded for op-amp/MCU circuits (the wired KiCad view, §20, reads better and could replace it) | §12.4 |

## 6. Working agreement with the user (also in `CLAUDE.md` §1)

- Work artifacts in English; conversation in Korean.
- Propose, then wait for approval before substantive work. For implementation details the user delegated decisions ("decide most things yourself"); still stop for `sudo` installs, large downloads, scope changes, and anything outward-facing (e.g. `git push` — ask each time).
- One question at a time.
- Stay inside the project directory.
- New dependencies at their latest versions (verify online); keep already-installed toolchains (Python 3.13, Node 22, Java 21) as they are.
- Commit after each completed step; end commit messages with the Co-Authored-By line used in the history.
