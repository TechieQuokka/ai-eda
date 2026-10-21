"""Entry point for the `eda` command. Every command prints one JSON object to stdout."""

import argparse
import json
import sys
import traceback
from importlib.metadata import version
from pathlib import Path

from pydantic import JsonValue

from eda import eseries, history, pipeline, vendor
from eda.project import ProjectPaths, find_project, load_parts
from eda.result import Diagnostics, ExitCode, Result
from eda.units import format_quantity, parse_any_quantity


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    command: str | None = args.command
    if command is None:
        parser.print_help()
        return ExitCode.OK
    try:
        return _dispatch(args, command)
    except Exception as exc:  # a bug in eda must still answer in JSON
        # The full traceback goes to stderr for whoever debugs eda itself.
        traceback.print_exc(file=sys.stderr)
        frame = traceback.extract_tb(exc.__traceback__)[-1]
        diag = Diagnostics()
        diag.fail_tool(
            "internal_error",
            f"eda failed unexpectedly: {type(exc).__name__}: {exc}",
            where=f"{Path(frame.filename).name}:{frame.lineno}",
        )
        return _emit(diag.to_result(command, "", {}))


def _dispatch(args: argparse.Namespace, command: str) -> int:
    if command == "part":
        return _part(args.action, args.id)
    if command == "value":
        return _value(args.value, args.series)
    if command == "model":
        return _model(args)

    root = _project_root(args.project)
    if root is None:
        return _emit(_no_project(command, args.project))
    paths = ProjectPaths(root)
    if paths.out_dir.exists() and not paths.out_dir.is_dir():
        diag = Diagnostics()
        diag.fail_tool(
            "out_not_a_directory",
            f"{paths.out_dir} exists but is not a directory; eda writes its results there",
        )
        return _emit(diag.to_result(command, root.name, {}))

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
    elif command == "pcb":
        result = pipeline.pcb(root)
    elif command == "fw":
        result = pipeline.fw(root, build=args.build)
    else:
        result = pipeline.run(root)
    history.record_run(paths, result, note=args.message)
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

    def with_note(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("-m", "--message", help="note for the iteration log (intent / fix)")
        return p

    with_note(with_project("check", "validate design files and run ERC"))
    sim = with_note(with_project("sim", "simulate every test and judge expectations"))
    sim.add_argument("-t", "--test", help="run only this test")
    with_note(with_project("render", "render the schematic and waveforms (SVG + PNG)"))
    fw = with_note(with_project("fw", "check firmware pin roles, generate pins.h, scan sources"))
    fw.add_argument("--build", action="store_true", help="also compile with the pico-sdk")
    with_note(with_project("pcb", "build, autoroute, DRC, and export the board"))
    with_note(with_project("run", "check, sim, render, and pcb (if board.yaml) in one call"))
    log = with_project("log", "append a note to the iteration log")
    log.add_argument("-m", "--message", required=True, help="what changed and why")
    watch = with_project("watch", "re-run on file changes and serve the live viewer")
    watch.add_argument("--host", default="127.0.0.1")
    watch.add_argument("--port", type=int, default=8765)
    watch.add_argument("--no-open", action="store_true", help="do not open a browser")

    value = sub.add_parser("value", help="nearest standard (E-series) values for a target")
    value.add_argument("value", help="target quantity, e.g. 315ohm or 2.2uF")
    value.add_argument("-s", "--series", default="E24", choices=eseries.SERIES)

    model = sub.add_parser("model", help="inspect or import vendor SPICE models")
    model_sub = model.add_subparsers(dest="action", required=True)
    inspect = model_sub.add_parser("inspect", help="list .subckt definitions and pin comments")
    inspect.add_argument("file")
    imp = model_sub.add_parser("import", help="copy a model into parts/vendor/ as a part")
    imp.add_argument("file")
    imp.add_argument("--id", required=True, help="new part id, e.g. tl072")
    imp.add_argument("--subckt", help="subcircuit name when the file has several")
    imp.add_argument(
        "--opamp",
        metavar="IN+,IN-,V+,V-,OUT",
        help="subckt node names for the op-amp roles; gives pins in+ in- v+ v- out",
    )
    imp.add_argument("--name", help="display name")
    imp.add_argument(
        "--optran",
        metavar="TIME",
        help="find the operating point by a transient run of TIME (e.g. 1ms) for models "
        "that do not converge otherwise",
    )
    imp.add_argument("--category", help="category (default: opamp with --opamp, else ic)")

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


def _value(text: str, series: str) -> int:
    diag = Diagnostics()
    data: dict[str, JsonValue] = {}
    try:
        target, unit = parse_any_quantity(text)
        pick = eseries.nearest(target, series)
    except ValueError as exc:
        diag.error("invalid_value", str(exc))
    else:
        data = {
            "target": format_quantity(target, unit),
            "series": series,
            "nearest": format_quantity(pick.nearest, unit),
            "error_percent": round(pick.error * 100, 3),
            "below": format_quantity(pick.below, unit),
            "above": format_quantity(pick.above, unit),
        }
    return _emit(diag.to_result("value", "", data))


def _model(args: argparse.Namespace) -> int:
    diag = Diagnostics()
    data: dict[str, JsonValue] = {}
    file = Path(args.file)
    if not file.is_file():
        diag.error("file_missing", f"{file} not found")
        return _emit(diag.to_result(f"model {args.action}", "", data))
    if args.action == "inspect":
        data["subckts"] = [
            {
                "name": s.name,
                "nodes": list(s.nodes),
                "params": dict(s.params),
                "line": s.line,
                "comments": list(s.comments),
            }
            for s in vendor.inspect(vendor.read_text(file))
        ]
    else:
        opamp = [n.strip() for n in args.opamp.split(",")] if args.opamp else None
        try:
            plan = vendor.plan_import(file, args.id, args.subckt, opamp, args.category)
            target = vendor.VENDOR_PARTS_DIR / f"{plan.part_id}.yaml"
            previous = target.read_bytes() if target.is_file() else None
            written = vendor.write_part(plan, file, args.name, target.parent, args.optran)
        except ValueError as exc:
            diag.error("import_failed", str(exc))
        else:
            data = {
                "part": plan.part_id,
                "file": str(written),
                "subckt": plan.subckt.name,
                "pins": list(plan.pins),
                "compat": plan.compat,
            }
            loaded = load_parts(diag)
            if plan.part_id not in loaded:
                # A part file that does not load breaks every project: put back what was there.
                if previous is None:
                    written.unlink()
                else:
                    written.write_bytes(previous)
                diag.error("import_failed", f"written part {plan.part_id!r} did not load; removed")
    return _emit(diag.to_result(f"model {args.action}", "", data))


def _emit(result: Result) -> int:
    sys.stdout.write(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
    sys.stdout.write("\n")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
