"""Rich live console and terminal human-in-the-loop prompts."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal, Self

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import IntPrompt, Prompt
from rich.table import Table

from sf.run import Invocation, Run, Usage


class LiveDisplay:
    """Streams agent activity and shows live token totals per stage/loop/run."""

    def __init__(self, run: Run, console: Console | None = None):
        self.run = run
        self.console = console or Console()
        self.activity = ""
        self.live = Live(self._render(), console=self.console, refresh_per_second=4, transient=True)

    def __enter__(self) -> Self:
        self.live.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.live.stop()
        self.console.print(self._tokens_table())

    @contextmanager
    def paused(self) -> Iterator[None]:
        self.live.stop()
        try:
            yield
        finally:
            self.live.start()

    def note(self, text: str) -> None:
        self.console.print(f"[bold cyan]sf[/] {text}")

    def agent_event(self, inv: Invocation, event: dict[str, Any]) -> None:
        kind = event.get("type")
        tag = f"[dim]{inv.stage}#{inv.loop} {inv.agent}[/]"
        if kind == "tool_execution_start":
            args = str(event.get("args", ""))[:100]
            self.console.print(f"{tag} → {event.get('toolName')} {args}", highlight=False, markup=True)
        elif kind == "message_update":
            u = event.get("usage") or {}
            suffix = f" ({u['output']} out tokens this message)" if "output" in u else ""
            self.activity = f"{inv.agent}: streaming{suffix}"
        elif kind == "auto_retry_start":
            self.console.print(f"{tag} [yellow]retry {event.get('attempt')}: {event.get('errorMessage')}[/]")
        self.live.update(self._render())

    def pi_event(self, inv: Invocation, event: dict[str, Any]) -> None:
        """Compatibility callback for existing Pi display clients."""
        self.agent_event(inv, event)

    def _tokens_table(self) -> Table:
        totals = self.run.state.totals()
        table = Table(title="Tokens", expand=False)
        for col in ("scope", "input", "output", "cache read", "cache write", "cost $"):
            table.add_column(col, justify="right" if col != "scope" else "left")
        rows = [(f"stage {k}", v) for k, v in totals["by_stage"].items()]
        rows += [(f"loop {k}", v) for k, v in totals["by_loop"].items()]
        rows.append(("run", totals["run"]))
        for name, u in rows:
            u = Usage(**u)
            values = []
            for key in ("input", "output", "cache_read", "cache_write", "cost"):
                value = f"{u.cost:.4f}" if key == "cost" else str(getattr(u, key))
                if key in u.unavailable:
                    value = f"{value} (partial)" if key in u.known else "N/A"
                if key == "cost" and u.estimated and key in u.known:
                    value += " (est.)"
                values.append(value)
            table.add_row(name, *values)
        return table

    def _render(self) -> Group:
        s = self.run.state
        header = f"run {s.run_id} · stage [bold]{s.stage}[/] · loop {s.loop}\n{self.activity}"
        return Group(Panel(header), self._tokens_table())


CapChoice = Literal["merge", "abort"] | int


class TerminalHuman:
    def __init__(self, display: LiveDisplay):
        self.display = display
        self.console = display.console

    def approve_plan(self, plan: str) -> bool:
        with self.display.paused():
            self.console.print(Panel(plan, title="plan.md"))
            return Prompt.ask("Approve plan?", choices=["y", "n"], default="y") == "y"

    def cap_reached(self, loops: int) -> CapChoice:
        with self.display.paused():
            self.console.print(f"[yellow]Reviewer has not approved after {loops} loops.[/]")
            choice = Prompt.ask("[m]erge anyway / [a]bort / [n] more loops", choices=["m", "a", "n"])
            if choice == "m":
                return "merge"
            if choice == "a":
                return "abort"
            return IntPrompt.ask("How many more loops?", default=1)
