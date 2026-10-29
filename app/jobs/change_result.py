"""The result a change job ends with (plans/10 P1.8): a JSON block validated here and rendered
into the PR description, so every description has the same sections.

The worker checks the same shape (sazed_worker/report.py) to decide on its one repair round;
keep the two in step. A reply that still doesn't fit goes through as prose.
"""

import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_BLOCK_RE = re.compile(r"```json[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)
_TRAILING_BLOCK_RE = re.compile(r"```json[ \t]*\n.*?\n[ \t]*```\s*$", re.DOTALL)

FORMAT_RULE = """When done, end your reply with one ```json block; Sazed builds the PR description
from it, so put everything Pryce needs there:
{{"summary": "2-4 sentences: what changed and why",
 "files": ["path: what changed, one line per file"],
 "decisions": ["a design choice, the alternatives you rejected, and why"],
 "not_verified": ["what you couldn't check or test"],
 "needed_elsewhere": ["changes other repos or services need for this to work"],
 "manual_steps": ["what Pryce must do by hand: config, secrets, migrations, installs"],
 "inert": "how the change stays harmless until those are done, or \\"\\" if it works once merged"}}
Use [] for an empty list. If the task can't or shouldn't be done, change nothing and say why in
summary. If you stop to ask (below), reply with only the question line: no JSON block."""

# The follow-up prompt is Pryce's or ci_watch's instruction; the session already has the format.
FOLLOW_UP_RULE = "\n\nWhen done, end your reply with the ```json block in the same format."


class ChangeResult(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    summary: str = Field(min_length=1)
    files: list[str] = []
    decisions: list[str] = []
    not_verified: list[str] = []
    needed_elsewhere: list[str] = []
    manual_steps: list[str] = []
    inert: str = ""


def without_trailing_block(reply: str) -> str:
    """The reply minus a JSON block at its very end, so a question written before the block
    still counts as the reply's last line."""
    return _TRAILING_BLOCK_RE.sub("", reply or "")


def parse(reply: str) -> ChangeResult | None:
    """The reply's last JSON block, or None if there's none or it doesn't fit the shape."""
    blocks = _BLOCK_RE.findall(reply or "")
    if not blocks:
        return None
    try:
        return ChangeResult.model_validate_json(blocks[-1])
    except ValidationError:
        return None


_SECTIONS = (
    ("files", "Files"),
    ("decisions", "Design decisions"),
    ("not_verified", "Not verified"),
    ("needed_elsewhere", "Needed elsewhere"),
    ("manual_steps", "Manual steps"),
)


def render(result: ChangeResult) -> str:
    """The PR description: the summary, then a section per non-empty field."""
    parts = [result.summary]
    for key, heading in _SECTIONS:
        items = [item for item in getattr(result, key) if item]
        if items:
            parts.append(f"### {heading}\n" + "\n".join(f"- {item}" for item in items))
    if result.inert:
        parts.append(f"### Until then\n{result.inert}")
    return "\n\n".join(parts)
