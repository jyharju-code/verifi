"""Append-only audit trail in PostgreSQL.

Best effort by design: an audit failure is logged but never breaks the
flow that produced the event. Money events must always call this.
"""
import json
import logging

from core.db.database import get_pool

log = logging.getLogger("verifi.audit")


async def audit(source: str, event: str, details: dict | None = None, actor: str | None = None) -> None:
    try:
        db = await get_pool()
        await db.execute(
            "INSERT INTO audit_log (source, event, actor, details) VALUES ($1, $2, $3, $4::jsonb)",
            source,
            event,
            actor,
            json.dumps(details or {}, ensure_ascii=False, default=str),
        )
    except Exception:
        log.exception("audit write failed: %s %s", source, event)
