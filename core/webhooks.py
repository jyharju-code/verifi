"""Webhook delivery for ready or failed verifies, with SSRF protection.

An agent can pass callback_url when creating a verify. When the verify
becomes ready or fails, this loop POSTs the next action to that URL. Result
content stays locked until gate 2. At-least-once, 3 attempts with backoff; polling
GET /verify/{id} always remains available.

SSRF rules (per the Ask This Finn security design): https only, default
port only, hostname must resolve to public unicast addresses, redirects
disabled, tight timeouts, response body ignored.
"""
import asyncio
import ipaddress
import json
import logging
import socket
from urllib.parse import urlparse

import httpx

from core import config, pricing
from core.audit import audit
from core.db.database import get_pool

log = logging.getLogger("verifi.webhooks")

MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = [0, 60, 300]


def _usdc_or_none(terms: dict, atomic) -> str | None:
    """A decimal USDC string on a USDC chain, None on any other asset."""
    if terms["asset_symbol"] != "USDC" or atomic is None:
        return None
    return f"{pricing.from_atomic(int(atomic), int(terms['asset_decimals'])):.2f}"


def _public_payload(row) -> dict:
    """The callback body. It never carries the locked result.

    For a contract v3 chain it carries the same window facts as the status
    view: when the human answered, which window applied, and the unlock
    price in the bound asset. *_usdc fields are null on non-USDC chains.
    """
    failed = row["status"] in ("expired", "failed")
    full_free = row["entry_source"] == "initial_free"
    v3 = row.get("contract_version") == 3 and row.get("bound_terms") is not None
    terms = pricing.as_json(row["bound_terms"]) if v3 else None
    payload = {
        "event": "verify.failed" if failed else "verify.ready",
        "verify_id": str(row["id"]),
        "work_id": str(row["id"]),
        "contract_version": 3 if v3 else 2,
        "status": "failed" if failed else "ready",
        "verdict": None,
        "explanation": None,
        "response": None,
        "next_action": "stop" if failed else "unlock",
        "poll_url": f"/verify/{row['id']}",
        "responded_at": row["responded_at"].isoformat() if row["responded_at"] else None,
    }
    if failed:
        granted = bool(row["failure_credit_granted"])
        payload["failure"] = {
            "reason": row["failure_reason"] or "processing_failed",
            "entry_credit_granted": granted,
            "entry_credit_value_usdc": (
                _usdc_or_none(terms, terms["admission"] if granted else 0)
                if v3 else ("0.10" if granted else "0.00")
            ),
        }
        if v3:
            payload["failure"]["entry_credit"] = "next_admission" if granted else None
    else:
        payload["unlock"] = {
            "method": "POST",
            "url": f"/verify-unlock?id={row['id']}",
            "price_usdc": (
                "0.00" if full_free
                else _usdc_or_none(terms, row["unlock_amount_atomic"]) if v3
                else "2.90"
            ),
            "payment_required": not full_free,
        }
        if v3:
            unlock_amount = pricing.atomic_str(row["unlock_amount_atomic"])
            payload["unlock_url"] = f"{config.PUBLIC_BASE_URL.rstrip('/')}/verify-unlock?id={row['id']}"
            payload["ready_at"] = row["ready_at"].isoformat() if row["ready_at"] else None
            payload["unlock"].update({
                "network": terms["network"],
                "asset": terms["asset"],
                "asset_symbol": terms["asset_symbol"],
                "amount_atomic": unlock_amount,
                "applied_window": row["applied_window"],
            })
            payload["service_window"] = {
                "applied": row["applied_window"],
                "sla_deadline": row["sla_deadline"].isoformat() if row["sla_deadline"] else None,
                "grace_deadline": row["grace_deadline"].isoformat() if row["grace_deadline"] else None,
                "network": terms["network"],
                "asset": terms["asset"],
                "unlock_amount": unlock_amount,
            }
    return payload


def _resolve_pinned(url: str) -> tuple[bool, str, str | None, str | None, str | None]:
    """Validate a callback URL and pin it to a resolved public IP.

    Returns (ok, reason, pinned_url, host_header, sni_host). The pinned URL
    connects straight to the validated IP, so the address cannot change
    between this check and the request (DNS rebinding). TLS still verifies the
    certificate against the original hostname via the SNI host.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "unparseable url", None, None, None
    if parsed.scheme != "https":
        return False, "https required", None, None, None
    if parsed.port is not None and parsed.port != 443:
        return False, "port 443 only", None, None, None
    if not parsed.hostname or parsed.username or parsed.password:
        return False, "invalid host", None, None, None
    try:
        infos = socket.getaddrinfo(parsed.hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, "hostname does not resolve", None, None, None
    pinned_ip = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False, "resolves to non-public address", None, None, None
        if pinned_ip is None:
            pinned_ip = info[4][0]
    if pinned_ip is None:
        return False, "hostname does not resolve", None, None, None
    host_literal = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    pinned_url = parsed._replace(netloc=f"{host_literal}:443").geturl()
    return True, "ok", pinned_url, parsed.netloc, parsed.hostname


async def _deliver_one(db, row) -> None:
    attempt = row["callback_attempts"] + 1
    ok, reason, pinned_url, host_header, sni_host = _resolve_pinned(row["callback_url"])
    delivered = False
    detail = reason
    if ok:
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, max_redirects=0
            ) as client:
                resp = await client.post(
                    pinned_url,
                    json=_public_payload(row),
                    headers={"user-agent": "verifi-webhook/1.0", "host": host_header},
                    extensions={"sni_hostname": sni_host},
                )
            delivered = 200 <= resp.status_code < 300
            detail = f"http {resp.status_code}"
        except httpx.HTTPError as exc:
            detail = f"{type(exc).__name__}"
    final = delivered or attempt >= MAX_ATTEMPTS or not ok
    await db.execute(
        """
        UPDATE verifies
        SET callback_delivered = $2,
            callback_attempts = $3,
            callback_last_attempt = now()
        WHERE id = $1
        """,
        row["id"],
        delivered,
        attempt if not delivered else attempt,
    )
    if delivered:
        log.info("callback delivered verify=%s attempt=%s", row["id"], attempt)
        await audit("core-api", "callback_delivered", {"verify_id": str(row["id"]), "attempt": attempt})
    elif final:
        log.warning("callback FAILED finally verify=%s: %s", row["id"], detail)
        await audit(
            "core-api",
            "callback_failed",
            {"verify_id": str(row["id"]), "attempts": attempt, "reason": detail},
        )
        # Stop retrying: mark as attempts exhausted.
        await db.execute(
            "UPDATE verifies SET callback_attempts = $2 WHERE id = $1",
            row["id"],
            MAX_ATTEMPTS,
        )


async def delivery_loop() -> None:
    while True:
        await asyncio.sleep(5)
        try:
            db = await get_pool()
            rows = await db.fetch(
                """
                SELECT * FROM verifies
                WHERE callback_url IS NOT NULL
                  AND callback_delivered = false
                  AND callback_attempts < $1
                  AND status IN ('accepted', 'rejected', 'refined', 'expired', 'failed')
                  AND (callback_last_attempt IS NULL
                       OR callback_last_attempt < now() - make_interval(secs =>
                            CASE callback_attempts WHEN 0 THEN 0 WHEN 1 THEN 60 ELSE 300 END))
                LIMIT 10
                """,
                MAX_ATTEMPTS,
            )
            for row in rows:
                await _deliver_one(db, row)
        except Exception:
            log.exception("webhook delivery loop failed")
