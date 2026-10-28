"""Shared context providers for agents: code, not models, gathers the facts a model step needs."""

import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import settings
from app.http_client import get_client

logger = logging.getLogger(__name__)

# Hand-kept: the repos' GitHub descriptions are mostly empty (2026-10-27), so this stays written
# by hand; the live service list is appended by stack_facts().
STACK = """Pryce's personal AI ecosystem, self-hosted on a Proxmox homelab (LXC containers,
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
Solo developer, a pytest suite in sazed only, CI is ruff on changed lines.
Already in place (an idea that extends one of these must say what's new): prompt caching on the
system prompt and tool schemas; context-window compression of old turns; Pushover alerts for
outages, disk, failed deploys and automations; per-tool approval gates; daily job caps."""

_stack_cache: dict[str, str] = {}


def _headers() -> dict[str, str]:
    return {"X-API-Key": settings.gateway_api_key}


async def _homelab_services() -> str:
    try:
        resp = await get_client().get(
            f"{settings.gateway_url}/homelab/services",
            params={"compact": "true"},
            headers=_headers(),
            timeout=20.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return ""
    services = data.get("services", data) if isinstance(data, dict) else data
    names = [s.get("name") or s.get("id") for s in services if isinstance(s, dict)]
    return ", ".join(n for n in names if n)


async def stack_facts() -> str:
    """The stack description plus the registered services, cached for the day."""
    day = datetime.now(UTC).date().isoformat()
    if day not in _stack_cache:
        text = STACK
        if services := await _homelab_services():
            text += f"\nRegistered homelab services right now: {services}."
        _stack_cache.clear()
        _stack_cache[day] = text
    return _stack_cache[day]


async def search_code(term: str, repos: list[str], limit: int = 5) -> list[str]:
    """Code-search hits for `term` across `repos`, as 'owner/repo:path'. Empty on any error."""
    query = f"{term} " + " ".join(f"repo:{r}" for r in repos)
    try:
        resp = await get_client().get(
            f"{settings.gateway_url}/github/search/code",
            params={"q": query, "per_page": limit},
            headers=_headers(),
            timeout=20.0,
        )
        resp.raise_for_status()
        hits = resp.json()
    except (httpx.HTTPError, ValueError):
        logger.warning("code search failed for %r", term, exc_info=True)
        return []
    return [f"{h['repo']}:{h['path']}" for h in hits if isinstance(h, dict)][:limit]


async def recent_releases(repos: list[str], since: datetime) -> list[dict[str, Any]]:
    """Each repo's latest release, if it was published after `since`. Repos that fail are
    skipped."""
    out = []
    for repo in repos:
        owner, name = repo.split("/", 1)
        try:
            resp = await get_client().get(
                f"{settings.gateway_url}/github/repos/{owner}/{name}/releases/latest",
                headers=_headers(),
                timeout=20.0,
            )
            resp.raise_for_status()
            release = resp.json()
            published = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            continue
        if published > since:
            out.append({"repo": repo, "tag": release.get("tag"), "url": release.get("url"),
                        "published": published.date().isoformat()})
    return out
