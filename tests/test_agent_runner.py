"""Agent runner (plans/12 phase 1): steps in order, gates, pauses and resumes, the model step
contract, the scout's code steps, and the approvals trigger. No database or network."""

import re
import uuid

import pytest
from pydantic import BaseModel, Field, ValidationError

from app.agents import approvals, context, model_step, registry, scout
from app.agents import runner as agent_runner
from app.agents.steps import AgentSpec, Outcome, Step, StepFailed
from app.automations.models import AgentAction, AutomationCreate
from app.migrations import _discover

RUN_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


class Ctx(BaseModel):
    run_id: str
    seen: list[str] = Field(default_factory=list)
    count: int = 0


def _step(name: str, kind: str = "code", result=None):
    async def run(ctx):
        ctx.seen.append(name)
        return result

    return Step(name, kind, run)


def _summarize(ctx: Ctx) -> Outcome:
    return Outcome("success", ",".join(ctx.seen))


@pytest.fixture
def recorded(monkeypatch):
    """The last step record written for the run."""
    saved: dict[str, list] = {}

    async def set_steps(run_id, steps):
        saved["steps"] = [dict(s) for s in steps]

    monkeypatch.setattr(agent_runner.run_store, "set_steps", set_steps)
    return saved


def _use(monkeypatch, spec: AgentSpec):
    monkeypatch.setitem(registry.SPECS, spec.name, spec)


async def test_steps_run_in_order_and_are_recorded(monkeypatch, recorded):
    spec = AgentSpec("t", Ctx, (_step("a", result={"job": "j1"}), _step("b")), _summarize)
    _use(monkeypatch, spec)
    outcome = await agent_runner.run({"name": "t", "params": {}}, RUN_ID)
    assert outcome == Outcome("success", "a,b")
    steps = recorded["steps"]
    assert [(s["name"], s["status"]) for s in steps] == [("a", "ok"), ("b", "ok")]
    assert steps[0]["job"] == "j1"
    assert all("duration_ms" in s for s in steps)


async def test_a_gate_stops_the_run_as_skipped(monkeypatch, recorded):
    spec = AgentSpec("t", Ctx, (_step("a"), _step("g", "gate", "nothing new"), _step("b")),
                     _summarize)
    _use(monkeypatch, spec)
    outcome = await agent_runner.run({"name": "t"}, RUN_ID)
    assert outcome == Outcome("skipped", "nothing new")
    assert [(s["name"], s["status"]) for s in recorded["steps"]] == [
        ("a", "ok"), ("g", "stopped")
    ]


async def test_a_failing_step_is_recorded_and_raised(monkeypatch, recorded):
    async def boom(ctx):
        raise StepFailed("job j2 failed: tool cap")

    spec = AgentSpec("t", Ctx, (_step("a"), Step("m", "model", boom), _step("b")), _summarize)
    _use(monkeypatch, spec)
    with pytest.raises(StepFailed):
        await agent_runner.run({"name": "t"}, RUN_ID)
    last = recorded["steps"][-1]
    assert (last["name"], last["status"]) == ("m", "error")
    assert "tool cap" in last["detail"]


async def test_pause_waits_for_approval_then_resumes_from_the_next_step(monkeypatch, recorded):
    created = {}

    async def create(kind, title, requested_by, payload):
        created.update(kind=kind, title=title, requested_by=requested_by, payload=payload)
        return uuid.UUID(int=7)

    monkeypatch.setattr(approvals, "create", create)
    spec = AgentSpec("t", Ctx, (_step("a"), _step("ask", "pause", "Send it?"), _step("b")),
                     _summarize)
    _use(monkeypatch, spec)

    outcome = await agent_runner.run({"name": "t"}, RUN_ID)
    assert outcome.status == "needs_approval"
    assert created["requested_by"] == f"automation-run:{RUN_ID}"
    assert created["payload"]["step"] == 1
    assert created["payload"]["context"]["seen"] == ["a", "ask"]
    assert recorded["steps"][-1]["status"] == "paused"

    outcome = await agent_runner.resume({"payload": created["payload"]})
    assert outcome == Outcome("success", "a,ask,b")  # context restored, "a" not run again
    assert [(s["name"], s["status"]) for s in recorded["steps"]] == [
        ("a", "ok"), ("ask", "approved"), ("b", "ok")
    ]


async def test_a_pause_that_needs_nothing_goes_straight_on(monkeypatch, recorded):
    spec = AgentSpec("t", Ctx, (_step("ask", "pause", None), _step("b")), _summarize)
    _use(monkeypatch, spec)
    assert (await agent_runner.run({"name": "t"}, RUN_ID)).status == "success"


async def test_repeat_until_stops_when_the_predicate_holds_or_attempts_run_out(
    monkeypatch, recorded
):
    async def bump(ctx):
        ctx.count += 1

    spec = AgentSpec("t", Ctx, (Step("r", "code", bump, lambda c: c.count >= 2, 5),), _summarize)
    _use(monkeypatch, spec)
    await agent_runner.run({"name": "t"}, RUN_ID)
    assert recorded["steps"][0]["attempts"] == 2

    spec = AgentSpec("t", Ctx, (Step("r", "code", bump, lambda c: False, 3),), _summarize)
    _use(monkeypatch, spec)
    await agent_runner.run({"name": "t"}, RUN_ID)
    assert recorded["steps"][0]["attempts"] == 3


async def test_unknown_agent_is_refused():
    with pytest.raises(ValueError, match="unknown agent"):
        await agent_runner.run({"name": "nope"}, RUN_ID)


# ---------------------------------------------------------------- model step contract


class Item(BaseModel):
    title: str


def test_parse_reads_fenced_and_bare_json_and_reports_problems():
    fenced = 'ok:\n```json\n[{"title": "A"}]\n```'
    assert model_step.parse(fenced, list[Item])[0] == [Item(title="A")]
    assert model_step.parse('Here: [{"title": "B"}] done', list[Item])[0] == [Item(title="B")]
    value, error = model_step.parse("no json here", list[Item])
    assert value is None and "no valid JSON" in error
    value, error = model_step.parse('[{"name": "x"}]', list[Item])
    assert value is None and "title" in error


@pytest.fixture
def fake_jobs(monkeypatch):
    """Jobs whose results come from a queue; records follow-up instructions."""
    state = {"results": [], "follow_ups": [], "status": "succeeded"}

    async def create(kind, title, prompt, params, requested_by, model):
        state["params"] = params
        return {"id": uuid.uuid4()}

    async def follow_up(parent_id, instruction, requested_by):
        state["follow_ups"].append(instruction)
        return {"id": uuid.uuid4()}

    async def wait_for(job_id, seconds):
        return {"id": job_id, "status": state["status"], "error": "boom",
                "result": state["results"].pop(0) if state["results"] else "",
                "meta": {"usage": {"total_tokens": 100}, "tool_calls": 2}}

    monkeypatch.setattr(model_step.jobs, "_create_agy_job", create)
    monkeypatch.setattr(model_step.jobs, "create_follow_up", follow_up)
    monkeypatch.setattr(model_step.jobs, "wait_for", wait_for)
    return state


async def _agy(**overrides):
    args = dict(kind="research", title="t", prompt="p", schema=list[Item], model="m",
                requested_by="automation-run:1", timeout_s=60, max_tool_calls=3)
    return await model_step.agy(**(args | overrides))


async def test_model_step_returns_the_validated_reply(fake_jobs):
    fake_jobs["results"] = ['```json\n[{"title": "A"}]\n```']
    value, record = await _agy()
    assert value == [Item(title="A")]
    assert record["tokens"] == 100 and record["tool_calls"] == 2
    assert fake_jobs["params"] == {"timeout_s": 60, "max_tool_calls": 3}
    assert fake_jobs["follow_ups"] == []


async def test_model_step_makes_one_repair_call(fake_jobs):
    fake_jobs["results"] = ["I think A", '[{"title": "A"}]']
    value, record = await _agy()
    assert value == [Item(title="A")]
    assert len(fake_jobs["follow_ups"]) == 1 and "no valid JSON" in fake_jobs["follow_ups"][0]
    assert "repair_job" in record

    fake_jobs["results"] = ["still prose", "more prose"]
    with pytest.raises(StepFailed, match="after one repair"):
        await _agy()


async def test_model_step_fails_on_a_failed_job(fake_jobs):
    fake_jobs["status"] = "failed"
    with pytest.raises(StepFailed, match="failed: boom"):
        await _agy()


# ---------------------------------------------------------------- scout


def _ctx(**kw) -> scout.ScoutContext:
    return scout.ScoutContext(run_id=str(RUN_ID), max_ideas=2, **kw)


async def test_scout_dedupes_against_everything_recorded(monkeypatch):
    async def all_titles():
        return ["Strict tool use for Sazed"]

    monkeypatch.setattr(scout.store, "all_titles", all_titles)
    ctx = _ctx(candidates=[scout.Candidate(title="Strict tool use for Sazed"),
                           scout.Candidate(title="Trafilatura for URL ingestion")])
    await scout._dedupe(ctx)
    assert [c.title for c in ctx.fresh] == ["Trafilatura for URL ingestion"]
    assert await scout._nothing_new(ctx) is None
    assert "all already recorded" in await scout._nothing_new(_ctx())


async def test_repo_check_flags_ideas_that_may_exist(monkeypatch):
    queries = []

    async def search_code(term, repos, limit=5):
        queries.append((term, tuple(repos)))
        return ["tharpep/sazed:app/agent/loop.py"] if term == "asyncio.gather" else []

    monkeypatch.setattr(context, "search_code", search_code)
    ctx = _ctx(fresh=[
        scout.Candidate(title="Concurrent reads", repos=["tharpep/sazed"],
                        search_terms=["asyncio.gather", "parallel", "third"]),
        scout.Candidate(title="New thing", search_terms=["nothingmatches"]),
    ])
    record = await scout._repo_check(ctx)
    assert ctx.fresh[0].existing == ["tharpep/sazed:app/agent/loop.py"]
    assert ctx.fresh[1].existing == []
    assert len(queries) == 3  # at most two terms per candidate
    assert record["detail"] == "1 of 2 may already exist"


async def test_store_caps_and_skips_duplicates(monkeypatch):
    created = []

    async def find_duplicate(title):
        return {"id": 1} if title == "Dup" else None

    async def create_item(**kwargs):
        created.append(kwargs)
        return {"id": len(created), **kwargs}

    monkeypatch.setattr(scout.store, "find_duplicate", find_duplicate)
    monkeypatch.setattr(scout.store, "create_item", create_item)
    ctx = _ctx(kept=[scout.Kept(title="Dup"), scout.Kept(title="Good one", effort="small",
                                                        verdict="Worth it."),
                     scout.Kept(title="Over the cap")])
    await scout._store(ctx)
    assert [c["title"] for c in created] == ["Good one"]  # cap 2: "Dup" skipped, third not seen
    assert created[0]["effort"] == "S" and "Second opinion: Worth it." in created[0]["why"]
    assert await scout._none_kept(ctx) is None
    assert scout._summarize(ctx).notify_url.endswith("&item=1")


def test_scout_judge_runs_without_web_tools():
    assert any(s.name == "judge" and s.kind == "model" for s in scout.SPEC.steps)
    assert "don't search the web or open pages" in scout._JUDGE_PROMPT
    assert "{known}" not in scout._RESEARCH_PROMPT  # no list of recorded titles any more


# ---------------------------------------------------------------- action and migration


def test_agent_action_validates_scout_params():
    AgentAction(name="scout", params={"max_ideas": 3})
    with pytest.raises(ValidationError, match="max_ideas"):
        AgentAction(name="scout", params={"max_ideas": 9})
    with pytest.raises(ValidationError):
        AutomationCreate(name="x", trigger={"cron": "0 7 * * *"},
                         action={"type": "pipeline", "name": "scout"})


def test_every_registered_agent_is_an_allowed_action_name():
    allowed = set(AgentAction.model_fields["name"].annotation.__args__)
    assert set(registry.SPECS) == allowed


def _approval_status_map() -> dict[str, str]:
    for _, _, path in reversed(_discover()):
        sql = path.read_text(encoding="utf-8")
        if "FUNCTION activity_on_approval()" in sql:
            body = sql.split("FUNCTION activity_on_approval()", 1)[1].split("END $$", 1)[0]
            case = re.search(r"CASE NEW\.status (WHEN '[^']+' THEN '[^']+'\s*)+ELSE 'error' END",
                             body)
            assert case, "the approval trigger's status CASE wasn't found"
            return dict(re.findall(r"WHEN '([^']+)' THEN '([^']+)'", case.group(0)))
    raise AssertionError("no migration defines activity_on_approval()")


def test_every_approval_status_is_mapped_in_activity():
    mapped = _approval_status_map()
    assert set(mapped) == {"pending", "approved", "denied", "expired"}
    assert mapped["pending"] == "pending"
    assert "error" not in mapped.values()
