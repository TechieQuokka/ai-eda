import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from watchfiles import Change

from eda import pipeline
from eda.project import ProjectPaths
from eda.viewer import server
from tests.conftest import MakeProject, requires_ngspice


def test_classify_changes(make_project: MakeProject) -> None:
    paths = ProjectPaths(make_project())
    design = str(paths.circuit_file)
    output = str(paths.out_dir / "sim.json")
    log = str(paths.log_file)
    assert server._classify(paths, [(Change.modified, design)]) == (True, False)  # pyright: ignore[reportPrivateUsage]
    assert server._classify(paths, [(Change.modified, output)]) == (False, True)  # pyright: ignore[reportPrivateUsage]
    assert server._classify(paths, [(Change.added, log)]) == (False, True)  # pyright: ignore[reportPrivateUsage]


@requires_ngspice
def test_http_routes(make_project: MakeProject) -> None:
    root = make_project()
    pipeline.run(root)
    paths = ProjectPaths(root)
    hub = server._Hub()  # pyright: ignore[reportPrivateUsage]
    handler = type("H", (server._Handler,), {"paths": paths, "hub": hub})  # pyright: ignore[reportPrivateUsage]
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{base}/api/state") as resp:
            state = json.loads(resp.read())
        assert state["project"] == "demo"
        assert state["sim"]["ok"] is True
        with urllib.request.urlopen(f"{base}/") as resp:
            assert b"eda viewer" in resp.read()
        with urllib.request.urlopen(f"{base}/out/sim.json") as resp:
            assert resp.status == 200
        try:
            urllib.request.urlopen(f"{base}/out/../project.yaml")
            raise AssertionError("path traversal was served")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        httpd.shutdown()
