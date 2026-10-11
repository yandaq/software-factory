"""`sf` command line."""

from pathlib import Path
from typing import Annotated

import typer

from sf.console import LiveDisplay, TerminalHuman
from sf.git import GitError
from sf.pi import StageFailed
from sf.run import Run, RunLock
from sf.target import TrunkError
from sf.workflow import Aborted, Workflow, new_run

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _factory() -> Path:
    root = Path.cwd()
    if not (root / "agents").is_dir():
        raise typer.BadParameter(f"{root} is not a factory repo (no agents/ directory)")
    return root


def _execute(run: Run) -> None:
    with LiveDisplay(run) as display:
        wf = Workflow(run, TerminalHuman(display), display)
        try:
            wf.execute()
        except Aborted as e:
            display.note(f"[yellow]run {run.state.run_id} aborted: {e}[/]")
            raise typer.Exit(2)
        except (StageFailed, GitError) as e:
            display.note(f"[red]run {run.state.run_id} failed in {run.state.stage}: {e}[/]")
            display.note(f"resume with: sf resume {run.state.run_id}")
            raise typer.Exit(1)
    typer.echo(f"run {run.state.run_id} done")


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
    """Serve a web dashboard of all runs, with a page to configure agent models and thinking levels."""
    from sf.dashboard import serve

    server = serve(_factory() / "runs", host, port, _factory() / "agents")
    typer.echo(f"sf dashboard on http://{host}:{port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
