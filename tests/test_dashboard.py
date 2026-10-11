import json
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from sf.agents import load_agent
from sf.dashboard import serve
from sf.run import Run, RunState

ROOT = Path(__file__).parent.parent


@pytest.fixture
def server(tmp_path, monkeypatch):
    shutil.copytree(ROOT / "agents", tmp_path / "agents")
    fake = tmp_path / "pi-list"
    fake.write_text("#!/bin/sh\nprintf 'provider model context\\nanthropic claude-x 1M\\n'\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SF_PI", str(fake))
    run = Run(tmp_path, RunState(run_id="r1", target="/t", trunk="main", branch="sf/r1"))
    run.state.stage, run.state.loop = "implement", 1
    run.state.results["review-1"] = "approve"
    run.log("stage_start")
    run.emit("pi", {"agent": "implementer", "event": {"type": "tool_execution_start", "toolName": "bash", "args": {}}})
    end = {"type": "message_end", "message": {"role": "assistant", "usage": {"totalTokens": 1234}}}
    run.emit("pi", {"agent": "implementer", "invocation": 1, "event": end})
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
    assert d["events_updated"] > 0
    assert [p[1:] for p in d["timeline"]] == [
        ["sf", "stage_start", None, None],
        ["implementer", "tool_execution_start", None, None],
        ["implementer", "message_end", 1234, 1],
    ]


def test_artefact_and_path_safety(server):
    assert get(server + "/api/runs/r1/plan.md") == "# Plan\n"
    for bad in ("/api/runs/r1/run-state.json", "/api/runs/r1/..%2F..%2Fsecret", "/api/runs/nope", "/api/runs/.."):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(server + bad)
        assert e.value.code == 404


def put(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def test_config_page_and_listing(server):
    assert "<title>sf config</title>" in get(server + "/config")
    cfg = json.loads(get(server + "/api/config"))
    assert cfg["agents"][0]["name"] == "planner"
    assert "xhigh" in cfg["thinking_levels"]
    assert cfg["models"] == ["anthropic/claude-x"] and cfg["models_error"] is None


def test_config_update_rewrites_only_frontmatter_lines(server, tmp_path):
    path = tmp_path / "agents" / "reviewer.md"
    before = path.read_text()
    assert put(server + "/api/config/reviewer", {"model": "anthropic/claude-x", "thinking": "low"}) == {"error": None}
    a = load_agent(tmp_path / "agents", "reviewer")
    assert (a.model, a.thinking) == ("anthropic/claude-x", "low")
    changed = [(x, y) for x, y in zip(before.splitlines(), path.read_text().splitlines()) if x != y]
    assert [y for _, y in changed] == ["model: anthropic/claude-x", "thinking: low"]


@pytest.mark.parametrize(
    "name,body",
    [
        ("reviewer", {"model": "m", "thinking": "huge"}),
        ("reviewer", {"model": "  ", "thinking": "low"}),
        ("reviewer", {"model": "a: b", "thinking": "low"}),
        ("nobody", {"model": "m", "thinking": "low"}),
        ("reviewer", {"thinking": "low"}),
    ],
)
def test_config_update_rejects_bad_input(server, tmp_path, name, body):
    before = (tmp_path / "agents" / "reviewer.md").read_text()
    with pytest.raises(urllib.error.HTTPError) as e:
        put(server + f"/api/config/{name}", body)
    assert e.value.code == 400 and json.loads(e.value.read())["error"]
    assert (tmp_path / "agents" / "reviewer.md").read_text() == before
