"""Which agent, model, worker profile and limits a job runs with (My-AI plans/14, contract 3).

A profile is a capability plus resources, resolved in one order: the capability's defaults, then
the source that asked (review_agent_by_source), then whatever the request names. The policy layer
still decides whether the result may run (model allowlist, daily cap); this only picks it.
"""

from dataclasses import dataclass

from app.config import settings

# Pryce's call (2026-10-18): Gemini 3.8 Flash at its highest effort suits research best. The effort
# is part of the model id ("-high" is Flash's top level); agy rejects a conflicting --effort.
DEFAULT_MODELS: dict[str, str] = {"agy": "gemini-3.8-flash-high", "claude": "claude-sonnet-5-5"}


@dataclass(frozen=True)
class Capability:
    profile: str  # the worker profile: research (web only), review (read-only), change (edits)
    agent: str
    timeout_s: int
    max_tool_calls: int


CAPABILITIES: dict[str, Capability] = {
    "research_quick": Capability("research", "agy", 300, 30),
    "research_deep": Capability("research", "agy", 600, 80),
    "second_opinion": Capability("research", "agy", 300, 20),
    "review": Capability("review", "claude", 900, 150),
    "change": Capability("change", "claude", 1800, 200),
}


@dataclass(frozen=True)
class Resolved:
    agent: str
    model: str
    profile: str
    timeout_s: int
    max_tool_calls: int


def resolve(
    capability: str,
    *,
    source: str,
    agent: str | None = None,
    model: str | None = None,
    timeout_s: int | None = None,
    max_tool_calls: int | None = None,
) -> Resolved:
    cap = CAPABILITIES[capability]
    by_source = settings.review_agent_by_source if cap.profile == "review" else {}
    chosen = agent or by_source.get(source) or cap.agent
    return Resolved(
        agent=chosen,
        model=model or DEFAULT_MODELS[chosen],
        profile=cap.profile,
        timeout_s=timeout_s or cap.timeout_s,
        max_tool_calls=max_tool_calls or cap.max_tool_calls,
    )
