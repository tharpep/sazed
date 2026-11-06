"""Pure computations behind the Claude usage read routes."""

import bisect
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

CONTEXT_SOFT = 200_000
CONTEXT_HARD = 400_000
MEDIAN_MIN_PROMPTS = 5
SERIES_POINTS = 60

LIMITS = {"context_soft": CONTEXT_SOFT, "context_hard": CONTEXT_HARD}


def context_size(input_tokens: int, cache_read_tokens: int, cache_write_tokens: int) -> int:
    return input_tokens + cache_read_tokens + cache_write_tokens


def median(values: Iterable[float]) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def rate(numerator: int, denominator: int, digits: int = 3) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, digits)


def iso_week(day: date | datetime) -> str:
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def sample_series(points: Sequence[Mapping[str, Any]], limit: int = SERIES_POINTS) -> list[Any]:
    """Evenly spaced points in time order, at most `limit`, always including the peak context."""
    if len(points) <= limit:
        return list(points)
    peak = max(range(len(points)), key=lambda i: points[i]["context"])
    last = len(points) - 1
    picked = sorted({round(i * last / (limit - 1)) for i in range(limit)})
    if peak not in picked:
        nearest = min(range(len(picked)), key=lambda i: abs(picked[i] - peak))
        picked[nearest] = peak
        picked.sort()
    return [points[i] for i in picked]


def context_before(series: Sequence[tuple[datetime, int]], ts: datetime) -> int:
    """Context of the latest point strictly before `ts` in a time-ordered series, else 0."""
    index = bisect.bisect_left([point[0] for point in series], ts)
    return series[index - 1][1] if index else 0


def main_model(models: Iterable[str | None]) -> str | None:
    counts = Counter(model for model in models if model)
    if not counts:
        return None
    top = max(counts.values())
    return min(model for model, n in counts.items() if n == top)


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def _named(rows: Iterable[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    return [{key: r["name"], "count": r["n"]} for r in rows]


def _weekly(rows: Mapping[str, Any]) -> list[dict[str, Any]]:
    weeks: dict[str, dict[str, Any]] = {}

    def week(day: date) -> dict[str, Any]:
        return weeks.setdefault(
            iso_week(day),
            {
                "prompts": 0,
                "sessions": set(),
                "confused": 0,
                "bash_writes": 0,
                "edit_writes": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
            },
        )

    for r in rows["prompt_days"]:
        w = week(r["day"])
        w["prompts"] += r["prompts"]
        w["confused"] += r["confused"]
        w["sessions"].add((r["machine"], r["session_id"]))
    for r in rows["tool_days"]:
        w = week(r["day"])
        w["bash_writes"] += r["bash_writes"]
        w["edit_writes"] += r["edit_writes"]
    for r in rows["response_days"]:
        w = week(r["day"])
        w["output_tokens"] += r["output_tokens"]
        w["cache_read_tokens"] += r["cache_read_tokens"]
    return [
        {
            "week": name,
            "prompts": w["prompts"],
            "sessions": len(w["sessions"]),
            "confusion_rate": rate(w["confused"], w["prompts"]),
            "bash_writes": w["bash_writes"],
            "edit_writes": w["edit_writes"],
            "output_tokens": w["output_tokens"],
            "cache_read_tokens": w["cache_read_tokens"],
        }
        for name, w in sorted(weeks.items())
    ]


def build_overview(rows: Mapping[str, Any], frm: datetime, to: datetime) -> dict[str, Any]:
    prompts = sum(r["prompts"] for r in rows["prompt_days"])
    confused = sum(r["confused"] for r in rows["prompt_days"])
    sessions = {(r["machine"], r["session_id"]) for r in rows["prompt_days"]}
    peaks = [peak for peak, _ in rows["peaks"]]
    working = [peak for peak, n in rows["peaks"] if n >= MEDIAN_MIN_PROMPTS]
    bash_writes = sum(r["bash_writes"] for r in rows["tool_days"])
    edit_writes = sum(r["edit_writes"] for r in rows["tool_days"])
    tool_calls = sum(r["calls"] for r in rows["tool_days"])
    tool_ratio = rate(tool_calls, prompts, 2)
    model_total = sum(r["n"] for r in rows["models"])
    return {
        "range": {"from": iso(frm), "to": iso(to)},
        "limits": LIMITS,
        "machines": list(rows["machines"]),
        "tiles": {
            "sessions": len(sessions),
            "prompts": prompts,
            "peak_context_max": max(peaks, default=None),
            "peak_context_median": median(working),
            "sessions_over_soft": sum(p > CONTEXT_SOFT for p in peaks),
            "sessions_over_hard": sum(p > CONTEXT_HARD for p in peaks),
            "confusion_rate": rate(confused, prompts),
            "bash_writes": bash_writes,
            "edit_writes": edit_writes,
            "bash_write_share": rate(bash_writes, bash_writes + edit_writes),
            "tool_calls_per_prompt": tool_ratio,
            "skills_used": len(rows["skills"]),
        },
        "weekly": _weekly(rows),
        "models": [
            {"model": r["model"], "responses": r["n"], "share": rate(r["n"], model_total)}
            for r in rows["models"]
        ],
        "skills": _named(rows["skills"], "name"),
        "subagents": [{"type": r["name"], "count": r["n"]} for r in rows["subagents"]],
        "slash_commands": [{"command": r["name"], "count": r["n"]} for r in rows["slash_commands"]],
        "styles": [
            {
                "style": r["style"],
                "prompts": r["prompts"],
                "confusion_rate": rate(r["confused"], r["prompts"]),
            }
            for r in rows["styles"]
        ],
    }


def build_sessions(rows: Mapping[str, Any]) -> dict[str, Any]:
    responses: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for r in rows["responses"]:
        responses.setdefault((r["machine"], r["session_id"]), []).append(r)
    counts: dict[tuple[str, str, str], int] = {
        (r["machine"], r["session_id"], r["kind"]): r["n"] for r in rows["counts"]
    }
    confused: dict[tuple[str, str], list[datetime]] = {}
    for r in rows["confused"]:
        confused.setdefault((r["machine"], r["session_id"]), []).append(r["ts"])

    out = []
    for s in rows["sessions"]:
        key = (s["machine"], s["session_id"])
        main = responses.get(key, [])
        timeline = [(r["ts"], r["context"]) for r in main]
        series = sample_series([{"ts": iso(ts), "context": c} for ts, c in timeline])
        out.append(
            {
                "session_id": s["session_id"],
                "machine": s["machine"],
                "project": s["project"],
                "title": s["title"],
                "first_ts": iso(s["first_ts"]),
                "last_ts": iso(s["last_ts"]),
                "prompts": s["prompts"],
                "peak_context": max((c for _, c in timeline), default=None),
                "main_model": main_model(r["model"] for r in main),
                "subagents": counts.get((*key, "subagents"), 0),
                "compactions": counts.get((*key, "compact"), 0),
                "interrupts": counts.get((*key, "interrupt"), 0),
                "confused_prompts": [
                    {"ts": iso(ts), "context": context_before(timeline, ts)}
                    for ts in sorted(confused.get(key, []))
                ],
                "context_series": series,
            }
        )
    return {"limits": LIMITS, "sessions": out}
