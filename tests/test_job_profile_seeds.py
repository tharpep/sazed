"""Migration 035 seeds the job profiles with the values of the code table it replaced, so a job
created after the switch runs exactly as one created before it."""

import re

import pytest

from app.board import profiles as registry
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


def registry_rows() -> dict[str, dict]:
    """The job profiles as the registry returns them right after migration 035."""
    keys = ("engine", "capability", "model", "timeout_s", "max_tool_calls")
    return {name: dict(zip(keys, values, strict=True)) for name, values in OLD_TABLE.items()}


def fake_registry(monkeypatch) -> dict[str, dict]:
    rows = registry_rows()

    async def fetch(name):
        return rows.get(name)

    monkeypatch.setattr(registry, "fetch", fetch)
    return rows


@pytest.mark.parametrize(
    ("name", "kwargs", "expected"),
    [
        ("research_quick", {}, ("agy", "gemini-3.8-flash-high", "research", 300, 30)),
        ("research_quick", {"agent": "agy"}, ("agy", "gemini-3.8-flash-high", "research", 300, 30)),
        ("research_deep", {}, ("agy", "gemini-3.8-flash-high", "research", 600, 80)),
        ("second_opinion", {}, ("agy", "gemini-3.8-flash-high", "research", 300, 20)),
        ("review", {}, ("claude", "claude-sonnet-5-5", "review", 900, 150)),
        ("change", {}, ("claude", "claude-sonnet-5-5", "change", 1800, 200)),
        (
            "change",
            {"model": "claude-opus-5-5"},
            ("claude", "claude-opus-5-5", "change", 1800, 200),
        ),
        ("review", {"source": "webhook"}, ("agy", "gemini-3.8-flash-high", "review", 900, 150)),
        ("change", {"source": "webhook"}, ("claude", "claude-sonnet-5-5", "change", 1800, 200)),
    ],
)
async def test_each_job_resolves_as_it_did_from_the_code_table(monkeypatch, name, kwargs, expected):
    fake_registry(monkeypatch)
    run = await profiles.resolve(name, **{"source": "console", **kwargs})
    assert (run.agent, run.model, run.profile, run.timeout_s, run.max_tool_calls) == expected


async def test_a_registry_edit_reaches_the_next_job(monkeypatch):
    rows = fake_registry(monkeypatch)
    rows["research_deep"] = {**rows["research_deep"], "timeout_s": 900, "max_tool_calls": 120}
    run = await profiles.resolve("research_deep", source="console")
    assert (run.timeout_s, run.max_tool_calls) == (900, 120)
