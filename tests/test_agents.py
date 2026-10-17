from pathlib import Path

import pytest
from jsonschema.exceptions import SchemaError
from pydantic import ValidationError

from sf.agents import AGENT_NAMES, load_agent, parse_agent

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", AGENT_NAMES)
def test_shipped_agents_load(name):
    agent = load_agent(ROOT / "agents", name)
    assert agent.body and agent.tools


def test_missing_frontmatter_rejected():
    with pytest.raises(ValueError, match="frontmatter"):
        parse_agent("no frontmatter")


def test_invalid_contract_schema_rejected():
    text = "---\nname: x\npurpose: p\nmodel: m\nthinking: low\ntools: []\ncontract: {type: 7}\n---\nbody"
    with pytest.raises((SchemaError, ValidationError)):
        parse_agent(text)


def test_invalid_thinking_rejected():
    text = "---\nname: x\npurpose: p\nmodel: m\nthinking: huge\ntools: []\ncontract: {}\n---\nbody"
    with pytest.raises(ValidationError):
        parse_agent(text)


def test_legacy_agent_defaults_to_pi():
    assert load_agent(ROOT / "agents", "planner").harness == "pi"


@pytest.mark.parametrize("harness", ["claude-code", "codex"])
def test_harness_effort_validation(harness):
    text = (ROOT / "agents" / "planner.md").read_text().replace("harness: pi", f"harness: {harness}", 1)
    assert parse_agent(text).harness == harness
    with pytest.raises(ValidationError, match="unsupported"):
        parse_agent(text.replace("thinking: xhigh", "thinking: max", 1))


def test_invalid_harness_rejected():
    text = (ROOT / "agents" / "planner.md").read_text().replace("harness: pi", "harness: unknown", 1)
    with pytest.raises(ValidationError):
        parse_agent(text)


@pytest.mark.parametrize("name", AGENT_NAMES)
def test_task_templates_render_literal_context(name):
    from sf.agents import TASK_VARIABLES, render_task

    literal = '{{unknown}} {"json": 1} $HOME `command` £'
    values = dict.fromkeys(TASK_VARIABLES[name], literal)
    text = render_task(ROOT / "agents", name, values)
    assert literal in text
    if "feedback" in values:
        values["feedback"] = ""
        assert literal in render_task(ROOT / "agents", name, values)


@pytest.mark.parametrize("text,reason", [
    ("", "empty"), ("{{other}}", "unknown"), ("No context", "missing"),
    ("{{ request }}", "malformed"), ("{{request", "malformed"), ("{{request}} }}", "malformed"),
])
def test_invalid_task_template(text, reason):
    from sf.agents import validate_task_template

    with pytest.raises(ValueError, match=f"planner.user.md: .*{reason}"):
        validate_task_template("planner", text)


def test_missing_task_template_and_context(tmp_path):
    from sf.agents import load_task_template, render_task

    with pytest.raises(ValueError, match="planner.user.md"):
        load_task_template(tmp_path, "planner")
    (tmp_path / "planner.user.md").write_text("{{request}} {{documentation}}")
    with pytest.raises(ValueError, match="context"):
        render_task(tmp_path, "planner", {})
