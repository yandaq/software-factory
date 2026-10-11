from sf.target import TargetConfig, install_missing_standards, load_config, run_validation, validation_commands


def test_validation_commands_from_fenced_blocks(tmp_path):
    (tmp_path / "validation.md").write_text("x\n```sh\n# comment\necho a\n\necho b\n```\n```python\nno\n```\n")
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


def test_install_missing_standards_keeps_existing(tmp_path):
    templates = tmp_path / "t"
    templates.mkdir()
    for n in ("coding-standards.md", "architecture.md", "validation.md", "agents.md"):
        (templates / n).write_text("template")
    target = tmp_path / "target"
    target.mkdir()
    (target / "agents.md").write_text("mine")
    copied = install_missing_standards(target, templates)
    assert "agents.md" not in copied and len(copied) == 3
    assert (target / "agents.md").read_text() == "mine"
