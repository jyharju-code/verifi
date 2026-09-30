"""Contract v3 pricing: the one function that builds the terms of a work item.

Implements the terms of docs/specs/extension-service-windows.md. Prices are
defined in a basis currency (the euro). Each accepted asset gets atomic amounts:
an asset denominated in the basis currency takes the basis prices as they are,
and any other asset is converted at the quote-time rate, rounded up to the next
cent. The resulting object is exactly the `info` an agent sees in the 402.

Load-based or time-of-day pricing would change only build_terms. The gates
never compute a price themselves: gate 1 reads a stored quote, and the unlock
amount is decided once when the human's answer is recorded (core/windows.py).

All money arithmetic uses Decimal. Nothing here touches floats.
"""
import hashlib
import json
import os
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_CEILING, Decimal, InvalidOperation

EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")
CENT = Decimal("0.01")
SPEC_VERSION = 1


class PricingConfigError(RuntimeError):
    """Invalid pricing configuration. The core API refuses to start with one."""


class PricingUnavailable(RuntimeError):
    """No accepted asset can be priced right now, for example before any rate."""


@dataclass(frozen=True)
class Asset:
    symbol: str
    address: str
    network: str
    decimals: int
    eip712_name: str
    eip712_version: str
    # ISO 4217 code of what one token is worth: USD for USDC, EUR for EURC.
    currency: str


@dataclass(frozen=True)
class PricingConfig:
    price_basis: str
    admission: Decimal
    sla_unlock: Decimal
    # None when the deployment offers no grace window (GRACE_SECONDS=0).
    grace_unlock: Decimal | None
    sla_seconds: int
    grace_seconds: int | None
    quote_valid_seconds: int
    network: str
    # In the order a 402 offers them. USDC comes first so that x402 clients
    # whose default is accepts[0] keep paying in the asset they hold.
    assets: tuple[Asset, ...]
    public_base_url: str

    @property
    def grace_enabled(self) -> bool:
        return self.grace_seconds is not None


@dataclass(frozen=True)
class FxRate:
    rate: Decimal
    as_of: date
    source: str
    base: str = "EUR"
    quote: str = "USD"


def _decimal(env, name: str, default: str) -> Decimal:
    raw = env.get(name, default)
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise PricingConfigError(f"{name} is not a decimal: {raw!r}") from exc
    if not value.is_finite() or value <= 0:
        raise PricingConfigError(f"{name} must be a positive amount, got {raw!r}")
    if value != value.quantize(CENT):
        raise PricingConfigError(f"{name} must have at most two decimals, got {raw!r}")
    return value


def _int(env, name: str, default: str) -> int:
    raw = env.get(name, default)
    try:
        return int(raw)
    except ValueError as exc:
        raise PricingConfigError(f"{name} is not an integer: {raw!r}") from exc


def _asset(env, symbol: str, network: str, *, address: str, name: str, version: str, currency: str) -> Asset:
    """Token metadata comes from config. The defaults were read on-chain from
    Base mainnet (see docs/plans/contract-v3-three-gate.md, section 1.5), and
    core re-checks them against the contract at startup (core/wallets.py)."""
    asset = Asset(
        symbol=symbol,
        address=env.get(f"{symbol}_ADDRESS", address),
        network=network,
        decimals=_int(env, f"{symbol}_DECIMALS", "6"),
        eip712_name=env.get(f"{symbol}_EIP712_NAME", name),
        eip712_version=env.get(f"{symbol}_EIP712_VERSION", version),
        currency=currency,
    )
    if not EVM_ADDRESS.match(asset.address):
        raise PricingConfigError(f"{symbol}_ADDRESS is not an EVM address")
    if not 0 <= asset.decimals <= 18:
        raise PricingConfigError(f"{symbol}_DECIMALS must be between 0 and 18")
    if not asset.eip712_name or not asset.eip712_version:
        raise PricingConfigError(f"{symbol} needs an EIP-712 name and version")
    return asset


def load_config(env=None) -> PricingConfig:
    """Read and validate the D12 settings. Raises PricingConfigError."""
    env = os.environ if env is None else env
    basis = env.get("PRICE_BASIS", "EUR")
    if basis != "EUR":
        raise PricingConfigError(f"PRICE_BASIS {basis!r} is not supported, only EUR")

    sla_seconds = _int(env, "SLA_SECONDS", "3600")
    grace_raw = _int(env, "GRACE_SECONDS", "86400")
    if sla_seconds < 1:
        raise PricingConfigError("SLA_SECONDS must be at least 1")
    if grace_raw < 0:
        raise PricingConfigError("GRACE_SECONDS must be 0 (no grace window) or positive")
    # 0 is the explicit way to run without a grace window. Any other value
    # must extend past the SLA window, so equal lengths are refused.
    grace_seconds = None if grace_raw == 0 else grace_raw
    if grace_seconds is not None and grace_seconds <= sla_seconds:
        raise PricingConfigError(
            f"GRACE_SECONDS ({grace_seconds}) must be greater than SLA_SECONDS ({sla_seconds}), "
            "or 0 for no grace window"
        )

    admission = _decimal(env, "ADMISSION_EUR", "0.10")
    sla_unlock = _decimal(env, "SLA_UNLOCK_EUR", "2.90")
    grace_unlock = None
    if grace_seconds is not None:
        grace_unlock = _decimal(env, "GRACE_UNLOCK_EUR", "1.45")
        if grace_unlock > sla_unlock:
            raise PricingConfigError(
                f"GRACE_UNLOCK_EUR ({grace_unlock}) must not exceed SLA_UNLOCK_EUR ({sla_unlock})"
            )

    quote_valid = _int(env, "QUOTE_VALID_SECONDS", "600")
    if quote_valid < 30:
        raise PricingConfigError("QUOTE_VALID_SECONDS must be at least 30")

    network = env.get("X402_NETWORK", "eip155:8453")
    known = {
        "USDC": dict(address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                     name="USD Coin", version="2", currency="USD"),
        "EURC": dict(address="0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42",
                     name="EURC", version="2", currency="EUR"),
    }
    symbols = [s.strip().upper() for s in env.get("X402_ASSETS", "USDC,EURC").split(",") if s.strip()]
    if not symbols:
        raise PricingConfigError("X402_ASSETS must name at least one asset")
    unknown = [s for s in symbols if s not in known]
    if unknown:
        raise PricingConfigError(f"X402_ASSETS names unknown assets: {', '.join(unknown)}")
    if len(set(symbols)) != len(symbols):
        raise PricingConfigError("X402_ASSETS names an asset twice")
    assets = tuple(_asset(env, s, network, **known[s]) for s in symbols)

    return PricingConfig(
        price_basis=basis,
        admission=admission,
        sla_unlock=sla_unlock,
        grace_unlock=grace_unlock,
        sla_seconds=sla_seconds,
        grace_seconds=grace_seconds,
        quote_valid_seconds=quote_valid,
        network=network,
        assets=assets,
        public_base_url=env.get("PUBLIC_BASE_URL", "https://verifi.cloud").rstrip("/"),
    )


def ceil_to_cent(amount: Decimal) -> Decimal:
    """Round up to the next cent. An exact cent stays as it is."""
    return amount.quantize(CENT, rounding=ROUND_CEILING)


def convert(amount: Decimal, rate: Decimal) -> Decimal:
    """Basis amount times rate, rounded up to the cent: a small intended markup."""
    return ceil_to_cent(amount * rate)


def to_atomic(amount: Decimal, decimals: int) -> int:
    """Exact conversion to the token's smallest unit. Refuses to round."""
    scaled = amount * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"{amount} cannot be expressed in {decimals} decimals")
    return int(scaled)


def from_atomic(amount_atomic: int, decimals: int) -> Decimal:
    return Decimal(int(amount_atomic)) / (Decimal(10) ** decimals)


def atomic_str(value) -> str | None:
    """An atomic amount as plain digits. PostgreSQL numerics can come back as
    Decimal('2.90E+6'), whose str() is not a valid x402 amount."""
    if value is None:
        return None
    if value != int(value):
        raise ValueError(f"atomic amount {value} is not an integer")
    return str(int(value))


def _money(amount: Decimal) -> str:
    return str(amount.quantize(CENT))


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _asset_amounts(cfg: PricingConfig, asset: Asset, fx: FxRate | None) -> tuple[dict, dict | None] | None:
    """Basis prices expressed in one asset, or None if it cannot be priced."""
    basis = {"admission": cfg.admission, "sla": cfg.sla_unlock}
    if cfg.grace_enabled:
        basis["grace"] = cfg.grace_unlock
    if asset.currency == cfg.price_basis:
        return {k: to_atomic(v, asset.decimals) for k, v in basis.items()}, None
    if fx is None or fx.base != cfg.price_basis or fx.quote != asset.currency:
        return None
    converted = {k: convert(v, fx.rate) for k, v in basis.items()}
    conversion = {
        "from": cfg.price_basis,
        "rate": format(fx.rate.normalize(), "f"),
        "source": fx.source,
        "as_of": fx.as_of.isoformat(),
        "rounding": "up_to_cent",
    }
    return {k: to_atomic(v, asset.decimals) for k, v in converted.items()}, conversion


def build_terms(
    cfg: PricingConfig,
    fx: FxRate | None,
    *,
    commission_eur: Decimal,
    terms_id: str,
    issued_at: datetime,
) -> tuple[dict, dict]:
    """The pricing function (D13). Returns (terms, internal).

    terms is the public service-windows `info` object. internal carries what
    the servers need and agents never see: token metadata for building x402
    requirements, and the responder commission snapshotted for earnings.
    """
    windows = {"sla": {"within_seconds": cfg.sla_seconds}}
    price_basis = {
        "currency": cfg.price_basis,
        "admission": _money(cfg.admission),
        "sla": _money(cfg.sla_unlock),
    }
    if cfg.grace_enabled:
        windows["grace"] = {"within_seconds": cfg.grace_seconds}
        price_basis["grace"] = _money(cfg.grace_unlock)

    prices, assets_meta = [], {}
    for asset in cfg.assets:
        priced = _asset_amounts(cfg, asset, fx)
        if priced is None:
            # A partial offer is refused rather than served. An EURC-only 402
            # would make every x402 client that picks the first option sign a
            # payment in an asset it may not hold. In practice this only
            # happens on a new database before its first rate: an old rate is
            # always used rather than none.
            raise PricingUnavailable(
                f"{asset.symbol} cannot be priced: no {cfg.price_basis}/{asset.currency} rate is recorded"
            )
        amounts, conversion = priced
        entry = {"network": asset.network, "asset": asset.address}
        entry.update({k: str(v) for k, v in amounts.items()})
        if conversion:
            entry["conversion"] = conversion
        prices.append(entry)
        assets_meta[asset.address.lower()] = {
            "symbol": asset.symbol,
            "address": asset.address,
            "decimals": asset.decimals,
            "name": asset.eip712_name,
            "version": asset.eip712_version,
            "currency": asset.currency,
        }
    terms = {
        "version": SPEC_VERSION,
        "terms_id": terms_id,
        "terms_valid_until": _iso(issued_at + timedelta(seconds=cfg.quote_valid_seconds)),
        "windows": windows,
        "price_basis": price_basis,
        "prices": prices,
        "asset_binding": "admission_asset",
        "expiry": {"admission_credit": "next_admission"},
        "cancellation": "none",
        "status_url_template": f"{cfg.public_base_url}/verify/{{work_id}}",
        "unlock_url_template": f"{cfg.public_base_url}/verify-unlock?id={{work_id}}",
    }
    internal = {"assets": assets_meta, "responder_commission_eur": _money(commission_eur)}
    return terms, internal


def bind_terms(terms: dict, internal: dict, asset_address: str) -> dict | None:
    """The terms a work item keeps for its whole life, for the asset it paid in.

    Per the spec's "Binding the terms": the windows, the asset and its
    admission, sla and grace amounts, and the cancellation and expiry terms,
    plus the conversion that produced them. Returns None when the asset is not
    part of the quote. The result is stored once and never recomputed.
    """
    meta = internal.get("assets", {}).get(asset_address.lower())
    price = next((p for p in terms["prices"] if p["asset"].lower() == asset_address.lower()), None)
    if meta is None or price is None:
        return None
    bound = {
        "version": terms["version"],
        "terms_id": terms["terms_id"],
        "windows": terms["windows"],
        "price_basis": terms["price_basis"],
        "network": price["network"],
        "asset": meta["address"],
        "asset_symbol": meta["symbol"],
        "asset_decimals": meta["decimals"],
        "asset_eip712": {"name": meta["name"], "version": meta["version"]},
        "admission": price["admission"],
        "sla": price["sla"],
        "asset_binding": terms["asset_binding"],
        "expiry": terms["expiry"],
        "cancellation": terms["cancellation"],
    }
    if "grace" in price:
        bound["grace"] = price["grace"]
    if "conversion" in price:
        bound["conversion"] = price["conversion"]
    return bound


def fingerprint(cfg: PricingConfig, fx: FxRate | None, commission_eur: Decimal) -> str:
    """Identity of everything a quote is built from, except its id and time.

    A new 402 reuses a stored quote only while this matches, so any change to
    config, rate or commission starts a fresh quote at once.
    """
    material = {
        "config": asdict(cfg),
        "fx": asdict(fx) if fx else None,
        "commission": str(commission_eur),
    }
    blob = json.dumps(material, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def new_terms_id() -> str:
    return "st_" + secrets.token_hex(12)


# ---------------------------------------------------------------------------
# Persistence. Quotes live in PostgreSQL so every process sees the same ones
# and a restart loses nothing.
# ---------------------------------------------------------------------------

async def current_quote(conn, cfg: PricingConfig, fx: FxRate | None, commission_eur: Decimal):
    """The quote a new 402 should advertise, reusing a fresh one when possible.

    A stored quote is reused while more than half of its validity remains, so
    crawler probes do not write a row each, and a payer always has at least
    half the validity window left to sign and retry.
    """
    fp = fingerprint(cfg, fx, commission_eur)
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(hashtext('verifi-pricing-quote'))")
        row = await conn.fetchrow(
            """
            SELECT * FROM pricing_terms
            WHERE fingerprint = $1
              AND valid_until > now() + make_interval(secs => $2)
            ORDER BY valid_until DESC
            LIMIT 1
            """,
            fp,
            cfg.quote_valid_seconds / 2,
        )
        if row is not None:
            return row, False
        issued_at = await conn.fetchval("SELECT now()")
        terms_id = new_terms_id()
        terms, internal = build_terms(
            cfg, fx, commission_eur=commission_eur, terms_id=terms_id, issued_at=issued_at
        )
        row = await conn.fetchrow(
            """
            INSERT INTO pricing_terms (terms_id, issued_at, valid_until, fingerprint, terms, internal)
            VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
            RETURNING *
            """,
            terms_id,
            issued_at,
            issued_at + timedelta(seconds=cfg.quote_valid_seconds),
            fp,
            json.dumps(terms),
            json.dumps(internal),
        )
        return row, True


def as_json(value):
    """asyncpg returns jsonb as text unless a codec is registered."""
    return json.loads(value) if isinstance(value, str) else value
