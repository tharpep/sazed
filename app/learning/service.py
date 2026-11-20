"""Learning v1: classify finished jobs and red dispatch rows, and open one guard item when a
lesson repeats."""

import logging
from typing import Any

from app.config import settings
from app.learning import store
from app.learning.classes import CLASSES, classify

logger = logging.getLogger(__name__)

HITS_TO_PROPOSE = 2
EVIDENCE_JOBS = 5


async def record(job: dict[str, Any]) -> str | None:
    """Never raises: a learning failure must not break job completion."""
    try:
        cls = classify(job)
        if cls is None:
            return None
        await store.set_class(job["id"], cls)
        if _teaches(cls):
            await maybe_propose(cls)
        return cls
    except Exception:
        logger.warning("Recording failure class for job %s failed", job.get("id"), exc_info=True)
        return None


async def record_dispatch(plan: str, task: str, state: str, meta: dict) -> str | None:
    """Never raises: a learning failure must not break row intake or the reaper."""
    try:
        cls = await store.dispatch_class(state, meta)
        if cls is not None and _teaches(cls):
            await maybe_propose(cls)
        return cls
    except Exception:
        logger.warning("Recording failure class for task %s/%s failed", plan, task, exc_info=True)
        return None


def _teaches(cls: str) -> bool:
    return CLASSES[cls].family == "lesson" and cls != "unclassified"


def _is_task(hit: dict[str, Any]) -> bool:
    return "plan" in hit


def _source(hit: dict[str, Any]) -> str:
    return f"dispatch:{hit['plan']}:{hit['task']}" if _is_task(hit) else f"job:{hit['id']}"


def _repo(hit: dict[str, Any]) -> str | None:
    return hit.get("repo") if _is_task(hit) else (hit.get("params") or {}).get("repo")


def _evidence_line(job: dict[str, Any]) -> str:
    if _is_task(job):
        return _task_evidence_line(job)
    meta = job.get("meta") or {}
    detail = (
        job.get("error")
        or meta.get("pr_review_error")
        or meta.get("pr_error")
        or (f"CI {meta['ci_state']}" if meta.get("ci_state") else None)
        or (meta.get("check_result") and "repo checks failed")
        or meta.get("result_format")
        or "no files changed"
    )
    finished = job["finished_at"].date().isoformat() if job.get("finished_at") else "?"
    detail = " ".join(str(detail).split())[:160]
    return f"- {str(job['id'])[:8]} {job['title']}: {detail} ({finished})"


def _task_evidence_line(hit: dict[str, Any]) -> str:
    detail = " ".join((hit.get("line") or hit.get("reason") or "no reason").split())[:160]
    finished = hit["finished_at"].date().isoformat()
    return f"- dispatch {hit['plan']}/{hit['task']}: {detail} ({finished})"


def _summary(cls: str, hits: list[dict[str, Any]]) -> str:
    spec = CLASSES[cls]
    today = "\n".join(_evidence_line(j) for j in hits[:EVIDENCE_JOBS])
    runs, run = ("runs", "run") if any(_is_task(h) for h in hits) else ("jobs", "job")
    return (
        f'Goal: stop {runs} from ending as "{spec.label}" ({cls}).\n\n'
        f"Today: this class has been hit {len(hits)} times since the last guard proposal:\n"
        f"{today}\n\n"
        f"Change: add {spec.guard_hint}. Prefer a harness check or test over a prompt rule. "
        "Write the guard as When / Do not / Unless / Instead.\n\n"
        f"First: read the evidence {runs} and confirm they share one cause.\n\n"
        f"Done when: a {run} like the evidence {runs} is stopped or fixed by the guard, pinned "
        "by a test."
    )


async def maybe_propose(cls: str) -> dict[str, Any] | None:
    async with store.class_lock(cls):
        guards = await store.guard_items(cls)
        if any(g["status"] in store.GUARD_OPEN for g in guards):
            return None
        since = guards[0]["created_at"] if guards else None
        hits = await store.hits_since(cls, since)
        if len(hits) < HITS_TO_PROPOSE:
            return None
        spec = CLASSES[cls]
        repos = sorted({r for h in hits if (r := _repo(h))})
        item = await store.create_guard(
            title=f"Guard: {spec.label}"[:200],
            summary=_summary(cls, hits),
            why=f"{cls} hit {len(hits)} times since the last guard proposal.",
            repos=repos,
            sources=[f"failure_class:{cls}", *(_source(h) for h in hits[:EVIDENCE_JOBS])],
        )
    from app.jobs.service import notify_simple  # lazy: the jobs service imports learning

    await notify_simple(
        f"Guard proposed: {spec.label}",
        f"{cls} hit {len(hits)} times; the guard item is in your board inbox.",
        settings.console_url,
        "Sazed",
    )
    return item


async def backfill() -> int:
    n = 0
    for job in await store.unset_settled():
        cls = classify(job)
        if cls and await store.set_class(job["id"], cls):
            n += 1
    return n


async def summary() -> dict[str, Any]:
    families: dict[str, dict[str, dict[str, int]]] = {"stop": {}, "lesson": {}}
    for row in await store.class_counts():
        spec = CLASSES.get(row["cls"])
        if spec:
            families[spec.family][row["cls"]] = {
                "total": row["total"],
                "last_30d": row["last_30d"],
            }
    return {
        "classes": families,
        "unclassified": [
            {"id": str(r["id"]), "title": r["title"]} for r in await store.unclassified()
        ],
        "open_guards": [
            {"id": str(g["id"]), "title": g["title"], "status": g["status"]}
            for g in await store.open_guards()
        ],
    }
