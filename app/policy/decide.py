"""The actions the policy layer knows, its rules, and decide()."""

import logging
from dataclasses import dataclass
from typing import Literal

from app.config import settings

logger = logging.getLogger(__name__)

Verdict = Literal["allow", "block", "pause"]


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str = ""


_ALLOW = Decision("allow")


@dataclass(frozen=True)
class ToolCall:
    """Sazed's agent loop about to run one of its tools."""

    tool: str
    interactive: bool  # someone can answer a pause (a chat, or an automation run's approvals)


@dataclass(frozen=True)
class StartJob:
    """A worker job about to be created or continued. Only the facts given are checked, in the
    order repo, model, cap, so a caller can check the repo before looking it up on GitHub."""

    agent: str
    requested_by: str
    model: str | None = None
    repo: str | None = None
    used_today: int | None = None  # the agent's capped jobs in the last 24h; None skips the cap


@dataclass(frozen=True)
class GatewayWrite:
    """An automation's direct gateway call (no Sazed turn, so no approval gate)."""

    method: str
    path: str


Action = ToolCall | StartJob | GatewayWrite

# Direct gateway calls skip Sazed's approval gate, so writes are limited to harmless endpoints.
# Anything else (email, deletes, container power) must go through a sazed_prompt action, where
# sensitive tools become pending approvals instead of running unattended.
GATEWAY_WRITE_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/notify"),
    }
)

# Jobs started by automations, think runs, CI fix-ups or Sazed with no conversation count against
# the daily cap (it protects Pryce's subscription limits from runaway loops). Jobs he starts from
# the console, a chat he's in, or MCP don't: Claude's and Google's own limits are the backstop.
_CAPPED_PREFIXES = ("automation", "think:", "ci-fix", "sazed")


def counts_toward_cap(requested_by: str) -> bool:
    return requested_by.startswith(_CAPPED_PREFIXES)


def _tool_call(action: ToolCall) -> Decision:
    if (
        action.interactive
        and settings.confirmation_required
        and action.tool in settings.sensitive_tools
    ):
        return Decision("pause", f"{action.tool} needs confirmation")
    return _ALLOW


def _start_job(action: StartJob) -> Decision:
    if action.repo is not None and action.repo.lower() in {
        r.lower() for r in settings.code_task_excluded_repos
    }:
        return Decision("block", f"{action.repo} is excluded from code tasks")
    if action.model is not None:
        allowed = settings.agy_models if action.agent == "agy" else settings.claude_models
        if action.model not in allowed:
            return Decision(
                "block",
                f"model '{action.model}' is not allowed for {action.agent}. "
                f"Allowed: {', '.join(allowed)}",
            )
    if action.used_today is not None and counts_toward_cap(action.requested_by):
        cap = (
            settings.jobs_daily_cap_agy if action.agent == "agy" else settings.jobs_daily_cap_claude
        )
        if action.used_today >= cap:
            return Decision(
                "block",
                f"daily cap reached for {action.agent} ({action.used_today}/{cap} jobs in the "
                "last 24 hours). It frees up as older jobs age out.",
            )
    return _ALLOW


def _gateway_write(action: GatewayWrite) -> Decision:
    if action.method != "GET" and (action.method, action.path) not in GATEWAY_WRITE_ALLOWLIST:
        allowed = ", ".join(f"{m} {p}" for m, p in sorted(GATEWAY_WRITE_ALLOWLIST))
        return Decision(
            "block",
            f"direct gateway writes are limited to: {allowed}. "
            "Use a sazed_prompt action for anything else (it goes through approvals).",
        )
    return _ALLOW


def decide(action: Action) -> Decision:
    """Allow, block or pause one action. Never raises; callers act on the verdict."""
    if isinstance(action, ToolCall):
        decision = _tool_call(action)
    elif isinstance(action, StartJob):
        decision = _start_job(action)
    else:
        decision = _gateway_write(action)
    if decision.verdict != "allow":
        logger.info("policy %s: %s", decision.verdict, decision.reason)
    return decision
