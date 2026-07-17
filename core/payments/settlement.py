"""Earnings tracking and settlements.

Money model: every answered verify credits the associate immediately
(earnings). Payouts increase paid_total. Pending balance is always
earnings minus paid_total, so it survives restarts and needs no
separate ledger state.
"""
from decimal import Decimal

import asyncpg

from core import config


async def credit_for_verify(conn: asyncpg.Connection, verify_id) -> Decimal:
    """Credit the associate for one answered verify. Returns the credited amount.

    Paid tier: the instance's associate_commission.
    Free tier: flat FREE_COMMISSION_USD.
    Idempotent by design: call this only from the single place that moves
    a verify out of 'pending' (the bot button handler runs in one transaction).
    """
    row = await conn.fetchrow(
        """
        SELECT v.tier, v.associate_id, i.associate_commission
        FROM verifies v
        JOIN instances i ON i.id = v.instance
        WHERE v.id = $1
        """,
        verify_id,
    )
    if row is None or row["associate_id"] is None:
        return Decimal("0")
    amount = (
        Decimal(str(config.FREE_COMMISSION_USD))
        if row["tier"] == "free"
        else Decimal(row["associate_commission"])
    )
    counter = "total_free" if row["tier"] == "free" else "total_paid"
    await conn.execute(
        f"""
        UPDATE associates
        SET earnings = earnings + $2, {counter} = {counter} + 1
        WHERE id = $1
        """,
        row["associate_id"],
        amount,
    )
    return amount


async def pending_balances(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch(
        """
        SELECT id, name, username, payout_method, wallet_address,
               earnings, paid_total, earnings - paid_total AS pending
        FROM associates
        WHERE status IN ('active', 'paused') AND earnings - paid_total > 0
        ORDER BY pending DESC
        """
    )


async def week_earnings(conn: asyncpg.Connection, associate_id: int) -> Decimal:
    """Earnings credited for verifies answered during the current ISO week."""
    row = await conn.fetchrow(
        """
        SELECT COALESCE(sum(
            CASE WHEN v.tier = 'free' THEN $2::numeric ELSE i.associate_commission END
        ), 0) AS total
        FROM verifies v
        JOIN instances i ON i.id = v.instance
        WHERE v.associate_id = $1
          AND v.status <> 'pending'
          AND v.responded_at >= date_trunc('week', now())
        """,
        associate_id,
        Decimal(str(config.FREE_COMMISSION_USD)),
    )
    return row["total"]


async def record_payout(
    conn: asyncpg.Connection,
    associate_id: int,
    amount: Decimal,
    method: str,
    tx_reference: str | None = None,
    note: str | None = None,
) -> None:
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO payouts (associate_id, amount, method, tx_reference, note)
            VALUES ($1, $2, $3, $4, $5)
            """,
            associate_id,
            amount,
            method,
            tx_reference,
            note,
        )
        await conn.execute(
            "UPDATE associates SET paid_total = paid_total + $2 WHERE id = $1",
            associate_id,
            amount,
        )
