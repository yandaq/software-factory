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


def test_run_validation_leaves_no_artifacts_in_the_target(tmp_path):
    import sys

    (tmp_path / "helper.py").write_text("VALUE = 1\n")
    log = tmp_path / "v.log"
    commands = [f'"{sys.executable}" -c "import helper"', 'echo "$PYTEST_ADDOPTS"']
    assert run_validation(tmp_path, commands, log)
    assert not (tmp_path / "__pycache__").exists()
    assert "no:cacheprovider" in log.read_text()


def test_install_missing_standards_keeps_existing(tmp_path, no_network):
    templates = tmp_path / "t"
    templates.mkdir()
    for n in ("coding-standards.md", "architecture.md", "gitignore"):
        (templates / n).write_text("template")
    target = tmp_path / "target"
    (target / "docs").mkdir(parents=True)
    (target / "docs" / "architecture.md").write_text("mine")
    installed = install_missing_standards(target, templates)
    assert installed == ["AGENTS.md", "docs/VALIDATION.md", "docs/coding-standards.md", ".gitignore"]
    assert no_network == [
        "https://raw.githubusercontent.com/yandaq/agents-default/main/AGENTS.md",
        "https://raw.githubusercontent.com/yandaq/agents-default/main/VALIDATION.md",
    ]
    assert (target / "AGENTS.md").read_text().startswith("# downloaded")
    assert (target / "docs" / "coding-standards.md").read_text() == "template"
    assert (target / "docs" / "architecture.md").read_text() == "mine"
    # Compare stored names rather than exists(): a case-insensitive filesystem resolves agents.md to AGENTS.md.
    assert sorted(p.name for p in target.iterdir()) == [".gitignore", "AGENTS.md", "docs"]
    assert sorted(p.name for p in (target / "docs").iterdir()) == [
        "VALIDATION.md", "architecture.md", "coding-standards.md",
    ]


def test_install_missing_standards_keeps_an_existing_gitignore(tmp_path, no_network):
    templates = tmp_path / "t"
    templates.mkdir()
    for n in ("coding-standards.md", "architecture.md", "gitignore"):
        (templates / n).write_text("template")
    target = tmp_path / "target"
    target.mkdir()
    (target / ".gitignore").write_text("own-rules/\n")
    assert install_missing_standards(target, templates) == [
        "AGENTS.md", "docs/VALIDATION.md", "docs/coding-standards.md", "docs/architecture.md",
    ]
    assert (target / ".gitignore").read_text() == "own-rules/\n"


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


def test_documentation_inventory_uses_git_files(target):
    from conftest import sh

    from sf.target import discover_documentation

    (target / ".gitignore").write_text("ignored/\nignored.MD\n")
    tracked = ["DESIGN.md", "components/widget/AGENTS.md", "ignored/tracked.md", "missing.md"]
    for name in tracked:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Document\n")
    sh(target, "git", "add", "-f", "--", *tracked)
    sh(target, "git", "commit", "-qm", "documents")
    (target / "missing.md").unlink()
    untracked = ['docs/nested/notes.MD', 'docs/space "quote" {{literal}}\nline.md', 'nested/readme.mD']
    for name in untracked + ["ignored/hidden.md", "ignored.MD", "docs/code.py"]:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content")
    (target / "linked.md").symlink_to(target / "DESIGN.md")
    (target / "broken.md").symlink_to(target / "absent.md")
    assert discover_documentation(target) == sorted([
        "README.md", "DESIGN.md", "components/widget/AGENTS.md", "ignored/tracked.md", *untracked,
    ])
    assert discover_documentation(target) == discover_documentation(target)


def test_documentation_inventory_empty_and_failure(target, tmp_path):
    from sf.git import GitError
    from sf.target import discover_documentation

    (target / "README.md").unlink()
    assert discover_documentation(target) == []
    with pytest.raises(GitError):
        discover_documentation(tmp_path)
