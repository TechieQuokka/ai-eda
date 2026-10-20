"""Entry point for the `eda` command. Every command prints one JSON object to stdout."""

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path

from pydantic import JsonValue

from eda import history, pipeline
from eda.project import ProjectPaths, find_project, load_parts
from eda.result import Diagnostics, ExitCode, Result


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    command: str | None = args.command
    if command is None:
        parser.print_help()
        return ExitCode.OK
    if command == "part":
        return _part(args.action, args.id)

    root = _project_root(args.project)
    if root is None:
        return _emit(_no_project(command, args.project))
    paths = ProjectPaths(root)

    if command == "log":
        entry = history.record_note(paths, args.message)
        return _emit(
            Result(
                ok=True,
                command="log",
                project=root.name,
                errors=[],
                warnings=[],
                data={"entry": entry},
            )
        )
    if command == "watch":
        from eda.viewer.server import watch  # optional heavy import

        return watch(root, host=args.host, port=args.port, open_browser=not args.no_open)

    result: Result
    if command == "check":
        result, _ = pipeline.check(root)
    elif command == "sim":
        result = pipeline.sim(root, args.test)
    elif command == "render":
        result = pipeline.render(root)
    else:
        result = pipeline.run(root)
    history.record_run(paths, result)
    return _emit(result)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eda",
        description="AI-native EDA toolchain. Output is JSON; exit code 0 = pass, "
        "1 = design errors, 2 = tool/environment failure.",
    )
    parser.add_argument("--version", action="version", version=f"eda {version('eda')}")
    sub = parser.add_subparsers(dest="command")

    def with_project(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "project", nargs="?", help="project directory (default: nearest project.yaml)"
        )
        return p

    with_project("check", "validate design files and run ERC")
    sim = with_project("sim", "simulate every test and judge expectations")
    sim.add_argument("-t", "--test", help="run only this test")
    with_project("render", "render the schematic and waveforms (SVG + PNG)")
    with_project("run", "check, sim, and render in one call")
    log = with_project("log", "append a note to the iteration log")
    log.add_argument("-m", "--message", required=True, help="what changed and why")
    watch = with_project("watch", "re-run on file changes and serve the live viewer")
    watch.add_argument("--host", default="127.0.0.1")
    watch.add_argument("--port", type=int, default=8765)
    watch.add_argument("--no-open", action="store_true", help="do not open a browser")

    part = sub.add_parser("part", help="inspect the part database")
    part.add_argument("action", choices=["list", "show"])
    part.add_argument("id", nargs="?", help="part id for `show`")
    return parser


def _project_root(arg: str | None) -> Path | None:
    if arg is not None:
        path = Path(arg)
        return path.resolve() if (path / "project.yaml").is_file() else None
    return find_project(Path.cwd())


def _no_project(command: str, arg: str | None) -> Result:
    diag = Diagnostics()
    where = arg or str(Path.cwd())
    diag.error("no_project", f"no project.yaml found at or above {where}")
    return diag.to_result(command, "", {})


def _part(action: str, part_id: str | None) -> int:
    diag = Diagnostics()
    root = find_project(Path.cwd())
    paths = ProjectPaths(root) if root else None
    specs = load_parts(diag, paths.parts_dir if paths else None, root)
    data: dict[str, JsonValue] = {}
    if action == "list":
        data["parts"] = {
            spec.id: {"name": spec.name, "category": spec.category, "pins": list(spec.pins)}
            for spec in specs.values()
        }
    elif part_id is None:
        diag.error("missing_argument", "usage: eda part show ID")
    elif part_id not in specs:
        diag.error("unknown_part_type", f"no part {part_id!r}; available: {', '.join(specs)}")
    else:
        data["part"] = specs[part_id].model_dump(mode="json")
    return _emit(diag.to_result(f"part {action}", root.name if root else "", data))


def _emit(result: Result) -> int:
    sys.stdout.write(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    sys.stdout.write("\n")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
