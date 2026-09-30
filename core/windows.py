"""The moment a human answers: where a v3 chain's unlock price is decided.

The price depends on ready_at alone (D6). One UPDATE records the answer and,
with the same transaction timestamp, picks the window and copies that
window's bound amount into unlock_amount_atomic. After this the price is only
ever read: the exchange rate and the pricing config are never consulted again.

`<=` makes a boundary instant belong to the earlier, dearer window. The same
statement refuses an answer after expires_at, so an answer that races the
expiry loop cannot produce a result that should have expired. For v3 chains
expires_at is the end of the last window; v2 chains keep their 60 minutes.
"""
from __future__ import annotations

import asyncpg


class AnswerRefused(LookupError):
    """The verify is no longer pending, or its last window has closed."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_RECORD_ANSWER = """
UPDATE verifies
SET status = $2,
    response = $3,
    responded_at = now(),
    ready_at = now(),
    response_time_ms = (EXTRACT(EPOCH FROM (now() - COALESCE(assigned_at, created_at))) * 1000)::int,
    applied_window = CASE
        WHEN contract_version <> 3 THEN NULL
        WHEN now() <= sla_deadline THEN 'sla'
        WHEN now() <= grace_deadline THEN 'grace'
    END,
    unlock_amount_atomic = CASE
        WHEN contract_version <> 3 THEN NULL
        WHEN now() <= sla_deadline THEN (bound_terms->>'sla')::numeric
        WHEN now() <= grace_deadline THEN (bound_terms->>'grace')::numeric
    END
WHERE id = $1
  AND status = 'pending'
  AND (expires_at IS NULL OR now() <= expires_at)
RETURNING response_time_ms, contract_version, ready_at, applied_window,
          unlock_amount_atomic, bound_asset, bound_terms
"""


async def record_answer(conn: asyncpg.Connection, verify_id, status: str, response: str):
    """Record the human's answer and decide the unlock price. Raises AnswerRefused."""
    row = await conn.fetchrow(_RECORD_ANSWER, verify_id, status, response)
    if row is not None:
        if row["contract_version"] == 3 and row["applied_window"] is None:
            # Unreachable while expires_at is the last deadline; kept so a
            # future schema change cannot silently create an unpriced result.
            raise AssertionError(f"verify {verify_id} became ready outside every window")
        return row
    current = await conn.fetchrow(
        "SELECT status, expires_at < now() AS closed FROM verifies WHERE id = $1", verify_id
    )
    if current is not None and current["status"] == "pending" and current["closed"]:
        raise AnswerRefused("expired")
    raise AnswerRefused("already_resolved")
