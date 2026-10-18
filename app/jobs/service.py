"""Job rules that need judgment: prompts, model allowlists, daily caps, and finish notifications.

The worker is a generic runner; everything about what a job may do is decided here.
"""

import asyncio
import logging
import re
import uuid
from typing import Any

import httpx

from app.config import settings
from app.http_client import get_client
from app.jobs import store

logger = logging.getLogger(__name__)

# depth -> (model, effort, timeout seconds, max tool calls, answer shape)
RESEARCH_DEPTHS: dict[str, tuple[str, str | None, int, int, str]] = {
    # Pryce's call (2026-10-18): Gemini 3.8 Flash at the highest effort suits research best.
    "quick": ("gemini-3.8-flash-high", "high", 300, 30, "a direct answer in 2-6 sentences"),
    "deep": (
        "gemini-3.8-flash-high",
        "max",
        600,
        80,
        "a thorough report: a short summary first, then sections covering the important details, "
        "trade-offs and any disagreement between sources",
    ),
}

_RESEARCH_PROMPT = """You are a research assistant answering a question for Pryce.
Use only the search_web and read_url_content tools. Never run commands, edit files, or open a
browser. Search as much as you need, prefer primary and recent sources, and say so when sources
disagree or when you could not confirm something.

Question: {question}
{context}
Reply in markdown: {shape}. End with a "Sources:" list of the URLs you relied on."""


class JobError(ValueError):
    """A job request that breaks a rule (unknown model, cap reached, ...)."""


def _check_model(agent: str, model: str) -> None:
    allowed = settings.agy_models if agent == "agy" else settings.claude_models
    if model not in allowed:
        raise JobError(f"model '{model}' is not allowed for {agent}. Allowed: {', '.join(allowed)}")


async def _check_cap(agent: str) -> None:
    cap = settings.jobs_daily_cap_agy if agent == "agy" else settings.jobs_daily_cap_claude
    used = await store.count_recent(agent)
    if used >= cap:
        raise JobError(
            f"daily cap reached for {agent} ({used}/{cap} jobs in the last 24 hours). "
            "It frees up as older jobs age out."
        )


def _title(text: str, limit: int = 80) -> str:
    one_line = re.sub(r"\s+", " ", text).strip()
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


async def create_research(
    question: str,
    depth: str,
    requested_by: str,
    model: str | None = None,
    context: str = "",
) -> dict[str, Any]:
    if not settings.worker_key:
        raise JobError("the worker isn't configured here (WORKER_KEY is empty)")
    if depth not in RESEARCH_DEPTHS:
        raise JobError(f"depth must be one of: {', '.join(RESEARCH_DEPTHS)}")
    default_model, effort, timeout_s, max_tools, shape = RESEARCH_DEPTHS[depth]
    model = model or default_model
    _check_model("agy", model)
    await _check_cap("agy")
    prompt = _RESEARCH_PROMPT.format(
        question=question.strip(),
        context=f"\nContext from the conversation: {context.strip()}\n" if context.strip() else "",
        shape=shape,
    )
    params: dict[str, Any] = {"depth": depth, "timeout_s": timeout_s, "max_tool_calls": max_tools}
    if effort:
        params["effort"] = effort
    return await store.create_job(
        kind="research",
        agent="agy",
        model=model,
        profile="research",
        title=_title(question),
        prompt=prompt,
        params=params,
        requested_by=requested_by,
    )


async def wait_for(job_id: uuid.UUID, seconds: float) -> dict[str, Any] | None:
    """Poll until the job finishes or `seconds` pass. Returns the finished row, else None."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        job = await store.get_job(job_id)
        if job and job["status"] in store.FINAL_STATUSES:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(2)


def _plain(text: str) -> str:
    """Markdown to plain text for Pushover."""
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


async def notify_finished(job: dict[str, Any]) -> None:
    """Pushover for a finished job whose requester stopped waiting for it."""
    if not job.get("notify"):
        return
    label = job["kind"].replace("_", " ").capitalize()
    if job["status"] == "succeeded":
        title, body = f"{label} done: {job['title']}", _plain(job.get("result") or "(empty)")
    else:
        title = f"{label} {job['status'].replace('_', ' ')}: {job['title']}"
        body = job.get("error") or "No details."
    try:
        await get_client().post(
            f"{settings.gateway_url}/notify",
            json={"title": title[:250], "message": body[:1024], "priority": 0},
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=30.0,
        )
    except httpx.HTTPError:
        logger.warning("Job notification failed for %s", job["id"], exc_info=True)
