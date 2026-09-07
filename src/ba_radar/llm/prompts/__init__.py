"""Versioned prompt files.

A prompt is a Markdown file named `<task>_v<version>.md` next to this module. The
version in use is pinned here, written to every run log, and stored on each scored
item, so a Stage 5 calibration pass can tell which rubric produced which score. Bump
the version by adding a new file and changing the pin — never by editing an old file
in place.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent

PROMPT_VERSIONS: dict[str, int] = {
    "relevance": 1,
    "analysis": 1,
}

# Appended to every system prompt so the English enum values the model must emit are
# spelled out once, next to what they mean. The model writes Ukrainian prose but
# English keys (answers doc Q19); Ukrainian labels live in `ba_radar.labels`.
ENUM_GUIDE = """## Enum values (use exactly these strings)

tags: specifications (requirements, specs, user stories, acceptance criteria written for
or with agents), context_engineering (CLAUDE.md/AGENTS.md, memory, MCP, docs for
agents, token/context savings), agent_orchestration (multi-agent workflows, planning,
delegation, review loops), evals (measuring agent output, testing, regressions),
tool_release (a release or launch of a tool or model relevant to the team),
role_change (how analysts, PMs and engineers work differently; productivity
evidence), evidence_critique (measured results, post-mortems, credible pushback),
security_quality (prompt injection, permissions, supply chain, quality decay).
action: try | read | note.  priority: critical | notable | background."""


@dataclass(frozen=True)
class PromptFile:
    task: str
    version: int
    text: str

    @property
    def name(self) -> str:
        return f"{self.task}_v{self.version}"


@cache
def load_prompt(task: str) -> PromptFile:
    version = PROMPT_VERSIONS[task]
    path = PROMPTS_DIR / f"{task}_v{version}.md"
    return PromptFile(task=task, version=version, text=path.read_text(encoding="utf-8").strip())
