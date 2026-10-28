"""The daily scout as an agent (plans/12 section 6): code gathers, dedupes, checks the repos and
stores; one agy job proposes candidates and a second, from a different family, judges them.

Changes from the old pipeline (plans/10 P4.1): the research prompt no longer lists every recorded
title (code dedupes against all of them anyway), the judge works from what it's given with no web
tools, and candidates that may already exist in the code are flagged for the judge.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from app.agents import context, model_step
from app.agents.steps import AgentSpec, Outcome, Step
from app.backlog import store
from app.config import settings

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
_UPSTREAM_REPOS = [
    "fastapi/fastapi",
    "anthropics/anthropic-sdk-python",
    "anthropics/claude-code",
    "pgvector/pgvector",
    "caddyserver/caddy",
    "tailscale/tailscale",
    "facebook/react",
]

_RESEARCH_PROMPT = """You are scouting improvement ideas for Pryce's setup. Today's focus: {focus}.

{stack}

Repos: {repos}
{releases}
Use the search_web and read_url_content tools to look for what would genuinely help: new
features or releases of the tools above, patterns from similar self-hosted assistant projects,
and gaps you can infer from the description. Never run commands, edit files, or open a browser.

Rules:
- Concrete and buildable in one or two pull requests on these repos. No vague advice.
- Not routine dependency or version bumps (Dependabot handles those).
- No paid SaaS unless the benefit is large and you say so.

Propose up to {count} candidates. Reply with only a JSON array in a ```json block, each item:
{{"title": "short specific title", "summary": "what it is and roughly how (markdown, under
150 words)", "why": "why it helps this setup specifically", "repos": ["owner/name"],
"effort": "S|M|L", "impact": "low|medium|high", "sources": ["https://..."],
"search_terms": ["1-3 code identifiers or library names that would appear in the repo if this
were already built"]}}"""

_JUDGE_PROMPT = """You are the second reviewer for improvement ideas proposed for Pryce's setup by
another model. Be skeptical: his time is the scarce resource, and an idea is only worth keeping if
it clearly helps this setup and is worth a pull request. Keeping none is fine.

{stack}

Candidates. `existing` lists code-search hits in his repos for the idea's search terms; when they
show the idea is already built, drop it:
{candidates}

Judge from what is given here: don't search the web or open pages.
Keep at most {max_ideas}. You may tighten a kept item's title, summary, effort or impact.
Reply with only a JSON array in a ```json block of the kept items, with the fields title,
summary, why, repos, effort, impact, sources, plus "verdict": one sentence on why it's worth
doing. Reply [] if none are."""


class Candidate(BaseModel):
    title: str
    summary: str = ""
    why: str = ""
    repos: list[str] = Field(default_factory=list)
    effort: str | None = None
    impact: str | None = None
    sources: list[str] = Field(default_factory=list)
    search_terms: list[str] = Field(default_factory=list)
    existing: list[str] = Field(default_factory=list)  # filled by the repo check


class Kept(BaseModel):
    title: str
    summary: str = ""
    why: str = ""
    repos: list[str] = Field(default_factory=list)
    effort: str | None = None
    impact: str | None = None
    sources: list[str] = Field(default_factory=list)
    verdict: str = ""


class ScoutContext(BaseModel):
    run_id: str
    max_ideas: int = Field(default_factory=lambda: settings.scout_max_ideas, ge=1, le=5)
    focus_label: str = ""
    focus: str = ""
    stack: str = ""
    releases: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    fresh: list[Candidate] = Field(default_factory=list)
    kept: list[Kept] = Field(default_factory=list)
    added: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def requested_by(self) -> str:
        return f"automation-run:{self.run_id}"


async def _focus(ctx: ScoutContext) -> dict[str, Any]:
    ctx.focus_label, ctx.focus = _FOCUS[datetime.now(UTC).weekday()]
    ctx.stack = await context.stack_facts()
    return {"detail": ctx.focus_label}


async def _releases(ctx: ScoutContext) -> dict[str, Any]:
    if ctx.focus_label != "upstream releases":
        return {"detail": "not the upstream day"}
    since = datetime.now(UTC) - timedelta(days=7)
    ctx.releases = await context.recent_releases(_UPSTREAM_REPOS, since)
    return {"detail": f"{len(ctx.releases)} new release(s)"}


def _releases_block(releases: list[dict[str, Any]]) -> str:
    if not releases:
        return ""
    lines = [f"- {r['repo']} {r['tag']} ({r['published']}): {r['url']}" for r in releases]
    return "\nNew releases in the last week (look at these first):\n" + "\n".join(lines) + "\n"


async def _research(ctx: ScoutContext) -> dict[str, Any]:
    prompt = _RESEARCH_PROMPT.format(
        focus=ctx.focus,
        stack=ctx.stack,
        repos=", ".join(settings.scout_repos),
        releases=_releases_block(ctx.releases),
        count=max(5, ctx.max_ideas * 2),
    )
    ctx.candidates, record = await model_step.agy(
        kind="research",
        title=f"Scout: {ctx.focus_label}",
        prompt=prompt,
        schema=list[Candidate],
        model=settings.scout_research_model,
        requested_by=ctx.requested_by,
        timeout_s=900,
        max_tool_calls=60,
    )
    ctx.candidates = [c for c in ctx.candidates if c.title.strip()]
    return record | {"detail": f"{len(ctx.candidates)} candidates"}


async def _dedupe(ctx: ScoutContext) -> dict[str, Any]:
    known = await store.all_titles()
    ctx.fresh = [c for c in ctx.candidates if not any(store.similar(c.title, t) for t in known)]
    return {"detail": f"{len(ctx.fresh)} new of {len(ctx.candidates)}"}


async def _nothing_new(ctx: ScoutContext) -> str | None:
    if ctx.fresh:
        return None
    return f"Nothing new ({len(ctx.candidates)} candidates, all already recorded)."


async def _repo_check(ctx: ScoutContext) -> dict[str, Any]:
    flagged = 0
    for c in ctx.fresh:
        repos = [r for r in c.repos if r in settings.scout_repos] or settings.scout_repos
        hits: list[str] = []
        for term in [t.strip() for t in c.search_terms if t.strip()][:2]:
            hits += await context.search_code(term, repos)
        c.existing = sorted(set(hits))[:5]
        flagged += bool(c.existing)
    return {"detail": f"{flagged} of {len(ctx.fresh)} may already exist"}


async def _judge(ctx: ScoutContext) -> dict[str, Any]:
    candidates = [c.model_dump(exclude={"search_terms"}) for c in ctx.fresh]
    prompt = _JUDGE_PROMPT.format(
        stack=ctx.stack, candidates=json.dumps(candidates, indent=1), max_ideas=ctx.max_ideas
    )
    ctx.kept, record = await model_step.agy(
        kind="second_opinion",
        title="Scout: second opinion",
        prompt=prompt,
        schema=list[Kept],
        model=settings.scout_judge_model,
        requested_by=ctx.requested_by,
        timeout_s=600,
        # No web tools in the judge: past this many calls the worker stops it and the step fails.
        max_tool_calls=3,
    )
    return record | {"detail": f"kept {len(ctx.kept)}"}


def _clean(item: Kept) -> dict[str, Any]:
    allowed = {r.lower(): r for r in settings.scout_repos}
    repos = [allowed[r.lower()] for r in item.repos if r.lower() in allowed]
    effort = (item.effort or "").upper()[:1]
    impact = (item.impact or "").lower()
    why = item.why.strip()
    if item.verdict.strip():
        why = f"{why}\n\nSecond opinion: {item.verdict.strip()}".strip()
    return {
        "title": item.title.strip()[:200],
        "summary": item.summary.strip()[:8000],
        "why": why[:4000],
        "repos": repos,
        "effort": effort if effort in ("S", "M", "L") else None,
        "impact": impact if impact in ("low", "medium", "high") else None,
        "sources": [u for u in item.sources if u.startswith("http")][:20],
    }


async def _store(ctx: ScoutContext) -> dict[str, Any]:
    for kept in ctx.kept[: ctx.max_ideas]:
        item = _clean(kept)
        if len(item["title"]) < 3 or await store.find_duplicate(item["title"]):
            continue
        ctx.added.append(await store.create_item(type_="idea", source="scout", **item))
    return {"detail": f"stored {len(ctx.added)}"}


async def _none_kept(ctx: ScoutContext) -> str | None:
    if ctx.added:
        return None
    return f"The second opinion kept none of {len(ctx.fresh)} new candidates."


def _summarize(ctx: ScoutContext) -> Outcome:
    lines = [f"Focus: {ctx.focus}", ""]
    for item in ctx.added:
        lines.append(f"- **{item['title']}** ({item.get('effort') or '?'}, "
                     f"{item.get('impact') or '?'} impact): {item['summary'][:300]}")
    noun = "idea" if len(ctx.added) == 1 else "ideas"
    gist = f"{len(ctx.added)} new {noun}: " + "; ".join(i["title"] for i in ctx.added)
    # One idea opens straight to it; several open the Backlog list.
    link = f"{settings.console_url}/?page=backlog"
    if len(ctx.added) == 1:
        link += f"&item={ctx.added[0]['id']}"
    return Outcome("success", "\n".join(lines), gist, link)


SPEC = AgentSpec(
    name="scout",
    context=ScoutContext,
    steps=(
        Step("focus", "code", _focus),
        Step("releases", "code", _releases),
        Step("research", "model", _research),
        Step("dedupe", "code", _dedupe),
        Step("nothing new", "gate", _nothing_new),
        Step("repo check", "code", _repo_check),
        Step("judge", "model", _judge),
        Step("store", "code", _store),
        Step("none kept", "gate", _none_kept),
    ),
    summarize=_summarize,
)
