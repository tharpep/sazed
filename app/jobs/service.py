"""Job rules that need judgment: prompts, model allowlists, daily caps, and what happens on finish.

The worker is a generic runner; everything about what a job may do is decided here.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from app import policy
from app.config import settings
from app.db import get_pool
from app.http_client import get_client
from app.jobs import change_result, store

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


# Appended to every job prompt Sazed writes (plans/11 phase 1, job controls). A reply whose last
# line matches becomes a needs_input job: paused, session kept, resumed by continue_job.
_ASK_RULE = (
    "\n\nIf you can't continue without a fact or a decision only Pryce can give, stop and make "
    "the last line of your reply exactly: NEEDS_INPUT: <one short question>. Don't guess at "
    "anything that matters."
)
_NEEDS_INPUT_RE = re.compile(r"^NEEDS_INPUT:\s*(\S.*?)\s*$")


def needs_input_question(result: str | None) -> str | None:
    """The question a worker asked, if its reply ends with a NEEDS_INPUT line."""
    lines = [line.strip() for line in (result or "").splitlines() if line.strip()]
    if not lines:
        return None
    match = _NEEDS_INPUT_RE.match(lines[-1].strip("*`_ "))  # tolerate markdown emphasis
    return match.group(1)[:500] if match else None


def _short(value: Any, limit: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def recent_steps(events: list[dict[str, Any]], limit: int = 5) -> list[str]:
    """The last few tool calls in a job's event stream, as 'tool: first argument' lines.

    Claude Code streams assistant messages with tool_use blocks; agy streams step_update events
    with step_type "tool" (one per state change, so they're keyed by step index)."""
    steps: dict[Any, str] = {}
    for i, row in enumerate(events):
        ev = row.get("event") or {}
        update = ev.get("step_update") or {}
        if update.get("step_type") == "tool" and update.get("tool_info"):
            info = update["tool_info"]
            params = info.get("parameters") or {}
            first = next(iter(params.values()), "") if isinstance(params, dict) else params
            steps[("agy", update.get("step_index", i))] = f"{info.get('name')}: {_short(first)}"
        elif ev.get("type") == "assistant":
            for j, block in enumerate((ev.get("message") or {}).get("content") or []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    args = block.get("input") or {}
                    first = next(iter(args.values()), "") if isinstance(args, dict) else args
                    steps[("claude", i, j)] = f"{block.get('name')}: {_short(first)}"
    return list(steps.values())[-limit:]


class JobError(ValueError):
    """A job request that breaks a rule (unknown model, cap reached, ...)."""


def _require(action: policy.StartJob) -> None:
    """Raise JobError with the policy's reason if it blocks this job."""
    decision = policy.decide(action)
    if decision.verdict != "allow":
        raise JobError(decision.reason)


def _check_model(agent: str, model: str) -> None:
    _require(policy.StartJob(agent, requested_by="", model=model))


async def _check_cap(agent: str, requested_by: str) -> None:
    if not policy.counts_toward_cap(requested_by):
        return
    used = await store.count_recent(agent)
    _require(policy.StartJob(agent, requested_by, used_today=used))


def _title(text: str, limit: int = 80) -> str:
    # The first line only: a backlog instruction's first line is the item title, then the prompt.
    one_line = re.sub(r"\s+", " ", text.strip().split("\n", 1)[0]).strip()
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
    await _check_cap("agy", requested_by)
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
    prompt = (
        _RESEARCH_PROMPT.format(
            question=question.strip(), context=_context_line(context), shape=shape
        )
        + _ASK_RULE
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
    prompt = (
        _SECOND_OPINION_PROMPT.format(subject=subject.strip(), context=_context_line(context))
        + _ASK_RULE
    )
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
files here. You can't use git or the network, and you can run commands only if a note at the top
says so; your edits become a pull request that Pryce reviews and merges himself.

Task: {instruction}
{context}
Keep the change focused: only what the task needs, matching the repo's existing style. Don't
edit .github/workflows (it will be rejected).

"""
# The format rule is part of the template: its braces are doubled for .format().
_CHANGE_PROMPT += change_result.FORMAT_RULE

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
    _require(policy.StartJob("claude", requested_by, repo=repo))
    info = await _repo_info(repo)
    model = model or DEFAULT_CLAUDE_MODEL
    _check_model("claude", model)
    await _check_cap("claude", requested_by)
    profile, timeout_s, max_tools = CODE_MODES[mode]
    prompt = (_CHANGE_PROMPT if mode == "change" else _REVIEW_PROMPT).format(
        repo=info.get("full_name") or repo,
        instruction=instruction.strip(),
        context=_context_line(context),
    ) + _ASK_RULE
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
        # MCP clients can't use Sazed's chat approval, so their code tasks wait in the console;
        # so do change tasks an automation starts (nobody approved those in a chat).
        status=(
            "needs_approval"
            if requested_by == "mcp" or (mode == "change" and requested_by.startswith("automation"))
            else "queued"
        ),
    )


# Worker stops that don't mean "give up" (sazed_worker.jobs.PAUSING_STOPS): a change job stopped
# this way is paused with its work so far on a draft PR (plans/10 P1.7). A rate_limit pause (Pryce's
# plan refused the run, P1.2) resumes on its own once the plan resets.
PAUSING_STOPS = ("timed_out", "tool_cap", "shutdown", "rate_limit")

_CONTINUABLE = ("succeeded", "needs_input", "cancelled", "timed_out", "paused")

_RESUME_INSTRUCTION = (
    "You were stopped before finishing ({reason}). Your edits so far are still in the working "
    "copy. Carry on with the task from where you left off."
)


async def create_follow_up(
    parent_id: uuid.UUID,
    instruction: str,
    requested_by: str,
    extra_params: dict[str, Any] | None = None,
    approved: bool = False,
) -> dict:
    """A child job that resumes the parent's agent session with a new instruction.

    A follow-up on a change job needs approval again, except CI fix-ups (they fix a PR Pryce
    already approved and are bounded: ci_watch.MAX_FIX_ATTEMPTS, daily cap) and resumes of a
    paused job (`approved`: the same task, carried on).
    """
    parent = await store.get_job(parent_id)
    if parent is None:
        raise JobError("no job with that id")
    # needs_input: answering its question. cancelled / timed_out / paused: stop and redirect,
    # resuming the session the worker reported (store.keep_session).
    if parent["status"] not in _CONTINUABLE or not parent.get("agent_session_id"):
        raise JobError(
            "only a finished, paused (needs_input) or stopped job with an agent session can be "
            "continued"
        )
    await _check_cap(parent["agent"], requested_by)
    params = dict(parent.get("params") or {}) | (extra_params or {})
    params.pop("save_to_kb", None)
    return await store.create_job(
        kind=parent["kind"],
        agent=parent["agent"],
        model=parent["model"],
        profile=parent["profile"],
        title=_title(f"Follow-up: {instruction}"),
        prompt=instruction.strip()
        + (change_result.FOLLOW_UP_RULE if parent["profile"] == "change" else ""),
        params=params,
        requested_by=requested_by,
        parent_id=parent["id"],
        status=(
            "needs_approval"
            if parent["profile"] == "change"
            and not approved
            and not requested_by.startswith("ci-fix")
            else "queued"
        ),
    )


def _until(epoch: Any) -> str:
    """'in about 2h 15m' for a reset time in epoch seconds."""
    if not isinstance(epoch, (int, float)):
        return "after the plan resets"
    minutes = max(0, int((epoch - datetime.now(UTC).timestamp()) // 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    span = f"{days}d {hours}h" if days else f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return f"in about {span}"


async def resume_rate_limited() -> int:
    """Resume change jobs paused by the plan limit once it has reset (plans/10 P1.2). Each
    resume keeps its parent's requester, so its priority and cap rules don't change; a CI fix-up
    keeps its attempt number. Returns how many were resumed."""
    resumed = 0
    for job in await store.due_rate_limit_resumes():
        try:
            await resume_job(job["id"], job["requested_by"])
            resumed += 1
        except JobError as e:
            logger.warning("Job %s not resumed after the plan limit: %s", job["id"], e)
    return resumed


async def resume_job(job_id: uuid.UUID, requested_by: str) -> dict:
    """Carry on with a paused job: same session, same working copy, same draft PR (plans/10
    P1.7). Pryce approved the task already, so it queues without asking again."""
    job = await store.get_job(job_id)
    if job is None:
        raise JobError("no job with that id")
    if job["status"] != "paused":
        raise JobError("only a paused job can be resumed")
    reason = ((job.get("meta") or {}).get("pause_reason") or "stopped").replace("_", " ")
    return await create_follow_up(
        job_id, _RESUME_INSTRUCTION.format(reason=reason), requested_by, approved=True
    )


async def wait_for(job_id: uuid.UUID, seconds: float) -> dict[str, Any] | None:
    """Poll until the job settles (finishes or pauses on a question) or `seconds` pass.
    Returns the settled row, else None."""
    deadline = asyncio.get_running_loop().time() + seconds
    while True:
        job = await store.get_job(job_id)
        if job and job["status"] in store.SETTLED_STATUSES:
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


_SHORT_LABEL = {"research": "Research", "second_opinion": "2nd opinion", "code_task": "Code review"}


def _label(job: dict[str, Any]) -> str:
    return job["kind"].replace("_", " ").capitalize()


def gist(text: str, limit: int = 220) -> str:
    """The first sentence or two of a result, as plain text (no headings, lists or links)."""
    lines = [
        _plain(line).strip(" -*>")
        for line in text.splitlines()  # filter before _plain, which strips the "#" of headings
        if line.strip()
        and not line.lstrip(" -*>").startswith(("#", "|", "```", "Sources", "http", "["))
    ]
    flat = " ".join(lines)
    sentences = re.split(r"(?<=[.!?])\s+", flat)
    out = ""
    for sentence in sentences:
        if len(out) + len(sentence) > limit:
            break
        out = f"{out} {sentence}".strip()
    return out or flat[:limit]


async def notify_simple(
    title: str, body: str, url: str | None = None, url_title: str | None = None
) -> None:
    """One Pushover message. Keep it a notification: short title, a sentence or two, and the
    link as a button (the gateway trims anything longer)."""
    payload: dict[str, Any] = {"title": title, "message": body or "(no details)", "priority": 0}
    if url:
        payload |= {"url": url, "url_title": url_title or "Open"}
    try:
        await get_client().post(
            f"{settings.gateway_url}/notify",
            json=payload,
            headers={"X-API-Key": settings.gateway_api_key},
            timeout=30.0,
        )
    except httpx.HTTPError:
        logger.warning("Notification failed: %s", title, exc_info=True)


async def _notify(job: dict[str, Any]) -> None:
    meta = job.get("meta") or {}
    label = _SHORT_LABEL.get(job["kind"], _label(job))
    if job["status"] == "needs_input":
        await notify_simple(
            f"{label} needs your input",
            f"{job['title']}\n{meta.get('question') or '(no question text)'}",
        )
    elif job["status"] == "paused":
        reason = (meta.get("pause_reason") or "stopped").replace("_", " ")
        if meta.get("pr_url"):
            saved = f"Draft PR #{meta.get('pr_number')} has the work so far"
        else:
            saved = "No files were changed yet"
        next_step = "resume it from Sazed (Jobs)"
        if meta.get("pause_reason") == "rate_limit":
            reason = "plan limit"
            next_step = (
                f"it resumes on its own {_until(meta.get('resume_after'))}, and other Claude jobs "
                "wait until then"
            )
        await notify_simple(
            f"{label} paused ({reason})",
            f"{job['title']}. {saved}; {next_step}.",
            meta.get("pr_url"),
            f"Open draft PR #{meta.get('pr_number')}" if meta.get("pr_url") else None,
        )
    elif job["status"] == "succeeded" and meta.get("pr_url") and meta.get("pr_draft"):
        repo = (job.get("params") or {}).get("repo", "").split("/")[-1]
        await notify_simple(
            f"Draft PR #{meta.get('pr_number')} · {repo}",
            f"{job['title']}. The repo's tests still fail with the change, so it's a draft.",
            meta["pr_url"],
            f"Open draft PR #{meta.get('pr_number')} on GitHub",
        )
    elif job["status"] == "succeeded" and meta.get("pr_url"):
        repo = (job.get("params") or {}).get("repo", "").split("/")[-1]
        verb = "updated" if meta.get("pr_updated") else "opened"
        await notify_simple(
            f"PR #{meta.get('pr_number')} {verb} · {repo}",
            f"{job['title']}. CI is running; you'll hear when it's green.",
            meta["pr_url"],
            f"Open PR #{meta.get('pr_number')} on GitHub",
        )
    elif job["status"] == "succeeded":
        await notify_simple(f"{label} done", f"{job['title']}\n{gist(job.get('result') or '')}")
    else:
        status = job["status"].replace("_", " ")
        reason = job.get("error") or "no details"
        await notify_simple(f"{label} {status}", f"{job['title']}: {reason}")


def session_note(job: dict[str, Any]) -> str:
    """Sazed's own note for the conversation that asked. Never the worker's text: job output is
    untrusted (it may quote web pages), so it stays in the job, read through job_status as data,
    and doesn't enter the history as Sazed's words (plans/11 phase 1, job controls)."""
    label, title, job_id = _label(job), job["title"], job["id"]
    if job["status"] == "succeeded":
        return f"**{label} finished:** {title}. The result is in job `{job_id}`; ask me for it."
    if job["status"] == "needs_input":
        return (
            f"**{label} is waiting on you:** {title}. It asked a question (sent to your phone); "
            f"ask me to show it, or answer and I'll continue job `{job_id}`."
        )
    if job["status"] == "paused":
        reason = ((job.get("meta") or {}).get("pause_reason") or "stopped").replace("_", " ")
        return (
            f"**{label} paused** ({reason}): {title}. Its work so far is saved; ask me to resume "
            f"job `{job_id}`."
        )
    status = job["status"].replace("_", " ")
    return f"**{label} didn't finish** ({title}): {status}. Details are in job `{job_id}`."


async def _post_to_session(job: dict[str, Any]) -> None:
    """Add Sazed's note about the outcome to the conversation that asked."""
    kind, _, raw_id = (job.get("requested_by") or "").partition(":")
    if kind not in ("session", "automation", "claude", "think"):
        return
    try:
        session_id = uuid.UUID(raw_id)
    except ValueError:
        return
    text = session_note(job)
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


def failing_tests_section(failing: list[str]) -> str:
    """The PR description's note when the worker's test run still fails after the fix round."""
    lines = "\n".join(f"- {line.removeprefix('tests: ')}" for line in failing)
    return (
        "\n\n### Tests still failing\n"
        "The worker ran the repo's tests: these pass without this change and fail with it, so "
        f"the PR is a draft.\n{lines}"
    )


async def _parent_meta(job: dict[str, Any]) -> dict[str, Any]:
    if not job.get("parent_id"):
        return {}
    parent = await store.get_job(job["parent_id"])
    return (parent or {}).get("meta") or {}


async def open_pull_request(
    job: dict[str, Any], changes: dict[str, str | None], draft: bool = False
) -> dict:
    """Have the gateway's bot commit the changed files and open (or update) the PR.

    A follow-up keeps working on its parent's PR branch; a new change gets sazed/<job id>.
    `draft`: a paused job's work so far (plans/10 P1.7). When a resume of a draft finishes, the
    draft gets this job's description and is marked ready.
    """
    params = job.get("params") or {}
    instruction = params.get("instruction") or job["title"]
    title = _title(instruction, 70)
    parent = await _parent_meta(job)
    # A follow-up's commit describes the follow-up; the PR keeps the original title.
    commit_title = _title(job["prompt"], 70) if job.get("parent_id") else title
    if draft and (job.get("meta") or {}).get("pause_reason"):
        reason = (job.get("meta") or {})["pause_reason"].replace("_", " ")
        text = (
            f"**Draft: paused before it finished** ({reason}). This is the work so far; resuming "
            "the job continues it here and marks the PR ready."
        )
    else:
        text = job.get("result") or ""
    body = (
        f"{text}\n\n---\n"
        f"Opened by Sazed's worker (Claude Code, `{job['model']}`) for job `{job['id']}`.\n"
        f"Task: {instruction}"
    )
    resp = await get_client().post(
        f"{settings.gateway_url}/github/app/pull-request",
        json={
            "repo": params["repo"],
            "branch": parent.get("branch") or f"sazed/{job['id']}",
            "title": title,
            "body": body[:60_000],
            "commit_message": f"{commit_title}\n\nSazed job {job['id']}",
            "files": changes,
            "draft": draft,
            "ready": not draft and bool(parent.get("pr_draft")),
            # A follow-up continues its parent's PR; the gateway refuses if it was closed.
            "pr_number": parent.get("pr_number"),
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


def should_notify(job: dict[str, Any]) -> bool:
    """Notify when the requester stopped waiting (notify was set at hand-off), or when a job
    asks a question or pauses and nobody is waiting on the call: MCP, console and think jobs.
    A chat that's still waiting reads the question from the tool result; automation runs alert
    on their own.
    """
    if job.get("notify"):
        return True
    requested_by = job.get("requested_by") or ""
    return job["status"] in ("needs_input", "paused") and not requested_by.startswith(
        ("session:", "automation")
    )


async def on_finished(job: dict[str, Any]) -> None:
    """Everything that follows a job settling (final, or paused on a question); each step fails
    on its own."""
    await _save_to_kb(job)
    if should_notify(job):
        # Nobody is waiting on the call: tell Pryce, and leave Sazed's note where it was asked.
        await _notify(job)
        try:
            await _post_to_session(job)
        except Exception:
            logger.warning("Posting job %s back to its session failed", job["id"], exc_info=True)
