"""Target-repo settings (`.sf.toml`), standards files and validation commands."""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from pydantic import BaseModel

STANDARDS_FILES = ("coding-standards.md", "architecture.md", "validation.md", "agents.md")


class TargetConfig(BaseModel):
    trunk: str = "main"
    validation: list[str] | None = None  # overrides commands parsed from validation.md


def load_config(target: Path) -> TargetConfig:
    path = target / ".sf.toml"
    if not path.exists():
        return TargetConfig()
    return TargetConfig(**tomllib.loads(path.read_text()))


def install_missing_standards(target: Path, templates: Path) -> list[str]:
    copied = []
    for name in STANDARDS_FILES:
        if not (target / name).exists():
            shutil.copyfile(templates / name, target / name)
            copied.append(name)
    (target / "docs").mkdir(exist_ok=True)
    return copied


_FENCE = re.compile(r"```(?:sh|bash|shell)\s*\n(.*?)```", re.DOTALL)


def validation_commands(target: Path, config: TargetConfig) -> list[str]:
    if config.validation is not None:
        return config.validation
    path = target / "validation.md"
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
