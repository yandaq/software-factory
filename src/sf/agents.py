"""Agent definitions: `agents/<name>.md` = YAML frontmatter + system-prompt body."""

from pathlib import Path
from typing import Any, Literal

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, field_validator

AGENT_NAMES = (
    "planner",
    "implementer",
    "reviewer",
    "documenter",
    "improver",
    "improvement-implementer",
)

Thinking = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]


class AgentDefinition(BaseModel):
    name: str
    purpose: str
    model: str
    thinking: Thinking
    tools: list[str]
    contract: dict[str, Any]
    body: str

    @field_validator("contract")
    @classmethod
    def _valid_schema(cls, v: dict[str, Any]) -> dict[str, Any]:
        Draft202012Validator.check_schema(v)
        return v


def parse_agent(text: str) -> AgentDefinition:
    if not text.startswith("---"):
        raise ValueError("agent file must start with YAML frontmatter ('---')")
    _, front, body = text.split("---", 2)
    meta = yaml.safe_load(front) or {}
    return AgentDefinition(**meta, body=body.strip())


def load_agent(agents_dir: Path, name: str) -> AgentDefinition:
    path = agents_dir / f"{name}.md"
    try:
        agent = parse_agent(path.read_text())
    except Exception as e:
        raise ValueError(f"invalid agent definition {path}: {e}") from e
    if agent.name != name:
        raise ValueError(f"{path}: name '{agent.name}' does not match file name")
    return agent
