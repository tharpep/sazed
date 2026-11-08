"""Pure computations behind the Claude usage read routes."""

import bisect
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.pricing import (
    LONG_PROMPT_TOKENS,
    PRICES_AS_OF,
    US_ONLY_MULTIPLIER,
    WEB_SEARCH_USD,
    rate_for,
    tokens_cost_usd,
)

CONTEXT_SOFT = 200_000
CONTEXT_HARD = 400_000
MEDIAN_MIN_PROMPTS = 5
SERIES_POINTS = 60

LIMITS = {"context_soft": CONTEXT_SOFT, "context_hard": CONTEXT_HARD}
TOKEN_KINDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


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


def priced(row: Mapping[str, Any]) -> tuple[float, float] | None:
    """(cost, cost without prompt caching) in USD for one group of calls, or None if unpriced."""
    if "cost_usd" in row:
        return row["cost_usd"], row["cost_usd"]
    model = row["model"]
    rate = model and rate_for(
        model, LONG_PROMPT_TOKENS + 1 if row["long_prompt"] else 0, row.get("fast", False)
    )
    if not rate:
        return None
    region = US_ONLY_MULTIPLIER if row.get("us_only") else 1.0
    searches = (row.get("web_searches") or 0) * WEB_SEARCH_USD
    tokens = [row[k] or 0 for k in TOKEN_KINDS]
    cost = tokens_cost_usd(rate, *tokens, row.get("cache_write_1h_tokens") or 0)
    prompt = tokens[0] + tokens[2] + tokens[3]
    uncached = tokens_cost_usd(rate, prompt, tokens[1])
    return cost * region + searches, uncached * region + searches


def source_of(row: Mapping[str, Any]) -> str:
    if "source" in row:
        return row["source"]
    return "subagents" if row["sidechain"] else "main"


def usd(value: float) -> float:
    return round(value, 4)


def _tally() -> dict[str, Any]:
    return {"usd": 0.0, "calls": 0, **dict.fromkeys(TOKEN_KINDS, 0)}


def _add(tally: dict[str, Any], row: Mapping[str, Any], cost: float) -> None:
    tally["usd"] += cost
    tally["calls"] += row["calls"]
    for kind in TOKEN_KINDS:
        tally[kind] += row[kind] or 0


def _rounded(tally: Mapping[str, Any]) -> dict[str, Any]:
    return {**tally, "usd": usd(tally["usd"])}


CLAUDE_CODE_SOURCES = ("main", "subagents", "side_calls")


def build_spend(rows: Iterable[Mapping[str, Any]], prompts: int) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    models: dict[str, dict[str, Any]] = {}
    side_calls: dict[str, dict[str, Any]] = {}
    unpriced: Counter[str] = Counter()
    saved = 0.0
    for row in rows:
        costs = priced(row)
        if costs is None:
            unpriced[row["model"] or "unknown"] += row["calls"]
            continue
        cost, uncached = costs
        source = source_of(row)
        _add(sources.setdefault(source, _tally()), row, cost)
        _add(models.setdefault(row["model"], _tally()), row, cost)
        if source == "side_calls":
            _add(side_calls.setdefault(row["query_source"] or "unknown", _tally()), row, cost)
        if source in ("main", "subagents"):
            saved += uncached - cost
    total = sum(t["usd"] for t in sources.values())
    claude_code = sum(sources.get(k, {}).get("usd", 0.0) for k in CLAUDE_CODE_SOURCES)
    return {
        "prices_as_of": PRICES_AS_OF,
        "total_usd": usd(total),
        "cost_per_prompt_usd": usd(claude_code / prompts) if prompts else None,
        "cache_savings_usd": usd(saved),
        "sources": [
            {"source": name, **_rounded(t)}
            for name, t in sorted(sources.items(), key=lambda kv: -kv[1]["usd"])
        ],
        "models": [
            {"model": name, **_rounded(t), "share": rate(round(t["usd"] * 1e4), round(total * 1e4))}
            for name, t in sorted(models.items(), key=lambda kv: -kv[1]["usd"])
        ],
        "side_calls": [
            {"query_source": name, **_rounded(t)}
            for name, t in sorted(side_calls.items(), key=lambda kv: -kv[1]["usd"])
        ],
        "unpriced": [{"model": m, "calls": n} for m, n in unpriced.most_common()],
    }


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
                **dict.fromkeys(TOKEN_KINDS, 0),
                "spend": dict.fromkeys((*CLAUDE_CODE_SOURCES, "sazed_api", "sazed_jobs"), 0.0),
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
    for r in [*rows["spend"], *rows["side_spend"], *rows["sazed_spend"]]:
        w = week(r["day"])
        source = source_of(r)
        if source in ("main", "subagents"):
            for kind in TOKEN_KINDS:
                w[kind] += r[kind] or 0
        costs = priced(r)
        if costs:
            w["spend"][source] += costs[0]
    return [
        {
            "week": name,
            "prompts": w["prompts"],
            "sessions": len(w["sessions"]),
            "confusion_rate": rate(w["confused"], w["prompts"]),
            "bash_writes": w["bash_writes"],
            "edit_writes": w["edit_writes"],
            **{kind: w[kind] for kind in TOKEN_KINDS},
            "cost_usd": usd(sum(w["spend"].values())),
            "main_cost_usd": usd(w["spend"]["main"]),
            "subagent_cost_usd": usd(w["spend"]["subagents"]),
            "side_cost_usd": usd(w["spend"]["side_calls"]),
            "sazed_cost_usd": usd(w["spend"]["sazed_api"] + w["spend"]["sazed_jobs"]),
            "cost_per_prompt_usd": (
                usd(sum(w["spend"][k] for k in CLAUDE_CODE_SOURCES) / w["prompts"])
                if w["prompts"]
                else None
            ),
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
        "spend": build_spend([*rows["spend"], *rows["side_spend"], *rows["sazed_spend"]], prompts),
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
    spend: dict[tuple[str, str], dict[str, float]] = {}
    for r in [*rows["spend"], *rows["side_spend"]]:
        costs = priced(r)
        if costs:
            totals = spend.setdefault(
                (r["machine"], r["session_id"]), dict.fromkeys(CLAUDE_CODE_SOURCES, 0.0)
            )
            totals[source_of(r)] += costs[0]
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
                "cost_usd": usd(sum(spend.get(key, {}).values())),
                "subagent_cost_usd": usd(spend.get(key, {}).get("subagents", 0.0)),
                "side_cost_usd": usd(spend.get(key, {}).get("side_calls", 0.0)),
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


WORKER_MACHINE = "homelab"
SAZED_MACHINE = "sazed"
PROJECTION_MIN_SECONDS = 600


def _day_row(
    row: Mapping[str, Any], source: str, machine: str, kind: str, cost: float | None
) -> dict[str, Any]:
    return {
        "day": row["day"].isoformat(),
        "source": source,
        "machine": machine,
        "model": row["model"],
        "kind": kind,
        "calls": row["calls"],
        **{k: row[k] or 0 for k in TOKEN_KINDS},
        "thinking_tokens": row.get("thinking_tokens") or 0,
        "usd": None if cost is None else usd(cost),
    }


def hub_days(rows: Mapping[str, Any]) -> tuple[list[dict[str, Any]], Counter[str]]:
    """One row per (day, source, machine, model): tokens, and cost where there is a price."""
    merged: dict[tuple[str, str, str, str | None, str], dict[str, Any]] = {}
    unpriced: Counter[str] = Counter()

    def add(row, source, machine, kind):
        cost = None
        if kind != "tokens":
            costs = priced(row)
            if costs is None:
                unpriced[row["model"] or "unknown"] += row["calls"]
            else:
                cost = costs[0]
        item = _day_row(row, source, machine, kind, cost)
        key = (item["day"], source, machine, item["model"], kind)
        have = merged.get(key)
        if have is None:
            merged[key] = item
            return
        for k in ("calls", *TOKEN_KINDS, "thinking_tokens"):
            have[k] += item[k]
        if item["usd"] is not None:
            have["usd"] = usd((have["usd"] or 0.0) + item["usd"])

    for r in rows["claude_code"]:
        add(r, "claude_code", r["machine"], "api_equivalent")
    for r in rows["side_calls"]:
        add(r, "claude_code", r["machine"], "api_equivalent")
    for r in rows["sazed_api"]:
        add(r, "sazed_api", SAZED_MACHINE, "billed")
    for r in rows["jobs"]:
        if r["agent"] == "claude":
            add(r, "worker_claude", WORKER_MACHINE, "api_equivalent")
        else:
            add(r, f"worker_{r['agent']}", WORKER_MACHINE, "tokens")
    days = sorted(merged.values(), key=lambda d: (d["day"], d["source"], d["machine"]))
    return days, unpriced


WINDOW_SPAN = {"five_hour": timedelta(hours=5), "seven_day": timedelta(days=7)}
PACE_LOOKBACK = {"five_hour": timedelta(hours=1), "seven_day": timedelta(days=1)}
PROBE_EVERY = timedelta(minutes=10)
ACTIVE_WITHIN = timedelta(minutes=15)
NO_PACE = {"even_pct": None, "projected_pct": None, "full_at": None, "allowance_per_day": None}


def _pace(readings: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    """Even pace, where the recent rate lands at reset, and when it would reach 100%."""
    last = readings[-1]
    resets, window = last["resets_at"], last["plan_window"]
    if resets is None or resets <= now:
        return dict(NO_PACE)
    span = WINDOW_SPAN[window]
    elapsed = (now - (resets - span)) / span
    left_days = (resets - now).total_seconds() / 86_400
    out = {
        **NO_PACE,
        "even_pct": round(min(max(elapsed * 100, 0.0), 100.0), 1),
        "allowance_per_day": round(max(100 - last["used_pct"], 0) / left_days, 1)
        if window == "seven_day"
        else None,
    }
    same = [r for r in readings if r["resets_at"] == resets]
    recent = [r for r in same if r["read_at"] >= last["read_at"] - PACE_LOOKBACK[window]]
    first = recent[0]
    if (last["read_at"] - first["read_at"]).total_seconds() < PROJECTION_MIN_SECONDS:
        first = same[0]
    seconds = (last["read_at"] - first["read_at"]).total_seconds()
    if seconds < PROJECTION_MIN_SECONDS:
        return out
    rate = max((last["used_pct"] - first["used_pct"]) / seconds, 0.0)
    ahead = (resets - last["read_at"]).total_seconds()
    out["projected_pct"] = round(min(last["used_pct"] + rate * ahead, 100.0), 1)
    if rate > 0 and last["used_pct"] < 100:
        full = last["read_at"] + timedelta(seconds=(100 - last["used_pct"]) / rate)
        out["full_at"] = iso(full) if full < resets else None
    return out


def hub_limits(readings: Iterable[Mapping[str, Any]], now: datetime) -> list[dict[str, Any]]:
    by_window: dict[str, list[Mapping[str, Any]]] = {}
    for r in readings:
        by_window.setdefault(r["plan_window"], []).append(r)
    out = []
    for window in ("five_hour", "seven_day"):
        series = by_window.get(window)
        if not series:
            continue
        last = series[-1]
        out.append(
            {
                "window": window,
                "used_pct": round(last["used_pct"], 1),
                "resets_at": iso(last["resets_at"]) if last["resets_at"] else None,
                "read_at": iso(last["read_at"]),
                "status": last["status"],
                "source": last["source"],
                "machine": last.get("machine"),
                "reset_since": bool(last["resets_at"] and last["resets_at"] <= now),
                **_pace(series, now),
                "points": [
                    {"read_at": iso(r["read_at"]), "used_pct": round(r["used_pct"], 1)}
                    for r in series
                ],
            }
        )
    return out


def _days_in_month(day: date) -> int:
    nxt = date(day.year + day.month // 12, day.month % 12 + 1, 1)
    return (nxt - date(day.year, day.month, 1)).days


def plan_value(
    periods: Sequence[Mapping[str, Any]], days: Sequence[Mapping[str, Any]], frm: date, to: date
) -> dict[str, Any] | None:
    """API-equivalent spend over the range next to what the plan cost for the same days."""
    if not periods:
        return None
    starts = [p["starts_on"] for p in periods]
    plan_usd = 0.0
    day = max(frm, starts[0])
    while day <= to:
        period = periods[bisect.bisect_right(starts, day) - 1]
        plan_usd += period["monthly_usd"] / _days_in_month(day)
        day += timedelta(days=1)
    api = sum(d["usd"] or 0.0 for d in days if d["kind"] == "api_equivalent")
    current = periods[-1]
    return {
        "plan": current["plan"],
        "monthly_usd": current["monthly_usd"],
        "since": max(frm, starts[0]).isoformat(),
        "plan_usd": usd(plan_usd),
        "api_equivalent_usd": usd(api),
        "multiple": round(api / plan_usd, 1) if plan_usd else None,
    }


def probe_due(state: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """Whether the homelab probe reads now: only in an open 5-hour window or after activity."""
    last_read = state.get("last_read")
    if last_read and now - last_read < PROBE_EVERY:
        return {"due": False, "reason": "fresh"}
    resets = state.get("five_hour_resets")
    if resets and resets > now:
        return {"due": True, "reason": "window_open"}
    active = state.get("last_active")
    if active and now - active < ACTIVE_WITHIN:
        return {"due": True, "reason": "active"}
    return {"due": False, "reason": "idle"}


def build_hub(
    rows: Mapping[str, Any],
    frm: datetime,
    to: datetime,
    timezone: str,
    billed: Mapping[str, Any],
) -> dict[str, Any]:
    days, unpriced = hub_days(rows)
    local = ZoneInfo(timezone)
    return {
        "range": {"from": iso(frm), "to": iso(to)},
        "timezone": timezone,
        "prices_as_of": PRICES_AS_OF,
        "limits": hub_limits(rows["readings"], to),
        "billed": dict(billed),
        "value": plan_value(
            rows["plans"], days, frm.astimezone(local).date(), to.astimezone(local).date()
        ),
        "days": days,
        "sources": [
            {
                "source": r["source"],
                "machine": r["machine"]
                or (SAZED_MACHINE if r["source"] == "sazed_api" else WORKER_MACHINE),
                "last_at": iso(r["last_at"]) if r["last_at"] else None,
            }
            for r in rows["fresh"]
        ],
        "unpriced": [{"model": m, "calls": n} for m, n in unpriced.most_common()],
    }
