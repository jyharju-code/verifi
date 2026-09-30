"""Earnings tracking and settlements.

Money model (contract v3): every answered verify appends one row to the
associate_earnings ledger, in the asset its chain is bound to, never
converted. Payouts are recorded per asset. The balance of an asset is the
ledger sum for that asset minus the payouts in that asset, so it survives
restarts and needs no other state.

associates.earnings and paid_total are frozen at the v3 migration, which
carried earnings into the ledger as a USDC opening balance. Nothing writes
them any more; the ledger and the payouts table are the only sources.

Earning rules, one per chain kind:
  v3: floor(unlock_amount_atomic * commission / sla_basis) in the bound
      asset, with the commission and the SLA basis price snapshotted at
      admission (Q2). An SLA answer earns the commission in full, a grace
      answer earns from the grace price.
  v2 paid: the instance commission, in USDC.
  v2 free: FREE_COMMISSION_USD, in USDC.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import asyncpg

from core import config, pricing

USDC_DECIMALS = 6


@dataclass(frozen=True)
class Earned:
    asset: str
    amount_atomic: int
    decimals: int
    applied_window: str | None
    rule: str

    @property
    def amount(self) -> Decimal:
        return pricing.from_atomic(self.amount_atomic, self.decimals)


def v3_earning(unlock_amount_atomic: int, commission_eur: Decimal, sla_basis_eur: Decimal) -> int:
    """floor(unlock * commission / sla basis), in exact integer arithmetic."""
    value = Decimal(int(unlock_amount_atomic)) * Decimal(commission_eur) / Decimal(sla_basis_eur)
    return int(value.to_integral_value(rounding=ROUND_DOWN))


async def credit_for_verify(conn: asyncpg.Connection, verify_id) -> Earned | None:
    """Record the associate's earning for one answered verify.

    Call only from the transaction that moves the verify out of 'pending'.
    Idempotent: the ledger allows one answer row per verify, so a replay
    records nothing and returns None.
    """
    row = await conn.fetchrow(
        """
        SELECT v.tier, v.associate_id, v.contract_version, v.bound_terms,
               v.bound_commission_eur, v.applied_window, v.unlock_amount_atomic,
               i.associate_commission
        FROM verifies v
        JOIN instances i ON i.id = v.instance
        WHERE v.id = $1
        """,
        verify_id,
    )
    if row is None or row["associate_id"] is None:
        return None

    if row["contract_version"] == 3:
        terms = pricing.as_json(row["bound_terms"])
        earned = Earned(
            asset=terms["asset_symbol"],
            amount_atomic=v3_earning(
                row["unlock_amount_atomic"],
                row["bound_commission_eur"],
                Decimal(terms["price_basis"]["sla"]),
            ),
            decimals=int(terms["asset_decimals"]),
            applied_window=row["applied_window"],
            rule=(
                f"floor({pricing.atomic_str(row['unlock_amount_atomic'])} x "
                f"{row['bound_commission_eur']} / {terms['price_basis']['sla']})"
            ),
        )
    elif row["tier"] == "free":
        commission = Decimal(str(config.FREE_COMMISSION_USD))
        earned = Earned("USDC", pricing.to_atomic(commission, USDC_DECIMALS), USDC_DECIMALS, None,
                        f"v2 free commission {commission}")
    else:
        commission = Decimal(row["associate_commission"])
        earned = Earned("USDC", pricing.to_atomic(commission, USDC_DECIMALS), USDC_DECIMALS, None,
                        f"v2 commission {commission}")

    inserted = await conn.fetchval(
        """
        INSERT INTO associate_earnings
            (associate_id, verify_id, kind, asset, amount_atomic, decimals, applied_window, rule)
        VALUES ($1, $2, 'answer', $3, $4, $5, $6, $7)
        ON CONFLICT DO NOTHING
        RETURNING id
        """,
        row["associate_id"],
        verify_id,
        earned.asset,
        earned.amount_atomic,
        earned.decimals,
        earned.applied_window,
        earned.rule,
    )
    if inserted is None:
        return None
    counter = "total_free" if row["tier"] == "free" else "total_paid"
    await conn.execute(f"UPDATE associates SET {counter} = {counter} + 1 WHERE id = $1", row["associate_id"])
    return earned


@dataclass(frozen=True)
class Balance:
    associate_id: int
    asset: str
    decimals: int
    earned: Decimal
    paid: Decimal

    @property
    def pending(self) -> Decimal:
        return self.earned - self.paid

    @property
    def payable(self) -> Decimal:
        """The pending balance in whole cents, rounded down: never more than owed."""
        return self.pending.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


_BALANCES = """
WITH earned AS (
    SELECT associate_id, asset, max(decimals) AS decimals,
           sum(amount_atomic / power(10::numeric, decimals)) AS earned
    FROM associate_earnings
    GROUP BY associate_id, asset
), paid AS (
    SELECT associate_id, asset, sum(amount) AS paid
    FROM payouts
    GROUP BY associate_id, asset
)
SELECT COALESCE(e.associate_id, p.associate_id) AS associate_id,
       COALESCE(e.asset, p.asset) AS asset,
       COALESCE(e.decimals, 6) AS decimals,
       COALESCE(e.earned, 0) AS earned,
       COALESCE(p.paid, 0) AS paid
FROM earned e
FULL JOIN paid p ON p.associate_id = e.associate_id AND p.asset = e.asset
"""


async def balances(conn: asyncpg.Connection, associate_id: int | None = None) -> list[Balance]:
    """Per-asset balances, for one associate or for everyone."""
    rows = await conn.fetch(
        f"SELECT * FROM ({_BALANCES}) b WHERE $1::int IS NULL OR associate_id = $1 ORDER BY associate_id, asset",
        associate_id,
    )
    return [
        Balance(r["associate_id"], r["asset"], r["decimals"], Decimal(r["earned"]), Decimal(r["paid"]))
        for r in rows
    ]


async def pending_balances(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    """Who is owed what, per asset, for the operator's payout report."""
    return await conn.fetch(
        f"""
        SELECT a.id, a.name, a.username, a.payout_method, a.wallet_address,
               b.asset, b.earned, b.paid, b.earned - b.paid AS pending
        FROM ({_BALANCES}) b
        JOIN associates a ON a.id = b.associate_id
        WHERE a.status IN ('active', 'paused') AND b.earned - b.paid > 0
        ORDER BY b.asset, pending DESC
        """
    )


async def week_earnings(conn: asyncpg.Connection, associate_id: int) -> dict[str, Decimal]:
    """Earnings recorded for answers during the current ISO week, per asset."""
    rows = await conn.fetch(
        """
        SELECT asset, sum(amount_atomic / power(10::numeric, decimals)) AS total
        FROM associate_earnings
        WHERE associate_id = $1 AND kind = 'answer'
          AND created_at >= date_trunc('week', now())
        GROUP BY asset
        ORDER BY asset
        """,
        associate_id,
    )
    return {r["asset"]: Decimal(r["total"]) for r in rows}


async def record_payout(
    conn: asyncpg.Connection,
    associate_id: int,
    amount: Decimal,
    method: str,
    tx_reference: str | None = None,
    note: str | None = None,
    asset: str = "USDC",
) -> None:
    await conn.execute(
        """
        INSERT INTO payouts (associate_id, amount, method, tx_reference, note, asset)
        VALUES ($1, $2, $3, $4, $5, $6)
        """,
        associate_id,
        amount,
        method,
        tx_reference,
        note,
        asset,
    )
