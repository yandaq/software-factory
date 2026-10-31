"""Thin git wrapper (every failure surfaces git's stderr), plus the per-job clone handoff via a target remote."""

import fcntl
import hashlib
import os
import re
import subprocess
import tempfile
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


# -- distributed working-tree handoff --------------------------------------------------------
class InfraError(Exception):
    """Clone/fetch/push against the target remote failed: the job is requeued, the run is not parked."""


def target_remote(target: Path) -> str:
    """Where runners fetch and push a target: its `origin` URL, else the checkout's own path (one machine)."""
    url = git(target, "remote", "get-url", "origin", check=False).strip()
    return url or str(target.resolve())


def expand_target_url(url: str) -> str:
    """A bare GitHub `owner/repo` becomes its SSH clone URL; anything else is returned stripped."""
    url = url.strip()
    if re.fullmatch(r"[\w.-]+/[\w.-]+", url) and not Path(url).exists():
        return f"git@github.com:{url}.git"
    return url


def target_key(url: str) -> str | None:
    """`host/owner/repo` for an SSH (`git@host:o/r`, `ssh://git@host/o/r`) or HTTPS URL, lowercase host,
    without `.git` or a trailing slash, so different spellings of one repository compare equal.
    None for anything else, such as a local path (one machine, or tests)."""
    url = url.strip().rstrip("/")
    m = (re.fullmatch(r"(?:ssh|https?|git)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+)", url)
         or re.fullmatch(r"[^@/:]+@([^/:]+):(.+)", url))
    if not m:
        return None
    path = m.group(2).rstrip("/").removesuffix(".git").strip("/")
    return f"{m.group(1).lower()}/{path}" if path else None


def ls_remote(url: str) -> None:
    """Check `url` is a reachable git repository (never prompting for credentials); GitError says why not."""
    proc = subprocess.run(["git", "ls-remote", "--quiet", url, "HEAD"], capture_output=True, text=True,
                          check=False, env={"GIT_SSH_COMMAND": "ssh -o BatchMode=yes"} | os.environ
                          | {"GIT_TERMINAL_PROMPT": "0"})
    if proc.returncode != 0:
        raise GitError(f"cannot reach {url}: {proc.stderr.strip() or f'git ls-remote exited {proc.returncode}'}")


def cache_dir() -> Path:
    return Path(os.environ.get("SF_CACHE_DIR") or Path.home() / ".cache" / "sf")


def remote_git(repo: Path, *args: str) -> str:
    try:
        return git(repo, *args)
    except GitError as e:
        raise InfraError(str(e)) from e


def _fetch_cache(remote: str) -> Path:
    """The bare clone cache for `remote`, fetched now; a lock file serialises this machine's users of it."""
    root = cache_dir() / "repos"
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(remote.encode()).hexdigest()[:16]
    cache = root / f"{key}.git"
    with (root / f"{key}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not cache.exists():
            remote_git(root, "clone", "--quiet", "--bare", remote, cache.name)
        remote_git(cache, "fetch", "--quiet", "--prune", remote, "+refs/heads/*:refs/heads/*")
    return cache


def checkout_job(remote: str, branch: str, trunk: str, dest: Path) -> str | None:
    """A fresh working tree at `dest`, cloned from `remote` borrowing the cache's objects.

    Checked out at `branch` if the remote has it (returns its sha), else at `trunk` (returns None);
    a local `trunk` branch exists either way. An independent clone, not a `git worktree` of the cache,
    because worktrees share branch refs and two jobs on one machine may both need the trunk checked out.
    """
    cache = _fetch_cache(remote)
    start = git(cache, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip() or None
    remote_git(dest.parent, "clone", "--quiet", "--reference", str(cache), "--branch", branch if start else trunk,
                remote, str(dest))
    if start:
        git(dest, "branch", trunk, f"origin/{trunk}")
    return start


def clone_latest(remote: str, dest: Path) -> None:
    """A fresh working tree of `remote`'s default branch at `dest`, borrowing the cache's objects."""
    cache = _fetch_cache(remote)
    remote_git(dest.parent, "clone", "--quiet", "--reference", str(cache), remote, str(dest))


def publish_branch(repo: Path, branch: str, start: str | None) -> str | None:
    """Push `branch` to origin if it moved since `start`; returns its sha (None if it does not exist)."""
    sha = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip() or None
    if sha and sha != start:
        remote_git(repo, "push", "--quiet", "origin", f"{branch}:{branch}")
    return sha


def job_dir(run_id: str) -> Path:
    root = cache_dir() / "jobs"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{run_id}-", dir=root)) / "work"
