"""Conversation history endpoints."""

from fastapi import APIRouter, HTTPException, Query

from app.agent.loop import get_session, list_sessions
from app.agent.session import process_session
from app.db import get_pool

router = APIRouter()


@router.get("")
async def list_conversations():
    return {"conversations": await list_sessions()}


@router.get("/{session_id}")
async def get_conversation(session_id: str):
    messages = await get_session(session_id)
    if messages is None:
        raise HTTPException(404, "Session not found")
    return {"session_id": session_id, "messages": messages, "message_count": len(messages)}


@router.post("/{session_id}/process")
async def trigger_process_session(session_id: str):
    messages = await get_session(session_id)
    if messages is None:
        raise HTTPException(404, "Session not found")
    if not messages:
        raise HTTPException(400, "Session has no messages to process")
    return await process_session(session_id, messages)


@router.post("/archive")
async def archive_sessions(
    older_than_days: int = Query(default=30, ge=1, description="Archive sessions with no activity in this many days"),
):
    """Move old sessions and their messages to archive tables in a single transaction.

    Deleting from sessions cascades to messages automatically, so the live
    tables stay lean while all data is preserved in archived_sessions and
    archived_messages.
    """
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Identify sessions to archive
            session_rows = await conn.fetch(
                """
                SELECT id FROM sessions
                WHERE last_activity < NOW() - ($1 || ' days')::INTERVAL
                """,
                str(older_than_days),
            )

            if not session_rows:
                return {"sessions_archived": 0, "messages_archived": 0}

            session_ids = [r["id"] for r in session_rows]

            # Copy sessions to archive
            await conn.execute(
                """
                INSERT INTO archived_sessions
                    (id, created_at, last_activity, message_count, processed_at,
                     summary_kb_id, context_summary, summarized_through)
                SELECT id, created_at, last_activity, message_count, processed_at,
                       summary_kb_id, context_summary, summarized_through
                FROM sessions
                WHERE id = ANY($1)
                ON CONFLICT (id) DO NOTHING
                """,
                session_ids,
            )

            # Copy messages to archive
            result = await conn.fetchrow(
                """
                WITH moved AS (
                    INSERT INTO archived_messages (id, session_id, role, content, timestamp)
                    SELECT id, session_id, role, content, timestamp
                    FROM messages
                    WHERE session_id = ANY($1)
                    ON CONFLICT (id) DO NOTHING
                    RETURNING 1
                )
                SELECT COUNT(*) AS count FROM moved
                """,
                session_ids,
            )
            messages_archived = result["count"]

            # Delete from live sessions — cascades to messages automatically
            await conn.execute(
                "DELETE FROM sessions WHERE id = ANY($1)",
                session_ids,
            )

    return {
        "sessions_archived": len(session_ids),
        "messages_archived": messages_archived,
    }
