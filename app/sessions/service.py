"""Claude Code sessions reporting through hooks (slice 3): ingest, card links and the reads.

Hooks post events; each event updates its session's newest-wins state and lands as a step under
its agent. Links to cards, "not heard from" and the open question are worked out on read, so a
card renamed or a branch relinked never leaves a stale link behind.
"""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, ValidationError, model_validator

from app.config import settings
from app.sessions import store

TEXT_MAX = 300

Kind = Literal[
    "start", "prompt", "tool", "agent_start", "agent_stop", "doing", "question", "stop", "end"
]
DoingSource = Literal["task", "commit", "pr"]

# The session state each kind leaves behind. A session waiting on Pryce (start, stop) is idle.
_STATE: dict[str, str] = {
    "start": "idle",
    "prompt": "running",
    "tool": "running",
    "agent_start": "running",
    "agent_stop": "running",
    "doing": "running",
    "question": "running",
    "stop": "idle",
    "end": "ended",
}
_CLEARS_QUESTION = {"prompt", "tool", "stop", "end"}

# Every session on a default branch would match a card that wrongly names it.
_UNMATCHED_BRANCHES = {"main", "master"}


# 2100-01-01. Bounds ts so a millisecond value is rejected alone instead of overflowing datetime.
TS_MAX = 4_102_444_800


def _cut(limit: int | None):
    def cut(value: Any) -> Any:
        # Postgres text cannot hold NUL; one in a result line would fail the whole batch.
        return value.replace("\x00", "")[:limit] if isinstance(value, str) else value

    return BeforeValidator(cut)


Short = Annotated[str | None, _cut(200)]
Line = Annotated[str | None, _cut(TEXT_MAX)]


class EventIn(BaseModel):
    session_id: Annotated[str, _cut(None)] = Field(..., min_length=1, max_length=200)
    ts: float = Field(..., gt=0, lt=TS_MAX)
    seq: int = Field(..., ge=0, le=2_000_000_000)
    kind: Kind
    agent_id: Short = None
    agent_type: Short = None
    tool: Annotated[str | None, _cut(100)] = None
    target: Line = None
    text: Annotated[str, _cut(TEXT_MAX)] = ""
    error: bool = False
    doing_source: DoingSource | None = None
    host: Short = None
    cwd: Line = None
    repo: Short = None
    branch: Short = None
    card_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "EventIn":
        if not math.isfinite(self.ts):
            raise ValueError("ts must be finite")
        if self.kind == "tool" and not self.tool:
            raise ValueError("a tool event needs tool")
        if self.kind == "doing" and not (self.text and self.doing_source):
            raise ValueError("a doing event needs text and doing_source")
        if self.kind in ("agent_start", "agent_stop") and not self.agent_id:
            raise ValueError(f"an {self.kind} event needs agent_id")
        if self.kind == "prompt":
            # Prompt text never leaves the machine; a hook that sends it anyway is not stored.
            self.text = ""
        if self.card_id is not None:
            self.card_id = str(parse_card_id(self.card_id))
        return self


def parse_card_id(value: str) -> uuid.UUID:
    raw = value.removeprefix("item:")
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise ValueError("card_id must be item:<uuid> or a uuid") from None


def parse_batch(raw: list[Any]) -> tuple[list[EventIn], list[dict[str, Any]]]:
    """Valid events, and one rejection per malformed event; one bad event never sinks the rest."""
    valid, rejected = [], []
    for index, item in enumerate(raw):
        try:
            valid.append(EventIn.model_validate(item))
        except ValidationError as exc:
            reason = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'event'}: {e['msg']}" for e in exc.errors()
            )
            rejected.append({"index": index, "error": reason})
    return valid, rejected


def _latest(events: list[EventIn]) -> EventIn | None:
    return max(events, key=lambda e: (e.ts, e.seq), default=None)


def _last(events: list[EventIn], field: str) -> Any:
    found = [e for e in events if getattr(e, field) is not None]
    return getattr(_latest(found), field) if found else None


def reduce(events: list[EventIn]) -> list[dict[str, Any]]:
    """One session row per session in the batch, holding the batch's newest value of each
    newest-wins column with the (ts, seq) that set it; the upsert keeps whichever is newer."""
    by_session: dict[str, list[EventIn]] = {}
    for e in events:
        by_session.setdefault(e.session_id, []).append(e)
    rows = []
    for session_id, batch in by_session.items():
        last = _latest(batch)
        doing = _latest([e for e in batch if e.kind == "doing"])
        question = _latest([e for e in batch if e.kind == "question"])
        # A subagent working on while the main thread waits on a prompt has not answered it.
        cleared = _latest([e for e in batch if e.kind in _CLEARS_QUESTION and not e.agent_id])
        state = _STATE[last.kind]
        rows.append(
            {
                "session_id": session_id,
                "host": _last(batch, "host"),
                "cwd": _last(batch, "cwd"),
                "repo": _last(batch, "repo"),
                "branch": _last(batch, "branch"),
                "card_id": uuid.UUID(c) if (c := _last(batch, "card_id")) else None,
                "started_at": datetime.fromtimestamp(min(e.ts for e in batch), UTC),
                "state": state,
                "state_ts": last.ts,
                "state_seq": last.seq,
                "ended_at": datetime.fromtimestamp(last.ts, UTC) if state == "ended" else None,
                "doing": doing.text if doing else None,
                "doing_source": doing.doing_source if doing else None,
                "doing_ts": doing.ts if doing else None,
                "doing_seq": doing.seq if doing else None,
                "question": question.text if question else None,
                "question_ts": question.ts if question else None,
                "question_seq": question.seq if question else None,
                "cleared_ts": cleared.ts if cleared else None,
                "cleared_seq": cleared.seq if cleared else None,
            }
        )
    return rows


def event_rows(events: list[EventIn]) -> list[dict[str, Any]]:
    return [
        {
            "session_id": e.session_id,
            "agent_id": e.agent_id or "",
            "agent_type": e.agent_type,
            "ts": e.ts,
            "seq": e.seq,
            "kind": e.kind,
            "tool": e.tool,
            "target": e.target,
            "text": e.text,
            "error": e.error,
        }
        for e in events
    ]


async def ingest(raw: list[Any]) -> dict[str, Any]:
    valid, rejected = parse_batch(raw)
    stored = await store.ingest(reduce(valid), event_rows(valid)) if valid else 0
    return {"accepted": len(valid), "stored": stored, "rejected": rejected}


# Reads


def status(session: dict[str, Any], now: datetime) -> str:
    """The reported state, or "not_heard_from" once it has been silent too long: a running session
    past SESSION_RUNNING_SILENT_S, an idle one (waiting on Pryce, so quiet by design) past
    SESSION_IDLE_SILENT_S. Nothing is reaped or invented: the last time stays as it was."""
    if session["state"] == "ended":
        return "ended"
    silent = (now - session["last_heard_at"]).total_seconds()
    limit = (
        settings.session_running_silent_s
        if session["state"] == "running"
        else settings.session_idle_silent_s
    )
    return "not_heard_from" if silent > limit else session["state"]


def open_question(session: dict[str, Any]) -> dict[str, Any] | None:
    """A question stays open until a later main-thread prompt, tool, stop or end event."""
    if session["question_ts"] is None or session["state"] == "ended":
        return None
    asked = (session["question_ts"], session["question_seq"])
    cleared = (session["cleared_ts"] or -1, session["cleared_seq"] or -1)
    if asked <= cleared:
        return None
    return {
        "text": session["question"],
        "asked_at": datetime.fromtimestamp(session["question_ts"], UTC),
    }


def candidates_from_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The cards a session can link to. A card is open while its stage is not done."""
    return [
        {
            "id": c["id"],
            "branch": (c.get("session_link") or {}).get("branch"),
            "done": c["stage"] == "done",
        }
        for c in cards
        if c["kind"] == "item"
    ]


def link(session: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """An explicit card id wins; else one open card whose session_branch is the session's
    branch. Two or more such cards link nothing, and the candidates say which."""
    if session["card_id"]:
        card_id = f"item:{session['card_id']}"
        if any(c["id"] == card_id for c in candidates):
            return {"status": "card_id", "card_id": card_id, "candidates": []}
        return {"status": "unknown_card", "card_id": None, "candidates": [card_id]}
    branch = session["branch"]
    if not branch or branch in _UNMATCHED_BRANCHES:
        return {"status": "none", "card_id": None, "candidates": []}
    matches = [c["id"] for c in candidates if c["branch"] == branch and not c["done"]]
    if len(matches) == 1:
        return {"status": "branch", "card_id": matches[0], "candidates": []}
    if matches:
        return {"status": "ambiguous", "card_id": None, "candidates": sorted(matches)}
    return {"status": "none", "card_id": None, "candidates": []}


def _rank(session: dict[str, Any]) -> tuple:
    return (session["state"] != "ended", session["last_heard_at"])


def sessions_by_card(
    sessions: list[dict[str, Any]], links: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Each linked card's session: one still open before an ended one, then the latest heard."""
    best: dict[str, dict[str, Any]] = {}
    for s in sessions:
        card_id = links[s["session_id"]]["card_id"]
        if card_id and (card_id not in best or _rank(s) > _rank(best[card_id])):
            best[card_id] = s
    return best


def short_id(session_id: str) -> str:
    return session_id[:8]


def card_fields(session: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    """What a card's session_link gains from its session."""
    if session is None:
        return {"session_id": None, "last_heard_at": None, "doing": None, "session_status": None}
    return {
        "session_id": session["session_id"],
        "last_heard_at": session["last_heard_at"],
        "doing": session["doing"],
        "session_status": status(session, now),
    }


def view(
    session: dict[str, Any],
    linked: dict[str, Any],
    cards: dict[str, dict[str, Any]],
    projects: dict[str, dict[str, str]],
    now: datetime,
) -> dict[str, Any]:
    card = cards.get(linked["card_id"]) if linked["card_id"] else None
    project = projects.get(card["workstream_id"]) if card and card["workstream_id"] else None
    return {
        "session_id": session["session_id"],
        "host": session["host"],
        "cwd": session["cwd"],
        "repo": session["repo"],
        "branch": session["branch"],
        "started_at": session["started_at"],
        "last_heard_at": session["last_heard_at"],
        "ended_at": session["ended_at"],
        "state": session["state"],
        "status": status(session, now),
        "doing": session["doing"],
        "doing_source": session["doing_source"],
        "doing_at": (
            datetime.fromtimestamp(session["doing_ts"], UTC) if session["doing_ts"] else None
        ),
        "question": open_question(session),
        "link": linked,
        "card": (
            {"id": card["id"], "title": card["title"], "stage": card["stage"], "next": card["next"]}
            if card
            else None
        ),
        "project": project,
        "pr": card.get("pr") if card else None,
    }


def _step(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "ts": e["ts"],
        "at": datetime.fromtimestamp(e["ts"], UTC),
        "seq": e["seq"],
        "kind": e["kind"],
        "tool": e["tool"],
        "target": e["target"],
        "text": e["text"],
        "error": e["error"],
    }


def agents(events: list[dict[str, Any]], session_state: str) -> list[dict[str, Any]]:
    """The main thread first, then subagents in the order they were first heard. Each holds its
    kept steps (the newest 80), oldest first."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in sorted(events, key=lambda e: (e["ts"], e["seq"])):
        groups.setdefault(e["agent_id"], []).append(e)
    order = sorted(groups, key=lambda a: (a != "", groups[a][0]["ts"]))
    result = []
    for agent_id in order:
        steps = groups[agent_id]
        types = [e["agent_type"] for e in steps if e["agent_type"]]
        if agent_id == "":
            state = session_state
        else:
            state = "done" if steps[-1]["kind"] == "agent_stop" else "running"
        result.append(
            {
                "agent_id": agent_id or None,
                "agent_type": types[-1] if types else ("main" if agent_id == "" else None),
                "state": state,
                "first_at": datetime.fromtimestamp(steps[0]["ts"], UTC),
                "last_at": datetime.fromtimestamp(steps[-1]["ts"], UTC),
                "steps": [_step(e) for e in steps],
            }
        )
    return result


@dataclass
class BoardRead:
    """One board read: the cards, the sessions heard in the live window, and each session's link,
    worked out once against every item card before the done window trims the list."""

    cards: list[dict[str, Any]]
    sessions: list[dict[str, Any]]
    links: dict[str, dict[str, Any]]


def _thresholds() -> dict[str, int]:
    return {
        "silent_after_s": settings.session_running_silent_s,
        "idle_silent_after_s": settings.session_idle_silent_s,
    }


async def list_sessions(read: BoardRead) -> dict[str, Any]:
    now = datetime.now(UTC)
    by_id = {c["id"]: c for c in read.cards}
    projects = await store.projects_by_workstream()
    views = [view(s, read.links[s["session_id"]], by_id, projects, now) for s in read.sessions]
    return {
        "live": [v for v in views if v["state"] != "ended"],
        "recent": [v for v in views if v["state"] == "ended"],
        **_thresholds(),
        "window_hours": settings.session_live_hours,
        "now": now,
    }


async def detail(session_id: str, cards: list[dict[str, Any]]) -> dict[str, Any] | None:
    session = await store.get(session_id)
    if session is None:
        return None
    now = datetime.now(UTC)
    projects = await store.projects_by_workstream()
    linked = link(session, candidates_from_cards(cards))
    found = view(session, linked, {c["id"]: c for c in cards}, projects, now)
    return {
        **found,
        "agents": agents(await store.events(session_id), session["state"]),
        "steps_per_agent": store.STEPS_PER_AGENT,
        **_thresholds(),
        "now": now,
    }


def _building_without_a_session(card: dict[str, Any]) -> bool:
    """A card that says "Being built in a terminal session" (a hand-set link with no job and no
    PR; a session's card_id alone never makes a card doing) whose linked session is neither
    running nor idle."""
    link_ = card.get("session_link") or {}
    return (
        card["kind"] == "item"
        and card["stage"] == "doing"
        and card["job_id"] is None
        and bool(link_)
        and link_.get("pr_number") is None
        and link_.get("session_status") not in ("running", "idle")
    )


async def command_part(read: BoardRead, now: datetime) -> dict[str, Any]:
    """Home's live sessions and the counts behind its quiet line and stale banner."""
    by_id = {c["id"]: c for c in read.cards}
    projects = await store.projects_by_workstream()
    return {
        "sessions": [
            view(s, read.links[s["session_id"]], by_id, projects, now)
            for s in read.sessions
            if s["state"] != "ended"
        ],
        "session_health": {
            **_thresholds(),
            "last_heard_at": await store.last_heard_at(),
            "stale_card_ids": [c["id"] for c in read.cards if _building_without_a_session(c)],
        },
    }
