"""Where a project stands, computed from its workstreams' cards (plans/13 projects layer).

Pure functions so a project's progress can't drift from the cards. The only stored states are
decisions (a workstream closed, a project paused); everything else here is derived.
"""

from typing import Any

from app.board.stages import STAGES

PROJECT_STATES = ("active", "paused", "done", "dropped")
WORKSTREAM_STATES = ("open", "done", "dropped")

# Ladder statuses, most urgent first. "next" is the first open step nothing blocks.
_NEEDS_ME, _IN_FLIGHT, _BLOCKED, _NEXT, _PLANNED = (
    "needs_me",
    "in_flight",
    "blocked",
    "next",
    "planned",
)


def stage_counts(cards: list[dict[str, Any]]) -> dict[str, int]:
    return {s: sum(c["stage"] == s for c in cards) for s in STAGES}


def _progress(counts: dict[str, int]) -> dict[str, int]:
    """The bar's segments. Workstream cards are never in the inbox (see stages.item_stage)."""
    return {
        "done": counts["done"],
        "in_flight": counts["doing"],
        "needs_me": counts["needs_me"],
        "next": counts["next"],
        "total": counts["done"] + counts["doing"] + counts["needs_me"] + counts["next"],
    }


def _open_status(ws: dict[str, Any], counts: dict[str, int], settled: set[str]) -> str:
    if counts["needs_me"]:
        return _NEEDS_ME
    if counts["doing"]:
        return _IN_FLIGHT
    if any(str(d) not in settled for d in ws["depends_on"]):
        return _BLOCKED
    return _PLANNED


def workstreams(
    rows: list[dict[str, Any]], cards_by_ws: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """Each workstream with its counts and computed status, in ladder order."""
    ordered = sorted(rows, key=lambda w: w["position"])
    settled = {str(w["id"]) for w in ordered if w["state"] != "open"}
    result, next_taken = [], False
    for ws in ordered:
        cards = cards_by_ws.get(str(ws["id"]), [])
        counts = stage_counts(cards)
        status = ws["state"] if ws["state"] != "open" else _open_status(ws, counts, settled)
        if status == _PLANNED and not next_taken:
            status, next_taken = _NEXT, True
        result.append(
            {
                "id": str(ws["id"]),
                "title": ws["title"],
                "summary": ws["summary"],
                "position": ws["position"],
                "state": ws["state"],
                "status": status,
                "depends_on": [str(d) for d in ws["depends_on"]],
                "progress": _progress(counts),
                "cards": cards,
            }
        )
    return result


def _current(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    for wanted in (_NEEDS_ME, _IN_FLIGHT, _NEXT, _BLOCKED):
        found = next((w for w in steps if w["status"] == wanted), None)
        if found:
            return found
    return None


_PHRASE = {
    _NEEDS_ME: "needs you",
    _IN_FLIGHT: "is in flight",
    _NEXT: "is next",
    _BLOCKED: "is blocked",
}


def headline(project: dict[str, Any], steps: list[dict[str, Any]]) -> str:
    """One plain line for when no agent has written an update yet."""
    if project["state"] != "active":
        return {"paused": "Paused.", "done": "Done.", "dropped": "Dropped."}[project["state"]]
    if not steps:
        return "No workstreams yet."
    done = sum(w["state"] == "done" for w in steps)
    live = [w for w in steps if w["state"] != "dropped"]
    current = _current(steps)
    lead = f"{current['title']} {_PHRASE[current['status']]}. " if current else ""
    return f"{lead}{done} of {len(live)} workstreams done."


def project(row: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {
        s: sum(w["progress"][k] for w in steps)
        for s, k in (
            ("done", "done"),
            ("in_flight", "in_flight"),
            ("needs_me", "needs_me"),
            ("next", "next"),
            ("total", "total"),
        )
    }
    current = _current(steps)
    return {
        "id": str(row["id"]),
        "slug": row["slug"],
        "title": row["title"],
        "summary": row["summary"],
        "state": row["state"],
        "rank": row["rank"],
        "repos": row["repos"],
        "plan": row["plan"],
        "latest_update": row["latest_update"],
        "latest_update_at": row["latest_update_at"],
        "updated_at": row["updated_at"],
        "headline": headline(row, steps),
        "progress": counts,
        "steps_done": sum(w["state"] == "done" for w in steps),
        "steps_total": sum(w["state"] != "dropped" for w in steps),
        "current": {"id": current["id"], "title": current["title"], "status": current["status"]}
        if current
        else None,
        "workstreams": steps,
    }
