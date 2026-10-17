"""`sf` command line."""

import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import Annotated

import typer

from sf import git as g
from sf import mailbox
from sf.console import LiveDisplay, TerminalHuman
from sf.git import GitError
from sf.run import Run, RunLock
from sf.runner import StageFailed
from sf.target import TrunkError, load_config, resolve_trunk
from sf.workflow import Aborted, Workflow, new_run

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _factory() -> Path:
    root = Path.cwd()
    if not (root / "agents").is_dir():
        raise typer.BadParameter(f"{root} is not a factory repo (no agents/ directory)")
    return root


def _drive(run: Run) -> None:
    """Run every remaining stage with the live console; failures propagate after their notes."""
    with LiveDisplay(run) as display:
        wf = Workflow(run, TerminalHuman(display), display)
        try:
            wf.execute()
        except Aborted as e:
            display.note(f"[yellow]run {run.state.run_id} aborted: {e}[/]")
            raise
        except (StageFailed, GitError) as e:
            display.note(f"[red]run {run.state.run_id} failed in {run.state.stage}: {e}[/]")
            display.note(f"resume with: sf resume {run.state.run_id}")
            raise
    typer.echo(f"run {run.state.run_id} done")


def _execute(run: Run) -> None:
    """`_drive` with the outcomes mapped to `sf`'s exit codes."""
    try:
        _drive(run)
    except Aborted:
        raise typer.Exit(2) from None
    except (StageFailed, GitError):
        raise typer.Exit(1) from None


@app.command()
def run(
    target: Annotated[Path, typer.Option(help="Target repository", exists=True, file_okay=False)],
    prompt: Annotated[str | None, typer.Argument(help="What to build")] = None,
    prompt_file: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    approve_plan: Annotated[bool, typer.Option(help="Require human approval of the plan")] = False,
) -> None:
    """Start a new run against TARGET."""
    if (prompt is None) == (prompt_file is None):
        raise typer.BadParameter("give exactly one of PROMPT or --prompt-file")
    text = prompt if prompt is not None else prompt_file.read_text()  # type: ignore[union-attr]
    factory = _factory()
    with RunLock(factory):
        try:
            r = new_run(factory, target, text, approve_plan)
        except TrunkError as e:
            typer.echo(f"error: {e}", err=True)
            raise typer.Exit(1) from e
        typer.echo(f"run {r.state.run_id}")
        _execute(r)


@app.command()
def watch(
    target: Annotated[Path, typer.Option(help="Target repository", exists=True, file_okay=False)],
    folder: Annotated[Path, typer.Argument(help="Folder holding todo/, doing/ and done/")],
    approve_plan: Annotated[bool, typer.Option(help="Require human approval of the plan")] = False,
) -> None:
    """Run every Markdown prompt dropped into FOLDER against TARGET, until Ctrl+C."""
    factory = _factory()
    if folder.exists() and not folder.is_dir():
        raise typer.BadParameter(f"{folder} is a file, not a folder")
    target = _check_target(target)
    box = mailbox.Mailbox(folder.resolve())
    box.ensure()
    _watch_loop(factory, target, box, approve_plan)


def _check_target(target: Path) -> Path:
    """Resolve the target once, so an unusable one fails the command instead of every prompt."""
    target = target.resolve()
    try:
        g.git(target, "rev-parse", "--git-dir")
        resolve_trunk(target, load_config(target))
    except (GitError, TrunkError) as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from e
    return target


def _watch_loop(
    factory: Path,
    target: Path,
    box: mailbox.Mailbox,
    approve_plan: bool,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Watch the queue until Ctrl+C. `sleep` is injectable so tests never wait."""
    typer.echo(f"watching {box.root} (one run at a time; Ctrl+C to stop)")
    typer.echo(f"  todo  {box.todo}")
    typer.echo(f"  doing {box.doing}")
    typer.echo(f"  done  {box.done}")
    box.monitor(
        partial(_watch_prompt, factory, target, box, approve_plan=approve_plan),
        note=typer.echo,
        busy=partial(_lock_busy, factory),
        sleep=sleep,
    )
    typer.echo(f"stopped watching {box.root}")


def _lock_busy(factory: Path) -> str | None:
    """Why the watch must wait: another `sf` process holds the run lock. Prompts stay in todo/."""
    pid = RunLock(factory).holder()
    return None if pid is None else f"another sf run is active (pid {pid})"


def _watch_prompt(factory: Path, target: Path, box: mailbox.Mailbox, path: Path, approve_plan: bool) -> None:
    """One prompt file is one run, under the run lock so `sf resume` still fits between runs.

    Only success returns, and the monitor then moves the file to done/. A failure leaves the file in
    doing/ with its run resumable, so a broken prompt is reported once instead of retried forever.
    """
    with RunLock(factory):
        run = new_run(factory, target, path.read_text(), approve_plan)
        run.log("mailbox_claimed", file=path.name, watch_root=str(box.root))
        typer.echo(f"run {run.state.run_id} for {path.name}")
        try:
            _drive(run)
        except (Aborted, StageFailed, GitError) as e:
            run.log("mailbox_failed", file=path.name, outcome=str(e), resume=f"sf resume {run.state.run_id}")
            raise
        run.log("mailbox_completed", file=path.name, done=str(box.done))


@app.command()
def resume(run_id: str) -> None:
    """Continue a run from its next incomplete stage."""
    factory = _factory()
    with RunLock(factory):
        _execute(Run.load(factory, run_id))


@app.command()
def improve(run_id: str) -> None:
    """Re-enter the improve phase of a merged run."""
    factory = _factory()
    with RunLock(factory):
        r = Run.load(factory, run_id)
        if "merge" not in r.state.completed:
            raise typer.BadParameter(f"run {run_id} has not merged yet; use `sf resume {run_id}`")
        if "improve" in r.state.completed:
            r.state.completed.remove("improve")
        _execute(r)


@app.command()
def dashboard(
    host: Annotated[str, typer.Option(help="Bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8765,
) -> None:
    """Serve a web dashboard with agent harness, model and thinking configuration."""
    from sf.dashboard import serve

    server = serve(_factory() / "runs", host, port, _factory() / "agents")
    typer.echo(f"sf dashboard on http://{host}:{port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    app()  # `python -m sf.cli ...`: the interpreter that serves the dashboard can start a retro with it
