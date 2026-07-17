"""Verifi core internal API. Localhost only, instances talk to this.

Run from the repo root:
    uvicorn core.api.server:app --host 127.0.0.1 --port 8700

Endpoints:
    POST /internal/verifies              create a verify, route it, notify the associate
    GET  /internal/verifies/{id}         current state
    GET  /internal/verifies/{id}/wait    long poll until resolved or timeout
    GET  /internal/quota                 free tier remaining for an instance
    GET  /health                         liveness
"""
import asyncio
import contextlib
import logging
import os
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from core import config, notify
from core.audit import audit
from core.db.database import close_pool, get_pool
from core.routing import pool as routing_pool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("verifi.core-api")

# A pending verify with no answer after this many minutes is expired.
EXPIRE_AFTER_MINUTES = 60
# Global cap on simultaneously pending verifies: the human queue's overload
# protection. Above this, new verifies get 503 + Retry-After.
MAX_PENDING_TOTAL = int(os.environ.get("MAX_PENDING_TOTAL", "25"))


class VerifyIn(BaseModel):
    instance: str = Field(min_length=1, max_length=50)
    intent: str = Field(min_length=1, max_length=2000)
    claim: str = Field(min_length=1, max_length=4000)
    agent_id: str | None = Field(default=None, max_length=100)
    tier: str = Field(pattern="^(free|paid)$")
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
        "unlock_paid": row["unlock_paid"],
        "created_at": row["created_at"].isoformat(),
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "responded_at": row["responded_at"].isoformat() if row["responded_at"] else None,
    }


async def _expire_stale_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            db = await get_pool()
            rows = await db.fetch(
                """
                UPDATE verifies
                SET status = 'expired', responded_at = now()
                WHERE status = 'pending' AND expires_at < now()
                RETURNING verify_no, associate_id
                """
            )
            for r in rows:
                log.info("verify #V-%s expired", r["verify_no"])
                await audit("core-api", "verify_expired", {"verify_no": r["verify_no"]})
                if r["associate_id"]:
                    assoc = await db.fetchrow(
                        "SELECT telegram_id FROM associates WHERE id = $1", r["associate_id"]
                    )
                    if assoc:
                        await notify.send_message(
                            assoc["telegram_id"],
                            f"⌛ Verify #V-{r['verify_no']} vanheni ilman vastausta.",
                        )
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
    pending_total = await db.fetchval("SELECT count(*) FROM verifies WHERE status = 'pending'")
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
            WHERE instance = $1 AND lower(agent_id) = lower($2) AND status = 'pending'
            """,
            body.instance,
            body.agent_id,
        )
        if pending > 0:
            raise HTTPException(
                status_code=429,
                detail="one pending verify per agent_id: wait for the previous one to resolve or expire",
            )

    async with db.acquire() as conn:
        # Free responses are always open. Paid responses unlock when the
        # settlement is recorded (record_payment), never before: the x402
        # middleware settles during the response, so creation time is too
        # early to consider the verify paid for.
        verify = await conn.fetchrow(
            """
            INSERT INTO verifies (instance, intent, claim, agent_id, tier, unlock_paid, callback_url)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            RETURNING *
            """,
            body.instance,
            body.intent,
            body.claim,
            body.agent_id,
            body.tier,
            body.tier == "free",
            body.callback_url,
        )
        associate = await routing_pool.select_associate(conn, body.instance)
        assigned = False
        if associate is not None:
            await routing_pool.assign(conn, verify["id"], associate["id"])
            msg_id = await notify.send_verify_card(associate["telegram_id"], verify)
            if msg_id:
                await conn.execute(
                    "UPDATE verifies SET telegram_message_id = $2 WHERE id = $1",
                    verify["id"],
                    msg_id,
                )
                assigned = True
            else:
                log.error("telegram send failed, verify %s stays unassigned", verify["id"])
                await conn.execute(
                    "UPDATE verifies SET associate_id = NULL, assigned_at = NULL WHERE id = $1",
                    verify["id"],
                )

    log.info(
        "verify created id=%s no=%s instance=%s tier=%s assigned=%s",
        verify["id"], verify["verify_no"], body.instance, body.tier, assigned,
    )
    await audit(
        "core-api",
        "verify_created",
        {
            "verify_no": verify["verify_no"],
            "verify_id": str(verify["id"]),
            "instance": body.instance,
            "tier": body.tier,
            "agent_id": body.agent_id,
            "assigned": assigned,
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


@app.get("/internal/verifies/{verify_id}/wait")
async def wait_verify(verify_id: UUID, timeout_s: int = Query(default=110, ge=1, le=600)) -> dict:
    """Long poll: return as soon as the verify leaves 'pending', or at timeout."""
    db = await get_pool()
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while True:
        row = await db.fetchrow("SELECT * FROM verifies WHERE id = $1", verify_id)
        if row is None:
            raise HTTPException(status_code=404, detail="verify not found")
        if row["status"] != "pending" or loop.time() >= deadline:
            return _row_to_dict(row)
        await asyncio.sleep(1)


class PaymentIn(BaseModel):
    kind: str = Field(pattern="^(payment|unlock)$")
    transaction: str = Field(min_length=4, max_length=80)
    payer: str | None = Field(default=None, max_length=80)


@app.post("/internal/verifies/{verify_id}/payment")
async def record_payment(verify_id: UUID, body: PaymentIn) -> dict:
    """Attach a settled x402 transaction to a verify. Unlock payments flip unlock_paid."""
    db = await get_pool()
    if body.kind == "payment":
        row = await db.fetchrow(
            """
            UPDATE verifies SET x402_payment_tx = $2, unlock_paid = true
            WHERE id = $1 RETURNING verify_no
            """,
            verify_id,
            body.transaction,
        )
    else:
        row = await db.fetchrow(
            """
            UPDATE verifies SET x402_unlock_tx = $2, unlock_paid = true
            WHERE id = $1 RETURNING verify_no
            """,
            verify_id,
            body.transaction,
        )
    if row is None:
        raise HTTPException(status_code=404, detail="verify not found")
    await audit(
        "core-api",
        "payment_recorded",
        {
            "verify_no": row["verify_no"],
            "kind": body.kind,
            "transaction": body.transaction,
            "payer": body.payer,
        },
    )
    return {"ok": True, "verify_no": row["verify_no"]}


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
    """Free tier is per wallet address: free_tier_count verifies per agent_id.

    Without an agent_id there is no free quota, because the allowance is
    tied to the address, not to a global pool.
    """
    db = await get_pool()
    row = await db.fetchrow("SELECT free_tier_count FROM instances WHERE id = $1", instance)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown instance")
    allowance = row["free_tier_count"]
    if not agent_id:
        return {"instance": instance, "agent_id": None, "free_allowance": allowance, "free_remaining": 0}
    row2 = await db.fetchrow(
        """
        SELECT count(*) FILTER (WHERE tier = 'free') AS free_used,
               count(*) FILTER (WHERE status = 'pending') AS pending_count,
               count(*) FILTER (WHERE tier = 'paid' AND status = 'expired') AS expired_paid
        FROM verifies
        WHERE instance = $1 AND lower(agent_id) = lower($2)
        """,
        instance,
        agent_id,
    )
    pending_total = await db.fetchval("SELECT count(*) FROM verifies WHERE status = 'pending'")
    # Credit policy: a paid verify that expired unanswered took money without
    # delivering, so each one permanently adds one free verify for that address.
    allowance_effective = allowance + row2["expired_paid"]
    return {
        "instance": instance,
        "agent_id": agent_id,
        "free_allowance": allowance_effective,
        "free_used": row2["free_used"],
        "free_remaining": max(0, allowance_effective - row2["free_used"]),
        "expired_paid_credits": row2["expired_paid"],
        "pending_count": row2["pending_count"],
        "pending_total": pending_total,
        "queue_full": pending_total >= MAX_PENDING_TOTAL,
    }
