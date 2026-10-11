import pytest

from sf.target import (
    TargetConfig,
    TrunkError,
    install_missing_standards,
    load_config,
    resolve_trunk,
    run_validation,
    validation_commands,
)


def test_validation_commands_from_fenced_blocks(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "VALIDATION.md").write_text("x\n```sh\n# comment\necho a\n\necho b\n```\n```python\nno\n```\n")
    assert validation_commands(tmp_path, TargetConfig()) == ["echo a", "echo b"]


def test_sf_toml_overrides(tmp_path):
    (tmp_path / ".sf.toml").write_text('trunk = "dev"\nvalidation = ["true"]\n')
    cfg = load_config(tmp_path)
    assert cfg.trunk == "dev"
    assert validation_commands(tmp_path, cfg) == ["true"]


def test_run_validation_stops_at_first_failure(tmp_path):
    log = tmp_path / "v.log"
    assert not run_validation(tmp_path, ["echo one", "exit 3", "echo never"], log)
    text = log.read_text()
    assert "[exit 3]" in text and "never" not in text


def test_install_missing_standards_keeps_existing(tmp_path, no_network):
    templates = tmp_path / "t"
    templates.mkdir()
    for n in ("coding-standards.md", "architecture.md"):
        (templates / n).write_text("template")
    target = tmp_path / "target"
    (target / "docs").mkdir(parents=True)
    (target / "docs" / "architecture.md").write_text("mine")
    installed = install_missing_standards(target, templates)
    assert installed == ["AGENTS.md", "docs/VALIDATION.md", "docs/coding-standards.md"]
    assert no_network == [
        "https://raw.githubusercontent.com/yandaq/agents-default/main/AGENTS.md",
        "https://raw.githubusercontent.com/yandaq/agents-default/main/VALIDATION.md",
    ]
    assert (target / "AGENTS.md").read_text().startswith("# downloaded")
    assert (target / "docs" / "coding-standards.md").read_text() == "template"
    assert (target / "docs" / "architecture.md").read_text() == "mine"
    assert not (target / "validation.md").exists() and not (target / "agents.md").exists()


def _repo(path, *branches):
    from conftest import init_repo, sh

    init_repo(path)  # creates main
    for b in branches:
        sh(path, "git", "branch", b)
    return path


def test_trunk_prefers_main_then_master(tmp_path):
    from conftest import sh

    both = _repo(tmp_path / "both", "master")
    assert resolve_trunk(both, TargetConfig()) == "main"
    only_master = _repo(tmp_path / "m")
    sh(only_master, "git", "branch", "-m", "main", "master")
    assert resolve_trunk(only_master, TargetConfig()) == "master"


def test_trunk_errors_are_actionable(tmp_path):
    repo = _repo(tmp_path / "r")
    with pytest.raises(TrunkError, match="trunk 'dev' from .*sf.toml"):
        resolve_trunk(repo, TargetConfig(trunk="dev"))
    from conftest import sh

    sh(repo, "git", "branch", "-m", "main", "trunk")
    with pytest.raises(TrunkError, match="no 'main' or 'master'.*set trunk"):
        resolve_trunk(repo, TargetConfig())
    empty = tmp_path / "empty"
    empty.mkdir()
    sh(empty, "git", "init", "-q", "-b", "main")
    with pytest.raises(TrunkError):
        resolve_trunk(empty, TargetConfig())
