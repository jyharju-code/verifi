"""Associate selection: who gets the next verify.

Rule: active, available (/vapaa), working on this instance or on any
instance (instance_active NULL), fewest open verifies first, then the
one who waited longest since the last assignment. All in one query so
two concurrent verifies cannot double-book the same logic in Python.
"""
import logging

import asyncpg

log = logging.getLogger(__name__)

_SELECT_BEST = """
SELECT a.id, a.telegram_id, a.name
FROM associates a
LEFT JOIN LATERAL (
    SELECT count(*) AS open_count
    FROM verifies v
    WHERE v.associate_id = a.id AND v.status = 'pending'
) open_v ON TRUE
WHERE a.status = 'active'
  AND a.available = TRUE
  AND (a.instance_active IS NULL OR a.instance_active = $1)
ORDER BY open_v.open_count ASC, a.last_assigned_at ASC NULLS FIRST
LIMIT 1
"""


async def select_associate(conn: asyncpg.Connection, instance: str) -> asyncpg.Record | None:
    """Pick the best associate for an instance, or None when nobody is available."""
    row = await conn.fetchrow(_SELECT_BEST, instance)
    if row is None:
        log.warning("no available associate for instance=%s", instance)
    return row


async def assign(conn: asyncpg.Connection, verify_id, associate_id: int) -> None:
    await conn.execute(
        """
        UPDATE verifies
        SET associate_id = $2, assigned_at = now()
        WHERE id = $1 AND status = 'pending'
        """,
        verify_id,
        associate_id,
    )
    await conn.execute(
        "UPDATE associates SET last_assigned_at = now() WHERE id = $1",
        associate_id,
    )


async def unassigned_pending(conn: asyncpg.Connection, limit: int = 10) -> list[asyncpg.Record]:
    """Pending verifies with no associate, oldest first. Reassigned when someone types /vapaa."""
    return await conn.fetch(
        """
        SELECT id, verify_no, instance, intent, claim, agent_id, tier, created_at
        FROM verifies
        WHERE status = 'pending' AND associate_id IS NULL
        ORDER BY created_at ASC
        LIMIT $1
        """,
        limit,
    )
