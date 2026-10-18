"""Job rules that need judgment: prompts, model allowlists, daily caps, and what happens on finish.

The worker is a generic runner; everything about what a job may do is decided here.
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Any

import httpx

from app.config import settings
from app.db import get_pool
from app.http_client import get_client
from app.jobs import store

logger = logging.getLogger(__name__)

# Pryce's call (2026-10-18): Gemini 3.8 Flash at its highest effort suits research best. The effort
# is part of the model id ("-high" is Flash's top level); agy rejects a conflicting --effort.
DEFAULT_AGY_MODEL = "gemini-3.8-flash-high"

# depth -> (timeout seconds, max tool calls, answer shape)
RESEARCH_DEPTHS: dict[str, tuple[int, int, str]] = {
    "quick": (300, 30, "a direct answer in 2-6 sentences"),
    "deep": (
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

_SECOND_OPINION_PROMPT = """You are giving Pryce an independent second opinion. Another assistant
(Sazed, a Claude model) or Pryce wrote what follows; judge it on its merits and be direct.
You may use the search_web and read_url_content tools to check facts. Never run commands, edit
files, or open a browser.

{subject}
{context}
Reply in markdown:
1. Verdict: one or two sentences (sound / sound with changes / rethink).
2. What's right about it.
3. Concerns: risks, mistakes, missing cases, ordered by importance.
4. What you would do differently, concretely.
If you checked facts online, end with a "Sources:" list."""


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


def _context_line(context: str) -> str:
    return f"\nContext from the conversation: {context.strip()}\n" if context.strip() else ""


async def _create_agy_job(
    kind: str, title: str, prompt: str, params: dict[str, Any], requested_by: str, model: str | None
) -> dict[str, Any]:
    if not settings.worker_key:
        raise JobError("the worker isn't configured here (WORKER_KEY is empty)")
    model = model or DEFAULT_AGY_MODEL
    _check_model("agy", model)
    await _check_cap("agy")
    return await store.create_job(
        kind=kind,
        agent="agy",
        model=model,
        profile="research",
        title=_title(title),
        prompt=prompt,
        params=params,
        requested_by=requested_by,
    )


async def create_research(
    question: str,
    depth: str,
    requested_by: str,
    model: str | None = None,
    context: str = "",
    save_to_kb: bool = False,
) -> dict[str, Any]:
    if depth not in RESEARCH_DEPTHS:
        raise JobError(f"depth must be one of: {', '.join(RESEARCH_DEPTHS)}")
    timeout_s, max_tools, shape = RESEARCH_DEPTHS[depth]
    prompt = _RESEARCH_PROMPT.format(
        question=question.strip(), context=_context_line(context), shape=shape
    )
    params = {
        "depth": depth,
        "timeout_s": timeout_s,
        "max_tool_calls": max_tools,
        "save_to_kb": save_to_kb,
        "question": question.strip(),
    }
    return await _create_agy_job("research", question, prompt, params, requested_by, model)


async def create_second_opinion(
    subject: str, requested_by: str, model: str | None = None, context: str = ""
) -> dict[str, Any]:
    prompt = _SECOND_OPINION_PROMPT.format(subject=subject.strip(), context=_context_line(context))
    params = {"timeout_s": 300, "max_tool_calls": 20}
    return await _create_agy_job("second_opinion", subject, prompt, params, requested_by, model)


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


# ---------------------------------------------------------------- on finish


def _plain(text: str) -> str:
    """Markdown to plain text for Pushover."""
    text = re.sub(r"\*{1,3}(.+?)\*{1,3}", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _label(job: dict[str, Any]) -> str:
    return job["kind"].replace("_", " ").capitalize()


async def _notify(job: dict[str, Any]) -> None:
    if job["status"] == "succeeded":
        title, body = f"{_label(job)} done: {job['title']}", _plain(job.get("result") or "(empty)")
    else:
        title = f"{_label(job)} {job['status'].replace('_', ' ')}: {job['title']}"
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


async def _post_to_session(job: dict[str, Any]) -> None:
    """Append the outcome to the conversation that asked, so it's there when Pryce looks."""
    requested_by = job.get("requested_by") or ""
    if not requested_by.startswith("session:"):
        return
    try:
        session_id = uuid.UUID(requested_by.removeprefix("session:"))
    except ValueError:
        return
    if job["status"] == "succeeded":
        text = f"**{_label(job)} finished:** {job['title']}\n\n{job.get('result') or ''}"
    else:
        reason = job.get("error") or job["status"].replace("_", " ")
        text = f"**{_label(job)} didn't finish** ({job['title']}): {reason}"
    # Content blocks so it merges cleanly with an adjacent assistant turn on the next load.
    await get_pool().execute(
        "INSERT INTO messages (session_id, role, content) VALUES ($1, 'assistant', $2)",
        session_id,
        json.dumps([{"type": "text", "text": text}]),
    )


async def _save_to_kb(job: dict[str, Any]) -> None:
    params = job.get("params") or {}
    if not params.get("save_to_kb") or job["status"] != "succeeded" or not job.get("result"):
        return
    question = params.get("question") or job["title"]
    content = f"# {question}\n\n{job['result']}\n"
    try:
        resp = await get_client().post(
            f"{settings.gateway_url}/kb/ingest/text",
            json={
                "title": f"Research: {job['title']}.md",
                "content": content,
                "category": "research",
                "source_id": f"job-{job['id']}",
            },
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=90.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError:
        logger.warning("KB save failed for job %s", job["id"], exc_info=True)


async def on_finished(job: dict[str, Any]) -> None:
    """Everything that follows a job reaching a final status; each step fails on its own."""
    await _save_to_kb(job)
    if job.get("notify"):
        # The requester stopped waiting: tell Pryce, and put the answer where it was asked.
        await _notify(job)
        try:
            await _post_to_session(job)
        except Exception:
            logger.warning("Posting job %s back to its session failed", job["id"], exc_info=True)
