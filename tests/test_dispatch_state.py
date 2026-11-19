"""dispatch_state (migration 035) is the one mapping from a dispatch row's raw word to a run state.
CI has no Postgres, so these read the function's text: every word the API accepts has a branch,
and every word it returns is in the run-state vocabulary of migration 017."""

import re
from typing import get_args

from app.migrations import _discover
from app.routers.dispatch import RowState

RUN_STATES = {
    "queued",
    "needs_approval",
    "running",
    "needs_input",
    "paused",
    "succeeded",
    "skipped",
    "failed",
    "cancelled",
    "timed_out",
}


def _function() -> str:
    """The newest migration's body of dispatch_state."""
    for _, _, path in reversed(_discover()):
        sql = path.read_text(encoding="utf-8")
        if "FUNCTION dispatch_state(" in sql:
            return sql.split("FUNCTION dispatch_state(", 1)[1].split("END $$", 1)[0]
    raise AssertionError("no migration defines dispatch_state")


def test_every_raw_word_has_a_branch():
    body = _function()
    branches = set(re.findall(r"WHEN '(\w+)' THEN", body)) | set(re.findall(r"raw = '(\w+)'", body))
    missing = set(get_args(RowState)) - branches
    assert not missing, f"dispatch_state has no branch for {missing}"


def test_every_returned_word_is_a_run_state():
    body = _function()
    returned = set(re.findall(r"(?:THEN|ELSE|RETURN) '(\w+)'", body))
    assert returned, "no returned words found"
    assert returned <= RUN_STATES, f"outside the run states: {returned - RUN_STATES}"


def test_a_reaped_row_is_timed_out_from_either_reason_key():
    body = _function()
    assert "'no_heartbeat' IN (meta->>'reason', meta->>'stop_reason')" in body
    assert re.search(r"THEN 'timed_out' ELSE 'failed'", body)


def test_cleaning_a_running_or_waiting_task_cancels_it():
    body = _function()
    assert re.search(
        r"IF before IS NULL OR before IN \('running', 'waiting'\) THEN\s+RETURN 'cancelled'", body
    )
