"""Shapes shared by the notes filer and its routes."""

from dataclasses import dataclass

FILER_VERSION = "1"
KINDS = ("idea", "list", "link", "reference", "todo")


class FilerError(Exception):
    """The model's answer could not be turned into a filing."""


@dataclass(frozen=True)
class Filing:
    topic: str
    new_topic: bool
    kind: str | None
    reason: str
    by_tag: bool
    filer: str = FILER_VERSION


@dataclass(frozen=True)
class FilerUsage:
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
