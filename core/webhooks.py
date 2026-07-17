"""Webhook delivery for resolved verifies, with SSRF protection.

An agent can pass callback_url when creating a verify. When the verify
leaves pending (answered or expired), this loop POSTs the public view of
the result to that URL. At-least-once, 3 attempts with backoff; polling
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

from core.audit import audit
from core.db.database import get_pool

log = logging.getLogger("verifi.webhooks")

MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = [0, 60, 300]


def _public_payload(row) -> dict:
    status = row["status"]
    if status == "accepted":
        verdict, explanation = "true", None
    elif status == "rejected":
        verdict, explanation = "false", None
    elif status == "refined":
        verdict, explanation = "refined", row["response"]
    else:
        verdict, explanation = None, None
    locked = row["tier"] == "paid" and not row["unlock_paid"]
    return {
        "event": "verify.resolved",
        "verify_id": str(row["id"]),
        "status": status,
        "verdict": None if locked else verdict,
        "explanation": None if locked else explanation,
        "response": None if locked else row["response"],
        "response_time_ms": None if locked else row["response_time_ms"],
        "tier": row["tier"],
        "unlock_paid": row["unlock_paid"],
        "responded_at": row["responded_at"].isoformat() if row["responded_at"] else None,
    }


def _ssrf_safe(url: str) -> tuple[bool, str]:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False, "unparseable url"
    if parsed.scheme != "https":
        return False, "https required"
    if parsed.port is not None and parsed.port != 443:
        return False, "port 443 only"
    if not parsed.hostname or parsed.username or parsed.password:
        return False, "invalid host"
    try:
        infos = socket.getaddrinfo(parsed.hostname, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False, "hostname does not resolve"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False, f"resolves to non-public address"
    return True, "ok"


async def _deliver_one(db, row) -> None:
    attempt = row["callback_attempts"] + 1
    ok, reason = _ssrf_safe(row["callback_url"])
    delivered = False
    detail = reason
    if ok:
        try:
            async with httpx.AsyncClient(
                timeout=10, follow_redirects=False, max_redirects=0
            ) as client:
                resp = await client.post(
                    row["callback_url"],
                    json=_public_payload(row),
                    headers={"user-agent": "verifi-webhook/1.0"},
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
                  AND status <> 'pending'
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
