# CLAUDE.md

Rules for Claude Code working in this repository. Background: `PROJECT_SUMMARY.md` (vision, architecture, roadmap) and `DESIGN_DECISIONS.md` (settled decisions).

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
3. `uv run eda run projects/<name>` — check → sim → render in one call. Exit code 0 = all pass, 1 = design errors, 2 = tool failure.
4. On failure, read `errors` in the JSON: each has `rule`, `message`, `file`, `line`, `path`, `details`. Use `data.sim.tests.<name>.operating_point` for node voltages and currents.
5. Record the fix and its reasoning: `uv run eda log projects/<name> -m "Fix: ..."`. Then edit and repeat from step 3.
6. Look at `out/schematic.png` and `out/waves/<test>.png` before reporting; when everything passes, report to the user.

Other commands: `eda check`, `eda sim [-t TEST]`, `eda render`, `eda part list`, `eda part show ID`.
The CLI never makes design decisions; it only reports.

## 4. Iteration Log and Viewer

- `iterations.jsonl` in each project (tracked by git) records every `eda` run (changed files, errors) and every `eda log` note (intent and fix).
- `uv run eda watch projects/<name>` re-runs on file changes and serves the live viewer at http://127.0.0.1:8765/ (schematic, verdicts, interactive waveforms, iteration log). The user watches it; start it when they want to observe.

## 5. Adding Parts

Built-in parts live in `parts/<id>.yaml` (one file per type, file name = id); project-specific parts go in `projects/<name>/parts/`. Fields: pins, params (`unit` or `choices`, optional `default`), `spice` template and current probe, netlistsvg `symbol`, `dc_path`.
