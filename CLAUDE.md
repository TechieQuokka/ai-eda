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

For a design task in `projects/<name>/`:

1. Edit the design YAML (`design/circuit.yaml`, `design/test.yaml`, ...).
2. `eda check` — structure and rule validation (ERC).
3. `eda sim` — simulation and test-criteria verdict.
4. `eda render` — schematic / waveform / board images.
5. On failure, read the JSON errors, fix the design files, and repeat.
6. When everything passes, report the results to the user.

## 4. Iteration Log

Each loop of the design work loop records:

- what changed (files and intent),
- which check failed (rule, location, values),
- how it was fixed.

The log is shown in the local viewer (`eda watch`).
