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
