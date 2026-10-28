"""Runs an agent's steps in order, records each one on the run, and pauses or resumes."""

import logging
import time
import uuid
from typing import Any

from app.agents import approvals
from app.agents.steps import AgentSpec, Outcome, Step
from app.automations import store as run_store

logger = logging.getLogger(__name__)


def _spec(name: str) -> AgentSpec:
    from app.agents.registry import SPECS  # lazy: the agents import the job service

    if name not in SPECS:
        raise ValueError(f"unknown agent '{name}'")
    return SPECS[name]


async def run(action: dict[str, Any], run_id: uuid.UUID) -> Outcome:
    """Run an automation's agent action from its first step."""
    spec = _spec(action["name"])
    ctx = spec.context(run_id=str(run_id), **(action.get("params") or {}))
    return await _run_from(spec, ctx, run_id, 0, [])


async def resume(approval: dict[str, Any]) -> Outcome:
    """Continue a run after Pryce approved its pause step, from the step after it."""
    payload = approval["payload"]
    spec = _spec(payload["agent"])
    ctx = spec.context.model_validate(payload["context"])
    records: list[dict[str, Any]] = payload["steps"]
    records[-1]["status"] = "approved"
    return await _run_from(spec, ctx, uuid.UUID(payload["run_id"]), payload["step"] + 1, records)


async def _run_from(
    spec: AgentSpec, ctx: Any, run_id: uuid.UUID, start: int, records: list[dict[str, Any]]
) -> Outcome:
    for index in range(start, len(spec.steps)):
        step = spec.steps[index]
        record: dict[str, Any] = {"name": step.name, "kind": step.kind, "status": "running"}
        records.append(record)
        await run_store.set_steps(run_id, records)
        t0 = time.monotonic()
        try:
            result = await _attempts(step, ctx, record)
        except Exception as e:
            record.update(status="error", detail=f"{type(e).__name__}: {e}"[:500],
                          duration_ms=int((time.monotonic() - t0) * 1000))
            await run_store.set_steps(run_id, records)
            raise
        record["duration_ms"] = int((time.monotonic() - t0) * 1000)

        if step.kind == "gate" and result:
            record.update(status="stopped", detail=str(result)[:500])
            await run_store.set_steps(run_id, records)
            return Outcome("skipped", str(result))
        if step.kind == "pause" and result:
            record.update(status="paused", detail=str(result)[:500])
            approval_id = await approvals.create(
                kind="agent_step",
                title=str(result),
                requested_by=f"automation-run:{run_id}",
                payload={"agent": spec.name, "run_id": str(run_id), "step": index,
                         "context": ctx.model_dump(mode="json"), "steps": records},
            )
            record["approval"] = str(approval_id)
            await run_store.set_steps(run_id, records)
            logger.info("agent %s run %s paused at %s", spec.name, run_id, step.name)
            return Outcome("needs_approval", f"Waiting for your approval: {result}",
                           notify_text=str(result))
        if isinstance(result, dict):
            record.update(result)
        record["status"] = "ok"
        await run_store.set_steps(run_id, records)
    return spec.summarize(ctx)


async def _attempts(step: Step, ctx: Any, record: dict[str, Any]) -> Any:
    attempts = 0
    while True:
        attempts += 1
        result = await step.run(ctx)
        if step.repeat_until is None:
            return result
        if step.repeat_until(ctx) or attempts >= step.max_attempts:
            record["attempts"] = attempts
            return result
