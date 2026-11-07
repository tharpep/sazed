"""Plan text: sections with stable ids, word-level diffs between versions, and the rewrite guard."""

import re
from difflib import SequenceMatcher
from typing import Any

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*(?:\{#([a-z0-9][a-z0-9-]*)\})?\s*$")
_WORD = re.compile(r"\S+|\s+")
REWRITE_SIMILARITY = 0.5


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "section"


def sections(body: str) -> list[dict[str, Any]]:
    """Each heading starts a section; a trailing `{#id}` pins its id, else its title is used."""
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    current: dict[str, Any] | None = None
    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            sid = m.group(3) or slugify(m.group(2))
            seen[sid] = seen.get(sid, 0) + 1
            if seen[sid] > 1:
                sid = f"{sid}-{seen[sid]}"
            current = {"id": sid, "title": m.group(2), "level": len(m.group(1)), "lines": []}
            out.append(current)
        elif current is not None:
            current["lines"].append(line)
    for s in out:
        s["text"] = "\n".join(s.pop("lines")).strip()
    return out


def preamble(body: str) -> str:
    """The text before the first heading."""
    lines = []
    for line in body.splitlines():
        if _HEADING.match(line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def section_text(body: str, section_id: str) -> str | None:
    return next((s["text"] for s in sections(body) if s["id"] == section_id), None)


def _norm(block: str) -> str:
    return " ".join(block.split())


def blocks(body: str) -> list[str]:
    return [b.strip() for b in re.split(r"\n\s*\n", body) if b.strip()]


def similarity(old: str, new: str) -> float:
    return SequenceMatcher(None, old.split(), new.split(), autojunk=False).ratio()


def is_rewrite(old: str, new: str) -> bool:
    return bool(old.strip()) and similarity(old, new) < REWRITE_SIMILARITY


def _words(old: str, new: str) -> list[dict[str, str]]:
    a, b = _WORD.findall(_norm(old)), _WORD.findall(_norm(new))
    segs: list[dict[str, str]] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            segs.append({"op": "same", "text": "".join(a[i1:i2])})
        if op in ("delete", "replace"):
            segs.append({"op": "del", "text": "".join(a[i1:i2])})
        if op in ("insert", "replace"):
            segs.append({"op": "add", "text": "".join(b[j1:j2])})
    return segs


def diff(old: str, new: str) -> list[dict[str, Any]]:
    """Block-level diff with word-level changes inside changed blocks; unchanged runs folded."""
    a, b = blocks(old), blocks(new)
    na, nb = [_norm(x) for x in a], [_norm(x) for x in b]
    out: list[dict[str, Any]] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, na, nb, autojunk=False).get_opcodes():
        if op == "equal":
            heads = [x.splitlines()[0] for x in b[j1:j2] if x.startswith("#")]
            out.append({"kind": "same", "count": j2 - j1, "headings": heads})
        elif op == "delete":
            out += [{"kind": "del", "text": x} for x in a[i1:i2]]
        elif op == "insert":
            out += [{"kind": "add", "text": x} for x in b[j1:j2]]
        else:
            pairs = min(i2 - i1, j2 - j1)
            for k in range(pairs):
                out.append({"kind": "chg", "segments": _words(a[i1 + k], b[j1 + k])})
            out += [{"kind": "del", "text": x} for x in a[i1 + pairs : i2]]
            out += [{"kind": "add", "text": x} for x in b[j1 + pairs : j2]]
    return out
