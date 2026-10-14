"""The watch-folder queue: layout, ordering, settle guard, claim/complete and the monitor loop."""

import json
import os
import time
from pathlib import Path

import pytest
from conftest import calls, sh
from test_workflow import script
from typer.testing import CliRunner

from sf import cli, mailbox
from sf.run import RunLock


@pytest.fixture
def box(tmp_path: Path) -> mailbox.Mailbox:
    box = mailbox.Mailbox(tmp_path / "watch")
    box.ensure()
    return box


def drop(box: mailbox.Mailbox, name: str, content: str | None = None, age: float = 60.0) -> Path:
    """Drop a prompt into `todo/`, already settled: its modification time is `age` seconds ago."""
    path = box.todo / name
    path.write_text(content if content is not None else f"prompt in {name}\n")
    when = time.time() - age
    os.utime(path, (when, when))
    return path


def take(box: mailbox.Mailbox) -> Path:
    """Claim the next file, asserting the test's expectation that something was claimable."""
    claimed = box.claim()
    assert claimed is not None, "expected a claimable prompt"
    return claimed


class Polls:
    """Stand-in for `time.sleep`: records poll intervals, then ends the monitor when `stop` holds.

    The limit means a monitor that never reaches the expected state fails the test instead of
    hanging it.
    """

    def __init__(self, stop=None, limit: int = 50):
        self.stop, self.limit = stop, limit
        self.seconds: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        if len(self.seconds) >= self.limit or (self.stop is not None and self.stop()):
            raise KeyboardInterrupt


def sf_events(runs_dir: Path) -> list[dict]:
    """Every orchestrator (`source: "sf"`) event across all runs."""
    events = [json.loads(line) for path in sorted(runs_dir.glob("*/events.jsonl"))
              for line in path.read_text().splitlines()]
    return [e for e in events if e.get("source") == "sf"]


def watch_box(tmp_path: Path) -> mailbox.Mailbox:
    """A mailbox whose settle window is closed, so prompts dropped by the test are claimable."""
    box = mailbox.Mailbox(tmp_path / "watch", settle_seconds=0)
    box.ensure()
    return box


def test_layout_is_created_inside_the_given_folder(tmp_path: Path):
    box = mailbox.Mailbox(tmp_path / "deep" / "watch")
    box.ensure()
    assert [box.todo, box.doing, box.done] == [tmp_path / "deep" / "watch" / n for n in ("todo", "doing", "done")]
    assert all(path.is_dir() for path in (box.todo, box.doing, box.done))


def test_layout_leaves_existing_folders_and_files_alone(tmp_path: Path):
    box = mailbox.Mailbox(tmp_path / "watch")
    box.todo.mkdir(parents=True)
    queued = box.todo / "queued.md"
    queued.write_text("queued earlier\n")
    (box.todo / "not-a-prompt").mkdir()
    box.ensure()
    assert queued.read_text() == "queued earlier\n"
    assert (box.todo / "not-a-prompt").is_dir()
    assert box.doing.is_dir() and box.done.is_dir()


def test_candidates_are_ordered_oldest_modification_first(box: mailbox.Mailbox):
    middle = drop(box, "a.md", age=60)
    oldest = drop(box, "c.md", age=300)
    newest = drop(box, "b.md", age=10)
    assert box.candidates() == [oldest, middle, newest]


def test_equal_modification_times_are_ordered_by_name(box: mailbox.Mailbox):
    when = time.time() - 120
    for name in ("zeta.md", "alpha.md", "mid.md"):
        (box.todo / name).write_text(f"{name}\n")
        os.utime(box.todo / name, (when, when))
    assert [p.name for p in box.candidates()] == ["alpha.md", "mid.md", "zeta.md"]


def test_only_markdown_files_directly_in_todo_are_candidates(box: mailbox.Mailbox):
    markdown = drop(box, "lower.md", age=300)
    capital = drop(box, "UPPER.MD", age=100)
    drop(box, "notes.txt")
    drop(box, "no-suffix")
    (box.todo / "nested").mkdir()
    (box.todo / "nested" / "inner.md").write_text("not a prompt\n")
    (box.todo / "folder.md").mkdir()  # a directory is not a prompt, whatever it is called
    assert box.candidates() == [markdown, capital]
    assert box.claimable() == [markdown, capital]


def test_a_file_still_being_written_is_not_claimable(box: mailbox.Mailbox):
    settled = drop(box, "settled.md", age=30)
    drop(box, "fresh.md", age=0)  # written now, so it is still inside the settle window
    assert box.claimable() == [settled]
    assert box.claim() == box.doing / "settled.md"
    assert box.claim() is None  # fresh.md has to finish being written first
    when = time.time() - 600
    os.utime(box.todo / "fresh.md", (when, when))
    assert box.claim() == box.doing / "fresh.md"


def test_claim_moves_the_oldest_file_into_doing(box: mailbox.Mailbox):
    drop(box, "one.md", "build one\n")
    claimed = take(box)
    assert claimed == box.doing / "one.md"
    assert claimed.read_text() == "build one\n"
    assert list(box.todo.iterdir()) == []
    assert box.claim() is None


def test_complete_lands_the_file_in_done_unchanged(box: mailbox.Mailbox):
    drop(box, "one.md", "build one\n")
    landed = box.complete(take(box))
    assert landed == box.done / "one.md"
    assert landed.read_bytes() == b"build one\n"
    assert list(box.doing.iterdir()) == []


def test_repeated_prompt_names_do_not_collide_in_done(box: mailbox.Mailbox):
    drop(box, "fix.md", "first\n")
    first = box.complete(take(box))
    drop(box, "fix.md", "second\n")
    second = box.complete(take(box))
    assert (first, second) == (box.done / "fix.md", box.done / "fix-1.md")
    assert (box.done / "fix.md").read_text() == "first\n"
    assert (box.done / "fix-1.md").read_text() == "second\n"


def test_a_retried_prompt_does_not_collide_in_doing(box: mailbox.Mailbox):
    """A failed prompt stays in doing/; dropping the same name again queues beside it."""
    drop(box, "fix.md", "first\n")
    kept = take(box)  # stays in doing/, exactly as a failed run's file does
    drop(box, "fix.md", "second\n")
    assert kept == box.doing / "fix.md"
    assert box.claim() == box.doing / "fix-1.md"
    assert (box.doing / "fix.md").read_text() == "first\n"
    assert (box.doing / "fix-1.md").read_text() == "second\n"


def test_monitor_runs_each_prompt_once_oldest_first(box: mailbox.Mailbox):
    drop(box, "one.md", "first prompt\n", age=300)
    drop(box, "two.md", "second prompt\n", age=100)
    runs: list[tuple[str, str]] = []
    notes: list[str] = []

    def execute(path: Path) -> None:
        runs.append((path.name, path.read_text()))
        assert path.parent == box.doing  # it left todo/ before being executed
        assert set(box.doing.iterdir()) == {path}  # one run at a time: nothing else is in flight
        assert not (box.todo / path.name).exists()

    box.monitor(execute, note=notes.append, sleep=Polls(lambda: len(runs) == 2), poll_seconds=0.01)
    assert [name for name, _ in runs] == ["one.md", "two.md"]
    assert [prompt for _, prompt in runs] == ["first prompt\n", "second prompt\n"]
    assert [p.name for p in box.done.iterdir()] == ["one.md", "two.md"]
    assert list(box.doing.iterdir()) == []


def test_monitor_keeps_a_failed_prompt_in_doing_and_continues(box: mailbox.Mailbox):
    drop(box, "broken.md", "cannot build\n", age=300)
    drop(box, "fine.md", "buildable\n", age=100)
    executed: list[str] = []
    notes: list[str] = []

    def execute(path: Path) -> None:
        executed.append(path.name)
        if path.name == "broken.md":
            raise RuntimeError("stage implement failed")

    box.monitor(execute, note=notes.append, sleep=Polls(lambda: len(executed) == 2), poll_seconds=0.01)
    assert executed == ["broken.md", "fine.md"]
    assert [p.name for p in box.doing.iterdir()] == ["broken.md"]
    assert [p.name for p in box.done.iterdir()] == ["fine.md"]
    assert (box.doing / "broken.md").read_text() == "cannot build\n"
    assert any("broken.md" in text and "stage implement failed" in text for text in notes)
    assert any(str(box.doing) in text for text in notes)


def test_monitor_waits_for_a_busy_run_lock_without_claiming(box: mailbox.Mailbox):
    drop(box, "queued.md", "build\n")
    executed: list[str] = []
    notes: list[str] = []
    waits = ["another sf run is active (pid 4242)", "another sf run is active (pid 4242)"]
    todo_at_poll: list[list[str]] = []

    def busy() -> str | None:
        return waits.pop(0) if waits else None

    def sleep(_seconds: float) -> None:
        todo_at_poll.append(sorted(p.name for p in box.todo.iterdir()))
        if len(todo_at_poll) > 20:
            raise AssertionError("the monitor never claimed the queued prompt")
        if (box.done / "queued.md").exists():
            raise KeyboardInterrupt

    box.monitor(lambda path: executed.append(path.name), note=notes.append, busy=busy, sleep=sleep,
                poll_seconds=0.01)
    assert executed == ["queued.md"]
    assert todo_at_poll[:2] == [["queued.md"], ["queued.md"]]  # nothing was claimed while busy
    assert [text for text in notes if "pid 4242" in text] == notes[:2]
    assert (box.done / "queued.md").read_text() == "build\n"


def test_monitor_ends_cleanly_on_keyboard_interrupt(box: mailbox.Mailbox):
    drop(box, "one.md", "build one\n")
    executed: list[str] = []

    def sleep(_seconds: float) -> None:
        raise KeyboardInterrupt

    box.monitor(lambda path: executed.append(path.name), sleep=sleep)
    assert executed == ["one.md"]
    assert [p.name for p in box.done.iterdir()] == ["one.md"]


def test_monitor_ends_cleanly_when_a_run_is_interrupted(box: mailbox.Mailbox):
    """Ctrl+C during a run: the watch stops, that prompt stays in doing/ for `sf resume`."""
    drop(box, "one.md", "build one\n")

    def execute(_path: Path) -> None:
        raise KeyboardInterrupt

    box.monitor(execute, sleep=lambda _seconds: pytest.fail("the watch stopped, so it must not poll again"))
    assert [p.name for p in box.doing.iterdir()] == ["one.md"]
    assert list(box.done.iterdir()) == []


def test_monitor_polls_an_empty_queue_at_the_given_interval(box: mailbox.Mailbox):
    polls = Polls(limit=3)
    box.monitor(lambda path: pytest.fail(f"nothing should run: {path}"), sleep=polls, poll_seconds=0.25)
    assert polls.seconds == [0.25, 0.25, 0.25]


def test_watch_holds_the_run_lock_during_each_run_and_releases_it_between(
    factory: Path, target: Path, tmp_path: Path, fake_pi, monkeypatch
):
    fake_pi(script())
    box = watch_box(tmp_path)
    drop(box, "one.md", "build one\n", age=300)
    drop(box, "two.md", "build two\n", age=100)
    real_drive = cli._drive
    held: list[bool] = []

    def probe(run):
        held.append(RunLock(factory).holder() == os.getpid())
        return real_drive(run)

    monkeypatch.setattr(cli, "_drive", probe)
    cli._watch_loop(factory, target, box, approve_plan=False,
                    sleep=Polls(lambda: len(list(box.done.iterdir())) == 2))
    assert held == [True, True]  # the lock is held while each run executes
    assert RunLock(factory).holder() is None  # and free in between, so `sf resume` still fits


def test_watch_waits_for_a_run_lock_held_by_another_process(factory: Path, target: Path, tmp_path: Path, fake_pi):
    fake_pi(script())
    box = watch_box(tmp_path)
    drop(box, "one.md", "build one\n")
    (factory / "runs").mkdir(exist_ok=True)
    lock = factory / "runs" / ".lock"
    lock.write_text(str(os.getpid()))  # stands in for a live `sf run` elsewhere
    polls: list[tuple[bool, list[str]]] = []

    def sleep(_seconds: float) -> None:
        held = RunLock(factory).holder() is not None
        polls.append((held, sorted(p.name for p in box.todo.iterdir())))
        if len(polls) > 20:
            raise AssertionError(f"the watch never ran the queued prompt: {polls}")
        if held:
            lock.unlink()  # the other run finishes
        if (box.done / "one.md").exists():
            raise KeyboardInterrupt

    cli._watch_loop(factory, target, box, approve_plan=False, sleep=sleep)
    assert polls[0] == (True, ["one.md"])  # nothing was claimed while the lock was held
    assert (box.done / "one.md").exists()  # and it ran once the lock was free


def test_watch_command_creates_the_queue_and_takes_no_prompt(factory: Path, target: Path, tmp_path: Path,
                                                            monkeypatch):
    """`sf watch --target <repo> <folder>`: no prompt argument, queue folders created."""
    monkeypatch.chdir(factory)  # the factory is the current directory
    started: list[tuple] = []
    monkeypatch.setattr(cli, "_watch_loop", lambda *args, **kwargs: started.append(args))
    folder = tmp_path / "prompts"

    result = CliRunner().invoke(cli.app, ["watch", "--target", str(target), str(folder)])

    assert result.exit_code == 0, result.output
    assert all((folder / name).is_dir() for name in ("todo", "doing", "done"))
    _factory_dir, target_arg, box, approve_plan = started[0]
    assert target_arg == target.resolve() and box.root == folder.resolve() and approve_plan is False

    result = CliRunner().invoke(cli.app, ["watch", "--target", str(target), str(folder), "--approve-plan"])
    assert result.exit_code == 0, result.output
    assert started[-1][3] is True


def test_watch_command_rejects_a_file_as_the_folder(factory: Path, target: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(factory)
    not_a_folder = tmp_path / "not-a-folder"
    not_a_folder.write_text("prompt\n")
    result = CliRunner().invoke(cli.app, ["watch", "--target", str(target), str(not_a_folder)])
    assert result.exit_code != 0
    assert "is a file, not a folder" in " ".join(result.output.replace("│", " ").split())


def test_watch_command_fails_fast_on_an_unusable_target(factory: Path, tmp_path: Path, monkeypatch):
    monkeypatch.chdir(factory)
    started: list[tuple] = []
    monkeypatch.setattr(cli, "_watch_loop", lambda *args, **kwargs: started.append(args))
    plain = tmp_path / "plain-directory"
    plain.mkdir()

    result = CliRunner().invoke(cli.app, ["watch", "--target", str(plain), str(tmp_path / "prompts")])

    assert result.exit_code == 1
    assert plain.name in " ".join(result.output.replace("│", " ").split())
    assert started == []  # the target is checked before any prompt is watched
    assert not (tmp_path / "prompts").exists()


def test_watch_end_to_end(factory: Path, target: Path, tmp_path: Path, fake_pi, capsys):
    script_path = fake_pi(script())
    box = watch_box(tmp_path)
    drop(box, "first.md", "add a health endpoint\n", age=300)
    drop(box, "second.md", "add a logout button\n", age=100)

    cli._watch_loop(factory, target, box, approve_plan=False,
                    sleep=Polls(lambda: len(list(box.done.iterdir())) == 2))

    out = capsys.readouterr().out
    assert str(box.todo) in out and str(box.doing) in out and str(box.done) in out
    assert [p.name for p in box.done.iterdir()] == ["first.md", "second.md"]
    assert (box.done / "first.md").read_text() == "add a health endpoint\n"
    assert list(box.todo.iterdir()) == [] and list(box.doing.iterdir()) == []

    events = sf_events(factory / "runs")
    assert not [e for e in events if e["type"] == "mailbox_failed"]
    assert {e["file"] for e in events if e["type"] == "mailbox_claimed"} == {"first.md", "second.md"}
    assert {e["watch_root"] for e in events if e["type"] == "mailbox_claimed"} == {str(box.root)}
    assert {e["file"] for e in events if e["type"] == "mailbox_completed"} == {"first.md", "second.md"}
    for name, prompt in (("first.md", "add a health endpoint\n"), ("second.md", "add a logout button\n")):
        run_ids = {e["run_id"] for e in events if e["type"] == "mailbox_claimed" and e["file"] == name}
        assert len(run_ids) == 1, name
        assert (factory / "runs" / run_ids.pop() / "prompt.md").read_text() == prompt

    branches = [json.loads(path.read_text())["branch"] for path in (factory / "runs").glob("*/run-state.json")]
    assert len(branches) == 2 and len(set(branches)) == 2
    log = sh(target, "git", "log", "--format=%s")
    assert all(f"merge {branch}" in log for branch in branches)
    assert sh(target, "git", "branch", "--show-current").strip() == "main"
    assert (target / "app.py").exists()
    assert [call["agent"] for call in calls(script_path)] == [
        "planner", "implementer", "reviewer", "documenter", "improver",
        "planner", "implementer", "reviewer", "documenter", "improver",
    ]


def test_watch_reports_a_failed_run_and_keeps_watching(factory: Path, target: Path, tmp_path: Path, fake_pi, capsys):
    fake_pi(script(documenter=[{"exit": 1}, {"text": '{"summary": "docs"}', "write": {"docs/app.md": "# App\n"}}]))
    box = watch_box(tmp_path)
    drop(box, "broken.md", "cannot build\n", age=300)
    drop(box, "fine.md", "buildable\n", age=100)

    cli._watch_loop(factory, target, box, approve_plan=False,
                    sleep=Polls(lambda: len(list(box.done.iterdir())) == 1 and len(list(box.doing.iterdir())) == 1))

    assert [p.name for p in box.doing.iterdir()] == ["broken.md"]
    assert (box.doing / "broken.md").read_text() == "cannot build\n"
    assert [p.name for p in box.done.iterdir()] == ["fine.md"]

    out = capsys.readouterr().out
    assert "resume with: sf resume" in out
    assert "broken.md" in out and "left in" in out

    failed = [e for e in sf_events(factory / "runs") if e["type"] == "mailbox_failed"]
    assert [e["file"] for e in failed] == ["broken.md"]
    failure = failed[0]
    assert failure["resume"] == f"sf resume {failure['run_id']}"
    assert failure["outcome"]
    assert not (box.done / "broken.md").exists()
    assert (target / "docs/app.md").exists()  # the next prompt still got its run
