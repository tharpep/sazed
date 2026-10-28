"""The step kinds an agent is made of, the spec that orders them, and a run's outcome."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

Kind = Literal["code", "gate", "model", "pause", "output"]


@dataclass(frozen=True)
class Step:
    """One step. `run(ctx)` does the work on the shared context.

    - code, model, output: may return a dict, merged into the step's run record (job id,
      tokens, counts).
    - gate: returns a reason to stop the run as skipped, or None to go on.
    - pause: returns the question to ask Pryce, or None when no approval is needed.
    - repeat_until: run the step again, up to max_attempts, until the predicate holds.
    """

    name: str
    kind: Kind
    run: Callable[[Any], Awaitable[Any]]
    repeat_until: Callable[[Any], bool] | None = None
    max_attempts: int = 1


@dataclass(frozen=True)
class Outcome:
    """How a run ended, in the shape the automation runner delivers."""

    status: Literal["success", "skipped", "needs_approval"]
    text: str
    notify_text: str | None = None
    notify_url: str | None = None


@dataclass(frozen=True)
class AgentSpec:
    name: str
    context: type[BaseModel]  # built from the automation's params plus run_id
    steps: tuple[Step, ...]
    summarize: Callable[[Any], Outcome]  # after the last step


class StepFailed(Exception):
    """A step that can't complete: a job that failed, or a reply that never matched its schema."""
