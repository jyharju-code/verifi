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
import hmac
import ipaddress
import json
import logging
import os
from decimal import Decimal
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from core import config, notify, pricing, wallets
from core import fx as fx_rates
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

# request_audit contains IP addresses and is operational evidence, not a
# permanent customer record. The privacy limit is deliberately capped at 30
# days even if an environment variable is accidentally set higher.
REQUEST_AUDIT_RETENTION_DAYS = min(
    30,
    max(1, int(os.environ.get("REQUEST_AUDIT_RETENTION_DAYS", "30"))),
)

# Shared secret between the instance servers and this core API. The /internal
# routes settle money and reveal request content, so they must never be trusted
# on network isolation alone. When set, every /internal request must present it
# in X-Internal-Secret. Empty keeps the isolation-only behaviour for older
# deployments that have not provisioned the secret on both sides yet.
CORE_INTERNAL_SECRET = os.environ.get("CORE_INTERNAL_SECRET", "")

# Contract v3 pricing configuration (D12). Loaded once and validated at
# startup: an invalid window or price setup stops the core API from starting.
_PRICING: pricing.PricingConfig | None = None


def pricing_config() -> pricing.PricingConfig:
    global _PRICING
    if _PRICING is None:
        _PRICING = pricing.load_config()
    return _PRICING


class VerifyIn(BaseModel):
    instance: str = Field(min_length=1, max_length=50)
    intent: str = Field(min_length=1, max_length=2000)
    claim: str = Field(min_length=1, max_length=4000)
    agent_id: str = Field(pattern="^0x[0-9a-fA-F]{40}$")
    admission_mode: str = Field(pattern="^(entitlement|x402)$")
    callback_url: str | None = Field(default=None, max_length=2048)
    # nginx request id, for correlation with the access log and request_audit.
    request_id: str | None = Field(default=None, max_length=128)
    # Contract v3, x402 admissions: the quote the payment matched and the asset
    # it was made in, read from the verified payment by verify-api.
    terms_id: str | None = Field(default=None, max_length=64)
    paid_asset: str | None = Field(default=None, pattern="^0x[0-9a-fA-F]{40}$")


def _admit_v3(terms_sql: str) -> str:
    """SET clause that admits a v3 row. The windows start now, when admission
    is recorded, not when the request was created (D5).

    terms_sql is the SQL expression holding the bound terms. It must not be
    the bound_terms column when the same UPDATE also sets that column, because
    the right-hand side of an UPDATE sees the old row. A missing grace window
    yields NULL and the item then expires at the SLA deadline. expires_at
    mirrors the last window, so one expiry query serves v2 and v3 alike.
    """
    return f"""
        admitted_at = now(),
        sla_deadline = now() + make_interval(secs => ({terms_sql} #>> '{{windows,sla,within_seconds}}')::int),
        grace_deadline = now() + make_interval(secs => ({terms_sql} #>> '{{windows,grace,within_seconds}}')::int),
        expires_at = now() + make_interval(secs => COALESCE(
            {terms_sql} #>> '{{windows,grace,within_seconds}}',
            {terms_sql} #>> '{{windows,sla,within_seconds}}')::int)
    """


async def _audit_terms_bound(row) -> None:
    """The binding at admission, as the spec requires it to be recorded."""
    bound = pricing.as_json(row["bound_terms"])
    await audit(
        "core-api",
        "terms_bound",
        {
            "verify_id": str(row["id"]),
            "verify_no": row["verify_no"],
            "wallet_address": row["agent_id"],
            "terms_id": row["terms_id"],
            "entry_source": row["entry_source"],
            "network": row["bound_network"],
            "asset": row["bound_asset"],
            "asset_symbol": bound.get("asset_symbol"),
            "admission": bound.get("admission"),
            "sla": bound.get("sla"),
            "grace": bound.get("grace"),
            "conversion": bound.get("conversion"),
            "admitted_at": _iso(row["admitted_at"]),
            "sla_deadline": _iso(row["sla_deadline"]),
            "grace_deadline": _iso(row["grace_deadline"]),
        },
    )


def _credit_asset(quote: dict, source_asset: str | None) -> str:
    """Q3: a credit-funded chain binds to the asset of the chain that earned
    the credit. A credit minted before v3 has no bound asset and binds to
    USDC, the only asset that existed. If that asset is not offered now, the
    first offered asset is used."""
    offered = [p["asset"] for p in quote["terms"]["prices"]]
    preferred = source_asset or next(
        (a.address for a in pricing_config().assets if a.symbol == "USDC"), None
    )
    for address in offered:
        if preferred and address.lower() == preferred.lower():
            return address
    return offered[0]


def _usdc_decimal(atomic_column: str) -> str:
    """The *_usdc columns stay truthful for USDC chains and read 0 otherwise.

    They are decimal USDC, so an EURC amount must never land in them. The
    asset-neutral *_atomic columns carry the amount for every asset.
    """
    return (
        "CASE WHEN bound_terms->>'asset_symbol' = 'USDC' "
        f"THEN {atomic_column} / power(10::numeric, (bound_terms->>'asset_decimals')::int) "
        "ELSE 0 END"
    )


def _iso(value):
    return value.isoformat() if value else None


def _atomic(value):
    return str(value) if value is not None else None


class RequestAuditIn(BaseModel):
    """One durable audit row. Callers must send only safe, redacted fields.

    Never accepted here: request or response bodies, claim or intent text,
    full callback URLs, or any secret header. claim_len and claim_sha256 are
    the only claim-derived values allowed.
    """
    source: str = Field(pattern="^(rest|mcp|website)$")
    request_id: str | None = Field(default=None, max_length=128)
    route: str | None = Field(default=None, max_length=200)
    client_ip: str | None = Field(default=None, max_length=64)
    forwarded_for: str | None = Field(default=None, max_length=512)
    forwarded_trusted: bool = False
    user_agent: str | None = Field(default=None, max_length=512)
    agent_id: str | None = Field(default=None, max_length=100)
    verify_id: str | None = Field(default=None, max_length=64)
    verify_no: int | None = None
    callback_host: str | None = Field(default=None, max_length=255)
    admission_source: str | None = Field(default=None, max_length=20)
    http_status: int | None = None
    payment_required: bool | None = None
    funding_state: str | None = Field(default=None, max_length=24)
    outcome: str | None = Field(default=None, max_length=32)
    wallet_ownership_proven: bool = False
    claim_len: int | None = None
    claim_sha256: str | None = Field(default=None, max_length=64)


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
        # Contract v3. Null on v2 rows.
        "contract_version": row["contract_version"],
        "terms_id": row["terms_id"],
        "bound_terms": pricing.as_json(row["bound_terms"]) if row["bound_terms"] else None,
        "bound_asset": row["bound_asset"],
        "bound_network": row["bound_network"],
        "entry_amount_atomic": _atomic(row["entry_amount_atomic"]),
        "entry_charged_atomic": _atomic(row["entry_charged_atomic"]),
        "sla_deadline": _iso(row["sla_deadline"]),
        "grace_deadline": _iso(row["grace_deadline"]),
        "ready_at": _iso(row["ready_at"]),
        "applied_window": row["applied_window"],
        "unlock_amount_atomic": _atomic(row["unlock_amount_atomic"]),
        "unlock_charged_atomic": _atomic(row["unlock_charged_atomic"]),
    }


async def _expire_stale_once() -> int:
    """Fail timed-out human work.

    An x402-paid entry that times out gets one entry-only credit, because a
    real 0.10 USDC settlement was made. An entitlement-funded entry (a free
    chain or a prior credit) instead releases its entitlement back to the
    wallet: the free use is not lost, and a credit cannot mint a fresh credit,
    which is what previously let an expiring chain regenerate free admissions
    to the human queue indefinitely.
    """
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
                    failure_credit_granted = (agent_id IS NOT NULL AND entry_source = 'x402')
                WHERE status = 'pending' AND expires_at < now()
                RETURNING id, verify_no, instance, agent_id, associate_id, entry_source
                """
            )
            for r in rows:
                if not r["agent_id"]:
                    continue
                if r["entry_source"] == "x402":
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
                else:
                    # initial_free or failure_credit: return the entitlement so
                    # the unanswered chain costs the wallet nothing and no new
                    # credit is minted.
                    await conn.execute(
                        """
                        UPDATE wallet_entitlements
                        SET consumed_by_verify_id = NULL, consumed_at = NULL
                        WHERE consumed_by_verify_id = $1
                        """,
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
        credit_granted = bool(r["agent_id"]) and r["entry_source"] == "x402"
        await audit(
            "core-api",
            "verify_failed_credit_granted" if credit_granted else "verify_failed_entitlement_returned",
            {
                "verify_no": r["verify_no"],
                "verify_id": str(r["id"]),
                "wallet_address": r["agent_id"],
                "entry_source": r["entry_source"],
                "credit_usdc": "0.10" if credit_granted else "0.00",
                "entitlement_returned": bool(r["agent_id"]) and not credit_granted,
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


# A settled transaction is retried this many times before it is flagged for
# manual reconciliation. Most retries resolve a transient failure or a verify
# that was not yet in an applicable state.
SETTLEMENT_MAX_ATTEMPTS = 8


async def _reconcile_settlements_once() -> int:
    """Finish applying journaled settlements that were reported but not applied."""
    db = await get_pool()
    pending = await db.fetch(
        """
        SELECT id, verify_id, kind, transaction, payer, attempts
        FROM settlement_journal
        WHERE applied = false AND attempts < $1
        ORDER BY created_at
        LIMIT 20
        """,
        SETTLEMENT_MAX_ATTEMPTS,
    )
    done = 0
    for j in pending:
        try:
            async with db.acquire() as conn:
                async with conn.transaction():
                    row, _, applied_now = await _apply_settlement(
                        conn, j["verify_id"], j["kind"], j["transaction"], j["payer"]
                    )
                    await conn.execute(
                        "UPDATE settlement_journal SET applied = true, applied_at = now() WHERE id = $1",
                        j["id"],
                    )
            done += 1
            if applied_now and j["kind"] == "entry" and row["contract_version"] == 3:
                await _audit_terms_bound(row)
            await audit(
                "core-api",
                "settlement_reconciled",
                {"verify_id": str(j["verify_id"]), "kind": j["kind"], "transaction": j["transaction"]},
            )
        except Exception as exc:
            attempts = j["attempts"] + 1
            await db.execute(
                "UPDATE settlement_journal SET attempts = $2, last_error = $3 WHERE id = $1",
                j["id"],
                attempts,
                str(exc)[:200],
            )
            if attempts >= SETTLEMENT_MAX_ATTEMPTS:
                log.error(
                    "settlement unresolved verify=%s kind=%s tx=%s: %s",
                    j["verify_id"], j["kind"], j["transaction"], exc,
                )
                await audit(
                    "core-api",
                    "settlement_unresolved",
                    {
                        "verify_id": str(j["verify_id"]),
                        "kind": j["kind"],
                        "transaction": j["transaction"],
                        "error": str(exc)[:200],
                    },
                )
    return done


async def _verify_settlements_onchain_once() -> int:
    """Confirm applied settlements against Base itself.

    The payment gate is the x402 middleware and the facilitator. This runs
    afterwards and independently, so a transaction hash that was never mined
    cannot sit in the ledger unnoticed. It never blocks a payment: an
    unreachable RPC just leaves the row unchecked for the next pass.
    """
    db = await get_pool()
    rows = await db.fetch(
        """
        SELECT id, verify_id, kind, transaction
        FROM settlement_journal
        WHERE applied = true AND chain_verified IS NULL
        ORDER BY created_at
        LIMIT 10
        """
    )
    checked = 0
    for r in rows:
        verified, detail = await wallets.verify_transaction_onchain(r["transaction"])
        if verified is None:
            continue  # transient: leave it for the next pass
        await db.execute(
            """
            UPDATE settlement_journal
            SET chain_verified = $2, chain_checked_at = now(), chain_detail = $3
            WHERE id = $1
            """,
            r["id"], verified, detail,
        )
        checked += 1
        if verified:
            continue
        # A recorded settlement the chain has never seen. Either the ledger was
        # written by something other than a real payment, or the hash is wrong.
        log.error(
            "settlement not on chain verify=%s kind=%s tx=%s: %s",
            r["verify_id"], r["kind"], r["transaction"], detail,
        )
        await audit(
            "core-api",
            "settlement_not_on_chain",
            {
                "verify_id": str(r["verify_id"]),
                "kind": r["kind"],
                "transaction": r["transaction"],
                "detail": detail,
            },
        )
        if config.ADMIN_TELEGRAM_ID:
            await notify.send_message(
                config.ADMIN_TELEGRAM_ID,
                "🚨 Kirjattua maksua ei löydy ketjulta.\n\n"
                f"Verify: {r['verify_id']}\nPortti: {r['kind']}\n"
                f"Tx: {r['transaction']}\nSyy: {detail}\n\n"
                "Tarkista facilitatorin lokit. Tama voi tarkoittaa etta "
                "maksukirjaus syntyi ilman oikeaa settlementtia.",
            )
    return checked


async def _reconcile_settlements_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await _reconcile_settlements_once()
            await _verify_settlements_onchain_once()
        except Exception:
            log.exception("settlement reconcile loop failed")


async def _gas_watch_once() -> str:
    """Alert the operator before the gas till stops settling payments.

    An empty gas wallet silently breaks every paid gate, so this is a money
    outage, not a cosmetic warning. The alert is throttled by reading the last
    alert out of audit_log, so it survives a restart and never spams.
    """
    status = await wallets.wallet_status()
    gas = status["gas_wallet"]
    if gas["state"] != "low":
        return gas["state"]
    db = await get_pool()
    last = await db.fetchval(
        """
        SELECT at FROM audit_log
        WHERE event = 'gas_wallet_low'
        ORDER BY at DESC LIMIT 1
        """
    )
    if last is not None:
        age = await db.fetchval("SELECT EXTRACT(EPOCH FROM (now() - $1))", last)
        if age is not None and float(age) < wallets.GAS_ALERT_INTERVAL_S:
            return "low_muted"
    await audit(
        "core-api",
        "gas_wallet_low",
        {
            "address": gas["address"],
            "eth": gas["eth"],
            "threshold_eth": gas["low_threshold_eth"],
        },
    )
    log.error("gas wallet is low: %s ETH", gas["eth"])
    if config.ADMIN_TELEGRAM_ID:
        await notify.send_message(
            config.ADMIN_TELEGRAM_ID,
            "⛽ Kaasulompakko on vähissä.\n\n"
            f"Saldo: {gas['eth']} ETH (raja {gas['low_threshold_eth']}).\n"
            f"Osoite: {gas['address']}\n\n"
            "Kun kaasu loppuu, x402-maksut eivät settlaannu eikä tuloja tule. "
            "Lisää pieni summa ETH:tä Basella.",
        )
    return "low_alerted"


async def _gas_watch_loop() -> None:
    while True:
        try:
            await _gas_watch_once()
        except Exception:
            log.exception("gas watch failed")
        await asyncio.sleep(900)


async def _prune_request_audit_once() -> str:
    """Delete expired network-identifying audit rows.

    This is application-managed so the 30 day privacy promise remains true
    without relying on an operator to remember a manual SQL command.
    """
    db = await get_pool()
    result = await db.execute(
        """
        DELETE FROM request_audit
        WHERE at < now() - ($1 * interval '1 day')
        """,
        REQUEST_AUDIT_RETENTION_DAYS,
    )
    if result != "DELETE 0":
        log.info("request audit retention: %s", result)
    return result


async def _request_audit_retention_loop() -> None:
    while True:
        try:
            await _prune_request_audit_once()
        except Exception:
            log.exception("request audit retention failed")
        await asyncio.sleep(86400)


async def _alert_once_per(event: str, interval_s: int, details: dict, message: str) -> bool:
    """Audit and tell the operator, at most once per interval.

    The throttle reads the last such event from audit_log, so it survives a
    restart and cannot spam.
    """
    db = await get_pool()
    age = await db.fetchval(
        "SELECT EXTRACT(EPOCH FROM (now() - max(at))) FROM audit_log WHERE event = $1", event
    )
    if age is not None and float(age) < interval_s:
        return False
    await audit("core-api", event, details)
    if config.ADMIN_TELEGRAM_ID:
        await notify.send_message(config.ADMIN_TELEGRAM_ID, message)
    return True


async def _fx_once() -> pricing.FxRate | None:
    """Fetch a new reference rate if one was published, and flag a stale one."""
    db = await get_pool()
    rate = await fx_rates.refresh_once(db)
    if rate is None:
        await _alert_once_per(
            "fx_rate_missing", 6 * 3600, {"source": fx_rates.SOURCE},
            "💱 Verifi has no EUR/USD rate yet, so USDC cannot be quoted. EURC still works.",
        )
        return None
    age_days = await db.fetchval("SELECT current_date - $1::date", rate.as_of)
    if age_days > fx_rates.STALE_DAYS:
        await _alert_once_per(
            "fx_rate_stale", 24 * 3600,
            {"source": rate.source, "as_of": rate.as_of.isoformat(), "age_days": age_days},
            f"💱 The latest EUR/USD rate is {age_days} days old ({rate.as_of}). "
            "USDC is still quoted from it. Check the ECB feed.",
        )
    return rate


async def _fx_loop() -> None:
    # Retry quickly until the first rate exists, then once an hour: the ECB
    # publishes once per working day, and a known date is not stored twice.
    while True:
        rate = None
        try:
            rate = await _fx_once()
        except Exception:
            log.exception("fx refresh failed")
        await asyncio.sleep(3600 if rate else 300)


async def _token_metadata_loop() -> None:
    """Check each accepted token's EIP-712 name, version and decimals daily."""
    while True:
        unknown = False
        for asset in pricing_config().assets:
            try:
                ok, detail = await wallets.check_token_metadata(asset)
            except Exception:
                log.exception("token metadata check failed for %s", asset.symbol)
                ok, detail = None, "check raised"
            if ok is None:
                unknown = True
                log.warning("token metadata not checked: %s", detail)
            elif ok:
                log.info("token metadata: %s", detail)
            else:
                log.error("TOKEN METADATA MISMATCH: %s", detail)
                await _alert_once_per(
                    f"token_metadata_mismatch_{asset.symbol.lower()}", 24 * 3600,
                    {"asset": asset.symbol, "address": asset.address, "detail": detail},
                    f"⚠️ {detail}. Payments in {asset.symbol} will fail verification until "
                    "the configured EIP-712 name, version or decimals are corrected.",
                )
            await asyncio.sleep(2)
        await asyncio.sleep(600 if unknown else 86400)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    from core.webhooks import delivery_loop

    # A private key in this process would mean the deployment leaked one into
    # the wrong container. Refuse to start rather than run with it.
    wallets.assert_no_private_keys()
    for problem in wallets.validate_addresses():
        log.warning("wallet configuration: %s", problem)
    # D12: an invalid window or price configuration refuses to start.
    cfg = pricing_config()
    log.info(
        "pricing: basis %s, admission %s, sla %s within %ss, grace %s, assets %s",
        cfg.price_basis, cfg.admission, cfg.sla_unlock, cfg.sla_seconds,
        f"{cfg.grace_unlock} within {cfg.grace_seconds}s" if cfg.grace_enabled else "none",
        ",".join(a.symbol for a in cfg.assets),
    )

    await get_pool()
    tasks = [
        asyncio.create_task(_expire_stale_loop()),
        asyncio.create_task(delivery_loop()),
        asyncio.create_task(_reconcile_settlements_loop()),
        asyncio.create_task(_gas_watch_loop()),
        asyncio.create_task(_request_audit_retention_loop()),
        asyncio.create_task(_fx_loop()),
        asyncio.create_task(_token_metadata_loop()),
    ]
    yield
    for task in tasks:
        task.cancel()
    await close_pool()


app = FastAPI(title="Verifi core internal API", lifespan=lifespan)


@app.middleware("http")
async def _guard_internal(request: Request, call_next):
    """Authenticate the internal money surface with a shared secret.

    Only /internal/* is guarded: /health, /contact, and /admin have their own
    contracts (liveness, public form, dashboard token). Enforced only when the
    secret is configured, so it can be rolled out on both sides without a
    flag day.
    """
    if CORE_INTERNAL_SECRET and request.url.path.startswith("/internal/"):
        supplied = request.headers.get("x-internal-secret", "")
        if not hmac.compare_digest(supplied, CORE_INTERNAL_SECRET):
            log.warning("rejected unauthenticated /internal call to %s", request.url.path)
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


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

    # Contract v3 terms, resolved before the admission transaction so the quote
    # lock is never held while an associate is being notified.
    #   x402: bind the quote the verified payment matched, in the asset it was
    #   made in. Validity was enforced when the payment was matched, so a quote
    #   that expired a moment ago still binds: the agent paid against it.
    #   entitlement: a credit-funded admission binds the current quote. Its
    #   asset is chosen inside the transaction, from the credit's source chain.
    paid_bound = None
    paid_commission = None
    current_quote = None
    if body.admission_mode == "x402":
        if not body.terms_id or not body.paid_asset:
            raise HTTPException(status_code=409, detail="admission terms are missing from the payment")
        quote = await db.fetchrow(
            "SELECT terms, internal FROM pricing_terms WHERE terms_id = $1", body.terms_id
        )
        if quote is None:
            raise HTTPException(status_code=409, detail="the paid quote is unknown")
        internal = pricing.as_json(quote["internal"])
        paid_bound = pricing.bind_terms(pricing.as_json(quote["terms"]), internal, body.paid_asset)
        if paid_bound is None:
            raise HTTPException(status_code=409, detail="the paid asset is not part of the quote")
        paid_commission = Decimal(internal["responder_commission_eur"])
    else:
        try:
            current_quote = await terms_current(body.instance)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            current_quote = None  # a credit admission refuses below; nothing is consumed

    assigned = False
    benefit_kind = None
    bound_for_audit = None
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
                    entry_source, callback_url, request_id,
                    contract_version, terms_id, bound_terms, bound_asset, bound_network,
                    bound_commission_eur, entry_amount_atomic
                )
                VALUES ($1, $2, $3, $4, $5, 'admission_pending', $6, $7, $8,
                        $9, $10, $11::jsonb, $12, $13, $14, $15)
                RETURNING *
                """,
                body.instance,
                body.intent,
                body.claim,
                body.agent_id,
                "free" if body.admission_mode == "entitlement" else "paid",
                "x402" if body.admission_mode == "x402" else None,
                body.callback_url,
                body.request_id,
                3 if paid_bound else 2,
                paid_bound["terms_id"] if paid_bound else None,
                json.dumps(paid_bound) if paid_bound else None,
                paid_bound["asset"] if paid_bound else None,
                paid_bound["network"] if paid_bound else None,
                paid_commission,
                int(paid_bound["admission"]) if paid_bound else None,
            )

            if body.admission_mode == "entitlement":
                allowance = instance["free_tier_count"]
                # Count free uses actually consumed, not merely ever granted.
                # A free chain that expires unanswered releases its slot (see
                # _expire_stale_once), so it is not counted here and the wallet
                # keeps its full free allowance. For verify-api that allowance
                # is now 0, so this branch does nothing; it stays generic for
                # any instance that sets free_tier_count above 0.
                free_used = await conn.fetchval(
                    """
                    SELECT count(*) FROM wallet_entitlements
                    WHERE instance = $1 AND lower(wallet_address) = lower($2)
                      AND kind = 'initial_free' AND consumed_by_verify_id IS NOT NULL
                    """,
                    body.instance,
                    body.agent_id,
                )
                # Platform-wide daily free budget. When spent, this wallet's
                # remaining free allowance waits: the request falls through to
                # the paid path instead of consuming a free slot.
                budget_open = config.FREE_DAILY_MAX <= 0 or (
                    await conn.fetchval(
                        """
                        SELECT count(*) FROM wallet_entitlements
                        WHERE kind = 'initial_free'
                          AND consumed_at >=
                              date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
                        """
                    )
                ) < config.FREE_DAILY_MAX
                if free_used < allowance and budget_open:
                    # Prefer reclaiming a released free slot over minting a new
                    # one. This keeps the number of free rows at or below the
                    # allowance and reuses the original free_use_number.
                    reusable = await conn.fetchrow(
                        """
                        SELECT id, free_use_number FROM wallet_entitlements
                        WHERE instance = $1 AND lower(wallet_address) = lower($2)
                          AND kind = 'initial_free' AND consumed_by_verify_id IS NULL
                        ORDER BY free_use_number
                        LIMIT 1 FOR UPDATE
                        """,
                        body.instance,
                        body.agent_id,
                    )
                    if reusable is not None:
                        free_no = reusable["free_use_number"]
                        await conn.execute(
                            """
                            UPDATE wallet_entitlements
                            SET consumed_by_verify_id = $2, consumed_at = now()
                            WHERE id = $1
                            """,
                            reusable["id"],
                            verify["id"],
                        )
                    else:
                        free_no = await conn.fetchval(
                            """
                            SELECT COALESCE(max(free_use_number), 0) + 1
                            FROM wallet_entitlements
                            WHERE instance = $1 AND lower(wallet_address) = lower($2)
                              AND kind = 'initial_free'
                            """,
                            body.instance,
                            body.agent_id,
                        )
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
                        SELECT e.id, s.bound_asset AS source_asset
                        FROM wallet_entitlements e
                        LEFT JOIN verifies s ON s.id = e.source_verify_id
                        WHERE e.instance = $1 AND lower(e.wallet_address) = lower($2)
                          AND e.kind = 'failure_credit' AND e.consumed_by_verify_id IS NULL
                        ORDER BY e.granted_at, e.id
                        LIMIT 1 FOR UPDATE OF e SKIP LOCKED
                        """,
                        body.instance,
                        body.agent_id,
                    )
                    if credit is None:
                        raise HTTPException(status_code=402, detail="no free chain or entry credit remains")
                    if current_quote is None:
                        # Refused before the credit is consumed, so it is kept.
                        raise HTTPException(
                            status_code=503,
                            detail="pricing is unavailable, retry shortly",
                            headers={"Retry-After": "60"},
                        )
                    asset = _credit_asset(current_quote, credit["source_asset"])
                    bound = pricing.bind_terms(current_quote["terms"], current_quote["internal"], asset)
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
                        f"""
                        UPDATE verifies
                        SET tier = 'paid', entry_source = 'failure_credit', status = 'pending',
                            contract_version = 3, terms_id = $2, bound_terms = $3::jsonb,
                            bound_asset = $4, bound_network = $5, bound_commission_eur = $6,
                            entry_amount_atomic = $7, entry_charged_atomic = 0,
                            {_admit_v3("$3::jsonb")}
                        WHERE id = $1 RETURNING *
                        """,
                        verify["id"],
                        bound["terms_id"],
                        json.dumps(bound),
                        bound["asset"],
                        bound["network"],
                        Decimal(current_quote["internal"]["responder_commission_eur"]),
                        int(bound["admission"]),
                    )
                    bound_for_audit = verify

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
            "contract_version": verify["contract_version"],
            "terms_id": verify["terms_id"],
            "bound_asset": verify["bound_asset"],
            "entry_amount_atomic": _atomic(verify["entry_amount_atomic"]),
            "entry_list_price_usdc": "0.10" if verify["contract_version"] == 2 else None,
            "entry_charged_usdc": "0.00",
            "free_use_number": verify["free_use_number"],
            "assigned": assigned,
        },
    )
    if bound_for_audit is not None:
        await _audit_terms_bound(bound_for_audit)
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


def _terms_payload(row, *, valid: bool) -> dict:
    return {
        "terms_id": row["terms_id"],
        "terms": pricing.as_json(row["terms"]),
        "internal": pricing.as_json(row["internal"]),
        "valid_until": row["valid_until"].isoformat(),
        "valid": valid,
    }


@app.get("/internal/terms/current")
async def terms_current(instance: str = "verify-api") -> dict:
    """The quote a new 402 advertises. Reused while it is fresh enough."""
    db = await get_pool()
    async with db.acquire() as conn:
        commission = await conn.fetchval(
            "SELECT associate_commission FROM instances WHERE id = $1", instance
        )
        if commission is None:
            raise HTTPException(status_code=404, detail="unknown instance")
        rate = await fx_rates.latest_rate(conn)
        try:
            row, created = await pricing.current_quote(conn, pricing_config(), rate, commission)
        except pricing.PricingUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc), headers={"Retry-After": "60"})
    if created:
        terms = pricing.as_json(row["terms"])
        await audit(
            "core-api",
            "terms_quoted",
            {
                "terms_id": row["terms_id"],
                "instance": instance,
                "valid_until": row["valid_until"].isoformat(),
                "windows": terms["windows"],
                "price_basis": terms["price_basis"],
                "prices": terms["prices"],
            },
        )
    return _terms_payload(row, valid=True)


@app.get("/internal/terms/{terms_id}")
async def terms_by_id(terms_id: str) -> dict:
    """A specific quote, and whether a payment may still be made against it."""
    if len(terms_id) > 64:
        raise HTTPException(status_code=404, detail="terms not found")
    db = await get_pool()
    row = await db.fetchrow(
        "SELECT *, valid_until >= now() AS still_valid FROM pricing_terms WHERE terms_id = $1",
        terms_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="terms not found")
    return _terms_payload(row, valid=row["still_valid"])


class PaymentIn(BaseModel):
    kind: str = Field(pattern="^(entry|unlock)$")
    transaction: str = Field(min_length=4, max_length=80)
    payer: str | None = Field(default=None, max_length=80)


async def _apply_settlement(conn, verify_id, kind: str, transaction: str, payer):
    """Advance exactly one paid gate for a settled transaction. Idempotent.

    Re-applying the same (verify, kind, transaction) is a no-op that returns
    the current row, so record_payment and the reconciliation loop can both
    call this safely. Returns (row, assigned, applied_now); applied_now is
    False on such a replay, so callers audit a binding exactly once.
    """
    current = await conn.fetchrow("SELECT * FROM verifies WHERE id = $1 FOR UPDATE", verify_id)
    if current is None:
        raise HTTPException(status_code=404, detail="verify not found")
    assigned = False
    applied_now = False
    if kind == "entry":
        if current["x402_payment_tx"]:
            if current["x402_payment_tx"] != transaction:
                raise HTTPException(status_code=409, detail="entry is already settled")
            row = current
        else:
            if current["status"] != "admission_pending" or current["entry_source"] != "x402":
                raise HTTPException(status_code=409, detail="verify is not awaiting x402 admission")
            if current["contract_version"] == 3:
                # Admission follows settlement: the windows start now, and the
                # amount charged is the bound admission amount in the paid asset.
                row = await conn.fetchrow(
                    f"""
                    UPDATE verifies
                    SET x402_payment_tx = $2, entry_payer = COALESCE($3, agent_id),
                        status = 'pending',
                        entry_charged_atomic = entry_amount_atomic,
                        entry_charged_usdc = {_usdc_decimal("entry_amount_atomic")},
                        {_admit_v3("bound_terms")}
                    WHERE id = $1 RETURNING *
                    """,
                    verify_id,
                    transaction,
                    payer,
                )
            else:
                row = await conn.fetchrow(
                    """
                    UPDATE verifies
                    SET x402_payment_tx = $2, entry_payer = COALESCE($3, agent_id),
                        entry_charged_usdc = 0.10, admitted_at = now(), status = 'pending'
                    WHERE id = $1 RETURNING *
                    """,
                    verify_id,
                    transaction,
                    payer,
                )
            assigned = await _route_admitted_verify(conn, row)
            applied_now = True
    else:
        if current["x402_unlock_tx"]:
            if current["x402_unlock_tx"] != transaction:
                raise HTTPException(status_code=409, detail="result is already unlocked")
            row = current
        else:
            if current["status"] not in ("accepted", "rejected", "refined"):
                raise HTTPException(status_code=409, detail="result is not ready")
            if current["result_unlocked"]:
                raise HTTPException(status_code=409, detail="result is already unlocked")
            if current["entry_source"] == "initial_free":
                raise HTTPException(status_code=409, detail="free chain must use entitlement unlock")
            if current["contract_version"] == 3:
                if current["unlock_amount_atomic"] is None:
                    raise HTTPException(status_code=409, detail="the unlock price was never decided")
                # The amount was decided once when the answer was recorded.
                # It is only read here, never recomputed.
                row = await conn.fetchrow(
                    f"""
                    UPDATE verifies
                    SET x402_unlock_tx = $2, unlock_source = 'x402',
                        unlock_payer = COALESCE($3, agent_id),
                        unlock_charged_atomic = unlock_amount_atomic,
                        unlock_charged_usdc = {_usdc_decimal("unlock_amount_atomic")},
                        result_unlocked = true, unlocked_at = now()
                    WHERE id = $1 RETURNING *
                    """,
                    verify_id,
                    transaction,
                    payer,
                )
            else:
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
                    transaction,
                    payer,
                )
            applied_now = True
    return row, assigned, applied_now


@app.post("/internal/verifies/{verify_id}/payment")
async def record_payment(verify_id: UUID, body: PaymentIn) -> dict:
    """Attach a settled x402 transaction and advance exactly one paid gate.

    The report is journaled durably before it is applied. If the process dies
    between the two, or the verify is not yet applicable, the reconciliation
    loop finishes the job from the journal, so a settled payment is never lost.
    """
    db = await get_pool()
    await db.execute(
        """
        INSERT INTO settlement_journal (verify_id, kind, transaction, payer)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (transaction, kind) DO NOTHING
        """,
        verify_id,
        body.kind,
        body.transaction,
        body.payer,
    )
    async with db.acquire() as conn:
        async with conn.transaction():
            row, assigned, applied_now = await _apply_settlement(
                conn, verify_id, body.kind, body.transaction, body.payer
            )
            await conn.execute(
                """
                UPDATE settlement_journal SET applied = true, applied_at = now()
                WHERE transaction = $1 AND kind = $2
                """,
                body.transaction,
                body.kind,
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
            "contract_version": row["contract_version"],
            "asset": row["bound_asset"],
            "amount_atomic": _atomic(
                row["entry_charged_atomic"] if body.kind == "entry" else row["unlock_charged_atomic"]
            ),
            "amount_usdc": (
                ("0.10" if body.kind == "entry" else "2.90") if row["contract_version"] == 2
                else str(row["entry_charged_usdc"] if body.kind == "entry" else row["unlock_charged_usdc"])
            ),
            "applied_window": row["applied_window"] if body.kind == "unlock" else None,
            "assigned": assigned if body.kind == "entry" else None,
        },
    )
    if applied_now and body.kind == "entry" and row["contract_version"] == 3:
        await _audit_terms_bound(row)
    return {"ok": True, "verify_no": row["verify_no"], "assigned": assigned}


class SettlementAlertIn(BaseModel):
    verify_id: str = Field(max_length=64)
    kind: str = Field(pattern="^(entry|unlock)$")
    transaction: str | None = Field(default=None, max_length=80)
    detail: str | None = Field(default=None, max_length=300)


@app.post("/internal/settlement-alerts")
async def settlement_alert(body: SettlementAlertIn) -> dict:
    """Record a settlement an instance settled on-chain but could not persist.

    Makes the failure durable and dashboard-visible for reconciliation, rather
    than leaving it only in the instance's stderr.
    """
    log.error(
        "settlement capture unresolved verify=%s kind=%s tx=%s",
        body.verify_id, body.kind, body.transaction,
    )
    await audit(
        "core-api",
        "settlement_capture_unresolved",
        {
            "verify_id": body.verify_id,
            "kind": body.kind,
            "transaction": body.transaction,
            "detail": body.detail,
        },
    )
    return {"ok": True}


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
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    email: str = Field(
        min_length=3,
        max_length=300,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    message: str = Field(min_length=1, max_length=5000)
    company_website: str = Field(default="", max_length=300)


@app.post("/contact")
async def contact(body: ContactIn, request: Request = None) -> dict:
    """Public contact form (proxied through nginx). Delivers to Telegram."""
    if request is not None:
        await create_request_audit(RequestAuditIn(
            source="website",
            route="/contact",
            request_id=request.headers.get("x-request-id"),
            client_ip=request.headers.get("x-real-ip"),
            forwarded_for=request.headers.get("x-forwarded-for"),
            forwarded_trusted=request.headers.get("x-forwarded-trusted") == "1",
            user_agent=(request.headers.get("user-agent") or "")[:512] or None,
            http_status=200,
            payment_required=False,
            outcome="contact_received",
        ))
    if body.company_website:
        await audit("core-api", "contact_spam_filtered", {})
        return {"ok": True, "delivery": "filtered"}

    await audit(
        "core-api",
        "contact_message_received",
        {"name": body.name, "email": body.email, "length": len(body.message)},
    )

    if not config.TELEGRAM_BOT_TOKEN or not config.ADMIN_TELEGRAM_ID:
        await audit("core-api", "contact_delivery_failed", {"reason": "not_configured"})
        raise HTTPException(status_code=503, detail="contact delivery is not configured")

    try:
        message_id = await notify.send_message(
            config.ADMIN_TELEGRAM_ID,
            "📬 Yhteydenotto verifi.cloudista\n\n"
            f"Nimi: {body.name}\n"
            f"Sähköposti: {body.email}\n\n"
            f"{body.message[:3500]}",
        )
    except Exception as exc:
        log.warning("contact Telegram delivery raised %s", type(exc).__name__)
        await audit("core-api", "contact_delivery_failed", {"reason": "telegram_error"})
        raise HTTPException(status_code=502, detail="Telegram delivery failed") from exc

    if message_id is None:
        await audit("core-api", "contact_delivery_failed", {"reason": "telegram_rejected"})
        raise HTTPException(status_code=502, detail="Telegram delivery failed")

    await audit(
        "core-api",
        "contact_message_delivered",
        {"telegram_message_id": message_id},
    )
    return {"ok": True, "delivery": "telegram"}


@app.post("/internal/request-audit")
async def create_request_audit(body: RequestAuditIn) -> dict:
    """Append one durable request audit row. Best effort, like the money audit:
    a failure here is logged but never breaks the request that produced it."""
    client_ip = body.client_ip
    if client_ip:
        try:
            ipaddress.ip_address(client_ip)
        except ValueError:
            client_ip = None
    try:
        db = await get_pool()
        await db.execute(
            """
            INSERT INTO request_audit (
                request_id, source, route, client_ip, forwarded_for,
                forwarded_trusted, user_agent, agent_id, verify_id, verify_no,
                callback_host, admission_source, http_status, payment_required,
                funding_state, outcome, wallet_ownership_proven, claim_len,
                claim_sha256
            )
            VALUES ($1, $2, $3, $4::inet, $5, $6, $7, $8, $9::uuid, $10,
                    $11, $12, $13, $14, $15, $16, $17, $18, $19)
            """,
            body.request_id, body.source, body.route, client_ip,
            body.forwarded_for, body.forwarded_trusted, body.user_agent,
            body.agent_id, body.verify_id, body.verify_no, body.callback_host,
            body.admission_source, body.http_status, body.payment_required,
            body.funding_state, body.outcome, body.wallet_ownership_proven,
            body.claim_len, body.claim_sha256,
        )
    except Exception:
        log.exception("request_audit write failed")
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
            "free_capacity_open": False,
            "entitlement_admission_available": False,
            "pending_count": 0,
        }
    row2 = await db.fetchrow(
        """
        SELECT count(*) FILTER (
                   WHERE kind = 'initial_free' AND consumed_by_verify_id IS NOT NULL
               ) AS free_used,
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
    free_capacity_open = True
    if config.FREE_DAILY_MAX > 0:
        free_today = await db.fetchval(
            """
            SELECT count(*) FROM wallet_entitlements
            WHERE kind = 'initial_free'
              AND consumed_at >=
                  date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
            """
        )
        free_capacity_open = free_today < config.FREE_DAILY_MAX
    full_free_remaining = max(0, allowance - row2["free_used"])
    # A free chain is only admissible when the wallet has allowance left AND the
    # platform's daily free budget is still open. Earned credits are never
    # gated by the budget.
    entitlement_admission_available = (
        row2["entry_credits_remaining"] > 0
        or (full_free_remaining > 0 and free_capacity_open)
    )
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
        "free_capacity_open": free_capacity_open,
        "entitlement_admission_available": entitlement_admission_available,
        "pending_count": pending_count,
        "pending_total": pending_total,
        "queue_full": pending_total >= MAX_PENDING_TOTAL,
    }
