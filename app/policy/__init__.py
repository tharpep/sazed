"""One place that decides whether something Sazed or the worker does may run (plans/12 section 4).

`decide(action)` returns allow, block (with the reason to show) or pause (wait for Pryce). It is a
pure function of the facts the caller passes, so every rule can be read here and tested without a
database. Phase 1 moved today's checks in unchanged: sensitive chat tools, worker job models,
excluded repos, daily job caps and the gateway write allowlist.
"""

from app.policy.decide import (
    GATEWAY_WRITE_ALLOWLIST,
    Decision,
    GatewayWrite,
    StartJob,
    ToolCall,
    counts_toward_cap,
    decide,
)

__all__ = [
    "GATEWAY_WRITE_ALLOWLIST",
    "Decision",
    "GatewayWrite",
    "StartJob",
    "ToolCall",
    "counts_toward_cap",
    "decide",
]
