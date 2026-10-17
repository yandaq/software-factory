import json
import os
import shutil
import sys
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
    monkeypatch.setattr("sf.dashboard.list_native_models", lambda h: (["sonnet"] if h == "claude-code"
                                                               else ["gpt-test"], None))
    run = Run(tmp_path, RunState(run_id="r1", target="/t", trunk="main", branch="sf/r1"))
    run.state.stage, run.state.loop = "implement", 1
    run.state.results["review-1"] = "approve"
    run.log("stage_start")
    run.log("pi_start", agent="implementer", cmd=["pi", "--model", "anthropic/claude-x", "--thinking", "high"])
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
    assert [e["type"] for e in d["events"]] == ["stage_start", "pi_start"]
    assert d["agents"] == {"implementer": {"model": "anthropic/claude-x", "thinking": "high"}}
    assert d["tools"][0]["event"]["toolName"] == "bash"
    assert d["artefacts"] == ["plan.md"]
    assert d["events_updated"] > 0
    assert [p[1:] for p in d["timeline"]] == [
        ["sf", "stage_start", None, None],
        ["implementer", "pi_start", None, None],
        ["implementer", "tool_execution_start", None, None],
        ["implementer", "message_end", 1234, 1],
    ]


def test_artefact_and_path_safety(server):
    assert get(server + "/api/runs/r1/plan.md") == "# Plan\n"
    for bad in ("/api/runs/r1/run-state.json", "/api/runs/r1/..%2F..%2Fsecret", "/api/runs/nope", "/api/runs/.."):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(server + bad)
        assert e.value.code == 404


def test_delete_run(server, tmp_path):
    for bad in ("nope", ".."):
        req = urllib.request.Request(f"{server}/api/runs/{bad}", method="DELETE")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 404
    urllib.request.urlopen(urllib.request.Request(server + "/api/runs/r1", method="DELETE")).close()
    assert not (tmp_path / "runs" / "r1").exists()
    assert [r["run_id"] for r in json.loads(get(server + "/api/runs"))] == ["broken"]


@pytest.fixture
def retro_started(monkeypatch):
    """Record the child process a retro would start, instead of running the improvement agent."""
    started = []

    class FakePopen:
        def __init__(self, cmd, **kwargs):
            started.append({"cmd": cmd, **kwargs})

    monkeypatch.setattr("sf.dashboard.subprocess.Popen", FakePopen)
    return started


def post(url):
    req = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def mark_merged(tmp_path):
    path = tmp_path / "runs" / "r1" / "run-state.json"
    data = json.loads(path.read_text())
    data["completed"] = ["branch", "prompt", "plan", "implement", "document", "merge", "improve"]
    path.write_text(json.dumps(data))


def test_retro_starts_the_improvement_agent_again(server, tmp_path, retro_started):
    assert 'class="retro"' in get(server + "/")
    mark_merged(tmp_path)
    assert post(server + "/api/runs/r1/retro") == (200, {"error": None})
    assert retro_started[0]["cmd"] == [sys.executable, "-m", "sf.cli", "improve", "r1"]
    assert retro_started[0]["cwd"] == tmp_path
    log = tmp_path / "runs" / "r1" / "retro.log"
    assert log.is_file()
    log.write_text("stage improve\n")
    assert "retro.log" in json.loads(get(server + "/api/runs/r1"))["artefacts"]
    assert get(server + "/api/runs/r1/retro.log") == "stage improve\n"


def test_retro_refused_before_merge_or_while_busy(server, tmp_path, retro_started):
    assert post(server + "/api/runs/r1/retro")[0] == 409  # r1 has not merged
    assert "not merged" in post(server + "/api/runs/r1/retro")[1]["error"]
    mark_merged(tmp_path)
    (tmp_path / "runs" / ".lock").write_text(str(os.getpid()))
    assert post(server + "/api/runs/r1/retro")[0] == 409  # another sf run holds the run lock
    assert post(server + "/api/runs/nope/retro")[0] == 404
    assert post(server + "/api/runs/../retro")[0] == 404
    assert retro_started == []


def test_run_list_marks_retro_eligibility(server, tmp_path):
    assert {r["run_id"]: r["can_retro"] for r in json.loads(get(server + "/api/runs"))} == {
        "r1": False, "broken": False}
    mark_merged(tmp_path)
    assert [r["can_retro"] for r in json.loads(get(server + "/api/runs")) if r["run_id"] == "r1"] == [True]


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
    assert {h["id"]: h["models"] for h in cfg["harnesses"]} == {
        "pi": ["anthropic/claude-x"], "claude-code": ["sonnet"], "codex": ["gpt-test"]}


def test_improvement_history(server, tmp_path):
    assert json.loads(get(server + "/api/improvements")) == []
    records = [{"run_id": "old", "title": "First", "decision": "approved"},
               {"run_id": "new", "title": "Second <script>", "decision": "automatic"}]
    path = tmp_path / "improvements.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    assert json.loads(get(server + "/api/improvements")) == records[::-1]
    assert path.read_text().count("\n") == 2
    page = get(server + "/config")
    assert page.index('id="agents"') < page.index('id="improvements"')
    path.write_text("{broken\n")
    with pytest.raises(urllib.error.HTTPError) as error:
        get(server + "/api/improvements")
    assert error.value.code == 500
    assert "Cannot read improvement log" in json.loads(error.value.read())["error"]


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


@pytest.mark.parametrize("harness,model", [("claude-code", "sonnet"), ("codex", "gpt-5-codex")])
def test_harness_config_roundtrip_preserves_prompt(server, tmp_path, harness, model):
    path = tmp_path / "agents" / "reviewer.md"
    before = path.read_text()
    assert put(server + "/api/config/reviewer", {"harness": harness, "model": model, "thinking": "medium"}) == {"error": None}
    after = load_agent(tmp_path / "agents", "reviewer")
    original = load_agent(ROOT / "agents", "reviewer")
    assert (after.harness, after.model, after.thinking) == (harness, model, "medium")
    assert after.body == original.body and after.tools == original.tools and after.contract == original.contract
    assert path.read_text().split("---", 2)[2] == before.split("---", 2)[2]
    cfg = json.loads(get(server + "/api/config"))
    assert cfg["agents"][2]["harness"] == harness
    assert [h["id"] for h in cfg["harnesses"]] == ["pi", "claude-code", "codex"]
    # Older callers may omit harness without resetting it to Pi.
    put(server + "/api/config/reviewer", {"model": model, "thinking": "low"})
    assert load_agent(tmp_path / "agents", "reviewer").harness == harness
    put(server + "/api/config/reviewer", {"harness": "pi", "model": "anthropic/claude-x", "thinking": "off"})
    assert load_agent(tmp_path / "agents", "reviewer").harness == "pi"


@pytest.mark.parametrize("body", [
    {"harness": "unknown", "model": "m", "thinking": "high"},
    {"harness": "codex", "model": "m", "thinking": "off"},
    {"harness": "claude-code", "model": "m", "thinking": "max"},
    {"harness": None, "model": "m", "thinking": "high"},
    {"harness": "pi", "model": "m\rharness: codex", "thinking": "high"},
    {"harness": "pi", "model": None, "thinking": "high"},
])
def test_invalid_harness_config_does_not_write(server, tmp_path, body):
    path = tmp_path / "agents" / "reviewer.md"
    before = path.read_text()
    with pytest.raises(urllib.error.HTTPError) as e:
        put(server + "/api/config/reviewer", body)
    assert e.value.code == 400 and path.read_text() == before


def test_config_catalog_failure_isolated(server, monkeypatch):
    monkeypatch.setattr("sf.dashboard.list_native_models",
                        lambda h: ([], "not authenticated") if h == "codex" else (["sonnet"], None))
    cfg = json.loads(get(server + "/api/config"))
    harnesses = {h["id"]: h for h in cfg["harnesses"]}
    assert harnesses["codex"]["models"] == []
    assert harnesses["codex"]["models_error"] == "not authenticated"
    assert harnesses["claude-code"]["models"] == ["sonnet"]
    assert cfg["models"] == ["anthropic/claude-x"]


@pytest.mark.parametrize("name", ["planner", "implementer", "reviewer", "documenter", "improver"])
def test_agent_file_roundtrip(server, tmp_path, name):
    path = tmp_path / "agents" / f"{name}.md"
    original = path.read_text()
    url = f"{server}/api/config/{name}/file"
    assert json.loads(get(url)) == {"content": original}
    content = original + "\nExtra instruction: preserve <markup> and Unicode £.\n"
    result = put(url, {"content": content, "original": original})
    assert result["error"] is None and result["name"] == name
    assert path.read_text() == content
    assert "Extra instruction" in load_agent(tmp_path / "agents", name).body


@pytest.mark.parametrize("kind", ["invalid", "name", "schema", "type", "stale"])
def test_agent_file_rejected_without_writing(server, tmp_path, kind):
    path = tmp_path / "agents" / "planner.md"
    original = path.read_text()
    content = original + "\nNew instruction\n"
    body = {"content": content, "original": original}
    if kind == "invalid":
        body["content"] = "no frontmatter"
    elif kind == "name":
        body["content"] = content.replace("name: planner", "name: reviewer")
    elif kind == "schema":
        body["content"] = content.replace("type: object", "type: invalid-type")
    elif kind == "type":
        body["content"] = None
    else:
        path.write_text(original + "\nConcurrent edit\n")
    before = path.read_text()
    with pytest.raises(urllib.error.HTTPError) as e:
        put(server + "/api/config/planner/file", body)
    assert e.value.code == (409 if kind == "stale" else 400)
    assert json.loads(e.value.read())["error"]
    assert path.read_text() == before


def test_agent_file_path_safety(server):
    for name in ("nobody", "..%2Fplanner"):
        url = f"{server}/api/config/{name}/file"
        for request in (lambda url=url: get(url), lambda url=url: put(url, {"content": "x", "original": "x"})):
            with pytest.raises(urllib.error.HTTPError) as e:
                request()
            assert e.value.code == 404


@pytest.mark.parametrize("name", ["planner", "implementer", "reviewer", "documenter", "improver"])
def test_user_file_roundtrip(server, tmp_path, name):
    from sf.agents import TASK_VARIABLES

    path = tmp_path / "agents" / f"{name}.user.md"
    role = (tmp_path / "agents" / f"{name}.md").read_bytes()
    original = path.read_text()
    url = f"{server}/api/config/{name}/user-file"
    data = json.loads(get(url))
    assert data == {"content": original, "placeholders": list(TASK_VARIABLES[name])}
    content = original + "\nExtra instruction £\n"
    assert put(url, {"content": content, "original": original}) == {"error": None, "name": name}
    assert path.read_text() == content
    assert (tmp_path / "agents" / f"{name}.md").read_bytes() == role


@pytest.mark.parametrize("kind", ["empty", "unknown", "missing", "malformed", "type", "stale"])
def test_user_file_rejected_without_writing(server, tmp_path, kind):
    path = tmp_path / "agents" / "planner.user.md"
    original = path.read_text()
    content = {"empty": "", "unknown": "{{request}} {{other}}", "missing": "no context",
               "malformed": "{{ request }}", "type": None, "stale": original}[kind]
    if kind == "stale":
        path.write_text(original + "\nConcurrent edit")
    before = path.read_bytes()
    with pytest.raises(urllib.error.HTTPError) as e:
        put(server + "/api/config/planner/user-file", {"content": content, "original": original})
    assert e.value.code == (409 if kind == "stale" else 400)
    assert json.loads(e.value.read())["error"]
    assert path.read_bytes() == before


def test_user_file_unknown_agent(server):
    for method in (get, lambda url: put(url, {"content": "x", "original": "x"})):
        with pytest.raises(urllib.error.HTTPError) as e:
            method(server + "/api/config/..%2Fplanner/user-file")
        assert e.value.code == 404


def test_legacy_portability_artifacts_stay_readable(server, tmp_path):
    """No run records portability artifacts any more, but older runs must stay browsable."""
    run_dir = tmp_path / "runs/r1"
    content = '{"verdict":"changes_requested","findings":[]}'
    (run_dir / "portability-1-result.json").write_text(content)
    detail = json.loads(get(server + "/api/runs/r1"))
    assert "portability-1-result.json" in detail["artefacts"]
    assert get(server + "/api/runs/r1/portability-1-result.json") == content


@pytest.fixture
def project(server, tmp_path):
    from conftest import init_repo, sh

    target = init_repo(tmp_path / "project")
    (target / "docs").mkdir()
    (target / "docs/design & notes £#.MD").write_text("# Design £\n")
    (target / ".gitignore").write_text("ignored.md\n")
    (target / "ignored.md").write_text("ignored")
    (target / "app.py").write_text("code")
    (target / "link.md").symlink_to(target / "README.md")
    sh(target, "git", "add", "README.md", ".gitignore", "docs")
    sh(target, "git", "commit", "-qm", "project docs")
    run = Run.load(tmp_path, "r1")
    run.state.target = str(target)
    run.save()
    return target


def document_url(server, path, run_id="r1"):
    return f"{server}/api/runs/{run_id}/document?" + urllib.parse.urlencode({"path": path})


def test_project_document_inventory_and_save(server, project):
    from conftest import sh

    detail = json.loads(get(server + "/api/runs/r1"))
    assert detail["documents"] == ["README.md", "docs/design & notes £#.MD"]
    assert detail["documents_error"] is None
    head = sh(project, "git", "rev-parse", "HEAD")
    path = "docs/design & notes £#.MD"
    original = json.loads(get(document_url(server, path)))["content"]
    content = original + "\nReviewed <markup> and Unicode £.\n"
    assert put(document_url(server, path), {"content": content, "original": original}) == {"error": None}
    assert (project / path).read_text() == content
    assert json.loads(get(document_url(server, path)))["content"] == content
    assert sh(project, "git", "rev-parse", "HEAD") == head  # saves do not commit
    (project / "new.md").write_text("# Newly available")
    assert "new.md" in json.loads(get(server + "/api/runs/r1"))["documents"]


@pytest.mark.parametrize("path", ["../README.md", "/etc/passwd", "docs/../README.md", "app.py",
                                  "ignored.md", "link.md", "missing.md", ""])
def test_project_document_scope_rejects_reads_and_writes(server, project, path):
    before = (project / "README.md").read_text()
    url = document_url(server, path)
    for action in (lambda: get(url), lambda: put(url, {"content": "forbidden", "original": before})):
        with pytest.raises(urllib.error.HTTPError) as e:
            action()
        assert e.value.code == 404
        assert json.loads(e.value.read())["error"]
    assert (project / "README.md").read_text() == before
    assert not (project / "missing.md").exists()


@pytest.mark.parametrize("body", [{}, [], {"content": None, "original": "# target\n"},
                                  {"content": "text", "original": 1}])
def test_project_document_invalid_payload(server, project, body):
    before = (project / "README.md").read_text()
    with pytest.raises(urllib.error.HTTPError) as e:
        put(document_url(server, "README.md"), body)
    assert e.value.code == 400
    assert (project / "README.md").read_text() == before


def test_project_document_stale_deleted_and_replaced_files(server, project, tmp_path):
    path = project / "README.md"
    original = path.read_text()
    path.write_text("Concurrent change")
    with pytest.raises(urllib.error.HTTPError) as e:
        put(document_url(server, "README.md"), {"content": "draft", "original": original})
    assert e.value.code == 409
    assert "changed since opening" in json.loads(e.value.read())["error"]
    assert path.read_text() == "Concurrent change"
    path.unlink()
    with pytest.raises(urllib.error.HTTPError) as e:
        put(document_url(server, "README.md"), {"content": "draft", "original": original})
    assert e.value.code == 404 and not path.exists()
    outside = tmp_path / "outside.md"
    outside.write_text(original)
    path.symlink_to(outside)
    with pytest.raises(urllib.error.HTTPError) as e:
        put(document_url(server, "README.md"), {"content": "draft", "original": original})
    assert e.value.code == 404 and outside.read_text() == original


def test_project_documents_unavailable_is_observable(server):
    detail = json.loads(get(server + "/api/runs/r1"))
    assert detail["documents"] == [] and "Could not load project documents" in detail["documents_error"]
    assert detail["state"]["run_id"] == "r1"
    for run_id in ("r1", "missing"):
        with pytest.raises(urllib.error.HTTPError) as e:
            get(document_url(server, "README.md", run_id))
        assert e.value.code == 404


def delete_document(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"}, method="DELETE")
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())


@pytest.mark.parametrize("name", ["README.md", "docs/design & notes £#.MD", "new.md"])
def test_project_document_delete_updates_inventory_without_commit(server, project, name):
    from conftest import sh

    if name == "new.md":
        (project / name).write_text("# Untracked doc\n")
    head = sh(project, "git", "rev-parse", "HEAD")
    url = document_url(server, name)
    opened = json.loads(get(url))
    assert opened["deletable"] is True
    assert delete_document(url, {"original": opened["content"]}) == {"error": None}
    assert not (project / name).exists()
    assert name not in json.loads(get(server + "/api/runs/r1"))["documents"]
    assert sh(project, "git", "rev-parse", "HEAD") == head
    assert (project / "app.py").read_text() == "code"


@pytest.mark.parametrize("name", ["../README.md", "/etc/passwd", "docs/../README.md", "app.py",
                                  "ignored.md", "link.md", "missing.md", ""])
def test_project_document_delete_rejects_out_of_scope(server, project, name):
    before = (project / "README.md").read_text()
    with pytest.raises(urllib.error.HTTPError) as e:
        delete_document(document_url(server, name), {"original": before})
    assert e.value.code == 404
    assert (project / "README.md").read_text() == before
    assert (project / "app.py").read_text() == "code"


@pytest.mark.parametrize("body", [{}, [], {"original": None}, {"original": 1}])
def test_project_document_delete_rejects_invalid_body(server, project, body):
    with pytest.raises(urllib.error.HTTPError) as e:
        delete_document(document_url(server, "README.md"), body)
    assert e.value.code == 400
    assert (project / "README.md").exists()


def test_project_document_delete_conflict_and_symlink_replacement(server, project, tmp_path):
    path = project / "README.md"
    original = path.read_text()
    path.write_text("Concurrent change")
    with pytest.raises(urllib.error.HTTPError) as e:
        delete_document(document_url(server, "README.md"), {"original": original})
    assert e.value.code == 409
    assert "Reopen it before deleting" in json.loads(e.value.read())["error"]
    assert path.read_text() == "Concurrent change"
    path.unlink()
    outside = tmp_path / "outside.md"
    outside.write_text(original)
    path.symlink_to(outside)
    with pytest.raises(urllib.error.HTTPError) as e:
        delete_document(document_url(server, "README.md"), {"original": original})
    assert e.value.code == 404 and outside.read_text() == original


@pytest.mark.parametrize("suffix", [".md", ".user.md"])
def test_agent_config_cannot_be_deleted_even_through_project_route(server, tmp_path, suffix):
    from conftest import init_repo

    # A run targeting the factory must not provide a back door to delete agent configuration.
    init_repo(tmp_path)
    run = Run.load(tmp_path, "r1")
    run.state.target = str(tmp_path)
    run.save()
    name = "planner" + suffix
    path = tmp_path / "agents" / name
    original = path.read_text()
    doc_url = document_url(server, "agents/" + name)
    assert json.loads(get(doc_url))["deletable"] is False
    with pytest.raises(urllib.error.HTTPError) as e:
        delete_document(doc_url, {"original": original})
    assert e.value.code == 403
    assert "Agent configuration" in json.loads(e.value.read())["error"]
    endpoint = "user-file" if suffix == ".user.md" else "file"
    with pytest.raises(urllib.error.HTTPError) as e:
        delete_document(server + "/api/config/planner/" + endpoint, {"original": original})
    assert e.value.code == 404 and path.read_text() == original
