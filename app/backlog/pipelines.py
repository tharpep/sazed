"""Backlog pipelines run by automations (action type 'pipeline', plans/07 section 9b).

scout:  research job (Gemini) proposes candidates -> code drops anything already recorded ->
        judge job (Claude Opus via agy, a different family) keeps at most N -> stored as ideas.
triage: open ideas older than BACKLOG_STALE_DAYS go stale -> judge ranks the rest -> top two are
        shortlisted.

The models only write and rank text. Code decides what gets stored, nothing is ever deleted, and
nothing becomes a PR until Pryce approves it in the console.
"""

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app.backlog import store
from app.config import settings
from app.http_client import get_client
from app.jobs import service as jobs

logger = logging.getLogger(__name__)

_STACK = """Pryce's personal AI ecosystem, self-hosted on a Proxmox homelab (LXC containers,
reachable only over his Tailscale tailnet; secrets in root-only env files; deploys by GitHub
self-hosted runners on push to main):
- sazed: the agent (FastAPI, Anthropic SDK tool loop with Haiku/Sonnet, asyncpg/Postgres memory
  and sessions, automations as database rows with cron triggers, worker jobs, approval gates on
  sensitive tools).
- api-gateway: one FastAPI proxy for Google Calendar/Tasks/Gmail/Drive/Sheets, GitHub (PAT plus
  a GitHub App bot that opens PRs), web search, Pushover, a homelab service registry with health
  polling and alerts, and an MCP server for Claude Code/Desktop.
- knowledge-base: RAG over uploads and ingested text/URLs (Postgres + pgvector, hybrid retrieval).
- sazed-frontend: the console (React 19, Vite, Tailwind) with Chat, Homelab, Knowledge, Gateway
  and Automations pages; used on desktop and an iPhone.
- sazed-worker: runs Claude Code (Pryce's Claude Pro subscription) and Antigravity (Gemini) jobs
  for research, second opinions, code reviews and change PRs, on its own container with an
  egress allowlist.
- sazed-mcp: exposes Sazed to Claude Desktop as one tool.
Other homelab services: Jellyfin and the *arr stack, Kavita, Suwayomi, SillyTavern, Postgres.
Solo developer, no test suites yet, CI is ruff on changed lines.
Already in place (an idea that extends one of these must say what's new): prompt caching on the
system prompt and tool schemas; context-window compression of old turns; Pushover alerts for
outages, disk, failed deploys and automations; per-tool approval gates; daily job caps."""

# The scout looks at one area a day so a week of runs covers the stack instead of repeating.
# (short label for titles, what to tell the model)
_FOCUS = [
    ("agent", "the agent itself: tools, memory, model use, prompts and cost"),  # Monday
    ("homelab ops", "homelab operations: reliability, monitoring, backups, deploys"),
    ("knowledge base", "the knowledge base and retrieval quality"),
    ("console UX", "the console UX on desktop and phone"),
    ("automations", "automations and integrations with services Pryce already uses"),
    ("security", "security and privacy of the setup"),
    (
        "upstream releases",
        "new releases and features in the tools this stack is built on (FastAPI, Anthropic API, "
        "Claude Code, Antigravity, Proxmox, Tailscale, Caddy, pgvector, React)",
    ),  # Sunday
]

_SCOUT_PROMPT = """You are scouting improvement ideas for Pryce's setup. Today's focus: {focus}.

{stack}

Repos: {repos}

Use the search_web and read_url_content tools to look for what would genuinely help: new
features or releases of the tools above, patterns from similar self-hosted assistant projects,
and gaps you can infer from the description. Never run commands, edit files, or open a browser.

Rules:
- Concrete and buildable in one or two pull requests on these repos. No vague advice.
- Not routine dependency or version bumps (Dependabot handles those).
- No paid SaaS unless the benefit is large and you say so.
- Not anything already recorded below, including reworded versions of it (these were proposed
  before; some were dismissed on purpose):
{known}

Propose up to {count} candidates. Reply with only a JSON array in a ```json block, each item:
{{"title": "short specific title", "summary": "what it is and roughly how (markdown, under
150 words)", "why": "why it helps this setup specifically", "repos": ["owner/name"],
"effort": "S|M|L", "impact": "low|medium|high", "sources": ["https://..."]}}"""

_JUDGE_PROMPT = """You are the second reviewer for improvement ideas proposed for Pryce's setup by
another model. Be skeptical: his time is the scarce resource, and an idea is only worth keeping if
it clearly helps this setup and is worth a pull request. Keeping none is fine.

{stack}

Candidates:
{candidates}

Keep at most {max_ideas}. You may tighten a kept item's title, summary, effort or impact, and you
may check facts with search_web/read_url_content. Never run commands or edit files.
Reply with only a JSON array in a ```json block of the kept items, same fields as the candidates
plus "verdict": one sentence on why it's worth doing. Reply [] if none are."""

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


async def _homelab_services() -> str:
    try:
        resp = await get_client().get(
            f"{settings.gateway_url}/homelab/services",
            params={"compact": "true"},
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return ""
    services = data.get("services", data) if isinstance(data, dict) else data
    names = [s.get("name") or s.get("id") for s in services if isinstance(s, dict)]
    return ", ".join(n for n in names if n)


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


def _clean(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {r.lower(): r for r in settings.scout_repos}
    repos = [allowed[r.lower()] for r in item.get("repos") or [] if str(r).lower() in allowed]
    effort = str(item.get("effort") or "").upper()[:1]
    impact = str(item.get("impact") or "").lower()
    why = str(item.get("why") or "").strip()
    if item.get("verdict"):
        why = f"{why}\n\nSecond opinion: {str(item['verdict']).strip()}".strip()
    return {
        "title": str(item.get("title") or "").strip()[:200],
        "summary": str(item.get("summary") or "").strip()[:8000],
        "why": why[:4000],
        "repos": repos,
        "effort": effort if effort in ("S", "M", "L") else None,
        "impact": impact if impact in ("low", "medium", "high") else None,
        "sources": [str(u) for u in item.get("sources") or [] if str(u).startswith("http")][:20],
    }


async def scout(run_id: uuid.UUID, max_ideas: int) -> tuple[str, str, str | None, str | None]:
    requested_by = f"automation-run:{run_id}"
    label, focus = _FOCUS[datetime.now(UTC).weekday()]
    stack = _STACK
    if services := await _homelab_services():
        stack += f"\nRegistered homelab services right now: {services}."
    known = await store.all_titles()
    known_text = "\n".join(f"  - {t}" for t in known[-300:]) or "  (nothing yet)"

    research = await _run_agy(
        "research",
        f"Scout: {label}",
        _SCOUT_PROMPT.format(
            focus=focus,
            stack=stack,
            repos=", ".join(settings.scout_repos),
            known=known_text,
            count=max(5, max_ideas * 2),
        ),
        settings.scout_research_model,
        requested_by,
        timeout_s=900,
        tools=60,
    )
    candidates = [c for c in _parse_json_list(research) if c.get("title")]
    fresh = [c for c in candidates if not any(store.similar(c["title"], t) for t in known)]
    if not fresh:
        note = f"Nothing new ({len(candidates)} candidates, all already recorded)."
        return "skipped", note, None, None

    verdict = await _run_agy(
        "second_opinion",
        "Scout: second opinion",
        _JUDGE_PROMPT.format(
            stack=stack, candidates=json.dumps(fresh, indent=1), max_ideas=max_ideas
        ),
        settings.scout_judge_model,
        requested_by,
        timeout_s=600,
        tools=20,
    )
    added: list[dict[str, Any]] = []
    for raw in _parse_json_list(verdict)[:max_ideas]:
        item = _clean(raw)
        if len(item["title"]) < 3 or await store.find_duplicate(item["title"]):
            continue
        added.append(await store.create_item(type_="idea", source="scout", **item))
    if not added:
        return (
            "skipped",
            f"The second opinion kept none of {len(fresh)} new candidates.",
            None,
            None,
        )

    lines = [f"Focus: {focus}", ""]
    for item in added:
        lines.append(f"- **{item['title']}** ({item.get('effort') or '?'}, "
                     f"{item.get('impact') or '?'} impact): {item['summary'][:300]}")
    noun = "idea" if len(added) == 1 else "ideas"
    gist = f"{len(added)} new {noun}: " + "; ".join(i["title"] for i in added)
    # One idea opens straight to it; several open the Backlog list.
    link = f"{settings.console_url}/?page=backlog"
    if len(added) == 1:
        link += f"&item={added[0]['id']}"
    return "success", "\n".join(lines), gist, link


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
            _TRIAGE_PROMPT.format(stack=_STACK, ideas=json.dumps(listing, indent=1)),
            settings.scout_judge_model,
            f"automation-run:{run_id}",
            timeout_s=600,
            tools=10,
        )
        ranked = sorted(_parse_json_list(reply), key=lambda r: int(r.get("rank") or 999))

    by_id = {str(i["id"]): i for i in ideas}
    order = [r for r in ranked if str(r.get("id")) in by_id]
    top = {str(r["id"]) for r in order[:2]}
    for n, r in enumerate(order, start=1):
        item = by_id[str(r["id"])]
        status = "shortlisted" if str(r["id"]) in top else "new"
        await store.update_item(item["id"], rank=n, status=status)

    lines = ["Top picks:"]
    for r in order[:2]:
        reason = f": {r['reason']}" if r.get("reason") else ""
        lines.append(f"1. **{by_id[str(r['id'])]['title']}**{reason}")
    lines.append(f"\nOpen ideas: {len(ideas)}. Went stale this week: {went_stale}.")
    gist = "Top: " + "; ".join(by_id[str(r["id"])]["title"] for r in order[:2])
    gist += f". {len(ideas)} open."
    return "success", "\n".join(lines), gist, f"{settings.console_url}/?page=backlog"


async def run(action: dict[str, Any], run_id: uuid.UUID) -> tuple[str, str, str | None, str | None]:
    if action["name"] == "map":
        from app.activity import map_report  # lazy: unrelated to the backlog pipelines

        return await map_report.run(run_id)
    if action["name"] == "scout":
        return await scout(run_id, int(action.get("max_ideas") or settings.scout_max_ideas))
    return await triage(run_id)
