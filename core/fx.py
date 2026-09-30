"""EUR/USD reference rate for converting euro prices into USDC amounts.

Source: the ECB euro foreign exchange reference rates, daily XML. Published
around 16:00 CET on TARGET working days, so on weekends and holidays the latest
rate is a few days old. Reuse is free with attribution ("Source: ECB"), and the
terms of every quote name the exact rate, source and reference date used.

The ECB publishes these rates "for information purposes only". Verifi does not
exchange anyone's money at this rate: it uses the rate to set its own published
USDC list price, rounded up to the cent, and discloses the rate it used.

One row is stored per publication date, so the rate is fetched once per
publication and every process reads the same value from PostgreSQL.
"""
import logging
import os
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from core.audit import audit
from core.pricing import FxRate

log = logging.getLogger("verifi.fx")

ECB_DAILY_URL = os.environ.get(
    "FX_ECB_URL", "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
)
SOURCE = "ECB euro reference rate"
# A latest rate older than this raises an operator alert. USDC keeps being
# quoted from it, because dropping USDC would strand every USDC-only agent.
STALE_DAYS = int(os.environ.get("FX_STALE_DAYS", "7"))
MAX_BYTES = 200_000
_ECB_NS = "{http://www.ecb.int/vocabulary/2002-08-01/eurofxref}"


class FxParseError(ValueError):
    pass


def parse_ecb_daily(xml_text: str) -> tuple[date, Decimal]:
    """Return (reference date, USD per EUR) from the ECB daily XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FxParseError(f"not XML: {exc}") from exc
    dated = next((c for c in root.iter(f"{_ECB_NS}Cube") if c.get("time")), None)
    if dated is None:
        raise FxParseError("no reference date in the ECB file")
    usd = next((c for c in dated.iter(f"{_ECB_NS}Cube") if c.get("currency") == "USD"), None)
    if usd is None:
        raise FxParseError("no USD rate in the ECB file")
    try:
        as_of = date.fromisoformat(dated.get("time"))
        rate = Decimal(usd.get("rate"))
    except (ValueError, InvalidOperation) as exc:
        raise FxParseError(f"malformed rate or date: {exc}") from exc
    if not rate.is_finite() or rate <= 0:
        raise FxParseError(f"implausible rate {rate}")
    return as_of, rate


async def fetch_ecb(client: httpx.AsyncClient | None = None) -> tuple[date, Decimal]:
    owned = client is None
    client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    try:
        resp = await client.get(ECB_DAILY_URL)
        resp.raise_for_status()
        if len(resp.content) > MAX_BYTES:
            raise FxParseError("ECB response is unexpectedly large")
        return parse_ecb_daily(resp.text)
    finally:
        if owned:
            await client.aclose()


async def store_rate(db, as_of: date, rate: Decimal) -> bool:
    """Record one publication. Returns True when it was new."""
    inserted = await db.fetchval(
        """
        INSERT INTO fx_rates (source, base, quote, rate, rate_date)
        VALUES ($1, 'EUR', 'USD', $2, $3)
        ON CONFLICT (source, base, quote, rate_date) DO NOTHING
        RETURNING id
        """,
        SOURCE,
        rate,
        as_of,
    )
    if inserted is not None:
        log.info("EUR/USD %s recorded for %s", rate, as_of)
        await audit("core-api", "fx_rate_recorded",
                    {"source": SOURCE, "rate": str(rate), "as_of": as_of.isoformat()})
    return inserted is not None


async def latest_rate(db) -> FxRate | None:
    row = await db.fetchrow(
        """
        SELECT rate, rate_date FROM fx_rates
        WHERE source = $1 AND base = 'EUR' AND quote = 'USD'
        ORDER BY rate_date DESC, fetched_at DESC
        LIMIT 1
        """,
        SOURCE,
    )
    if row is None:
        return None
    return FxRate(rate=row["rate"], as_of=row["rate_date"], source=SOURCE)


async def refresh_once(db) -> FxRate | None:
    """Fetch the latest publication if it is new, and return the latest rate."""
    try:
        as_of, rate = await fetch_ecb()
        await store_rate(db, as_of, rate)
    except (httpx.HTTPError, FxParseError) as exc:
        log.warning("ECB rate fetch failed: %s", exc)
        await audit("core-api", "fx_rate_fetch_failed", {"source": SOURCE, "error": str(exc)[:200]})
    return await latest_rate(db)
