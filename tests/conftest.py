import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def sh(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    sh(path, "git", "init", "-q", "-b", "main")
    sh(path, "git", "config", "user.email", "t@example.com")
    sh(path, "git", "config", "user.name", "t")
    (path / "README.md").write_text("# target\n")
    sh(path, "git", "add", "-A")
    sh(path, "git", "commit", "-qm", "init")
    return path


@pytest.fixture
def factory(tmp_path: Path) -> Path:
    f = tmp_path / "factory"
    f.mkdir()
    shutil.copytree(ROOT / "agents", f / "agents")
    shutil.copytree(ROOT / "templates", f / "templates")
    init_repo(f)
    return f


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return init_repo(tmp_path / "target")


@pytest.fixture
def fake_pi(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    script = tmp_path / "script.json"
    monkeypatch.setenv("SF_PI", str(ROOT / "tests" / "fake_pi.py"))
    monkeypatch.setenv("FAKE_PI_SCRIPT", str(script))

    def configure(steps: dict) -> Path:
        script.write_text(json.dumps(steps))
        return script

    return configure


def calls(script: Path) -> list[dict]:
    p = script.with_suffix(".calls.jsonl")
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub agents-default downloads; returns the URLs requested."""
    urls: list[str] = []

    def fake_download(url: str) -> str:
        urls.append(url)
        return f"# downloaded from {url}\n"

    monkeypatch.setattr("sf.target.download", fake_download)
    return urls
