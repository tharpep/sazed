"""Project rollups: workstream statuses and progress come from cards, never stored state."""

import uuid
from datetime import UTC, datetime

from app.board import rollup


def _ws(position, state="open", depends_on=()):
    return {
        "id": uuid.uuid4(),
        "title": f"Step {position}",
        "summary": "",
        "position": position,
        "state": state,
        "depends_on": list(depends_on),
    }


def _cards(*stages):
    return [{"id": f"item:{i}", "stage": s} for i, s in enumerate(stages)]


def _project(state="active"):
    now = datetime.now(UTC)
    return {
        "id": uuid.uuid4(),
        "slug": "agent-stack",
        "title": "Agent stack",
        "summary": "",
        "state": state,
        "rank": 1,
        "repos": [],
        "plan": None,
        "latest_update": None,
        "latest_update_at": None,
        "updated_at": now,
    }


def test_first_open_unblocked_step_is_next_and_the_rest_are_planned():
    rows = [_ws(1, "done"), _ws(2), _ws(3)]
    steps = rollup.workstreams(rows, {})
    assert [s["status"] for s in steps] == ["done", "next", "planned"]


def test_cards_waiting_on_pryce_outrank_cards_in_flight():
    rows = [_ws(1)]
    steps = rollup.workstreams(rows, {str(rows[0]["id"]): _cards("doing", "needs_me")})
    assert steps[0]["status"] == "needs_me"


def test_a_step_waiting_on_an_open_dependency_is_blocked_and_never_next():
    first = _ws(1)
    rows = [first, _ws(2, depends_on=[first["id"]])]
    by_ws = {str(first["id"]): _cards("doing")}
    assert [s["status"] for s in rollup.workstreams(rows, by_ws)] == ["in_flight", "blocked"]


def test_a_dropped_dependency_no_longer_blocks():
    first = _ws(1, "dropped")
    rows = [first, _ws(2, depends_on=[first["id"]])]
    assert rollup.workstreams(rows, {})[1]["status"] == "next"


def test_progress_bar_segments_add_up_to_the_total():
    rows = [_ws(1)]
    step = rollup.workstreams(rows, {str(rows[0]["id"]): _cards("done", "next", "doing")})[0]
    assert step["progress"] == {"done": 1, "in_flight": 1, "needs_me": 0, "next": 1, "total": 3}


def test_project_sums_its_steps_and_names_the_current_one():
    rows = [_ws(1, "done"), _ws(2), _ws(3, "dropped")]
    by_ws = {str(rows[0]["id"]): _cards("done", "done"), str(rows[1]["id"]): _cards("doing")}
    out = rollup.project(_project(), rollup.workstreams(rows, by_ws))
    assert out["progress"]["total"] == 3
    assert out["progress"]["done"] == 2
    assert (out["steps_done"], out["steps_total"]) == (1, 2)
    assert out["current"]["title"] == "Step 2"
    assert out["headline"] == "Step 2 is in flight. 1 of 2 workstreams done."


def test_a_paused_project_says_so_instead_of_its_next_step():
    out = rollup.project(_project("paused"), rollup.workstreams([_ws(1)], {}))
    assert out["headline"] == "Paused."


def test_a_closed_step_fills_its_bar_even_with_no_cards():
    steps = rollup.workstreams([_ws(1, "done")], {})
    assert steps[0]["bar"] == {"done": 1.0, "in_flight": 0.0, "needs_me": 0.0, "next": 0.0}


def test_the_project_bar_weighs_each_step_equally_and_splits_steps_by_cards():
    rows = [_ws(1, "done"), _ws(2), _ws(3), _ws(4, "dropped")]
    by_ws = {str(rows[1]["id"]): _cards("done", "doing", "next", "next")}
    bar = rollup.project(_project(), rollup.workstreams(rows, by_ws))["bar"]
    assert bar == {"done": 0.4167, "in_flight": 0.0833, "needs_me": 0.0, "next": 0.1667}
