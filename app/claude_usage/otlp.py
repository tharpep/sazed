"""Claude Code api_request events from an OTLP/JSON logs export."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

MACHINE_ATTRIBUTE = "sazed.machine"
_MACHINE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_TOKENS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cache_creation_tokens": "cache_write_tokens",
    "cache_creation_1h_tokens": "cache_write_1h_tokens",
}
_TEXT = ("model", "query_source", "speed", "effort")


def _value(any_value: Any) -> Any:
    if not isinstance(any_value, dict):
        return None
    for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if kind in any_value:
            return any_value[kind]
    return None


def _attributes(items: Any) -> dict[str, Any]:
    if not isinstance(items, list):
        return {}
    return {
        item["key"]: _value(item.get("value"))
        for item in items
        if isinstance(item, dict) and isinstance(item.get("key"), str)
    }


def _count(value: Any) -> int:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str | None:
    return str(value)[:500] if value not in (None, "") else None


def _timestamp(attrs: dict[str, Any], record: dict[str, Any]) -> datetime | None:
    stamp = attrs.get("event.timestamp")
    if isinstance(stamp, str):
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            pass
    nanos = _count(record.get("timeUnixNano"))
    return datetime.fromtimestamp(nanos / 1e9, UTC) if nanos else None


def _row(attrs: dict[str, Any], record: dict[str, Any], machine: str) -> dict[str, Any] | None:
    request_id = _text(attrs.get("request_id") or attrs.get("client_request_id"))
    ts = _timestamp(attrs, record)
    try:
        cost = Decimal(str(attrs.get("cost_usd")))
    except InvalidOperation:
        return None
    if request_id is None or ts is None or not cost.is_finite() or cost < 0:
        return None
    return {
        "request_id": request_id,
        "machine": machine,
        "session_id": _text(attrs.get("session.id")),
        "ts": ts,
        **{name: _text(attrs.get(name)) for name in _TEXT},
        **{column: _count(attrs.get(key)) for key, column in _TOKENS.items()},
        "cost_usd": cost,
    }


def api_requests(payload: Any) -> list[dict[str, Any]]:
    """One row per api_request event; every other event and every malformed record is dropped."""
    rows: list[dict[str, Any]] = []
    resource_logs = payload.get("resourceLogs") if isinstance(payload, dict) else None
    for resource_log in resource_logs if isinstance(resource_logs, list) else []:
        if not isinstance(resource_log, dict):
            continue
        resource = _attributes((resource_log.get("resource") or {}).get("attributes"))
        for scope_log in resource_log.get("scopeLogs") or []:
            for record in (scope_log or {}).get("logRecords") or []:
                if not isinstance(record, dict):
                    continue
                attrs = {**resource, **_attributes(record.get("attributes"))}
                if attrs.get("event.name") != "api_request":
                    continue
                machine = str(attrs.get(MACHINE_ATTRIBUTE) or "")
                row = _row(attrs, record, machine if _MACHINE.match(machine) else "unknown")
                if row is not None:
                    rows.append(row)
    return rows
