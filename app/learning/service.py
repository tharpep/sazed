"""Learning v1: classify finished jobs, and open one guard item when a lesson repeats."""

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
        if CLASSES[cls].family == "lesson" and cls != "unclassified":
            await maybe_propose(cls)
        return cls
    except Exception:
        logger.warning("Recording failure class for job %s failed", job.get("id"), exc_info=True)
        return None


def _evidence_line(job: dict[str, Any]) -> str:
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


def _summary(cls: str, hits: list[dict[str, Any]]) -> str:
    spec = CLASSES[cls]
    today = "\n".join(_evidence_line(j) for j in hits[:EVIDENCE_JOBS])
    return (
        f'Goal: stop jobs from ending as "{spec.label}" ({cls}).\n\n'
        f"Today: this class has been hit {len(hits)} times since the last guard proposal:\n"
        f"{today}\n\n"
        f"Change: add {spec.guard_hint}. Prefer a harness check or test over a prompt rule. "
        "Write the guard as When / Do not / Unless / Instead.\n\n"
        "First: read the evidence jobs and confirm they share one cause.\n\n"
        f"Done when: a job like the evidence jobs is stopped or fixed by the guard, pinned by a "
        "test."
    )


async def maybe_propose(cls: str) -> dict[str, Any] | None:
    guards = await store.guard_items(cls)
    if any(g["status"] in store.GUARD_OPEN for g in guards):
        return None
    since = guards[0]["created_at"] if guards else None
    hits = await store.hits_since(cls, since)
    if len(hits) < HITS_TO_PROPOSE:
        return None
    spec = CLASSES[cls]
    repos = sorted({r for j in hits if (r := (j.get("params") or {}).get("repo"))})
    item = await store.create_guard(
        title=f"Guard: {spec.label}"[:200],
        summary=_summary(cls, hits),
        why=f"{cls} hit {len(hits)} times since the last guard proposal.",
        repos=repos,
        sources=[f"failure_class:{cls}", *(f"job:{j['id']}" for j in hits[:EVIDENCE_JOBS])],
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
