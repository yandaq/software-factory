"""Target-repo settings (`.sf.toml`), standards files and validation commands."""

import re
import shutil
import subprocess
import tomllib
import urllib.request
from pathlib import Path

from pydantic import BaseModel

from sf import git as g

VALIDATION_FILE = "docs/VALIDATION.md"
STANDARDS_FILES = ("AGENTS.md", VALIDATION_FILE, "docs/coding-standards.md", "docs/architecture.md")
DEFAULTS_URL = "https://raw.githubusercontent.com/yandaq/agents-default/main"
DOWNLOADED = {"AGENTS.md": f"{DEFAULTS_URL}/AGENTS.md", VALIDATION_FILE: f"{DEFAULTS_URL}/VALIDATION.md"}
TEMPLATED = {"docs/coding-standards.md": "coding-standards.md", "docs/architecture.md": "architecture.md"}


class TargetConfig(BaseModel):
    trunk: str | None = None  # default: main, else master
    validation: list[str] | None = None  # overrides commands parsed from docs/VALIDATION.md


DEFAULT_TRUNKS = ("main", "master")


class TrunkError(Exception):
    pass


def resolve_trunk(target: Path, config: TargetConfig) -> str:
    """The configured trunk, else `main`, else `master`; it must be a branch with a commit."""

    def exists(branch: str) -> bool:
        return g.git(target, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}", check=False) != ""

    if config.trunk:
        if exists(config.trunk):
            return config.trunk
        raise TrunkError(f"trunk '{config.trunk}' from {target / '.sf.toml'} is not a branch with a commit in {target}")
    for branch in DEFAULT_TRUNKS:
        if exists(branch):
            return branch
    raise TrunkError(
        f"{target} has no 'main' or 'master' branch with a commit; commit to one of them "
        'or set trunk = "<branch>" in .sf.toml'
    )


def load_config(target: Path) -> TargetConfig:
    path = target / ".sf.toml"
    if not path.exists():
        return TargetConfig()
    return TargetConfig(**tomllib.loads(path.read_text()))


class StandardsError(Exception):
    pass


def download(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode()
    except OSError as e:
        raise StandardsError(f"could not download {url}: {e}") from e


def install_missing_standards(target: Path, templates: Path) -> list[str]:
    """Add missing standards: AGENTS.md and docs/VALIDATION.md from agents-default, the rest from templates."""
    (target / "docs").mkdir(exist_ok=True)
    installed = []
    for path, url in DOWNLOADED.items():
        if not (target / path).exists():
            (target / path).write_text(download(url))
            installed.append(path)
    for path, template in TEMPLATED.items():
        if not (target / path).exists():
            shutil.copyfile(templates / template, target / path)
            installed.append(path)
    return installed


def discover_documentation(target: Path) -> list[str]:
    """Inventory existing repository Markdown using Git's tracked/visible files."""
    files = g.git(target, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    root = target.resolve()
    documents = set()
    for name in files.split("\0"):
        path = target / name
        if (name and path.suffix.lower() == ".md" and not path.is_symlink()
                and path.is_file() and path.resolve().is_relative_to(root)):
            documents.add(name)
    return sorted(documents)


_FENCE = re.compile(r"```(?:sh|bash|shell)\s*\n(.*?)```", re.DOTALL)


def validation_commands(target: Path, config: TargetConfig) -> list[str]:
    if config.validation is not None:
        return config.validation
    path = target / VALIDATION_FILE
    if not path.exists():
        return []
    commands = []
    for block in _FENCE.findall(path.read_text()):
        commands += [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    return commands


def run_validation(target: Path, commands: list[str], log_path: Path) -> bool:
    ok = True
    with log_path.open("w") as log:
        for cmd in commands:
            proc = subprocess.run(cmd, shell=True, cwd=target, capture_output=True, text=True, check=False)
            log.write(f"$ {cmd}\n{proc.stdout}{proc.stderr}[exit {proc.returncode}]\n\n")
            if proc.returncode != 0:
                ok = False
                break
    return ok
