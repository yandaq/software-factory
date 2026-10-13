"""Agent definitions: `agents/<name>.md` = YAML frontmatter + system-prompt body."""

import re
from pathlib import Path
from typing import Any, Literal, get_args

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, field_validator, model_validator

AGENT_NAMES = (
    "planner",
    "implementer",
    "reviewer",
    "documenter",
    "improver",
    "improvement-implementer",
    "improvement-reviewer",
)

Thinking = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
Harness = Literal["pi", "claude-code", "codex"]
THINKING_LEVELS = {
    "pi": list(get_args(Thinking)),
    "claude-code": ["low", "medium", "high", "xhigh"],
    "codex": ["low", "medium", "high", "xhigh"],
}


class AgentDefinition(BaseModel):
    name: str
    purpose: str
    harness: Harness = "pi"
    model: str
    thinking: Thinking
    tools: list[str]
    contract: dict[str, Any]
    body: str

    @model_validator(mode="after")
    def _harness_settings(self) -> AgentDefinition:
        if self.thinking not in THINKING_LEVELS[self.harness]:
            raise ValueError(f"thinking {self.thinking!r} is unsupported by {self.harness}")
        if not self.model.strip() or "\n" in self.model or "\r" in self.model:
            raise ValueError("model must be a single non-empty line")
        return self

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


TASK_VARIABLES = {
    "planner": ("request",),
    "implementer": ("plan", "feedback"),
    "reviewer": ("plan", "trunk"),
    "documenter": ("trunk", "branch", "standards_files"),
    "improver": ("run_id", "target", "trunk", "branch", "run_dir", "agents_dir", "cap_reached", "past_decisions"),
    "improvement-implementer": ("title", "proposed_change", "rationale", "allowed_files"),
    "improvement-reviewer": ("idea", "changes"),
}
TASK_VARIABLES = {name: (*variables, "documentation") for name, variables in TASK_VARIABLES.items()}

PLACEHOLDER = re.compile(r"{{([a-z_]+)}}")


def validate_task_template(name: str, text: str) -> None:
    filename = f"{name}.user.md"
    if name not in TASK_VARIABLES:
        raise ValueError(f"{filename}: unknown agent")
    if not text.strip():
        raise ValueError(f"{filename}: empty task template")
    remainder = PLACEHOLDER.sub("", text)
    if "{{" in remainder or "}}" in remainder:
        raise ValueError(f"{filename}: malformed placeholder; use {{{{identifier}}}}")
    found = set(PLACEHOLDER.findall(text))
    required = set(TASK_VARIABLES[name])
    if found - required:
        raise ValueError(f"{filename}: unknown placeholders: {', '.join(sorted(found - required))}")
    if required - found:
        raise ValueError(f"{filename}: missing placeholders: {', '.join(sorted(required - found))}")


def load_task_template(agents_dir: Path, name: str) -> str:
    path = agents_dir / f"{name}.user.md"
    try:
        text = path.read_text()
        validate_task_template(name, text)
    except (OSError, ValueError) as e:
        raise ValueError(f"invalid task template {path}: {e}") from e
    return text


def render_task(agents_dir: Path, name: str, values: dict[str, str]) -> str:
    """Substitute once: inserted requests and logs are literal, never templates."""
    return render_task_text(name, load_task_template(agents_dir, name), values)


def render_task_text(name: str, text: str, values: dict[str, str]) -> str:
    """Render an already loaded template, including a pinned review template."""
    validate_task_template(name, text)
    if set(values) != set(TASK_VARIABLES[name]) or any(not isinstance(v, str) for v in values.values()):
        raise ValueError(f"{name}.user.md: context must supply all declared variables as strings")
    return PLACEHOLDER.sub(lambda match: values[match[1]], text)


def validate_agents(agents_dir: Path) -> None:
    for name in AGENT_NAMES:
        load_agent(agents_dir, name)
        load_task_template(agents_dir, name)
