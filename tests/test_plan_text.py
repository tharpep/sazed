"""Plan text (plans/18): stable section ids, word-level diffs, and the rewrite guard."""

from app.board import plan_text

PLAN = """# Plan 17: Dispatch

Status: started.

## Goal {#goal}

One plan becomes parallel tasks.

## Build steps

- D0 probe
- D1 guardrails

## Build steps

Second heading with the same title.
"""


def test_sections_keep_pinned_ids_and_slug_the_rest():
    ids = [s["id"] for s in plan_text.sections(PLAN)]
    assert ids == ["plan-17-dispatch", "goal", "build-steps", "build-steps-2"]


def test_a_pinned_id_survives_a_renamed_heading():
    renamed = PLAN.replace("## Goal {#goal}", "## What this is for {#goal}")
    assert plan_text.section_text(renamed, "goal") == "One plan becomes parallel tasks."


def test_preamble_is_the_text_before_the_first_heading():
    assert plan_text.preamble("Draft notes.\n\n# Title\nBody") == "Draft notes."
    assert plan_text.preamble(PLAN) == ""


def test_reflowed_whitespace_is_not_a_change():
    reflowed = PLAN.replace(
        "One plan becomes parallel tasks.", "One plan   becomes\nparallel tasks."
    )
    assert [b["kind"] for b in plan_text.diff(PLAN, reflowed)] == ["same"]


def test_a_changed_block_shows_word_level_edits():
    new = PLAN.replace("Status: started.", "Status: D3 done.")
    chg = next(b for b in plan_text.diff(PLAN, new) if b["kind"] == "chg")
    ops = [(s["op"], s["text"]) for s in chg["segments"] if s["op"] != "same"]
    assert ops == [("del", "started."), ("add", "D3 done.")]


def test_added_and_removed_blocks_and_folded_runs():
    new = PLAN.replace(
        "- D0 probe\n- D1 guardrails\n", "- D0 probe\n- D1 guardrails\n\nD2 is next.\n"
    )
    kinds = [b["kind"] for b in plan_text.diff(PLAN, new)]
    assert "add" in kinds and kinds[0] == "same"
    removed = PLAN.replace("Second heading with the same title.\n", "")
    assert any(b["kind"] == "del" for b in plan_text.diff(PLAN, removed))


def test_the_rewrite_guard_trips_only_on_wholesale_change():
    assert not plan_text.is_rewrite(PLAN, PLAN.replace("started", "D3 done"))
    assert plan_text.is_rewrite(PLAN, "# Something else entirely\n\nNew words here.")
    assert not plan_text.is_rewrite("", "anything")
