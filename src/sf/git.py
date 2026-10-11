"""Thin git wrapper; every failure surfaces git's stderr."""

import subprocess
from pathlib import Path


class GitError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip()}")
    return proc.stdout


def commit_all(repo: Path, message: str, paths: tuple[str, ...] = (".",)) -> bool:
    """Stage `paths` and commit; returns False when there was nothing to commit."""
    paths = tuple(p for p in paths if (repo / p).exists() or git(repo, "ls-files", "--", p).strip())
    if not paths:
        return False
    git(repo, "add", "-A", "--", *paths)
    if not git(repo, "diff", "--cached", "--name-only").strip():
        return False
    git(repo, "commit", "-m", message)
    return True


def changed_paths(repo: Path) -> list[tuple[str, str]]:
    """(status, path) for every uncommitted change, untracked files included."""
    out = git(repo, "status", "--porcelain", "-uall", "-z")
    entries = []
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        if not entry:
            i += 1
            continue
        status, path = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            i += 1  # skip rename source
        entries.append((status.strip(), path))
        i += 1
    return entries


def revert_paths(repo: Path, paths: list[str]) -> None:
    for p in paths:
        if git(repo, "ls-files", "--", p).strip():
            git(repo, "checkout", "HEAD", "--", p)
        else:
            (repo / p).unlink(missing_ok=True)
