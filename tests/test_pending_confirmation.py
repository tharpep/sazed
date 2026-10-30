"""Confirming a sensitive chat action (plans/10 P4.2): code decides, never a model, and only an
unqualified yes runs it. The replies below include the ones the old prefix regex wrongly ran."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.agent import loop

SID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class FakePool:
    def __init__(self, rows):
        self.rows, self.statements = rows, []

    async def execute(self, sql, *args):
        self.statements.append(sql)

    async def fetch(self, sql, *args):
        return self.rows


@pytest.fixture
def pending(monkeypatch):
    ran = []

    async def fake_execute_tool(name, tool_input):
        ran.append((name, tool_input))
        return SimpleNamespace(content="done", status="success", error=None, duration_ms=1)

    def no_model():
        raise AssertionError("a model must not decide a confirmation")

    monkeypatch.setattr(loop, "execute_tool", fake_execute_tool)
    monkeypatch.setattr(loop, "get_client", no_model)
    row = {
        "id": uuid.uuid4(),
        "tool_name": "restart_container",
        "tool_input": json.dumps({"id": 1}),
    }
    return FakePool([row]), ran


def _rejected(pool: FakePool) -> bool:
    return any("status = 'rejected'" in sql for sql in pool.statements)


@pytest.mark.parametrize(
    "reply",
    ["yes", "Yes.", "yes please", "Yeah", "yep", "y", "ok", "okay!", "sure", "confirm", "confirmed",
     "do it", "go ahead", "go ahead, thanks", "Approved", "proceed", "sounds good", "  YES  "],
)  # fmt: skip
async def test_a_plain_yes_runs_the_action(pending, reply):
    pool, ran = pending
    assert await loop._handle_pending_actions(pool, SID, reply) == "Confirmed. done"
    assert ran == [("restart_container", {"id": 1})]
    assert not _rejected(pool)


@pytest.mark.parametrize(
    "reply", ["no", "No.", "nope", "cancel", "stop", "never mind", "Nevermind!"]
)
async def test_a_plain_no_cancels_with_no_turn(pending, reply):
    pool, ran = pending
    assert await loop._handle_pending_actions(pool, SID, reply) == "Action cancelled."
    assert ran == [] and _rejected(pool)


@pytest.mark.parametrize(
    "reply",
    [
        "go ahead and delete the other one instead",
        "yes but change the title first",
        "yes, wait, not that one",
        "do it tomorrow",
        "confirm nothing yet",
        "approved? what does that mean",
        "yes?",
        "ok but first check the backup",
        "sure, after lunch",
        "yeah no",
        "don't do it",
        "what would that do?",
        "No, keep it.",
    ],
)
async def test_anything_else_cancels_and_goes_on_to_the_agent(pending, reply):
    pool, ran = pending
    assert await loop._handle_pending_actions(pool, SID, reply) is None
    assert ran == [] and _rejected(pool)


async def test_nothing_pending_changes_nothing(pending):
    _, ran = pending
    pool = FakePool([])
    assert await loop._handle_pending_actions(pool, SID, "yes") is None
    assert ran == [] and not _rejected(pool)
