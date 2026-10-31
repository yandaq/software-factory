"""Working-tree handoff through a target remote, infra requeue on push failure, and the serialised merge."""

import threading
from pathlib import Path

from conftest import sh
from test_queued_workflow import ALL_CAPS, create, jobs

from sf import git as g
from sf import queue, store
from sf.workflow import Finished, Next, run_job


def commit_on_branch(remote: Path, tmp: Path, branch: str, files: dict[str, str], base: str = "main") -> None:
    """Push `branch` = `base` + one commit writing `files`, as an earlier stage of a run would have."""
    work = tmp / f"seed-{branch.replace('/', '-')}"
    sh(tmp, "git", "clone", "-q", "--branch", base, str(remote), str(work))
    sh(work, "git", "checkout", "-qB", branch)
    for name, text in files.items():
        (work / name).write_text(text)
    sh(work, "git", "add", "-A")
    sh(work, "git", "commit", "-qm", f"change on {branch}")
    sh(work, "git", "push", "-q", "origin", branch)


def push_trunk(target: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (target / name).write_text(text)
    sh(target, "git", "add", "-A")
    sh(target, "git", "commit", "-qm", "trunk setup")
    sh(target, "git", "push", "-q", "origin", "main")


def merge_job(db, factory, target, runner="w"):
    """A run whose branch is ready and whose merge job is claimed by `runner`."""
    state = create(db, factory, target, completed=["branch", "prompt", "plan", "implement", "document"], loop=1)
    queue.enqueue(db, state.run_id, "merge")
    queue.register_runner(db, runner, "host", ALL_CAPS)
    return state, queue.claim(db, runner, ALL_CAPS)


def test_separate_job_clones_see_each_others_commits(tmp_path, target, remote):
    first = g.job_dir("r1")
    assert g.checkout_job(str(remote), "sf/r1", "main", first) is None  # no branch yet: trunk
    sh(first, "git", "checkout", "-qb", "sf/r1")
    (first / "a.txt").write_text("a")
    sh(first, "git", "add", "-A")
    sh(first, "git", "commit", "-qm", "a")
    sha = g.publish_branch(first, "sf/r1", None)
    second = g.job_dir("r1")
    assert g.checkout_job(str(remote), "sf/r1", "main", second) == sha
    assert (second / "a.txt").read_text() == "a"
    assert sh(second, "git", "branch", "--show-current").strip() == "sf/r1"
    assert sh(second, "git", "rev-parse", "main") == sh(remote, "git", "rev-parse", "main")
    assert g.publish_branch(second, "sf/r1", sha) == sha  # unchanged: nothing pushed


def test_snapshot_records_target_remote(db, factory, target, remote):
    state = create(db, factory, target)
    assert store.load_config(db, state.run_id)["target_config"]["remote"] == str(remote)


def test_push_failure_requeues_job_as_infra_failure(db, factory, target, remote):
    hook = remote / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho rejected by test >&2\nexit 1\n")
    hook.chmod(0o755)
    state = create(db, factory, target)
    queue.enqueue(db, state.run_id, "branch")
    queue.register_runner(db, "w", "host", ALL_CAPS)
    assert run_job(db, factory, queue.claim(db, "w", ALL_CAPS)) is None
    [job] = db.execute("SELECT status, infra_retries FROM stage_jobs").fetchall()
    assert job == {"status": "queued", "infra_retries": 1}
    assert store.load_state(db, state.run_id).status == "running"  # not parked
    assert db.execute("SELECT count(*) AS n FROM stage_results").fetchone()["n"] == 0
    assert "rejected by test" in store.events(db, state.run_id)[-1]["reason"]


def test_concurrent_merges_on_one_target_serialise_and_both_land(db, db_url, factory, target, remote, tmp_path):
    log = tmp_path / "validation.log"
    validate = f"echo start >> {log}; sleep 0.5; echo end >> {log}"
    push_trunk(target, {".sf.toml": f"validation = [{validate!r}]\n".replace("'", '"')})
    runs = [merge_job(db, factory, target, runner=f"w{i}") for i in (1, 2)]
    for i, (state, _) in enumerate(runs):
        commit_on_branch(remote, tmp_path, state.branch, {f"feature{i}.txt": str(i)})
    results, errors = {}, []

    def work(state, job):
        conn = store.connect(db_url)
        try:
            results[state.run_id] = run_job(conn, factory, job)
        except Exception as e:  # noqa: BLE001 - a thread must hand its failure to the test
            errors.append(e)
        finally:
            conn.close()

    threads = [threading.Thread(target=work, args=run) for run in runs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors
    assert all(isinstance(step, Next) and step.stage == "improve" for step in results.values())
    assert log.read_text().split() == ["start", "end", "start", "end"]  # never overlapping
    tree = sh(remote, "git", "ls-tree", "--name-only", "main").split()
    assert {"feature0.txt", "feature1.txt"} <= set(tree)
    assert sh(remote, "git", "log", "--merges", "--oneline", "main") == ""  # rebased + fast-forwarded


def test_conflicting_change_returns_conflict_and_loops_back(db, factory, target, remote, tmp_path):
    state, job = merge_job(db, factory, target)
    commit_on_branch(remote, tmp_path, state.branch, {"README.md": "branch version\n"})
    push_trunk(target, {"README.md": "trunk version\n"})
    step = run_job(db, factory, job)
    assert isinstance(step, Next) and step.stage == "implement" and step.inputs["loop"] == 2
    assert "README.md" in step.inputs["feedback"]
    assert db.execute("SELECT outcome FROM stage_results").fetchone()["outcome"] == "conflict"
    assert store.load_state(db, state.run_id).merge_conflicts == 1
    assert sh(remote, "git", "show", "main:README.md") == "trunk version\n"
    assert jobs(db, state.run_id)[-1]["stage"] == "implement"


def test_merge_revalidation_failure_returns_validation_failed(db, factory, target, remote, tmp_path):
    push_trunk(target, {".sf.toml": 'validation = ["test ! -f bad"]\n'})
    state, job = merge_job(db, factory, target)
    commit_on_branch(remote, tmp_path, state.branch, {"bad": "x"})
    trunk_before = sh(remote, "git", "rev-parse", "main")
    step = run_job(db, factory, job)
    assert isinstance(step, Next) and step.stage == "implement"
    assert "test ! -f bad" in step.inputs["feedback"] and "[exit 1]" in step.inputs["feedback"]
    assert db.execute("SELECT outcome FROM stage_results").fetchone()["outcome"] == "validation_failed"
    assert sh(remote, "git", "rev-parse", "main") == trunk_before  # trunk untouched
    # the rebased branch is published so the next implement pass starts from the latest trunk
    assert sh(remote, "git", "merge-base", "--is-ancestor", "main", state.branch) == ""


def test_merge_conflict_twice_aborts(db, factory, target, remote, tmp_path):
    state, job = merge_job(db, factory, target)
    state.merge_conflicts = 2
    store.save_state(db, state)
    commit_on_branch(remote, tmp_path, state.branch, {"README.md": "branch\n"})
    push_trunk(target, {"README.md": "trunk\n"})
    assert isinstance(run_job(db, factory, job), Finished)
