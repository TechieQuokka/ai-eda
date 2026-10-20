# Handoff — where the work stopped and how to continue

> Written 2026-10-20 at the end of the session that built M0–M3. Read this first, then
> `CLAUDE.md` (working rules and design loop), then `DESIGN_DECISIONS.md` §10–§14 for detail.

## 1. Status

| Milestone | State | Record |
|---|---|---|
| M0 Vertical slice: YAML → ngspice → JSON verdict → schematic → live viewer | ✅ done | `DESIGN_DECISIONS.md` §10–§11 |
| M1 Vendor SPICE models, op-amps, AC analysis | ✅ done | §12 |
| M2 Boards: KiCad layout, Freerouting, DRC JSON, Gerbers | ✅ done | §13 |
| M3 Firmware integration for the Raspberry Pi Pico (pins.h, cross-checks) | ✅ done | §14 |
| **M4** Firmware build, MCU emulation co-simulated with the circuit | **not started** — stopped by the user before the toolchain install | §4 below |

- All six open decisions from `PROJECT_SUMMARY.md` §11 are settled (Python, YAML, no tscircuit fork, YAML is the PCB source of truth, RP2040/Pico, 2-layer boards).
- Last commit: `02eac90` (M3), pushed to https://github.com/TechieQuokka/ai-eda (public, `master`).
- Quality gates at the stop: `ruff format --check`, `ruff check`, `pyright` (strict), and `pytest` (145 tests) all pass; all five example projects pass `eda run`.

## 2. Resuming on this machine

Everything below is already installed here; on a fresh machine see §3.

```bash
cd ai-eda
uv sync                                   # Python deps (pinned in pyproject.toml)
uv run ruff format --check && uv run ruff check && uv run pyright && uv run pytest
uv run eda run projects/pico_io           # check → fw → sim → render → pcb
uv run eda watch projects/pico_io         # live viewer at http://127.0.0.1:8765/
```

Example projects: `led_demo` (op, board), `rc_filter` (tran + op), `opamp_amp` (generic op-amp: op/tran/ac, board), `tl072_amp` (TI TL072 vendor model, board), `pico_io` (RP2040: firmware, sim, board).

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

Local-only, git-ignored state worth knowing about:

- `parts/vendor/` holds the imported TI models (`tl072`, `tlv9002`). `projects/tl072_amp` needs `tl072`: `uv run eda model import build/ngspice-47/examples/optran/models/TL072.301 --id tl072 --opamp 1,2,3,4,5`. **Never commit vendor models** (public repo; vendor licenses).
- `build/` (278 MB: ngspice source/build), `tools/freerouting/dl/` (64 MB jar), `tools/freerouting/data/` (router settings/logs, kept inside the repo on purpose), every project's `out/` (regenerated, includes the route cache).
- KiCad 3D models (`kicad-packages3d`, several GB) are not installed, so 3D renders show bare boards.

## 4. Next: M4 plan (approved scope not yet started)

Goal (`PROJECT_SUMMARY.md` §12): firmware runs against the simulated circuit.

1. **Toolchain** — needs the user, because of `sudo` and a large download:
   - `sudo apt install gcc-arm-none-eabi libnewlib-arm-none-eabi cmake` (the user runs it; `!` prefix does not work in a plain terminal).
   - pico-sdk into `tools/pico-sdk/` (git-ignored) with submodules (`tinyusb` is the large one). Check the latest release tag first (working rule 6).
   - Then build `projects/pico_io/firmware` (`CMakeLists.txt` is ready) and add `eda fw --build` with compiler errors mapped to `Issue`s (file, line).
2. **Emulator** — verify before choosing: Renode's RP2040 support is a community plugin, not upstream (earlier advice overstated it). Alternatives to evaluate: rp2040js (Wokwi's JS emulator), QEMU (no upstream RP2040 machine at last check). Criteria: runs the pico-sdk `.elf` unmodified, exposes GPIO/ADC/UART to an external process, scriptable headless.
3. **Co-simulation (level 2)** — the emulator drives GPIO outputs; ngspice computes the analog side; ADC reads come back from node voltages. Start with a lock-step loop over `tran` time windows (PWM duty → average, ADC samples at window boundaries) before anything fancier.
4. Done when: `pico_io` firmware blinks the LED in emulation and a `tran` test sees the LED current toggle, with the button and pot readings flowing back to the firmware.

## 5. Known issues and carry-overs

| Area | Issue | Where noted |
|---|---|---|
| Boards | Auto-placement is naive shelf packing; MCU modules need hand placement | §13.5, §14.6 |
| Boards | No copper pours / ground plane; no KiCad schematic (`.kicad_sch`) export | §13.5 |
| Boards | Freerouting is nondeterministic (handled by a route cache keyed on the `.dsn` hash); one unexplained 10-min hang (now a 180 s timeout) | §13.3, adapters/freerouting.py |
| Simulation | TLV6001-style vendor models need `.option optran` to converge; not supported | §12.4 |
| Simulation | Old vendor macromodels (TL072 1989) do not model supply current | §12.4 |
| Firmware | Static pin model only (no timing, PWM duty, ADC conversion) until M4 | §14.6 |
| YAML | Values with commas inside `{ ... }` must be quoted; the loader explains it when it happens | §12.1, §14.6 |
| Schematic | netlistsvg layout is automatic only; crowded for op-amp/MCU circuits | §12.4 |

## 6. Working agreement with the user (also in `CLAUDE.md` §1)

- Work artifacts in English; conversation in Korean.
- Propose, then wait for approval before substantive work. For implementation details the user delegated decisions ("decide most things yourself"); still stop for `sudo` installs, large downloads, scope changes, and anything outward-facing (e.g. `git push` — ask each time).
- One question at a time.
- Stay inside the project directory.
- New dependencies at their latest versions (verify online); keep already-installed toolchains (Python 3.13, Node 22, Java 21) as they are.
- Commit after each completed step; end commit messages with the Co-Authored-By line used in the history.
