"""Verifi core internal API. Localhost only, instances talk to this.

Run from the repo root:
    uvicorn core.api.server:app --host 127.0.0.1 --port 8700

Endpoints:
    POST /internal/verifies              create a verify, route it, notify the associate
    GET  /internal/verifies/{id}         current state
    GET  /internal/quota                 wallet entitlements and queue state
    GET  /health                         liveness
"""
import asyncio
import contextlib
import logging
import os
from uuid import UUID

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from core import config, notify
from core.audit import audit
from core.db.database import close_pool, get_pool
from core.routing import pool as routing_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
# Outbound notification URLs can contain credentials. Keep application events
# at INFO, but never log dependency request URLs at that level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("verifi.core-api")

# Global cap on simultaneously pending verifies: the human queue's overload
# protection. Above this, new verifies get 503 + Retry-After.
MAX_PENDING_TOTAL = int(os.environ.get("MAX_PENDING_TOTAL", "25"))


class VerifyIn(BaseModel):
    instance: str = Field(min_length=1, max_length=50)
    intent: str = Field(min_length=1, max_length=2000)
    claim: str = Field(min_length=1, max_length=4000)
    agent_id: str = Field(pattern="^0x[0-9a-fA-F]{40}$")
    admission_mode: str = Field(pattern="^(entitlement|x402)$")
    callback_url: str | None = Field(default=None, max_length=2048)


# The verdict an agent can parse without guessing: the human's button maps to
# a fixed vocabulary, and refine text travels separately in explanation.
def _verdict_of(row) -> tuple[str | None, str | None]:
    status = row["status"]
    if status == "accepted":
        return "true", None
    if status == "rejected":
        return "false", None
    if status == "refined":
        return "refined", row["response"]
    return None, None


def _row_to_dict(row) -> dict:
    verdict, explanation = _verdict_of(row)
    return {
        "id": str(row["id"]),
        "verdict": verdict,
        "explanation": explanation,
        "verify_no": row["verify_no"],
        "instance": row["instance"],
        "intent": row["intent"],
        "claim": row["claim"],
        "agent_id": row["agent_id"],
        "tier": row["tier"],
        "status": row["status"],
        "response": row["response"],
        "response_time_ms": row["response_time_ms"],
        "entry_source": row["entry_source"],
        "entry_list_price_usdc": str(row["entry_list_price_usdc"]),
        "entry_charged_usdc": str(row["entry_charged_usdc"]),
        "entry_payer": row["entry_payer"],
        "unlock_source": row["unlock_source"],
        "unlock_list_price_usdc": str(row["unlock_list_price_usdc"]),
        "unlock_charged_usdc": str(row["unlock_charged_usdc"]),
        "unlock_payer": row["unlock_payer"],
        "result_unlocked": row["result_unlocked"],
        "free_use_number": row["free_use_number"],
        "failure_credit_granted": row["failure_credit_granted"],
        "x402_payment_tx": row["x402_payment_tx"],
        "x402_unlock_tx": row["x402_unlock_tx"],
        "admitted_at": row["admitted_at"].isoformat() if row["admitted_at"] else None,
        "unlocked_at": row["unlocked_at"].isoformat() if row["unlocked_at"] else None,
        "failure_reason": row["failure_reason"],
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "responded_at": row["responded_at"].isoformat() if row["responded_at"] else None,
    }


async def _expire_stale_once() -> int:
    """Fail timed-out human work and grant one entry-only wallet credit."""
    db = await get_pool()
    async with db.acquire() as conn:
        async with conn.transaction():
            abandoned = await conn.fetch(
                """
                UPDATE verifies
                SET status = 'failed', responded_at = now(),
                    failure_reason = 'entry_not_settled'
                WHERE status = 'admission_pending'
                  AND created_at < now() - interval '10 minutes'
                RETURNING id, verify_no, agent_id
                """
            )
            rows = await conn.fetch(
                """
                UPDATE verifies
                SET status = 'expired', responded_at = now(),
                    failure_reason = 'human_timeout',
                    failure_credit_granted = agent_id IS NOT NULL
                WHERE status = 'pending' AND expires_at < now()
                RETURNING id, verify_no, instance, agent_id, associate_id
                """
            )
            for r in rows:
                if r["agent_id"]:
                    await conn.execute(
                        """
                        INSERT INTO wallet_entitlements (
                            instance, wallet_address, kind, covers_entry, covers_unlock,
                            source_verify_id, details
                        )
                        VALUES ($1, $2, 'failure_credit', true, false, $3,
                                jsonb_build_object('reason', 'human_timeout'))
                        ON CONFLICT DO NOTHING
                        """,
                        r["instance"],
                        r["agent_id"],
                        r["id"],
                    )
    for r in abandoned:
        log.warning("verify #V-%s admission abandoned", r["verify_no"])
        await audit(
            "core-api",
            "admission_abandoned",
            {
                "verify_no": r["verify_no"],
                "verify_id": str(r["id"]),
                "wallet_address": r["agent_id"],
                "reason": "entry_not_settled",
                "entry_credit_granted": False,
            },
        )
    for r in rows:
        log.info("verify #V-%s expired", r["verify_no"])
        await audit(
            "core-api",
            "verify_failed_credit_granted",
            {
                "verify_no": r["verify_no"],
                "verify_id": str(r["id"]),
                "wallet_address": r["agent_id"],
                "credit_usdc": "0.10",
                "reason": "human_timeout",
            },
        )
        if r["associate_id"]:
            assoc = await db.fetchrow(
                "SELECT telegram_id FROM associates WHERE id = $1", r["associate_id"]
            )
            if assoc:
                await notify.send_message(
                    assoc["telegram_id"],
                    f"⌛ Verify #V-{r['verify_no']} vanheni ilman vastausta.",
                )
    return len(abandoned) + len(rows)


async def _expire_stale_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await _expire_stale_once()
        except Exception:
            log.exception("expire loop failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    from core.webhooks import delivery_loop

    await get_pool()
    task = asyncio.create_task(_expire_stale_loop())
    webhook_task = asyncio.create_task(delivery_loop())
    yield
    task.cancel()
    webhook_task.cancel()
    await close_pool()


app = FastAPI(title="Verifi core internal API", lifespan=lifespan)

from core.api.dashboard import router as dashboard_router  # noqa: E402

app.include_router(dashboard_router)


@app.get("/health")
async def health() -> dict:
    db = await get_pool()
    await db.fetchval("SELECT 1")
    return {"ok": True}


async def _route_admitted_verify(conn, verify) -> bool:
    """Put an admitted verify in the human queue and notify one associate."""
    associate = await routing_pool.select_associate(conn, verify["instance"])
    if associate is None:
        return False
    await routing_pool.assign(conn, verify["id"], associate["id"])
    msg_id = await notify.send_verify_card(associate["telegram_id"], verify)
    if msg_id:
        await conn.execute(
            "UPDATE verifies SET telegram_message_id = $2 WHERE id = $1",
            verify["id"],
            msg_id,
        )
        return True
    log.error("telegram send failed, verify %s stays unassigned", verify["id"])
    await conn.execute(
        "UPDATE verifies SET associate_id = NULL, assigned_at = NULL WHERE id = $1",
        verify["id"],
    )
    return False


@app.post("/internal/verifies")
async def create_verify(body: VerifyIn) -> dict:
    db = await get_pool()
    instance = await db.fetchrow("SELECT * FROM instances WHERE id = $1", body.instance)
    if instance is None or instance["status"] == "disabled":
        raise HTTPException(status_code=404, detail="unknown or disabled instance")
    if instance["status"] == "paused":
        raise HTTPException(status_code=503, detail="instance is paused")

    if body.callback_url and not body.callback_url.startswith("https://"):
        raise HTTPException(status_code=422, detail="callback_url must be https")

    # Overload protection: the queue feeds real humans, so it has a hard cap.
    pending_total = await db.fetchval(
        "SELECT count(*) FROM verifies WHERE status IN ('admission_pending', 'pending')"
    )
    if pending_total >= MAX_PENDING_TOTAL:
        raise HTTPException(
            status_code=503,
            detail="human queue is full, retry shortly",
            headers={"Retry-After": "120"},
        )

    # One active pending verify per agent_id keeps a single agent from
    # flooding the human queue (429 per the 17.7.2026 spec).
    if body.agent_id:
        pending = await db.fetchval(
            """
            SELECT count(*) FROM verifies
            WHERE instance = $1 AND lower(agent_id) = lower($2)
              AND status IN ('admission_pending', 'pending')
            """,
            body.instance,
            body.agent_id,
        )
        if pending > 0:
            raise HTTPException(
                status_code=429,
                detail="one pending verify per agent_id: wait for the previous one to resolve or expire",
            )

    assigned = False
    benefit_kind = None
    async with db.acquire() as conn:
        async with conn.transaction():
            # Serialize admission decisions per wallet. This makes the five
            # full-free uses and entry credits safe under concurrent calls.
            await conn.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1::text || lower($2::text)))",
                body.instance,
                body.agent_id,
            )
            active = await conn.fetchval(
                """
                SELECT count(*) FROM verifies
                WHERE instance = $1 AND lower(agent_id) = lower($2)
                  AND status IN ('admission_pending', 'pending')
                """,
                body.instance,
                body.agent_id,
            )
            if active > 0:
                raise HTTPException(
                    status_code=429,
                    detail="one active verify per agent_id",
                )
            # Every chain starts in admission_pending. An entitlement admits
            # it inside this transaction. An x402 chain stays outside the
            # human queue until record_payment receives the settled entry tx.
            verify = await conn.fetchrow(
                """
                INSERT INTO verifies (
                    instance, intent, claim, agent_id, tier, status,
                    entry_source, callback_url
                )
                VALUES ($1, $2, $3, $4, $5, 'admission_pending', $6, $7)
                RETURNING *
                """,
                body.instance,
                body.intent,
                body.claim,
                body.agent_id,
                "free" if body.admission_mode == "entitlement" else "paid",
                "x402" if body.admission_mode == "x402" else None,
                body.callback_url,
            )

            if body.admission_mode == "entitlement":
                allowance = instance["free_tier_count"]
                free_used = await conn.fetchval(
                    """
                    SELECT count(*) FROM wallet_entitlements
                    WHERE instance = $1 AND lower(wallet_address) = lower($2)
                      AND kind = 'initial_free'
                    """,
                    body.instance,
                    body.agent_id,
                )
                if free_used < allowance:
                    free_no = free_used + 1
                    await conn.execute(
                        """
                        INSERT INTO wallet_entitlements (
                            instance, wallet_address, kind, covers_entry, covers_unlock,
                            free_use_number, consumed_by_verify_id, consumed_at,
                            details
                        )
                        VALUES ($1, $2, 'initial_free', true, true, $3, $4, now(),
                                jsonb_build_object('entry_usdc', '0.10', 'unlock_usdc', '2.90'))
                        """,
                        body.instance,
                        body.agent_id,
                        free_no,
                        verify["id"],
                    )
                    benefit_kind = "initial_free"
                    verify = await conn.fetchrow(
                        """
                        UPDATE verifies
                        SET tier = 'free', entry_source = 'initial_free',
                            free_use_number = $2, status = 'pending', admitted_at = now()
                        WHERE id = $1 RETURNING *
                        """,
                        verify["id"],
                        free_no,
                    )
                else:
                    credit = await conn.fetchrow(
                        """
                        SELECT id FROM wallet_entitlements
                        WHERE instance = $1 AND lower(wallet_address) = lower($2)
                          AND kind = 'failure_credit' AND consumed_by_verify_id IS NULL
                        ORDER BY granted_at, id
                        LIMIT 1 FOR UPDATE SKIP LOCKED
                        """,
                        body.instance,
                        body.agent_id,
                    )
                    if credit is None:
                        raise HTTPException(status_code=402, detail="no free chain or entry credit remains")
                    await conn.execute(
                        """
                        UPDATE wallet_entitlements
                        SET consumed_by_verify_id = $2, consumed_at = now()
                        WHERE id = $1
                        """,
                        credit["id"],
                        verify["id"],
                    )
                    benefit_kind = "failure_credit"
                    verify = await conn.fetchrow(
                        """
                        UPDATE verifies
                        SET tier = 'paid', entry_source = 'failure_credit',
                            status = 'pending', admitted_at = now()
                        WHERE id = $1 RETURNING *
                        """,
                        verify["id"],
                    )

                assigned = await _route_admitted_verify(conn, verify)

    log.info(
        "verify created id=%s no=%s instance=%s mode=%s source=%s assigned=%s",
        verify["id"], verify["verify_no"], body.instance, body.admission_mode,
        verify["entry_source"], assigned,
    )
    await audit(
        "core-api",
        "verify_created",
        {
            "verify_no": verify["verify_no"],
            "verify_id": str(verify["id"]),
            "instance": body.instance,
            "tier": verify["tier"],
            "agent_id": body.agent_id,
            "admission_mode": body.admission_mode,
            "entry_source": verify["entry_source"],
            "entry_list_price_usdc": "0.10",
            "entry_charged_usdc": "0.00",
            "free_use_number": verify["free_use_number"],
            "assigned": assigned,
        },
    )
    if benefit_kind:
        await audit(
            "core-api",
            "entry_entitlement_consumed",
            {
                "verify_id": str(verify["id"]),
                "verify_no": verify["verify_no"],
                "wallet_address": body.agent_id,
                "kind": benefit_kind,
                "free_use_number": verify["free_use_number"],
                "entry_list_price_usdc": "0.10",
                "entry_charged_usdc": "0.00",
                "covers_unlock": benefit_kind == "initial_free",
            },
        )
    return {**_row_to_dict(verify), "assigned": assigned}


@app.get("/internal/verifies/{verify_id}")
async def get_verify(verify_id: UUID) -> dict:
    db = await get_pool()
    row = await db.fetchrow("SELECT * FROM verifies WHERE id = $1", verify_id)
    if row is None:
        raise HTTPException(status_code=404, detail="verify not found")
    return _row_to_dict(row)


class PaymentIn(BaseModel):
    kind: str = Field(pattern="^(entry|unlock)$")
    transaction: str = Field(min_length=4, max_length=80)
    payer: str | None = Field(default=None, max_length=80)


@app.post("/internal/verifies/{verify_id}/payment")
async def record_payment(verify_id: UUID, body: PaymentIn) -> dict:
    """Attach a settled x402 transaction and advance exactly one paid gate."""
    db = await get_pool()
    assigned = False
    async with db.acquire() as conn:
        async with conn.transaction():
            current = await conn.fetchrow(
                "SELECT * FROM verifies WHERE id = $1 FOR UPDATE", verify_id
            )
            if current is None:
                raise HTTPException(status_code=404, detail="verify not found")
            if body.kind == "entry":
                if current["x402_payment_tx"]:
                    if current["x402_payment_tx"] != body.transaction:
                        raise HTTPException(status_code=409, detail="entry is already settled")
                    row = current
                else:
                    if current["status"] != "admission_pending" or current["entry_source"] != "x402":
                        raise HTTPException(status_code=409, detail="verify is not awaiting x402 admission")
                    row = await conn.fetchrow(
                        """
                        UPDATE verifies
                        SET x402_payment_tx = $2, entry_payer = COALESCE($3, agent_id),
                            entry_charged_usdc = 0.10, admitted_at = now(), status = 'pending'
                        WHERE id = $1 RETURNING *
                        """,
                        verify_id,
                        body.transaction,
                        body.payer,
                    )
                    assigned = await _route_admitted_verify(conn, row)
            else:
                if current["x402_unlock_tx"]:
                    if current["x402_unlock_tx"] != body.transaction:
                        raise HTTPException(status_code=409, detail="result is already unlocked")
                    row = current
                else:
                    if current["status"] not in ("accepted", "rejected", "refined"):
                        raise HTTPException(status_code=409, detail="result is not ready")
                    if current["result_unlocked"]:
                        raise HTTPException(status_code=409, detail="result is already unlocked")
                    if current["entry_source"] == "initial_free":
                        raise HTTPException(status_code=409, detail="free chain must use entitlement unlock")
                    row = await conn.fetchrow(
                        """
                        UPDATE verifies
                        SET x402_unlock_tx = $2, unlock_source = 'x402',
                            unlock_payer = COALESCE($3, agent_id),
                            unlock_charged_usdc = 2.90,
                            result_unlocked = true, unlocked_at = now()
                        WHERE id = $1 RETURNING *
                        """,
                        verify_id,
                        body.transaction,
                        body.payer,
                    )
    await audit(
        "core-api",
        "payment_recorded",
        {
            "verify_no": row["verify_no"],
            "verify_id": str(verify_id),
            "kind": body.kind,
            "transaction": body.transaction,
            "payer": body.payer,
            "wallet_address": row["agent_id"],
            "amount_usdc": "0.10" if body.kind == "entry" else "2.90",
            "assigned": assigned if body.kind == "entry" else None,
        },
    )
    return {"ok": True, "verify_no": row["verify_no"], "assigned": assigned}


class EntitlementUnlockIn(BaseModel):
    source: str = Field(pattern="^initial_free$")


@app.post("/internal/verifies/{verify_id}/entitlement-unlock")
async def entitlement_unlock(verify_id: UUID, body: EntitlementUnlockIn) -> dict:
    """Consume the second gate of the same full-free entitlement."""
    db = await get_pool()
    row = await db.fetchrow(
        """
        UPDATE verifies
        SET unlock_source = 'initial_free', unlock_charged_usdc = 0.00,
            result_unlocked = true, unlocked_at = now()
        WHERE id = $1
          AND entry_source = 'initial_free'
          AND status IN ('accepted', 'rejected', 'refined')
          AND result_unlocked = false
        RETURNING *
        """,
        verify_id,
    )
    if row is None:
        current = await db.fetchrow("SELECT * FROM verifies WHERE id = $1", verify_id)
        if current is None:
            raise HTTPException(status_code=404, detail="verify not found")
        if current["result_unlocked"] and current["entry_source"] == "initial_free":
            return _row_to_dict(current)
        raise HTTPException(status_code=409, detail="free result is not ready to unlock")
    await audit(
        "core-api",
        "unlock_entitlement_consumed",
        {
            "verify_id": str(verify_id),
            "verify_no": row["verify_no"],
            "wallet_address": row["agent_id"],
            "kind": "initial_free",
            "free_use_number": row["free_use_number"],
            "unlock_list_price_usdc": "2.90",
            "unlock_charged_usdc": "0.00",
        },
    )
    return _row_to_dict(row)


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=300)
    message: str = Field(min_length=1, max_length=5000)


@app.post("/contact")
async def contact(body: ContactIn) -> dict:
    """Public contact form (proxied through nginx). Delivers to Telegram."""
    await audit(
        "core-api",
        "contact_message",
        {"name": body.name, "email": body.email, "length": len(body.message)},
    )
    if config.ADMIN_TELEGRAM_ID:
        await notify.send_message(
            config.ADMIN_TELEGRAM_ID,
            "📬 Yhteydenotto verifi.cloudista\n\n"
            f"Nimi: {body.name}\n"
            f"Sähköposti: {body.email}\n\n"
            f"{body.message[:3500]}",
        )
    return {"ok": True}


@app.get("/internal/quota")
async def quota(instance: str, agent_id: str | None = None) -> dict:
    """Full-free chains and entry-only credits for one wallet address.

    Without an agent_id there is no free quota, because the allowance is
    tied to the address, not to a global pool.
    """
    db = await get_pool()
    row = await db.fetchrow("SELECT free_tier_count FROM instances WHERE id = $1", instance)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown instance")
    allowance = row["free_tier_count"]
    if not agent_id:
        return {
            "instance": instance,
            "agent_id": None,
            "free_allowance": allowance,
            "free_remaining": 0,
            "full_free_remaining": 0,
            "entry_credits_remaining": 0,
            "entry_credits_granted": 0,
            "has_entry_entitlement": False,
            "pending_count": 0,
        }
    row2 = await db.fetchrow(
        """
        SELECT count(*) FILTER (WHERE kind = 'initial_free') AS free_used,
               count(*) FILTER (
                   WHERE kind = 'failure_credit' AND consumed_by_verify_id IS NULL
               ) AS entry_credits_remaining,
               count(*) FILTER (WHERE kind = 'failure_credit') AS entry_credits_granted
        FROM wallet_entitlements
        WHERE instance = $1 AND lower(wallet_address) = lower($2)
        """,
        instance,
        agent_id,
    )
    pending_count = await db.fetchval(
        """
        SELECT count(*) FROM verifies
        WHERE instance = $1 AND lower(agent_id) = lower($2)
          AND status IN ('admission_pending', 'pending')
        """,
        instance,
        agent_id,
    )
    pending_total = await db.fetchval(
        "SELECT count(*) FROM verifies WHERE status IN ('admission_pending', 'pending')"
    )
    full_free_remaining = max(0, allowance - row2["free_used"])
    return {
        "instance": instance,
        "agent_id": agent_id,
        "free_allowance": allowance,
        "free_used": row2["free_used"],
        "free_remaining": full_free_remaining,
        "full_free_remaining": full_free_remaining,
        "entry_credits_remaining": row2["entry_credits_remaining"],
        "entry_credits_granted": row2["entry_credits_granted"],
        "has_entry_entitlement": full_free_remaining > 0 or row2["entry_credits_remaining"] > 0,
        "pending_count": pending_count,
        "pending_total": pending_total,
        "queue_full": pending_total >= MAX_PENDING_TOTAL,
    }
