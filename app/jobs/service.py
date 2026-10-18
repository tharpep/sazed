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


DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
DEFAULT_GITHUB_OWNER = "tharpep"

# mode -> (profile, timeout seconds, max tool calls)
CODE_MODES: dict[str, tuple[str, int, int]] = {
    "review": ("review", 900, 150),
    "change": ("change", 1800, 200),
}

_CHANGE_PROMPT = """You are making a change to the GitHub repository {repo} for Pryce. A copy of its
default branch is in the current directory (no .git). You can read, search, edit and create
files here. You cannot run commands, tests or git; your edits become a pull request that Pryce
reviews and merges himself.

Task: {instruction}
{context}
Keep the change focused: only what the task needs, matching the repo's existing style. Don't
edit .github/workflows (it will be rejected). When done, reply in markdown with a PR
description: what you changed and why, file by file, and anything Pryce should check or test.
If the task can't or shouldn't be done, change nothing and explain why."""

_REVIEW_PROMPT = """You are reviewing the GitHub repository {repo} for Pryce. It is checked out
in the current directory (default branch, last 50 commits). You can read and search files and use
git log/diff/show/blame. You cannot change anything; don't try.

Task: {instruction}
{context}
Reply in markdown: a short summary first, then findings ordered by importance, each with
file:line references and a concrete suggestion. Say what you did not check."""

_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


async def _repo_info(repo: str) -> dict[str, Any]:
    owner, name = repo.split("/", 1)
    try:
        resp = await get_client().get(
            f"{settings.gateway_url}/github/repos/{owner}/{name}",
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=20.0,
        )
    except httpx.HTTPError as e:
        raise JobError(f"couldn't look up {repo}: {e}") from e
    if resp.status_code == 404:
        raise JobError(f"repo {repo} not found")
    if not resp.is_success:
        raise JobError(f"couldn't look up {repo} (HTTP {resp.status_code})")
    return resp.json()


async def create_code_task(
    repo: str,
    instruction: str,
    mode: str,
    requested_by: str,
    model: str | None = None,
    context: str = "",
) -> dict[str, Any]:
    if not settings.worker_key:
        raise JobError("the worker isn't configured here (WORKER_KEY is empty)")
    if mode not in CODE_MODES:
        raise JobError(f"mode must be one of: {', '.join(CODE_MODES)}")
    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if "/" not in repo:
        repo = f"{DEFAULT_GITHUB_OWNER}/{repo}"
    if not _REPO_RE.match(repo):
        raise JobError(f"'{repo}' isn't a repo name like owner/name")
    if repo.lower() in {r.lower() for r in settings.code_task_excluded_repos}:
        raise JobError(f"{repo} is excluded from code tasks")
    info = await _repo_info(repo)
    model = model or DEFAULT_CLAUDE_MODEL
    _check_model("claude", model)
    await _check_cap("claude")
    profile, timeout_s, max_tools = CODE_MODES[mode]
    prompt = (_CHANGE_PROMPT if mode == "change" else _REVIEW_PROMPT).format(
        repo=info.get("full_name") or repo,
        instruction=instruction.strip(),
        context=_context_line(context),
    )
    params = {
        "repo": repo,
        "mode": mode,
        "private": bool(info.get("private")),
        "instruction": instruction.strip(),
        "timeout_s": timeout_s,
        "max_tool_calls": max_tools,
    }
    return await store.create_job(
        kind="code_task",
        agent="claude",
        model=model,
        profile=profile,
        title=_title(f"{repo.split('/', 1)[1]}: {instruction}"),
        prompt=prompt,
        params=params,
        requested_by=requested_by,
        # MCP clients can't use Sazed's chat approval, so their code tasks wait in the console.
        status="needs_approval" if requested_by == "mcp" else "queued",
    )


async def create_follow_up(parent_id: uuid.UUID, instruction: str, requested_by: str) -> dict:
    """A child job that resumes the parent's agent session with a new instruction."""
    parent = await store.get_job(parent_id)
    if parent is None:
        raise JobError("no job with that id")
    if parent["status"] != "succeeded" or not parent.get("agent_session_id"):
        raise JobError("only a finished job with an agent session can be continued")
    await _check_cap(parent["agent"])
    params = dict(parent.get("params") or {})
    params.pop("save_to_kb", None)
    return await store.create_job(
        kind=parent["kind"],
        agent=parent["agent"],
        model=parent["model"],
        profile=parent["profile"],
        title=_title(f"Follow-up: {instruction}"),
        prompt=instruction.strip(),
        params=params,
        requested_by=requested_by,
        parent_id=parent["id"],
        status="needs_approval" if parent["profile"] == "change" else "queued",
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


async def clone_token(repo: str) -> str | None:
    """A read-only, single-repo token from the gateway's GitHub App, for private clones."""
    try:
        resp = await get_client().post(
            f"{settings.gateway_url}/github/app/clone-token",
            json={"repo": repo},
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=30.0,
        )
        resp.raise_for_status()
        return resp.json()["token"]
    except (httpx.HTTPError, KeyError):
        logger.warning("No clone token for %s", repo, exc_info=True)
        return None


async def open_pull_request(job: dict[str, Any], changes: dict[str, str | None]) -> dict:
    """Have the gateway's bot commit the changed files on sazed/<job id> and open a PR."""
    params = job.get("params") or {}
    instruction = params.get("instruction") or job["title"]
    title = _title(instruction, 70)
    body = (
        f"{job.get('result') or ''}\n\n---\n"
        f"Opened by Sazed's worker (Claude Code, `{job['model']}`) for job `{job['id']}`.\n"
        f"Task: {instruction}"
    )
    resp = await get_client().post(
        f"{settings.gateway_url}/github/app/pull-request",
        json={
            "repo": params["repo"],
            "branch": f"sazed/{job['id']}",
            "title": title,
            "body": body[:60_000],
            "commit_message": f"{title}\n\nSazed job {job['id']}",
            "files": changes,
        },
        headers={"X-API-Key": settings.gateway_api_key},
        timeout=120.0,
    )
    if not resp.is_success:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise JobError(f"couldn't open the PR: {detail}")
    return resp.json()


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
