"""Configured targets: URL normalisation, the targets store, dashboard submission and DB-mode `sf watch`."""

from pathlib import Path

import pytest
from conftest import sh
from test_dashboard import server  # noqa: F401 - file-mode dashboard fixture
from test_mailbox import Polls, drop, watch_box
from test_queued_improve import call, db_server  # noqa: F401 - Postgres dashboard fixture

from sf import cli, store
from sf import git as g


@pytest.mark.parametrize("url, key", [
    ("git@github.com:Owner/Repo.git", "github.com/Owner/Repo"),
    ("git@GitHub.com:o/r", "github.com/o/r"),
    ("ssh://git@github.com/o/r.git", "github.com/o/r"),
    ("ssh://git@host.example:2222/o/r/", "host.example/o/r"),
    ("https://GitHub.com/o/r.git", "github.com/o/r"),
    ("https://github.com/o/r/", "github.com/o/r"),
    ("/srv/git/repo.git", None),
    ("./relative", None),
])
def test_target_key(url, key):
    assert g.target_key(url) == key


def test_bare_owner_repo_expands_to_github_ssh():
    assert g.expand_target_url(" owner/repo ") == "git@github.com:owner/repo.git"
    assert g.expand_target_url("https://x.org/o/r") == "https://x.org/o/r"


def test_ls_remote_reports_unreachable_repositories(tmp_path: Path, remote: Path):
    g.ls_remote(str(remote))
    with pytest.raises(g.GitError, match="cannot reach"):
        g.ls_remote(str(tmp_path / "missing"))


def test_targets_store_round_trip(db):
    added = store.add_target(db, "git@github.com:o/r.git", "github.com/o/r")
    with pytest.raises(ValueError, match="already"):
        store.add_target(db, "https://github.com/o/r", "github.com/o/r")
    assert [t["key"] for t in store.list_targets(db)] == ["github.com/o/r"]
    assert store.get_target(db, "github.com/o/r")["url"] == "git@github.com:o/r.git"
    assert store.delete_target(db, added["id"]) and not store.delete_target(db, added["id"])
    assert store.list_targets(db) == []


def test_file_mode_refuses_targets_and_submission(server):  # noqa: F811
    assert call(server + "/api/mode") == (200, {"queued": False})
    for method, path, body in (("GET", "/api/targets", None), ("POST", "/api/targets", {"url": "o/r"}),
                               ("DELETE", "/api/targets/1", None), ("GET", "/api/runs?target=x", None),
                               ("POST", "/api/runs", {"target": "x", "prompt": "p"})):
        status, payload = call(server + path, method, body)
        assert status == 409 and "SF_DATABASE_URL" in payload["error"], path


def test_targets_api_and_dashboard_submission(db_server, db, factory, target, remote, tmp_path):  # noqa: F811
    (target / ".sf.toml").write_text('trunk = "main"\n')
    sh(target, "git", "add", "-A")
    sh(target, "git", "commit", "-qm", "config")
    sh(target, "git", "push", "-q", "origin", "main")
    assert call(db_server + "/api/mode") == (200, {"queued": True})

    status, added = call(db_server + "/api/targets", "POST", {"url": str(remote)})
    assert status == 201 and added["target"]["key"] == str(remote.resolve())
    key = added["target"]["key"]
    assert call(db_server + "/api/targets", "POST", {"url": str(remote)})[0] == 409
    status, bad = call(db_server + "/api/targets", "POST", {"url": str(tmp_path / "nope")})
    assert status == 400 and "cannot reach" in bad["error"]
    assert call(db_server + "/api/targets", "POST", {})[0] == 400
    assert [t["key"] for t in call(db_server + "/api/targets")[1]] == [key]

    status, submitted = call(db_server + "/api/runs", "POST", {"target": key, "prompt": "build it"})
    assert status == 201
    run_id = submitted["run_id"]
    state = store.load_state(db, run_id)
    assert state.target == key and state.trunk == "main"
    assert store.load_config(db, run_id)["target_config"]["remote"] == key
    assert store.get_document(db, run_id, "prompt.md") == "build it\n"
    assert not list((g.cache_dir() / "submit").iterdir())  # the throwaway clone is gone
    assert call(db_server + "/api/runs", "POST", {"target": "nope", "prompt": "x"})[0] == 404
    assert call(db_server + "/api/runs", "POST", {"target": key, "prompt": " "})[0] == 400

    [run] = call(db_server + "/api/runs")[1]
    assert run["run_id"] == run_id and run["target_key"] == key
    assert [r["run_id"] for r in call(db_server + f"/api/runs?target={key}")[1]] == [run_id]
    assert call(db_server + "/api/runs?target=github.com/o/r")[1] == []

    target_id = added["target"]["id"]
    assert call(db_server + f"/api/targets/{target_id}", "DELETE") == (200, {"error": None})
    assert call(db_server + f"/api/targets/{target_id}", "DELETE")[0] == 404
    assert call(db_server + "/api/runs")[1][0]["run_id"] == run_id  # runs remain


def test_submission_reports_a_target_that_cannot_be_cloned(db_server, db, remote, tmp_path):  # noqa: F811
    status, added = call(db_server + "/api/targets", "POST", {"url": str(remote)})
    moved = tmp_path / "moved.git"
    remote.rename(moved)
    status, payload = call(db_server + "/api/runs", "POST", {"target": added["target"]["key"], "prompt": "x"})
    assert status == 502 and "cannot clone" in payload["error"]
    assert db.execute("SELECT count(*) AS n FROM runs").fetchone()["n"] == 0


def test_watch_queues_prompts_when_a_database_is_configured(db, db_url, factory, target, tmp_path, monkeypatch):
    monkeypatch.setenv("SF_DATABASE_URL", db_url)
    box = watch_box(tmp_path)
    drop(box, "first.md", "add a health endpoint\n", age=300)
    drop(box, "second.md", "add a logout button\n", age=100)

    cli._watch_loop(factory, target, box, approve_plan=False,
                    sleep=Polls(lambda: len(list(box.done.iterdir())) == 2))

    assert sorted(p.name for p in box.done.iterdir()) == ["first.md", "second.md"]
    prompts = sorted(r["body"] for r in db.execute("SELECT body FROM run_documents WHERE name = 'prompt.md'"))
    assert prompts == ["add a health endpoint\n", "add a logout button\n"]
    jobs = db.execute("SELECT stage, status FROM stage_jobs").fetchall()
    assert len(jobs) == 2 and {j["status"] for j in jobs} == {"queued"}
    assert not (factory / "runs").exists() or not list((factory / "runs").glob("*/run-state.json"))
