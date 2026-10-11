import json
import threading
import urllib.error
import urllib.request

import pytest

from sf.dashboard import serve
from sf.run import Run, RunState


@pytest.fixture
def server(tmp_path):
    run = Run(tmp_path, RunState(run_id="r1", target="/t", trunk="main", branch="sf/r1"))
    run.state.stage, run.state.loop = "implement", 1
    run.state.results["review-1"] = "approve"
    run.log("stage_start")
    run.emit("pi", {"agent": "implementer", "event": {"type": "tool_execution_start", "toolName": "bash", "args": {}}})
    (run.dir / "plan.md").write_text("# Plan\n")
    run.save()
    (tmp_path / "runs" / "broken").mkdir()
    (tmp_path / "runs" / "broken" / "run-state.json").write_text("{")
    srv = serve(tmp_path / "runs", "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url) as r:
        return r.read().decode()


def test_index_served(server):
    assert "<title>sf runs</title>" in get(server + "/")


def test_runs_list_includes_unreadable(server):
    runs = {r["run_id"]: r for r in json.loads(get(server + "/api/runs"))}
    assert runs["r1"]["stage"] == "implement" and runs["r1"]["cost"] == 0
    assert runs["broken"]["status"] == "unreadable"


def test_run_detail(server):
    d = json.loads(get(server + "/api/runs/r1"))
    assert d["state"]["results"]["review-1"] == "approve"
    assert [e["type"] for e in d["events"]] == ["stage_start"]
    assert d["tools"][0]["event"]["toolName"] == "bash"
    assert d["artefacts"] == ["plan.md"]


def test_artefact_and_path_safety(server):
    assert get(server + "/api/runs/r1/plan.md") == "# Plan\n"
    for bad in ("/api/runs/r1/run-state.json", "/api/runs/r1/..%2F..%2Fsecret", "/api/runs/nope", "/api/runs/.."):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(server + bad)
        assert e.value.code == 404
