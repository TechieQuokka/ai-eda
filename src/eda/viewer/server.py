"""`eda watch`: re-run the pipeline on file changes and serve a live local viewer.

A stdlib HTTP server serves the static page, the project's `out/` files, `/api/state`, and
`/events` (Server-Sent Events). Design changes re-run `check → sim → render`; changes to
`out/` or the iteration log (e.g. from `eda` runs in another terminal) only refresh viewers.
"""

import json
import mimetypes
import sys
import threading
import webbrowser
from collections.abc import Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlparse

from pydantic import JsonValue
from watchfiles import Change
from watchfiles import watch as watch_files

from eda import history, pipeline
from eda.project import BUILTIN_PARTS_DIR, ProjectPaths

STATIC_DIR = Path(__file__).parent / "static"
KEEPALIVE_S = 15.0


class _Hub:
    """Version counter that SSE clients wait on."""

    def __init__(self) -> None:
        self.version = 0
        self._cond = threading.Condition()

    def bump(self) -> None:
        with self._cond:
            self.version += 1
            self._cond.notify_all()

    def wait(self, seen: int, timeout: float) -> int:
        with self._cond:
            self._cond.wait_for(lambda: self.version != seen, timeout=timeout)
            return self.version


def _read_json(path: Path) -> JsonValue:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def state(paths: ProjectPaths, version: int) -> dict[str, JsonValue]:
    out = paths.out_dir
    return {
        "project": paths.name,
        "version": version,
        "check": _read_json(out / "check.json"),
        "sim": _read_json(out / "sim.json"),
        "render": _read_json(out / "render.json"),
        "pcb": _read_json(out / "pcb.json"),
        "fw": _read_json(out / "fw.json"),
        "log": history.read_log(paths),
    }


class _Handler(BaseHTTPRequestHandler):
    paths: ClassVar[ProjectPaths]
    hub: ClassVar[_Hub]

    def log_message(self, format: str, *args: object) -> None:
        return  # keep the terminal quiet

    def do_GET(self) -> None:
        route = unquote(urlparse(self.path).path)
        if route == "/events":
            self._events()
        elif route == "/api/state":
            body = json.dumps(state(self.paths, self.hub.version)).encode()
            self._send(HTTPStatus.OK, "application/json", body)
        elif route.startswith("/out/"):
            self._file(self.paths.out_dir, route.removeprefix("/out/"))
        else:
            self._file(STATIC_DIR, route.lstrip("/") or "index.html")

    def _file(self, base: Path, relative: str) -> None:
        target = (base / relative).resolve()
        if not target.is_relative_to(base.resolve()) or not target.is_file():
            self._send(HTTPStatus.NOT_FOUND, "text/plain", b"not found")
            return
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, kind, target.read_bytes())

    def _send(self, status: HTTPStatus, kind: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        seen = -1
        try:
            while True:
                version = self.hub.wait(seen, KEEPALIVE_S)
                if version != seen:
                    seen = version
                    self.wfile.write(f"event: update\ndata: {version}\n\n".encode())
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


def _run(paths: ProjectPaths, hub: _Hub, reason: str) -> None:
    result = pipeline.run(paths.root)
    history.record_run(paths, result, only_if_changed=True)
    status = "ok" if result.ok else f"{len(result.errors)} error(s)"
    print(f"[eda watch] {reason}: {status}", file=sys.stderr, flush=True)
    hub.bump()


def _classify(paths: ProjectPaths, changes: Iterable[tuple[Change, str]]) -> tuple[bool, bool]:
    """(design changed → re-run, outputs changed → refresh only)."""
    rerun = refresh = False
    out = paths.out_dir.resolve()
    for _, name in changes:
        path = Path(name).resolve()
        if path.is_relative_to(out) or path == paths.log_file.resolve():
            refresh = True
        elif path.suffix in {".yaml", ".yml"}:
            rerun = True
    return rerun, refresh


def watch(root: Path, host: str, port: int, open_browser: bool) -> int:
    paths = ProjectPaths(root)
    hub = _Hub()
    handler = type("Handler", (_Handler,), {"paths": paths, "hub": hub})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://{host}:{server.server_address[1]}/"
    print(json.dumps({"ok": True, "command": "watch", "project": paths.name, "url": url}))
    sys.stdout.flush()
    if open_browser:
        webbrowser.open(url)

    _run(paths, hub, "initial run")
    targets = [p for p in (root, BUILTIN_PARTS_DIR) if p.exists()]
    try:
        for changes in watch_files(*targets, debounce=300):
            rerun, refresh = _classify(paths, changes)
            if rerun:
                _run(paths, hub, "design changed")
            elif refresh:
                hub.bump()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0
