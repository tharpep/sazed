"""Agent name -> spec. The `agent` automation action names one of these."""

from app.agents import legacy, scout
from app.agents.steps import AgentSpec

SPECS: dict[str, AgentSpec] = {
    "scout": scout.SPEC,
    "triage": legacy.TRIAGE,
    "map": legacy.MAP,
}
