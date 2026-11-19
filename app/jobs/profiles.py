"""Which agent, model, worker profile and limits a job runs with (My-AI plans/14, contract 3).

A profile is a capability plus resources, read from the agent_profiles registry (migration 035),
then the source that asked (review_agent_by_source), then whatever the request names. The policy
layer still decides whether the result may run (model allowlist, daily cap); this only picks it.
The job row freezes the result, so a registry edit reaches only jobs created after it.
"""

from dataclasses import dataclass

from app.board import profiles as registry
from app.config import settings


@dataclass(frozen=True)
class Resolved:
    agent: str
    model: str
    profile: str
    timeout_s: int
    max_tool_calls: int | None


async def resolve(
    name: str,
    *,
    source: str,
    agent: str | None = None,
    model: str | None = None,
    timeout_s: int | None = None,
    max_tool_calls: int | None = None,
) -> Resolved:
    row = await registry.fetch(name)
    if row is None:
        raise LookupError(f"no agent profile {name!r}")
    by_source = settings.review_agent_by_source if row["capability"] == "review" else {}
    chosen = agent or by_source.get(source) or row["engine"]
    own_model = (
        row["model"] if chosen == row["engine"] else settings.default_model_by_engine[chosen]
    )
    return Resolved(
        agent=chosen,
        model=model or own_model,
        profile=row["capability"],
        timeout_s=timeout_s or row["timeout_s"],
        max_tool_calls=max_tool_calls or row["max_tool_calls"],
    )
