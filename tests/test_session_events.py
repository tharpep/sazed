"""Claude Code session events (slice 3): ingest, the step cap, card links, questions, the reads."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.board import command, service
from app.routers import sessions as router
from app.sessions import service as sessions
from app.sessions import store

SID = "3f1c-session"
CARD = uuid.UUID("11111111-2222-3333-4444-555555555555")
OTHER = uuid.UUID("99999999-2222-3333-4444-555555555555")


def _ev(seq, kind="tool", ts=None, **fields):
    base = {"session_id": SID, "ts": ts or 1_763_600_000 + seq, "seq": seq, "kind": kind}
    if kind == "tool":
        base |= {"tool": "Bash", "target": "pytest -q", "text": "12 passed"}
    return base | fields


class FakeDb:
    """Holds rows the way migration 039's keys and the prune statement do."""

    def __init__(self):
        self.sessions: dict[str, tuple] = {}
        self.events: dict[tuple, dict] = {}
        self.prunes: list[tuple] = []

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def executemany(self, sql, args):
        if "INSERT INTO agent_sessions" in sql:
            assert "ON CONFLICT (session_id) DO UPDATE" in sql
            for row in args:
                self.sessions[row[0]] = row
        elif "DELETE FROM agent_session_events" in sql:
            for sid, agent, keep in args:
                self.prunes.append((sid, agent, keep))
                mine = sorted(
                    (k for k, e in self.events.items() if (e["sid"], e["agent"]) == (sid, agent)),
                    key=lambda k: (self.events[k]["ts"], self.events[k]["seq"]),
                    reverse=True,
                )
                for key in mine[keep:]:
                    del self.events[key]

    async def fetch(self, sql, *columns):
        if "FOR UPDATE" in sql:
            rows = [
                dict(zip(store._SESSION_COLUMNS, r, strict=True)) for r in self.sessions.values()
            ]
            return [r for r in rows if r["session_id"] in columns[0]]
        with_agent = "ON CONFLICT (session_id, agent_id, ts, seq) DO NOTHING" in sql
        assert with_agent or "ON CONFLICT (session_id, ts, seq) DO NOTHING" in sql
        new = []
        for sid, agent, _type, ts, seq, *_ in zip(*columns, strict=True):
            key = (sid, agent, ts, seq) if with_agent else (sid, ts, seq)
            if key not in self.events:
                self.events[key] = {"sid": sid, "agent": agent, "ts": ts, "seq": seq}
                new.append({"session_id": sid, "agent_id": agent})
        return new


@pytest.fixture
def db(monkeypatch):
    fake = FakeDb()
    monkeypatch.setattr(store, "get_pool", lambda: fake)
    return fake


def _session(**fields):
    now = datetime.now(UTC)
    base = {
        "session_id": SID,
        "host": "laptop",
        "cwd": "/code/sazed",
        "repo": "tharpep/sazed",
        "branch": "feature/session-events",
        "card_id": None,
        "started_at": now - timedelta(minutes=20),
        "last_heard_at": now,
        "state": "running",
        "state_ts": 2.0,
        "state_seq": 0,
        "ended_at": None,
        "doing": "Write the contract",
        "doing_source": "task",
        "doing_ts": 1.0,
        "doing_seq": 0,
        "question": None,
        "question_ts": None,
        "question_seq": None,
        "cleared_ts": None,
        "cleared_seq": None,
    }
    return base | fields


# S2: batches, idempotency, the 300-character cut, malformed events alone


def test_a_batch_is_one_to_two_hundred_events():
    with pytest.raises(ValidationError):
        router.EventsIn(events=[])
    with pytest.raises(ValidationError):
        router.EventsIn(events=[_ev(0)] * 201)
    assert len(router.EventsIn(events=[_ev(0)] * 200).events) == 200


async def test_a_malformed_event_is_rejected_alone(db):
    batch = [
        _ev(0, "start", host="laptop"),
        _ev(1, kind="dance"),
        _ev(2, tool=None),
        _ev(3, "doing", text="Write tests"),
        _ev(4, card_id="job:abc"),
        _ev(5),
    ]
    result = await router.post_events(router.EventsIn(events=batch))
    assert result["accepted"] == 2
    assert result["stored"] == 2
    assert [r["index"] for r in result["rejected"]] == [1, 2, 3, 4]
    assert "kind" in result["rejected"][0]["error"]
    assert "doing_source" in result["rejected"][2]["error"]


async def test_events_are_idempotent_on_session_ts_and_seq(db):
    batch = [_ev(0, "start"), _ev(1), _ev(2)]
    first = await sessions.ingest(batch)
    again = await sessions.ingest(batch)
    assert (first["stored"], again["stored"]) == (3, 0)
    assert again["accepted"] == 3
    assert len(db.events) == 3


def test_text_is_cut_to_three_hundred_characters_and_prompt_text_is_never_kept():
    [tool, prompt], rejected = sessions.parse_batch(
        [
            _ev(0, target="t" * 900, text="r" * 900, cwd="c" * 900),
            _ev(1, "prompt", text="my secret prompt"),
        ]
    )
    assert not rejected
    assert (len(tool.target), len(tool.text), len(tool.cwd)) == (300, 300, 300)
    assert prompt.text == ""


def test_a_nul_byte_is_stripped_from_every_text_field():
    nul = "a\x00b"
    fields = {f: nul for f in ("agent_id", "agent_type", "target", "text", "host", "cwd", "repo")}
    [event], rejected = sessions.parse_batch(
        [_ev(0, session_id="s\x00id", tool="Ba\x00sh", branch=nul, **fields)]
    )
    assert not rejected
    [session] = sessions.reduce([event])
    stored = list(sessions.event_rows([event])[0].values()) + list(session.values())
    assert not any("\x00" in v for v in stored if isinstance(v, str))
    assert (event.session_id, event.tool, event.text) == ("sid", "Bash", "ab")


def test_a_lone_surrogate_is_replaced_so_the_event_stores():
    [event], rejected = sessions.parse_batch(
        [_ev(0, text="cut \ud83d", target="\udc00x", host="lap\ud83dtop")]
    )
    assert not rejected
    assert (event.text, event.target, event.host) == ("cut ?", "?x", "lap?top")
    for value in sessions.event_rows([event])[0].values():
        if isinstance(value, str):
            value.encode("utf-8")


async def test_a_millisecond_or_far_future_ts_is_rejected_alone(db):
    batch = [_ev(0, ts=1_763_650_800_123), _ev(1, ts=253_402_300_800), _ev(2)]
    result = await sessions.ingest(batch)
    assert [r["index"] for r in result["rejected"]] == [0, 1]
    assert result["stored"] == 1


async def test_a_non_object_event_is_rejected_alone(db):
    body = router.EventsIn(events=["tool", None, 7, _ev(0)])
    result = await router.post_events(body)
    assert [r["index"] for r in result["rejected"]] == [0, 1, 2]
    assert result["stored"] == 1


async def test_two_agents_may_share_a_ts_and_seq(db):
    batch = [_ev(0, ts=1_763_650_800.0), _ev(0, ts=1_763_650_800.0, agent_id="a1")]
    assert (await sessions.ingest(batch))["stored"] == 2
    assert (await sessions.ingest(batch))["stored"] == 0


def test_identity_columns_keep_the_newest_value_by_ts_and_seq():
    """A late, older batch fills a missing value but never replaces a newer one."""
    for column in ("host", "cwd", "repo", "branch", "card_id"):
        newer = f"COALESCE(EXCLUDED.{column}, s.{column})"
        older = f"COALESCE(s.{column}, EXCLUDED.{column})"
        assert newer in store._UPSERT and older in store._UPSERT, column


def test_a_body_over_the_cap_answers_413(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    async def no_store(sessions_, events):
        return len(events)

    monkeypatch.setattr(store, "ingest", no_store)
    app = FastAPI()
    app.include_router(router.router, prefix="/sessions")
    client = TestClient(app)
    big = {"events": [_ev(n, text="x" * 6000) for n in range(200)]}
    assert client.post("/sessions/events", json=big).status_code == 413
    small = client.post("/sessions/events", json={"events": [_ev(0)]})
    assert (small.status_code, small.json()["stored"]) == (200, 1)


# S7: at most 80 steps per agent


async def test_a_session_keeps_at_most_eighty_steps_per_agent(db):
    main = [_ev(n) for n in range(150)]
    sub = [_ev(1000 + n, agent_id="a1", agent_type="Explore") for n in range(10)]
    for chunk in (main[:100], main[100:] + sub):
        await sessions.ingest(chunk)
    main_rows = [e for e in db.events.values() if e["agent"] == ""]
    assert len(main_rows) == store.STEPS_PER_AGENT
    assert min(e["seq"] for e in main_rows) == 70
    assert sum(e["agent"] == "a1" for e in db.events.values()) == 10
    assert {(a, keep) for _, a, keep in db.prunes} == {("", 80), ("a1", 80)}


# The newest-wins session row


def test_a_batch_reduces_to_one_row_per_session_with_its_newest_state():
    events, _ = sessions.parse_batch(
        [
            _ev(0, "start", host="laptop", branch="feature/x", card_id=f"item:{CARD}"),
            _ev(1, "doing", text="Write the store", doing_source="task"),
            _ev(2, "question", text="Allow Bash: rm -rf build?"),
            _ev(3, "stop"),
            _ev(4, "start", session_id="other"),
        ]
    )
    rows = {r["session_id"]: r for r in sessions.reduce(events)}
    mine = rows[SID]
    assert (mine["state"], mine["state_seq"]) == ("idle", 3)
    assert (mine["doing"], mine["doing_source"]) == ("Write the store", "task")
    assert (mine["question_seq"], mine["cleared_seq"]) == (2, 3)
    assert mine["card_id"] == CARD
    assert mine["branch"] == "feature/x"
    assert rows["other"]["state"] == "idle"
    end, _ = sessions.parse_batch([_ev(9, "end")])
    assert sessions.reduce(end)[0]["ended_at"] is not None


# S6: questions


def test_a_question_is_open_until_the_next_tool_event_or_stop():
    asked = _session(question="Allow Bash?", question_ts=5.0, question_seq=0)
    assert sessions.open_question(asked)["text"] == "Allow Bash?"
    assert sessions.open_question(asked | {"cleared_ts": 4.0, "cleared_seq": 9}) is not None
    assert sessions.open_question(asked | {"cleared_ts": 6.0, "cleared_seq": 0}) is None
    assert sessions.open_question(asked | {"cleared_ts": 5.0, "cleared_seq": 1}) is None
    assert sessions.open_question(asked | {"state": "ended"}) is None
    assert sessions.open_question(_session()) is None


def test_tool_prompt_stop_and_end_clear_a_question_and_nothing_else_does():
    kinds = ["start", "agent_start", "agent_stop", "doing", "question", "tool", "prompt", "stop"]
    cleared = set()
    for kind in kinds + ["end"]:
        fields = {"agent_id": "a1"} if kind.startswith("agent") else {}
        if kind == "doing":
            fields = {"text": "x", "doing_source": "commit"}
        events, rejected = sessions.parse_batch([_ev(0, kind, **fields)])
        assert not rejected, kind
        if sessions.reduce(events)[0]["cleared_ts"] is not None:
            cleared.add(kind)
    assert cleared == {"tool", "prompt", "stop", "end"}


def test_a_subagent_event_does_not_clear_the_main_thread_question():
    events, _ = sessions.parse_batch(
        [
            _ev(0, "question", text="Allow Bash?"),
            _ev(1, agent_id="a1"),
            _ev(2, "stop", agent_id="a1"),
        ]
    )
    [row] = sessions.reduce(events)
    assert row["cleared_ts"] is None
    assert sessions.open_question(_session(**{k: row[k] for k in row if "question" in k}))


def test_a_subagent_question_clears_on_that_subagent_next_event():
    asked_in_batch, _ = sessions.parse_batch(
        [_ev(0, "question", text="Allow Edit?", agent_id="a1"), _ev(1, agent_id="a1")]
    )
    assert sessions.reduce(asked_in_batch)[0]["cleared_seq"] == 1
    stored = {SID: {"question_agent": "a1", "question_ts": 1.0, "question_seq": 0}}
    later, _ = sessions.parse_batch([_ev(2, agent_id="a2"), _ev(3, agent_id="a1")])
    [row] = sessions.reduce(later, stored)
    assert row["cleared_seq"] == 3
    other_only, _ = sessions.parse_batch([_ev(4, agent_id="a2")])
    assert sessions.reduce(other_only, stored)[0]["cleared_ts"] is None


async def test_sessions_are_locked_and_upserted_in_one_fixed_order(db, monkeypatch):
    seen = []
    upsert = db.executemany

    async def record(sql, args):
        if "INSERT INTO agent_sessions" in sql:
            seen.extend(row[0] for row in args)
        await upsert(sql, args)

    monkeypatch.setattr(db, "executemany", record)
    await sessions.ingest([_ev(0, session_id="zeta"), _ev(1, session_id="alpha")])
    assert seen == ["alpha", "zeta"]
    assert "ORDER BY session_id" in store._OPEN_QUESTIONS


async def test_ingest_reads_the_stored_asker_before_it_builds_the_row(db):
    await sessions.ingest([_ev(0, "question", text="Allow Edit?", agent_id="a1")])
    await sessions.ingest([_ev(1, agent_id="a1")])
    row = dict(zip(store._SESSION_COLUMNS, db.sessions[SID], strict=True))
    assert row["cleared_seq"] == 1


# N3: the session's state


def _state_of(*events):
    parsed, rejected = sessions.parse_batch(list(events))
    assert not rejected
    [row] = sessions.reduce(parsed)
    return row["state"], row["state_seq"]


def test_work_from_any_agent_after_the_main_stop_reads_running():
    assert _state_of(_ev(0, "stop"), _ev(1, agent_id="a1")) == ("running", 1)
    assert _state_of(_ev(0, "stop"), _ev(1, "prompt")) == ("running", 1)


def test_a_subagent_stop_never_makes_the_session_idle():
    assert _state_of(_ev(0), _ev(1, "stop", agent_id="a1")) == ("running", 0)
    assert _state_of(_ev(0), _ev(1, "agent_stop", agent_id="a1")) == ("running", 0)
    alone, _ = sessions.parse_batch([_ev(5, "agent_stop", agent_id="a1")])
    assert sessions.reduce(alone)[0]["state_ts"] == 0.0
    assert _state_of(_ev(0), _ev(1, "stop")) == ("idle", 1)


# S5: not heard from

NONE = {"status": "none", "card_id": None, "candidates": []}


def test_a_silent_running_session_reads_not_heard_from_and_keeps_its_last_time():
    now = datetime.now(UTC)
    quiet = _session(last_heard_at=now - timedelta(seconds=901))
    view = sessions.view(quiet, NONE, {}, {}, now)
    assert view["status"] == "not_heard_from"
    assert view["state"] == "running"
    assert view["last_heard_at"] == quiet["last_heard_at"]
    assert sessions.status(quiet | {"state": "ended"}, now) == "ended"


def test_an_idle_session_stays_idle_until_a_day_of_silence():
    now = datetime.now(UTC)
    idle = _session(state="idle")
    assert sessions.status(idle | {"last_heard_at": now - timedelta(hours=3)}, now) == "idle"
    day = idle | {"last_heard_at": now - timedelta(hours=24, seconds=1)}
    assert sessions.status(day, now) == "not_heard_from"


def test_a_session_waiting_on_a_question_uses_the_idle_threshold():
    now = datetime.now(UTC)
    asking = _session(question="Allow Bash?", question_ts=5.0, question_seq=0)
    hour = asking | {"last_heard_at": now - timedelta(hours=1)}
    assert sessions.status(hour, now) == "running"
    day = asking | {"last_heard_at": now - timedelta(hours=24, seconds=1)}
    assert sessions.status(day, now) == "not_heard_from"
    answered = hour | {"cleared_ts": 6.0, "cleared_seq": 0}
    assert sessions.status(answered, now) == "not_heard_from"


# S3: card links


def _candidates():
    return [
        {"id": f"item:{CARD}", "branch": "feature/session-events", "done": False},
        {"id": f"item:{OTHER}", "branch": "feature/shared", "done": False},
        {"id": "item:old", "branch": "feature/session-events", "done": True},
    ]


def test_an_explicit_card_id_wins_over_the_branch():
    found = sessions.link(_session(card_id=OTHER), _candidates())
    assert found == {"status": "card_id", "card_id": f"item:{OTHER}", "candidates": []}


def test_a_branch_matches_one_open_card():
    found = sessions.link(_session(), _candidates())
    assert found == {"status": "branch", "card_id": f"item:{CARD}", "candidates": []}


def test_an_ambiguous_branch_links_nothing_and_names_the_candidates():
    cards = _candidates() + [{"id": "item:b", "branch": "feature/shared", "done": False}]
    found = sessions.link(_session(branch="feature/shared"), cards)
    assert found == {
        "status": "ambiguous",
        "card_id": None,
        "candidates": sorted([f"item:{OTHER}", "item:b"]),
    }


def test_no_match_an_unknown_card_and_a_default_branch_link_nothing():
    assert sessions.link(_session(branch="feature/none"), _candidates())["status"] == "none"
    unknown = sessions.link(_session(card_id=uuid.uuid4()), _candidates())
    assert (unknown["status"], unknown["card_id"]) == ("unknown_card", None)
    main = [{"id": "item:m", "branch": "main", "done": False}]
    assert sessions.link(_session(branch="main"), main)["status"] == "none"


async def test_get_session_shows_an_ambiguous_link_and_its_agents(monkeypatch):
    found = _session(branch="feature/shared")
    rows = [
        {"agent_id": "", "agent_type": None, "ts": 1.0, "seq": 0, "kind": "start", "tool": None,
         "target": None, "text": "", "error": False},
        {"agent_id": "a1", "agent_type": "Explore", "ts": 2.0, "seq": 0, "kind": "agent_start",
         "tool": None, "target": None, "text": "", "error": False},
        {"agent_id": "a1", "agent_type": "Explore", "ts": 3.0, "seq": 0, "kind": "tool",
         "tool": "Grep", "target": "session_link", "text": "4 files", "error": False},
        {"agent_id": "a1", "agent_type": "Explore", "ts": 4.0, "seq": 0, "kind": "agent_stop",
         "tool": None, "target": None, "text": "Found it", "error": False},
    ]  # fmt: skip

    async def get(session_id):
        return found if session_id == SID else None

    async def events(session_id):
        return rows

    async def projects():
        return {}

    monkeypatch.setattr(store, "get", get)
    monkeypatch.setattr(store, "events", events)
    monkeypatch.setattr(store, "projects_by_workstream", projects)
    cards = [_card(OTHER, "feature/shared"), _card(uuid.uuid4(), "feature/shared")]
    detail = await sessions.detail(SID, cards)
    assert detail["link"]["status"] == "ambiguous"
    assert detail["card"] is None
    assert [a["agent_id"] for a in detail["agents"]] == [None, "a1"]
    assert detail["agents"][0]["agent_type"] == "main"
    sub = detail["agents"][1]
    assert (sub["agent_type"], sub["state"], len(sub["steps"])) == ("Explore", "done", 3)
    assert await sessions.detail("missing", cards) is None


# S4: the board and Home


def _card(card_id, branch, **fields):
    base = {
        "id": f"item:{card_id}",
        "kind": "item",
        "title": "Session events",
        "stage": "doing",
        "next": "Being built in a terminal session",
        "workstream_id": "ws-1",
        "job_id": None,
        "session_link": {"session": "builder", "branch": branch, "pr_number": None},
        "pr": None,
    }
    return base | fields


async def test_home_lists_live_sessions_with_card_project_doing_host_and_pr(monkeypatch):
    now = datetime.now(UTC)
    live = _session()
    ended = _session(session_id="gone", state="ended", ended_at=now)
    pr = {"repo": "tharpep/sazed", "number": 212, "state": "open"}

    async def projects():
        return {"ws-1": {"slug": "board", "title": "The board"}}

    async def last_heard():
        return live["last_heard_at"]

    monkeypatch.setattr(store, "projects_by_workstream", projects)
    monkeypatch.setattr(store, "last_heard_at", last_heard)
    cards = [_card(CARD, "feature/session-events", pr=pr)]
    candidates = sessions.candidates_from_cards(cards)
    links = {s["session_id"]: sessions.link(s, candidates) for s in (live, ended)}
    part = await sessions.command_part(sessions.BoardRead(cards, [live, ended], links), now)
    [row] = part["sessions"]
    assert row["card"]["id"] == f"item:{CARD}"
    assert row["project"] == {"slug": "board", "title": "The board"}
    assert (row["doing"], row["doing_source"], row["host"]) == (
        "Write the contract",
        "task",
        "laptop",
    )
    assert row["started_at"] == live["started_at"]
    assert row["last_heard_at"] == live["last_heard_at"]
    assert row["pr"] == pr
    assert part["session_health"]["silent_after_s"] == 900
    assert part["session_health"]["idle_silent_after_s"] == 86_400


async def test_the_stale_list_holds_building_cards_no_session_is_reporting_for(monkeypatch):
    async def no_projects():
        return {}

    async def never():
        return None

    monkeypatch.setattr(store, "projects_by_workstream", no_projects)
    monkeypatch.setattr(store, "last_heard_at", never)
    reporting = _card(CARD, "feature/a")
    reporting["session_link"] |= {"session_status": "running"}
    waiting = _card(uuid.uuid4(), "feature/w")
    waiting["session_link"] |= {"session_status": "idle"}
    silent = _card(OTHER, "feature/b")
    silent["session_link"] |= {"session_status": "not_heard_from"}
    unlinked = _card(uuid.uuid4(), "feature/c")
    with_pr = _card(uuid.uuid4(), "feature/d")
    with_pr["session_link"] |= {"pr_number": 3}
    cards = [reporting, waiting, silent, unlinked, with_pr]
    part = await sessions.command_part(sessions.BoardRead(cards, [], {}), datetime.now(UTC))
    assert part["session_health"]["stale_card_ids"] == [silent["id"], unlinked["id"]]
    assert part["session_health"]["last_heard_at"] is None


async def test_command_keeps_every_old_key_and_adds_the_session_ones(monkeypatch):
    async def nothing(*args):
        return []

    async def board():
        return sessions.BoardRead([], [], {})

    async def part(read, now):
        return {"sessions": [], "session_health": {"silent_after_s": 900}}

    monkeypatch.setattr(command.service, "read", board)
    monkeypatch.setattr(command.store, "working_jobs", nothing)
    monkeypatch.setattr(command.store, "finished_since", nothing)
    monkeypatch.setattr(command.dispatch, "command_parts", _no_dispatch)
    monkeypatch.setattr(command.sessions, "command_part", part)
    summary = await command.summary()
    assert set(summary) == {"needs", "working", "done", "sessions", "session_health"}


async def _no_dispatch(now):
    return [], []


def _item(**fields):
    now = datetime.now(UTC)
    base = {
        "id": CARD,
        "type": "planned",
        "status": "new",
        "title": "Session events",
        "repos": ["tharpep/sazed"],
        "effort": None,
        "impact": None,
        "rank": 1,
        "job_id": None,
        "workstream_id": None,
        "plan_id": None,
        "plan_section": None,
        "plan_version": None,
        "session_label": None,
        "session_branch": None,
        "pr_repo": None,
        "pr_number": None,
        "created_at": now,
        "updated_at": now,
        "decided_at": None,
    }
    return base | fields


def test_a_card_session_link_gains_the_session_fields_and_keeps_its_old_ones():
    linked = service._item_card(_item(session_label="builder", session_branch="feature/x"), [], {})
    assert linked["session_link"] == {
        "session": "builder",
        "branch": "feature/x",
        "pr_repo": None,
        "pr_number": None,
        "session_id": None,
        "last_heard_at": None,
        "doing": None,
        "session_status": None,
    }
    assert service._item_card(_item(), [], {})["session_link"] is None
    by_id = service._item_card(_item(), [], {}, _session(card_id=CARD))
    assert by_id["session_link"]["session"] == SID[:8]
    assert by_id["session_link"]["session_id"] == SID
    assert by_id["session_link"]["doing"] == "Write the contract"
    assert by_id["session_link"]["session_status"] == "running"
    assert by_id["stage"] == "next"


def test_session_link_always_names_its_session():
    """The console's card schema (sazed-frontend src/widgets/catalog.ts) requires session to be
    a string, branch, pr_repo and pr_number nullable; a null session drops the whole widget."""
    cases = [
        (_item(session_branch="feature/x"), None, "feature/x"),
        (_item(pr_repo="tharpep/sazed", pr_number=12), None, "tharpep/sazed#12"),
        (_item(session_branch="feature/x"), _session(card_id=CARD), SID[:8]),
        (_item(session_label="cc-7", session_branch="feature/x"), _session(), "cc-7"),
    ]
    for item, session, name in cases:
        link = service._item_card(item, [], {}, session)["session_link"]
        assert link["session"] == name
        assert all(link[k] is None or isinstance(link[k], str) for k in ("branch", "pr_repo"))
        assert link["pr_number"] is None or isinstance(link["pr_number"], int)


def _stub_board(monkeypatch, items, recent_sessions, calls=None):
    async def all_items():
        return items

    async def empty():
        return {}

    async def no_jobs():
        return []

    async def recent(hours):
        if calls is not None:
            calls.append(hours)
        return recent_sessions

    monkeypatch.setattr(service.store, "items", all_items)
    monkeypatch.setattr(service.store, "chains", empty)
    monkeypatch.setattr(service.store, "pr_snapshots", empty)
    monkeypatch.setattr(service.store, "active_jobs", no_jobs)
    monkeypatch.setattr(service.sessions_store, "recent", recent)


async def test_board_cards_pick_the_open_session_over_an_ended_one(monkeypatch):
    now = datetime.now(UTC)
    item = _item(session_label="builder", session_branch="feature/x")
    newer_but_ended = _session(session_id="ended", branch="feature/x", state="ended")
    older_open = _session(branch="feature/x", last_heard_at=now - timedelta(minutes=5))
    _stub_board(monkeypatch, [item], [newer_but_ended, older_open])
    [card] = await service.cards()
    assert card["session_link"]["session_id"] == SID


async def test_a_card_whose_stage_is_done_is_not_open_on_either_side(monkeypatch):
    done_by_stage = _item(id=OTHER, status="approved", session_branch="feature/x")
    open_card = _item(session_label="builder", session_branch="feature/x")
    original = service.stages.item_stage

    def stage(item, job, now=None, pr=None):
        return ("done", "Done") if item["id"] == OTHER else original(item, job, now, pr)

    monkeypatch.setattr(service.stages, "item_stage", stage)
    _stub_board(monkeypatch, [done_by_stage, open_card], [_session(branch="feature/x")])
    read = await service.read(all_done=True)
    assert read.links[SID] == {"status": "branch", "card_id": f"item:{CARD}", "candidates": []}
    by_id = {c["id"]: c for c in read.cards}
    assert by_id[f"item:{CARD}"]["session_link"]["session_id"] == SID


async def test_home_reads_the_sessions_once_per_poll(monkeypatch):
    calls: list = []
    _stub_board(monkeypatch, [_item(session_branch="feature/x")], [_session()], calls)

    async def nothing(*args):
        return []

    async def no_projects():
        return {}

    async def never():
        return None

    monkeypatch.setattr(command.store, "working_jobs", nothing)
    monkeypatch.setattr(command.store, "finished_since", nothing)
    monkeypatch.setattr(command.dispatch, "command_parts", _no_dispatch)
    monkeypatch.setattr(store, "projects_by_workstream", no_projects)
    monkeypatch.setattr(store, "last_heard_at", never)
    summary = await command.summary()
    assert len(calls) == 1
    assert [s["session_id"] for s in summary["sessions"]] == [SID]
