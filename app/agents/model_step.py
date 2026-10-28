"""The model step contract (plans/12 section 3): a short prompt built from named fields, a JSON
reply validated against a schema, and one repair call in the same session if it doesn't match."""

import json
import re
import uuid
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.agents.steps import StepFailed
from app.jobs import service as jobs

_REPAIR = (
    "Your reply didn't match the required format: {error}\n\n"
    "Reply again with only the JSON, in a ```json block, in the format the first message asked "
    "for. Don't search or open pages again."
)


def parse(text: str | None, schema: Any) -> tuple[Any, str | None]:
    """The JSON in a reply (fenced or bare), validated against `schema`: (value, None) or
    (None, the error to send back)."""
    text = text or ""
    fenced = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
        start = min(starts) if starts else -1
        end = max(text.rfind("]"), text.rfind("}"))
        raw = text[start : end + 1] if start >= 0 and end > start else ""
    try:
        return TypeAdapter(schema).validate_python(json.loads(raw)), None
    except json.JSONDecodeError as e:
        return None, f"no valid JSON found ({e.msg})"
    except ValidationError as e:
        return None, str(e)[:800]


async def _settle(job: dict[str, Any], timeout_s: int) -> dict[str, Any]:
    done = await jobs.wait_for(job["id"], timeout_s + 300)
    if done is None:
        raise StepFailed(f"job {job['id']} didn't finish in time")
    if done["status"] != "succeeded":
        raise StepFailed(f"job {done['id']} {done['status']}: {done.get('error') or ''}".strip())
    return done


def _usage(job: dict[str, Any]) -> dict[str, Any]:
    meta = job.get("meta") or {}
    usage = meta.get("usage") or {}
    return {"tokens": usage.get("total_tokens"), "tool_calls": meta.get("tool_calls")}


async def agy(
    *,
    kind: str,
    title: str,
    prompt: str,
    schema: Any,
    model: str,
    requested_by: str,
    timeout_s: int,
    max_tool_calls: int,
) -> tuple[Any, dict[str, Any]]:
    """Run one agy job and return (validated reply, record fields for the run)."""
    job = await jobs._create_agy_job(
        kind, title, prompt, {"timeout_s": timeout_s, "max_tool_calls": max_tool_calls},
        requested_by, model,
    )
    done = await _settle(job, timeout_s)
    value, error = parse(done.get("result"), schema)
    record: dict[str, Any] = {"job": str(job["id"]), **_usage(done)}
    if error is None:
        return value, record

    follow = await jobs.create_follow_up(
        uuid.UUID(str(job["id"])), _REPAIR.format(error=error), requested_by
    )
    done = await _settle(follow, timeout_s)
    value, error = parse(done.get("result"), schema)
    record["repair_job"] = str(follow["id"])
    if error is not None:
        raise StepFailed(f"{title}: the reply didn't match its schema after one repair: {error}")
    return value, record
