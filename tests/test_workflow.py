import json

import pytest
from conftest import calls, sh

from sf.improve import load_decisions
from sf.pi import StageFailed
from sf.run import Run
from sf.workflow import Aborted, Workflow, new_run

APPROVE = {"text": '{"verdict": "approve", "findings": []}'}
CHANGES = {
    "text": '{"verdict": "changes_requested", "findings": '
    '[{"severity": "major", "location": "app.py:1", "description": "bad name"}]}'
}
IDEA = {"title": "Tighten reviewer", "file": "agents/reviewer.md", "rationale": "r", "evidence": "e",
        "proposed_change": "c"}


def script(**over):
    s = {
        "planner": [{"text": '{"plan": "# Plan\\n1. write app.py"}'}],
        "implementer": [{"text": '{"summary": "s"}', "write": {"app.py": "print(1)\n"}}],
        "reviewer": [APPROVE],
        "documenter": [{"text": '{"summary": "docs"}', "write": {"docs/app.md": "# App\n"}}],
        "improver": [{"text": '{"ideas": []}'}],
    }
    s.update(over)
    return s


class Human:
    def __init__(self, plan=True, cap="merge"):
        self.plan, self.cap = plan, cap
        self.caps = 0

    def approve_plan(self, plan):
        return self.plan

    def cap_reached(self, loops):
        self.caps += 1
        return self.cap.pop(0) if isinstance(self.cap, list) else self.cap


class Display:
    def note(self, text):
        pass

    def pi_event(self, inv, event):
        pass


def start(factory, target, human=None, approve_plan=False):
    run = new_run(factory, target, "build an app", approve_plan)
    return run, Workflow(run, human or Human(), Display())


def test_runs_created_in_the_same_second_get_their_own_ids(factory, target):
    """The watch monitor can start two runs inside one second; they must not share a run directory."""
    first = new_run(factory, target, "build an app", False)
    second = new_run(factory, target, "build another app", False)
    assert first.state.run_id != second.state.run_id
    assert first.dir != second.dir
    assert first.dir.is_dir() and second.dir.is_dir()
    assert (first.dir / "prompt.md").read_text() == "build an app\n"
    assert (second.dir / "prompt.md").read_text() == "build another app\n"
    assert second.state.branch == f"sf/{second.state.run_id}"
    assert Run.load(factory, first.state.run_id).state.run_id == first.state.run_id
    assert Run.load(factory, second.state.run_id).state.run_id == second.state.run_id


def test_happy_path(factory, target, fake_pi):
    s = fake_pi(script())
    run, wf = start(factory, target)
    wf.execute()
    st = run.state
    assert st.status == "done" and st.completed == [
        "branch",
        "prompt",
        "plan",
        "implement",
        "document",
        "merge",
        "improve",
    ]
    assert (run.dir / "plan.md").read_text().startswith("# Plan")
    # plan committed on the branch before implementation
    log = sh(target, "git", "log", "--reverse", "--format=%s").splitlines()
    assert log.index(f"sf {st.run_id}: plan") < log.index(f"sf {st.run_id}: implement (loop 1)")
    assert (target / f"docs/plans/{st.run_id}.md").read_text().startswith("# Plan")
    assert "**Verdict:** approve" in (run.dir / "review-1.md").read_text()
    # target: standards installed, merged with --no-ff, docs written
    assert sh(target, "git", "branch", "--show-current").strip() == "main"
    assert (target / "app.py").exists() and (target / "docs/app.md").exists()
    assert (target / "docs/coding-standards.md").exists() and (target / "AGENTS.md").exists()
    assert len(sh(target, "git", "log", "-1", "--format=%P").split()) == 2
    # factory: run artefacts are never committed
    assert "runs/" not in sh(factory, "git", "log", "--name-only", "--format=")
    # validation skipped (template has no commands) and logged
    events = [json.loads(l) for l in (run.dir / "events.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "validation_skipped" for e in events)
    reviewer_task = next(c for c in calls(s) if c["agent"] == "reviewer")["message"]
    assert "Validation gate (loop 1): SKIPPED" in reviewer_task
    assert "no validation commands configured" in reviewer_task
    assert "Validation gate (loop 1): PASS" not in reviewer_task
    # git actions are logged for the dashboard's sf lane
    git_events = [e for e in events if e.get("type", "").startswith("git_")]
    assert {e["type"] for e in git_events} == {"git_checkout", "git_commit", "git_merge"}
    assert any(e["type"] == "git_commit" and e["repo"] == "target" for e in git_events)
    assert [c["agent"] for c in calls(s)] == ["planner", "implementer", "reviewer", "documenter", "improver"]
    assert json.loads((run.dir / "run-state.json").read_text())["tokens"]["by_stage"]["implement"]["input"] == 20


def test_validation_failure_skips_reviewer_and_feeds_back(factory, target, fake_pi):
    (target / ".sf.toml").write_text('validation = ["test -f ok"]\n')
    sh(target, "git", "add", "-A")
    sh(target, "git", "commit", "-qm", "cfg")
    s = fake_pi(
        script(
            implementer=[
                {"text": '{"summary": "s"}', "write": {"app.py": "x"}},
                {"text": '{"summary": "s"}', "write": {"ok": "1"}},
            ]
        )
    )
    run, wf = start(factory, target)
    wf.execute()
    agents = [c["agent"] for c in calls(s)]
    assert agents[:4] == ["planner", "implementer", "implementer", "reviewer"]
    second_impl = calls(s)[2]["message"]
    assert "Latest validation output (failed)" in second_impl and "test -f ok" in second_impl
    assert run.state.results["validation-1"] == "failed" and run.state.results["implement"] == "approved in loop 2"
    assert "[exit 1]" in second_impl
    assert not (run.dir / "review-1.md").exists()
    reviewer_task = next(c for c in calls(s) if c["agent"] == "reviewer")["message"]
    assert "Validation gate (loop 2): PASS" in reviewer_task
    assert "[exit 0]" in reviewer_task
    assert "[exit 1]" not in reviewer_task


def test_successful_validation_evidence_reaches_reviewer(factory, target, fake_pi):
    commands = ["printf 'gate {{literal}} passed\\n'", "printf 'second check passed\\n'"]
    (target / ".sf.toml").write_text("validation = " + json.dumps(commands) + "\n")
    sh(target, "git", "add", "-A")
    sh(target, "git", "commit", "-qm", "cfg")
    s = fake_pi(script())
    run, wf = start(factory, target)
    wf.execute()
    message = next(c for c in calls(s) if c["agent"] == "reviewer")["message"]
    assert "Validation gate (loop 1): PASS" in message
    for command in commands:
        assert command in message
    assert (run.dir / "validation-1.log").read_text() in message
    assert message.count("[exit 0]") == 2
    assert "gate {{literal}} passed" in message


def test_reviewer_validation_diagnostics_are_bounded(factory, target, fake_pi):
    command = "printf START; printf '%20000s' x; printf END"
    s = fake_pi(script())
    run, wf = start(factory, target)
    (run.dir / "plan.md").write_text("Plan")
    wf._loop(1, [command])
    message = next(c for c in calls(s) if c["agent"] == "reviewer")["message"]
    assert command in message
    assert "diagnostic output truncated; last 12000 characters" in message
    assert "END[exit 0]" in message
    assert len(message) < 16000


def test_review_findings_fed_to_next_loop(factory, target, fake_pi):
    s = fake_pi(script(reviewer=[CHANGES, APPROVE]))
    start(factory, target)[1].execute()
    impl2 = [c for c in calls(s) if c["agent"] == "implementer"][1]["message"]
    assert "bad name" in impl2 and "app.py:1" in impl2


def test_cap_reached_merge_anyway_is_flagged(factory, target, fake_pi):
    s = fake_pi(script(reviewer=[CHANGES]))
    human = Human(cap=[2, "merge"])
    run, wf = start(factory, target, human)
    wf.execute()
    assert human.caps == 2 and run.state.loop == 7 and run.state.cap_reached
    improver_task = next(c for c in calls(s) if c["agent"] == "improver")["message"]
    assert "cap reached without approval: yes" in improver_task


def test_cap_abort(factory, target, fake_pi):
    fake_pi(script(reviewer=[CHANGES]))
    run, wf = start(factory, target, Human(cap="abort"))
    with pytest.raises(Aborted):
        wf.execute()
    assert run.state.status == "aborted" and "merge" not in run.state.completed


def test_plan_rejected_aborts(factory, target, fake_pi):
    s = fake_pi(script())
    _run, wf = start(factory, target, Human(plan=False), approve_plan=True)
    with pytest.raises(Aborted):
        wf.execute()
    assert [c["agent"] for c in calls(s)] == ["planner"]
    assert not (target / "docs/plans").exists()


def test_documenter_scope_enforced(factory, target, fake_pi):
    fake_pi(
        script(
            documenter=[{"text": '{"summary": "d"}', "write": {"app.py": "hacked", "docs/coding-standards.md": "x"}}]
        )
    )
    run, wf = start(factory, target)
    with pytest.raises(StageFailed, match="outside its scope"):
        wf.execute()
    assert (target / "app.py").read_text() == "print(1)\n"
    assert run.state.status == "failed"
    events = [json.loads(line) for line in (run.dir / "events.jsonl").read_text().splitlines()]
    assert any(e.get("type") == "git_revert" and e["repo"] == "target" for e in events)


def test_merge_conflict_aborts(factory, target, fake_pi):
    fake_pi(script())
    _run, wf = start(factory, target)
    real_document = wf.stage_document

    def document_then_conflict():
        result = real_document()
        sh(target, "git", "checkout", "-q", "main")
        (target / "app.py").write_text("conflict\n")
        sh(target, "git", "add", "-A")
        sh(target, "git", "commit", "-qm", "c")
        return result

    wf.stage_document = document_then_conflict
    with pytest.raises(Aborted, match="merge conflict"):
        wf.execute()
    assert not sh(target, "git", "status", "--porcelain")


def test_resume_continues_from_failed_stage(factory, target, fake_pi):
    s = fake_pi(script(documenter=[{"exit": 1}]))
    run, wf = start(factory, target)
    with pytest.raises(StageFailed):
        wf.execute()
    assert run.state.stage == "document" and run.state.status == "failed"
    fake_pi(script())
    s.with_suffix(".calls.jsonl").unlink()
    loaded = Run.load(factory, run.state.run_id)
    Workflow(loaded, Human(), Display()).execute()
    assert loaded.state.status == "done"
    assert [c["agent"] for c in calls(s)] == ["documenter", "improver"]


def test_improve_records_suggestions_without_applying_them(factory, target, fake_pi):
    ideas = [IDEA, {**IDEA, "title": "Old bad idea"}, {**IDEA, "title": "Another improvement"}]
    (factory / "improvements.jsonl").write_text(json.dumps({"title": "old  BAD idea", "decision": "rejected"}) + "\n")
    agents_file = factory / "agents" / "reviewer.md"
    prompt_before = agents_file.read_bytes()
    s = fake_pi(script(improver=[{"text": json.dumps({"ideas": ideas})}]))
    run, wf = start(factory, target, Human())
    wf.execute()
    decisions = load_decisions(factory)
    assert [(d["title"], d["decision"]) for d in decisions[1:]] == [
        ("Tighten reviewer", "automatic"),
        ("Another improvement", "automatic"),
    ]
    assert decisions[1]["note"] == "" and decisions[1]["run_id"] == run.state.run_id
    assert decisions[1]["file"] == "agents/reviewer.md"
    assert run.state.results["improve"] == "2 improvements recorded"
    assert [c["agent"] for c in calls(s)] == ["planner", "implementer", "reviewer", "documenter", "improver"]
    assert '"reason": "previously rejected"' in (run.dir / "events.jsonl").read_text()
    # Recording is the whole stage: nothing is edited or applied in either repository.
    assert agents_file.read_bytes() == prompt_before
    assert f"sf {run.state.run_id}: improvement decisions" in sh(factory, "git", "log", "--format=%s")
    assert "improvement —" not in sh(factory, "git", "log", "--format=%s")
    assert not sh(factory, "git", "status", "--porcelain")
    assert sh(target, "git", "log", "-1", "main", "--format=%s").strip() == f"sf {run.state.run_id}: merge sf/{run.state.run_id}"


@pytest.mark.parametrize("file", [None, "/factory/agents/reviewer.md", "../outside.md", "agents/reviewer.txt",
                                  "app.py", "tests/test_runner.py", "src/other/runner.py",
                                  "src/sf/../outside.py"])
def test_recorded_idea_must_name_one_relative_document_or_runner_file(factory, target, fake_pi, file):
    """The dashboard applies an idea to the file it names, so the contract keeps that path usable."""
    idea = {k: v for k, v in IDEA.items() if k != "file"}
    if file is not None:
        idea["file"] = file
    fake_pi(script(improver=[{"text": json.dumps({"ideas": [idea]})}]))
    run, wf = start(factory, target)
    with pytest.raises(StageFailed):
        wf._stage("improve")
    assert run.state.status == "failed"
    assert '"type": "contract_invalid"' in (run.dir / "events.jsonl").read_text()
    assert load_decisions(factory) == []


def test_recorded_improvements_need_no_human_interface(factory, target, fake_pi):
    previous = {"title": "Previously rejected", "decision": "rejected", "note": "keep out"}
    (factory / "improvements.jsonl").write_text(json.dumps(previous) + "\n")
    ideas = [IDEA, {**IDEA, "title": " previously REJECTED "}, {**IDEA, "title": "Second improvement"}]
    s = fake_pi(script(improver=[{"text": json.dumps({"ideas": ideas})}]))
    run, wf = start(factory, target)
    wf.human = object()  # Improve must not require any human callbacks.
    wf._stage("improve")
    assert run.state.results["improve"] == "2 improvements recorded"
    history = load_decisions(factory)
    assert history[0] == previous
    assert [(d["title"], d["decision"]) for d in history[1:]] == [
        ("Tighten reviewer", "automatic"), ("Second improvement", "automatic"),
    ]
    assert [c["agent"] for c in calls(s)] == ["improver"]
    assert '"reason": "previously rejected"' in (run.dir / "events.jsonl").read_text()


def test_master_trunk_used_by_default(factory, target, fake_pi):
    sh(target, "git", "branch", "-m", "main", "master")
    fake_pi(script())
    run, wf = start(factory, target)
    assert run.state.trunk == "master"
    wf.execute()
    assert sh(target, "git", "branch", "--show-current").strip() == "master"
    assert (target / "app.py").exists()


def test_resume_re_resolves_trunk_after_fix(factory, target, fake_pi):
    fake_pi(script())
    run, _ = start(factory, target)
    run.state.trunk = "gone"  # e.g. a run created before main/master detection
    run.save()
    sh(target, "git", "branch", "-m", "main", "master")
    loaded = Run.load(factory, run.state.run_id)
    Workflow(loaded, Human(), Display()).execute()
    assert loaded.state.trunk == "master" and loaded.state.status == "done"


def test_standards_download_failure_fails_branch_stage(factory, target, fake_pi, monkeypatch):
    from sf.target import StandardsError

    def offline(url):
        raise StandardsError(f"could not download {url}: offline")

    monkeypatch.setattr("sf.target.download", offline)
    fake_pi(script())
    run, wf = start(factory, target)
    with pytest.raises(StageFailed, match="could not download .*AGENTS.md"):
        wf.execute()
    assert run.state.stage == "branch" and run.state.status == "failed"


def test_task_template_edits_apply_to_later_invocations(factory, target, fake_pi):
    """Prompt files are re-read per invocation, so a dashboard edit takes effect immediately."""
    path = factory / "agents" / "planner.user.md"
    s = fake_pi(script())
    _run, wf = start(factory, target, Human())
    wf._agent("planner", {"request": "{{literal}}"})
    path.write_text(path.read_text() + "\nTuned instruction.\n")
    wf._agent("planner", {"request": "{{literal}}"})
    message = calls(s)[-1]["message"]
    assert "Tuned instruction." in message and "{{literal}}" in message


def test_missing_task_template_fails_visibly(factory, target, fake_pi):
    s = fake_pi(script())
    (factory / "agents" / "planner.user.md").unlink()
    run, wf = start(factory, target)
    with pytest.raises(StageFailed, match="planner.user.md"):
        wf.execute()
    assert not calls(s)
    assert run.state.status == "failed"
    assert '"type": "prompt_invalid"' in (run.dir / "events.jsonl").read_text()


def test_validation_feedback_precedes_review_feedback(factory, target, fake_pi):
    s = fake_pi(script())
    run, wf = start(factory, target)
    (run.dir / "plan.md").write_text("Plan context")
    (run.dir / "review-1.md").write_text("Review context")
    (run.dir / "validation-1.log").write_text("Validation {{literal}} context")
    run.state.results["validation-1"] = "failed"
    wf._loop(2, [])
    message = calls(s)[0]["message"]
    assert "Plan context" in message and "Validation {{literal}} context" in message
    assert "Review context" not in message


def test_documenter_updates_guidance_to_match_code(factory, target, fake_pi):
    from sf.target import STANDARDS_FILES

    updates = {path: f"# {path}\nAccurate description of implemented behaviour.\n" for path in STANDARDS_FILES}
    updates["README.md"] = "# Updated usage\n"
    updates["docs/feature.md"] = "# New feature documentation\n"
    fake_pi(script(documenter=[{"text": '{"summary":"drift corrected"}', "write": updates}]))
    run, wf = start(factory, target)
    wf.execute()
    assert run.state.status == "done"
    for path, content in updates.items():
        assert (target / path).read_text() == content
    assert f"sf {run.state.run_id}: document" in sh(target, "git", "log", "--format=%s")


@pytest.mark.parametrize("path", ["app.py", ".sf.toml", "new-root.md"])
def test_documenter_rejects_code_config_and_new_root_docs(factory, target, fake_pi, path):
    fake_pi(script(documenter=[{"text": '{"summary":"s"}', "write": {path: "forbidden"}}]))
    _run, wf = start(factory, target)
    with pytest.raises(StageFailed, match="outside its scope"):
        wf.execute()
    if path == "app.py":
        assert (target / path).read_text() == "print(1)\n"
    else:
        assert not (target / path).exists()


def test_build_artifacts_left_by_the_documenter_are_not_out_of_scope(factory, target, fake_pi):
    """Running the target's tests drops `__pycache__` in it; the installed .gitignore keeps that harmless."""
    fake_pi(
        script(
            documenter=[
                {"text": '{"summary": "s"}', "write": {"__pycache__/test_app.cpython-314.pyc": "junk"}}
            ]
        )
    )
    run, wf = start(factory, target)
    wf.execute()
    assert run.state.status == "done"
    assert not [p for p in sh(target, "git", "ls-files").splitlines() if "__pycache__" in p]


def test_all_agents_receive_target_documentation_inventory(factory, target, fake_pi):
    (target / "DESIGN.md").write_text("# Design\n")
    sh(target, "git", "add", "DESIGN.md")
    sh(target, "git", "commit", "-qm", "design")
    s = fake_pi(script(improver=[{"text": json.dumps({"ideas": [IDEA]})}]))
    _run, wf = start(factory, target, Human())
    wf.execute()
    messages = {c["agent"]: c["message"] for c in calls(s)}
    assert len(messages) == 5
    for message in messages.values():
        assert f"Target repository: {json.dumps(str(target))}" in message
        assert '"DESIGN.md"' in message
        assert '"agents/planner.md"' not in message
    assert '"docs/app.md"' in messages["improver"]


def test_documentation_inventory_refresh_and_literal_paths(factory, target, fake_pi):
    s = fake_pi(script())
    _run, wf = start(factory, target)
    wf._agent("planner", {"request": "Task"})
    name = 'space "quoted" {{request}}\nDESIGN.MD'
    (target / name).write_text("# Design\n")
    wf._agent("planner", {"request": "Task"})
    messages = [c["message"] for c in calls(s)]
    assert json.dumps(name, ensure_ascii=False) not in messages[0]
    assert json.dumps(name, ensure_ascii=False) in messages[1]


def test_documentation_discovery_failure_observable(factory, target, fake_pi, monkeypatch):
    from sf.git import GitError

    def fail(_target):
        raise GitError("inventory unavailable")

    s = fake_pi(script())
    run, wf = start(factory, target)
    monkeypatch.setattr("sf.workflow.discover_documentation", fail)
    with pytest.raises(StageFailed, match="documentation discovery.*inventory unavailable"):
        wf.execute()
    assert not calls(s)
    assert run.state.status == "failed"
    assert '"type": "documentation_discovery_failed"' in (run.dir / "events.jsonl").read_text()


def test_documentation_inventory_pinned_for_contract_retry(factory, target, fake_pi):
    s = fake_pi(script(planner=[{"text": "no json"}, {"text": '{"plan":"ok"}'}]))
    _run, wf = start(factory, target)
    wf._agent("planner", {"request": "Task"})
    messages = [c["message"] for c in calls(s)]
    assert "Repository-relative Markdown paths" in messages[0]
    assert "Repository-relative Markdown paths" not in messages[1]
    assert "corrected JSON object" in messages[1]




@pytest.mark.parametrize("name", ["src/sf/workflow.py", "src/sf/nested/runner.py"])
def test_improver_can_record_runner_changes_without_applying(factory, target, fake_pi, name):
    source = factory / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("# Existing runner\n")
    idea = IDEA | {"file": name, "title": "Fix runner validation"}
    invocation = fake_pi(script(improver=[{"text": json.dumps({"ideas": [idea]})}]))
    run, wf = start(factory, target)
    wf.execute()
    assert load_decisions(factory)[0]["file"] == name
    assert source.read_text() == "# Existing runner\n"
    assert run.state.results["improve"] == "1 improvements recorded"
    improver_call = next(c for c in calls(invocation) if c["agent"] == "improver")
    assert improver_call["cwd"] == str(factory)
    assert "src/sf/" in improver_call["message"]
