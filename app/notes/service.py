"""Filing real notes: one captured note, and the sweep over the inbox."""

import logging

import anthropic

from app.config import settings
from app.notes.client import NotesError, file_under, list_notes, read_note, topics
from app.notes.filer import file_text, tag_topic
from app.notes.filing import FilerError

logger = logging.getLogger(__name__)

INBOX = "inbox"


class ModelNeeded(Exception):
    pass


async def file_note(path: str, allow_model: bool = True) -> dict:
    if not path.startswith(f"{INBOX}/"):
        raise ValueError("only an inbox note can be filed")
    note = await read_note(path)
    names, _ = await topics()
    if not allow_model and tag_topic(note["text"], names) is None:
        raise ModelNeeded(path)
    filing, usage = await file_text(note["text"], names)
    stored = await file_under(path, filing)
    return {
        "path": stored["path"],
        "from": path,
        "topic": stored["topic"],
        "new_topic": filing.new_topic,
        "kind": filing.kind,
        "reason": filing.reason,
        "by_tag": filing.by_tag,
        "filer": filing.filer,
        "usage": (
            {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": usage.cost_usd,
            }
            if usage
            else None
        ),
    }


async def sweep_once(limit: int | None = None) -> dict:
    listing = await list_notes(folder=INBOX, limit=limit or settings.notes_sweep_batch)
    paths = [note["path"] for note in reversed(listing["notes"])]
    filed: list[dict] = []
    waiting = 0
    failed = 0
    model_down = False
    for path in paths:
        try:
            result = await file_note(path, allow_model=not model_down)
        except anthropic.APIError as exc:
            if not model_down:
                logger.warning("Notes sweep: the model is down, tagged notes only: %s", exc)
            model_down = True
            waiting += 1
        except ModelNeeded:
            waiting += 1
        except (FilerError, NotesError) as exc:
            logger.warning("Notes sweep: could not file %s: %s", path, exc)
            failed += 1
        else:
            filed.append({k: result[k] for k in ("path", "from", "topic", "by_tag")})
    return {
        "seen": len(paths),
        "filed": filed,
        "waiting": waiting,
        "failed": failed,
        "model_down": model_down,
    }
