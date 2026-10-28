"""The weekly backlog triage (plans/07 section 9b), run by the agent runner as one step until
plans/12 phase 2 ports it. The scout moved to app/agents/scout.py.

triage: open ideas older than BACKLOG_STALE_DAYS go stale -> judge orders the rest -> top two are
        shortlisted (the item's rank, Pryce's priority, is left alone).

The models only write and rank text. Code decides what gets stored, nothing is ever deleted, and
nothing becomes a PR until Pryce approves it in the console.
"""

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from app.agents.context import STACK
from app.backlog import store
from app.config import settings
from app.jobs import service as jobs

logger = logging.getLogger(__name__)

_TRIAGE_PROMPT = """Rank Pryce's open backlog ideas by what is most worth doing next for his setup:
value for the effort, how much it unblocks, and risk. Be concrete in the reasons.

{stack}

Ideas:
{ideas}

Reply with only a JSON array in a ```json block covering every id, best first:
[{{"id": "...", "rank": 1, "reason": "one sentence"}}]"""


def _parse_json_list(text: str) -> list[dict[str, Any]]:
    """The JSON array from a model reply (fenced or bare). Raises ValueError if there isn't one."""
    fenced = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text[text.find("[") : text.rfind("]") + 1]
    data = json.loads(raw) if raw else None
    if not isinstance(data, list):
        raise ValueError("reply has no JSON array")
    return [d for d in data if isinstance(d, dict)]


async def _run_agy(
    kind: str, title: str, prompt: str, model: str, requested_by: str, timeout_s: int, tools: int
) -> str:
    job = await jobs._create_agy_job(
        kind,
        title,
        prompt,
        {"timeout_s": timeout_s, "max_tool_calls": tools},
        requested_by,
        model,
    )
    done = await jobs.wait_for(job["id"], timeout_s + 300)
    if done is None:
        raise RuntimeError(f"{title}: job {job['id']} didn't finish in time")
    if done["status"] != "succeeded":
        raise RuntimeError(f"{title}: job {done['id']} {done['status']}: {done.get('error') or ''}")
    return done.get("result") or ""


async def triage(run_id: uuid.UUID) -> tuple[str, str, str | None, str | None]:
    went_stale = await store.mark_stale(settings.backlog_stale_days)
    ideas = await store.list_items("idea", "open")
    if not ideas:
        return "skipped", f"No open ideas ({went_stale} went stale).", None, None

    if len(ideas) <= 2:
        ranked = [{"id": str(i["id"]), "rank": n + 1, "reason": ""} for n, i in enumerate(ideas)]
    else:
        now = datetime.now(UTC)
        listing = [
            {
                "id": str(i["id"]),
                "title": i["title"],
                "summary": i["summary"][:600],
                "why": i["why"][:400],
                "effort": i.get("effort"),
                "impact": i.get("impact"),
                "age_days": (now - i["created_at"]).days,
            }
            for i in ideas
        ]
        reply = await _run_agy(
            "second_opinion",
            "Weekly idea triage",
            _TRIAGE_PROMPT.format(stack=STACK, ideas=json.dumps(listing, indent=1)),
            settings.scout_judge_model,
            f"automation-run:{run_id}",
            timeout_s=600,
            tools=10,
        )
        ranked = sorted(_parse_json_list(reply), key=lambda r: int(r.get("rank") or 999))

    by_id = {str(i["id"]): i for i in ideas}
    order = [r for r in ranked if str(r.get("id")) in by_id]
    top = {str(r["id"]) for r in order[:2]}
    for r in order:
        item = by_id[str(r["id"])]
        status = "shortlisted" if str(r["id"]) in top else "new"
        # Shortlisting only: rank is Pryce's saved priority and the triage never touches it.
        await store.update_item(item["id"], status=status)

    lines = ["Top picks:"]
    for r in order[:2]:
        reason = f": {r['reason']}" if r.get("reason") else ""
        lines.append(f"1. **{by_id[str(r['id'])]['title']}**{reason}")
    lines.append(f"\nOpen ideas: {len(ideas)}. Went stale this week: {went_stale}.")
    gist = "Top: " + "; ".join(by_id[str(r["id"])]["title"] for r in order[:2])
    gist += f". {len(ideas)} open."
    return "success", "\n".join(lines), gist, f"{settings.console_url}/?page=backlog"
