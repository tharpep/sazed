"""Migration 035 seeds the job profiles with the values of the code table it replaced, so a job
created after the switch runs exactly as one created before it."""

import re

from app.jobs import profiles
from app.migrations import _MIGRATIONS_DIR

# The table app/jobs/profiles.py held until migration 035: name -> (engine, capability, model,
# timeout_s, max_tool_calls).
OLD_TABLE = {
    "research_quick": ("agy", "research", "gemini-3.8-flash-high", 300, 30),
    "research_deep": ("agy", "research", "gemini-3.8-flash-high", 600, 80),
    "second_opinion": ("agy", "research", "gemini-3.8-flash-high", 300, 20),
    "review": ("claude", "review", "claude-sonnet-5-5", 900, 150),
    "change": ("claude", "change", "claude-sonnet-5-5", 1800, 200),
}

_ROW = re.compile(
    r"\('(\w+)', '(\w+)', '(\w+)', '([^']+)', (NULL|'\w+'), (NULL|[\d.]+), (\d+), (NULL|\d+),"
)


def _seeded() -> dict[str, tuple]:
    sql = (_MIGRATIONS_DIR / "035_one_registry_and_run_states.sql").read_text(encoding="utf-8")
    return {m[0]: m[1:] for m in _ROW.findall(sql)}


def test_the_old_table_is_the_code_table():
    code = {
        name: (
            cap.agent,
            cap.profile,
            profiles.DEFAULT_MODELS[cap.agent],
            cap.timeout_s,
            cap.max_tool_calls,
        )
        for name, cap in profiles.CAPABILITIES.items()
    }
    assert code == OLD_TABLE


def test_job_profiles_are_seeded_with_the_old_values():
    seeded = _seeded()
    for name, (engine, capability, model, timeout_s, max_tool_calls) in OLD_TABLE.items():
        row = seeded[name]
        assert row == (
            engine,
            capability,
            model,
            "NULL",
            "NULL",
            str(timeout_s),
            str(max_tool_calls),
        ), name


def test_flash_is_seeded_complete():
    assert _seeded()["flash"] == (
        "agy",
        "change",
        "gemini-3.8-flash-high",
        "'high'",
        "0.5",
        "1200",
        "NULL",
    )
