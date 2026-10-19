"""Weekly architecture map check (pipeline action `map`).

The map itself is live (gateway registry/map.yaml + health + the Activity stream). This run takes
a weekly snapshot and compares it with the last one:
- structure: nodes added or removed, registry services that aren't on the map yet;
- traffic: each line's events this week vs last week, from the same signals the console uses;
- drift: lines that saw nothing (quiet ones excepted) and activity that matches no line, which
  usually means the map is missing an arrow.
Services missing from the map are also filed in the backlog (deduped there).
"""

import json
import re
import uuid
from typing import Any

from app.backlog import service as backlog
from app.config import settings
from app.db import get_pool
from app.http_client import get_client


def _matches(signal: dict[str, Any], kind: str, title: str, agent: str | None) -> bool:
    if not re.fullmatch(signal["kind"], kind):
        return False
    if signal.get("title") and not re.search(signal["title"], title, re.IGNORECASE):
        return False
    return not (signal.get("agent") and agent != signal["agent"])


async def _fetch_map() -> dict[str, Any]:
    resp = await get_client().get(
        f"{settings.gateway_url}/homelab/map",
        headers={"X-API-Key": settings.gateway_api_key},
        timeout=60.0,
    )
    resp.raise_for_status()
    return resp.json()


async def _week_traffic(edges: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int]]:
    """Events per line over 7 days, and counts of event kinds no line claims."""
    rows = await get_pool().fetch(
        """
        SELECT kind, title, refs->>'agent' AS agent FROM activity_events
        WHERE at > NOW() - INTERVAL '7 days'
        """
    )
    counts = {f"{e['from']}>{e['to']}": 0 for e in edges}
    unmatched: dict[str, int] = {}
    for r in rows:
        hit = False
        for e in edges:
            if any(_matches(s, r["kind"], r["title"], r["agent"]) for s in e["signals"]):
                counts[f"{e['from']}>{e['to']}"] += 1
                hit = True
        if not hit:
            unmatched[r["kind"]] = unmatched.get(r["kind"], 0) + 1
    return counts, unmatched


async def run(run_id: uuid.UUID) -> tuple[str, str, str | None, str | None]:
    arch = await _fetch_map()
    nodes = {n["id"]: n for n in arch["nodes"]}
    edges = arch["edges"]
    counts, unmatched = await _week_traffic(edges)
    labels = {n["id"]: n["label"] for n in arch["nodes"]}

    prev_rec = await get_pool().fetchrow(
        "SELECT data FROM map_snapshots ORDER BY at DESC LIMIT 1"
    )
    prev = json.loads(prev_rec["data"]) if prev_rec else None
    snapshot = {
        "nodes": {i: {"label": n["label"], "status": n["status"], "placed": n["placed"]}
                  for i, n in nodes.items()},
        "traffic": counts,
    }
    await get_pool().execute(
        "INSERT INTO map_snapshots (data) VALUES ($1::jsonb)", json.dumps(snapshot)
    )

    def line(key: str) -> str:
        a, b = key.split(">")
        return f"{labels.get(a, a)} → {labels.get(b, b)}"

    changes: list[str] = []
    drift: list[str] = []
    if prev:
        added = [labels[i] for i in nodes if i not in prev["nodes"]]
        removed = [v["label"] for i, v in prev["nodes"].items() if i not in nodes]
        if added:
            changes.append("New on the map: " + ", ".join(added))
        if removed:
            changes.append("Gone from the map: " + ", ".join(removed))
        for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            before = prev["traffic"].get(key, 0)
            if n >= 10 and n >= 2 * max(before, 1):
                changes.append(f"{line(key)}: {n} events, up from {before}")
            elif before >= 10 and n <= before / 2:
                changes.append(f"{line(key)}: {n} events, down from {before}")
    else:
        changes.append("First weekly snapshot; next week's run will compare against it.")

    unplaced = [n for n in arch["nodes"] if not n["placed"]]
    for n in unplaced:
        drift.append(f"{n['label']} is in the registry but not on the map")
        await backlog.add_item(
            type="gap",
            title=f"Put {n['label']} on the architecture map",
            source="map",
            summary=(
                f"`{n['services'][0]['id'] if n['services'] else n['id']}` is in the gateway "
                "registry but no node in `registry/map.yaml` covers it, so the map shows it as "
                "an unplaced box. Give it a node (or add it to an existing one's services) and "
                "the lines it uses."
            ),
            why="Keeps the Homelab map a true picture of the system.",
            repos=["tharpep/api-gateway"],
        )
    for e in edges:
        key = f"{e['from']}>{e['to']}"
        if not e.get("quiet") and counts.get(key, 0) == 0:
            drift.append(f"{line(key)} saw no traffic this week")
    for kind, n in sorted(unmatched.items(), key=lambda kv: -kv[1]):
        drift.append(f"{n} {kind} events match no line on the map")
    down = [n["label"] for n in arch["nodes"] if n["status"] == "down"]

    lines = ["## Architecture map, weekly"]
    if down:
        lines.append(f"Down right now: {', '.join(down)}")
    lines.append("\n### What changed\n" + ("\n".join(f"- {c}" for c in changes) or "- Nothing"))
    lines.append("\n### Drift\n" + ("\n".join(f"- {d}" for d in drift) or "- None"))
    busiest = sorted(counts.items(), key=lambda kv: -kv[1])[:5]
    lines.append(
        "\n### Busiest lines this week\n"
        + "\n".join(f"- {line(k)}: {n}" for k, n in busiest if n)
    )
    text = "\n".join(lines)

    if prev and not changes and not drift and not down:
        return "skipped", text, None, None
    parts = []
    if changes and prev:
        parts.append(f"{len(changes)} change{'s' if len(changes) != 1 else ''}")
    if drift:
        parts.append(f"{len(drift)} drift")
    if down:
        parts.append(f"down: {', '.join(down)}")
    gist = "; ".join(parts) or "First snapshot taken"
    return "success", text, gist, f"{settings.console_url}/?page=homelab"
