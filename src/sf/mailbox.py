"""The watch-folder queue: prompt files move `todo/` → `doing/` → `done/`.

This module knows only about folders and files: no workflow, git or harness knowledge. The caller
supplies the per-file execution callback, the poll interval and the reason the queue may have to
wait, so the monitor can be tested without sleeping or running forever.
"""

import stat
import time
from collections.abc import Callable
from pathlib import Path

FOLDERS = ("todo", "doing", "done")
POLL_SECONDS = 2.0
SETTLE_SECONDS = 1.0


class Mailbox:
    """Prompts waiting to become runs, queued as files."""

    def __init__(self, root: Path, *, settle_seconds: float = SETTLE_SECONDS):
        self.root = root
        self.todo = root / "todo"
        self.doing = root / "doing"
        self.done = root / "done"
        self.settle_seconds = settle_seconds

    def ensure(self) -> None:
        """Create the three folders when missing, leaving anything already in them alone."""
        for name in FOLDERS:
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def candidates(self) -> list[Path]:
        """Regular Markdown files directly in `todo/`, oldest modification first, ties by name."""
        found: list[tuple[float, str, Path]] = []
        for path in self._todo_files():
            stamp = _modification_time(path)
            if stamp is not None and path.suffix.lower() == ".md":
                found.append((stamp, path.name, path))
        return [path for _, _, path in sorted(found)]

    def claimable(self) -> list[Path]:
        """Candidates unmodified for `settle_seconds`, so a half-written drop is never claimed."""
        cutoff = time.time() - self.settle_seconds
        return [p for p in self.candidates() if (stamp := _modification_time(p)) is not None and stamp <= cutoff]

    def claim(self) -> Path | None:
        """Move the oldest claimable file into `doing/`; None when there is nothing to claim."""
        for path in self.claimable():
            try:
                return self._move(path, self.doing)
            except FileNotFoundError:
                continue  # the file disappeared while todo/ was being listed
        return None

    def complete(self, claimed: Path) -> Path:
        """Move a claimed file into `done/`, contents unchanged; returns where it landed."""
        return self._move(claimed, self.done)

    def monitor(
        self,
        execute: Callable[[Path], None],
        *,
        note: Callable[[str], None] = lambda text: None,
        busy: Callable[[], str | None] = lambda: None,
        sleep: Callable[[float], None] = time.sleep,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        """Run one prompt file at a time, oldest first, until Ctrl+C ends the watch.

        `busy` answers why the queue must wait (None when it may run); nothing is claimed while it
        does, so a busy run lock leaves prompts in `todo/`. A file whose `execute` raises stays in
        `doing/`: the failure is reported and the next prompt still gets its turn, so one broken
        prompt neither stops the watch nor retries forever. A KeyboardInterrupt, whether it arrives
        from the poll or from the run, ends the monitor normally.
        """
        try:
            while True:
                if reason := busy():
                    note(f"waiting: {reason}")
                elif (path := self.claim()) is not None:
                    try:
                        execute(path)
                    except Exception as error:  # noqa: BLE001 - reported below, not swallowed
                        note(f"{path.name} failed: {error}; left in {self.doing}")
                    else:
                        note(f"{path.name} done -> {self.complete(path)}")
                sleep(poll_seconds)
        except KeyboardInterrupt:
            return  # quitting is the normal ending; a file mid-run stays in doing/ to resume

    def _todo_files(self) -> list[Path]:
        return list(self.todo.iterdir()) if self.todo.is_dir() else []

    def _move(self, path: Path, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        target = _free_path(folder / path.name)
        path.rename(target)
        return target


def _modification_time(path: Path) -> float | None:
    """`st_mtime` of a regular file, or None when it is gone or is not one (todo/ changes under us)."""
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime if stat.S_ISREG(info.st_mode) else None


def _free_path(path: Path) -> Path:
    """`path` when nothing has that name yet, else the first `<stem>-N<suffix>` that is free."""
    if not path.exists():
        return path
    n = 1
    while (candidate := path.with_name(f"{path.stem}-{n}{path.suffix}")).exists():
        n += 1
    return candidate
