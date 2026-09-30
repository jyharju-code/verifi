"""Admin dashboard: one HTML page served by the core API.

Auth: DASHBOARD_TOKEN env var. First visit with ?token=... sets an
HttpOnly cookie, after that the bookmark works without the query string.
Exposed through Nginx at /admin. Refuses to serve when no token is set.
"""
import hmac
import json
import logging
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from core import config, pricing, wallets
from core.audit import audit
from core.db.database import get_pool
from core.payments import settlement

log = logging.getLogger("verifi.dashboard")

router = APIRouter()

COOKIE = "verifi_dash"


def _check_auth(request: Request) -> bool:
    if not config.DASHBOARD_TOKEN:
        raise HTTPException(status_code=503, detail="DASHBOARD_TOKEN is not configured")
    supplied = request.query_params.get("token") or request.cookies.get(COOKIE) or ""
    return hmac.compare_digest(supplied, config.DASHBOARD_TOKEN)


def _harden(response):
    # The dashboard exposes wallet addresses, request text, and the audit
    # trail. Keep it out of caches and referrers so the token and the data do
    # not leak downstream.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    # When the token arrives in the query string, set the cookie and redirect
    # to the bare /admin so the token leaves the address bar and browser
    # history. Subsequent visits authenticate from the HttpOnly cookie.
    response = (
        RedirectResponse("/admin", status_code=303)
        if request.query_params.get("token")
        else HTMLResponse(DASHBOARD_HTML)
    )
    response.set_cookie(
        COOKIE,
        config.DASHBOARD_TOKEN,
        httponly=True,
        samesite="strict",
        secure=secure,
        path="/admin",
        max_age=60 * 60 * 24 * 90,
    )
    return _harden(response)


@router.get("/admin/data")
async def admin_data(request: Request) -> JSONResponse:
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    db = await get_pool()

    totals = await db.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE created_at >= date_trunc('day', now())) AS today,
               count(*) FILTER (WHERE created_at >= date_trunc('week', now())) AS week,
               count(*) FILTER (WHERE status = 'pending') AS pending_now,
               count(*) FILTER (WHERE tier = 'paid') AS paid_count,
               avg(response_time_ms) FILTER (
                   WHERE response_time_ms IS NOT NULL
                     AND responded_at >= now() - interval '7 days') AS avg_ms_7d
        FROM verifies
        """
    )
    # Revenue per asset, never converted: v2 chains in their USDC columns,
    # v3 chains in atomic units of the asset they are bound to.
    revenue = await db.fetch(
        """
        SELECT asset,
               sum(amount) AS total,
               COALESCE(sum(amount) FILTER (WHERE created_at >= date_trunc('week', now())), 0) AS week
        FROM (
            SELECT CASE WHEN contract_version = 3 THEN bound_terms->>'asset_symbol' ELSE 'USDC' END AS asset,
                   CASE WHEN contract_version = 3
                        THEN (COALESCE(entry_charged_atomic, 0) + COALESCE(unlock_charged_atomic, 0))
                             / power(10::numeric, (bound_terms->>'asset_decimals')::int)
                        ELSE entry_charged_usdc + unlock_charged_usdc END AS amount,
                   created_at
            FROM verifies
        ) charged
        GROUP BY asset ORDER BY asset
        """
    )
    async with db.acquire() as conn:
        balances = await settlement.balances(conn)
    removed = {r["id"] for r in await db.fetch("SELECT id FROM associates WHERE status = 'removed'")}
    owed: dict[str, Decimal] = {}
    earned_by: dict[int, dict] = {}
    for b in balances:
        earned_by.setdefault(b.associate_id, {})[b.asset] = b
        if b.associate_id not in removed:
            owed[b.asset] = owed.get(b.asset, Decimal(0)) + max(b.pending, Decimal(0))
    terms_row = await db.fetchrow(
        "SELECT terms_id, issued_at, valid_until, terms FROM pricing_terms ORDER BY issued_at DESC LIMIT 1"
    )
    daily = await db.fetch(
        """
        SELECT d::date AS day,
               count(v.id) AS total,
               count(v.id) FILTER (WHERE v.tier = 'paid') AS paid
        FROM generate_series(date_trunc('day', now()) - interval '13 days',
                             date_trunc('day', now()), interval '1 day') d
        LEFT JOIN verifies v ON date_trunc('day', v.created_at) = d
        GROUP BY d ORDER BY d
        """
    )
    associates = await db.fetch(
        """
        SELECT a.id, a.name, a.username, a.status, a.available, a.accuracy,
               count(v.id) FILTER (WHERE v.status <> 'pending') AS answered,
               avg(v.response_time_ms) AS avg_ms
        FROM associates a
        LEFT JOIN verifies v ON v.associate_id = a.id
        WHERE a.status <> 'removed'
        GROUP BY a.id
        ORDER BY a.status = 'active' DESC, answered DESC
        """
    )
    recent = await db.fetch(
        """
        SELECT verify_no, id, instance, agent_id, intent, claim, tier, status,
               entry_source, entry_list_price_usdc, entry_charged_usdc,
               unlock_source, unlock_list_price_usdc, unlock_charged_usdc,
               free_use_number, failure_credit_granted,
               x402_payment_tx, x402_unlock_tx, response_time_ms, created_at,
               contract_version, bound_terms, entry_charged_atomic, unlock_charged_atomic,
               applied_window
        FROM verifies ORDER BY created_at DESC LIMIT 15
        """
    )
    entitlements = await db.fetch(
        """
        SELECT e.id, e.instance, e.wallet_address, e.kind, e.covers_entry,
               e.covers_unlock, e.free_use_number, e.source_verify_id,
               e.consumed_by_verify_id, e.granted_at, e.consumed_at
        FROM wallet_entitlements e
        ORDER BY e.granted_at DESC, e.id DESC LIMIT 50
        """
    )
    instances = await db.fetch(
        """
        SELECT i.id, i.name, i.price_per_verify, i.associate_commission, i.status,
               i.free_tier_count AS free_allowance,
               count(DISTINCT v.agent_id) FILTER (WHERE v.tier = 'free') AS free_agents,
               count(v.id) FILTER (WHERE v.tier = 'free') AS free_used_total
        FROM instances i
        LEFT JOIN verifies v ON v.instance = i.id
        GROUP BY i.id ORDER BY i.id
        """
    )
    audit_rows = await db.fetch(
        "SELECT at, source, event, actor, details FROM audit_log ORDER BY at DESC LIMIT 25"
    )

    return _harden(JSONResponse(
        {
            "totals": {
                "total": totals["total"],
                "today": totals["today"],
                "week": totals["week"],
                "pending_now": totals["pending_now"],
                "paid_count": totals["paid_count"],
                "avg_ms_7d": float(totals["avg_ms_7d"]) if totals["avg_ms_7d"] else None,
                "revenue": [
                    {"asset": r["asset"], "week": float(r["week"]), "total": float(r["total"])}
                    for r in revenue
                ],
                "owed": [{"asset": k, "amount": float(v)} for k, v in sorted(owed.items())],
            },
            "daily": [
                {"day": r["day"].isoformat(), "total": r["total"], "paid": r["paid"]} for r in daily
            ],
            "associates": [
                {
                    "name": r["name"],
                    "username": r["username"],
                    "status": r["status"],
                    "available": r["available"],
                    "accuracy": float(r["accuracy"]),
                    "answered": r["answered"],
                    "avg_ms": float(r["avg_ms"]) if r["avg_ms"] else None,
                    "balances": [
                        {"asset": b.asset, "earned": float(b.earned), "pending": float(b.pending)}
                        for b in earned_by.get(r["id"], {}).values()
                    ],
                }
                for r in associates
            ],
            "recent": [
                {
                    "verify_no": r["verify_no"],
                    "verify_id": str(r["id"]),
                    "instance": r["instance"],
                    "wallet_address": r["agent_id"],
                    "intent": r["intent"],
                    "claim": r["claim"],
                    "tier": r["tier"],
                    "status": r["status"],
                    "entry_source": r["entry_source"],
                    "entry_list_price_usdc": float(r["entry_list_price_usdc"]),
                    "entry_charged_usdc": float(r["entry_charged_usdc"]),
                    "unlock_source": r["unlock_source"],
                    "unlock_list_price_usdc": float(r["unlock_list_price_usdc"]),
                    "unlock_charged_usdc": float(r["unlock_charged_usdc"]),
                    "total_charged_usdc": float(r["entry_charged_usdc"] + r["unlock_charged_usdc"]),
                    "charged": _charged(r),
                    "applied_window": r["applied_window"],
                    "free_use_number": r["free_use_number"],
                    "failure_credit_granted": r["failure_credit_granted"],
                    "entry_transaction": r["x402_payment_tx"],
                    "unlock_transaction": r["x402_unlock_tx"],
                    "response_time_ms": r["response_time_ms"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in recent
            ],
            "entitlements": [
                {
                    "id": r["id"],
                    "instance": r["instance"],
                    "wallet_address": r["wallet_address"],
                    "kind": r["kind"],
                    "covers_entry": r["covers_entry"],
                    "covers_unlock": r["covers_unlock"],
                    "free_use_number": r["free_use_number"],
                    "source_verify_id": str(r["source_verify_id"]) if r["source_verify_id"] else None,
                    "consumed_by_verify_id": str(r["consumed_by_verify_id"]) if r["consumed_by_verify_id"] else None,
                    "granted_at": r["granted_at"].isoformat(),
                    "consumed_at": r["consumed_at"].isoformat() if r["consumed_at"] else None,
                }
                for r in entitlements
            ],
            "instances": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "commission": float(r["associate_commission"]),
                    "status": r["status"],
                    "free_allowance": r["free_allowance"],
                    "free_agents": r["free_agents"],
                    "free_used_total": r["free_used_total"],
                }
                for r in instances
            ],
            "terms": (
                {
                    "terms_id": terms_row["terms_id"],
                    "issued_at": terms_row["issued_at"].isoformat(),
                    "valid_until": terms_row["valid_until"].isoformat(),
                    "info": pricing.as_json(terms_row["terms"]),
                }
                if terms_row else None
            ),
            "audit": [
                {
                    "at": r["at"].isoformat(),
                    "source": r["source"],
                    "event": r["event"],
                    "actor": r["actor"],
                    "details": json.loads(r["details"]) if isinstance(r["details"], str) else r["details"],
                }
                for r in audit_rows
            ],
        }
    ))


def _charged(r) -> dict:
    """What one chain has been charged, in its own asset."""
    if r["contract_version"] == 3 and r["bound_terms"]:
        terms = pricing.as_json(r["bound_terms"])
        decimals = int(terms["asset_decimals"])
        entry = pricing.from_atomic(r["entry_charged_atomic"] or 0, decimals)
        unlock = pricing.from_atomic(r["unlock_charged_atomic"] or 0, decimals)
        asset = terms["asset_symbol"]
    else:
        entry, unlock, asset = r["entry_charged_usdc"], r["unlock_charged_usdc"], "USDC"
    return {"asset": asset, "entry": float(entry), "unlock": float(unlock), "total": float(entry + unlock)}


@router.get("/admin/wallets")
async def admin_wallets(request: Request) -> JSONResponse:
    """Live balances for the two public money addresses.

    Public addresses only. This endpoint has no access to any private key and
    returns none: the gas wallet key exists solely in the facilitator
    container, and the receiving wallet has no key in the system at all.
    """
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    return _harden(JSONResponse(await wallets.wallet_status()))


class CommissionIn(BaseModel):
    commission: float


@router.post("/admin/instances/{instance_id}/pricing")
async def set_commission(instance_id: str, body: CommissionIn, request: Request) -> JSONResponse:
    """Change the responder commission, in euros per SLA answer.

    Prices are not editable here: contract v3 takes them from the core-api
    environment (ADMISSION_EUR, SLA_UNLOCK_EUR, GRACE_UNLOCK_EUR), validated
    at startup. A new commission applies to quotes issued from now on;
    chains already admitted keep the commission they were bound with.
    """
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    from core.api.server import pricing_config

    commission = Decimal(str(round(body.commission, 2)))
    ceiling = pricing_config().sla_unlock
    if not (Decimal(0) <= commission <= ceiling):
        raise HTTPException(status_code=422, detail=f"vaatimus: 0 <= palkkio <= {ceiling} EUR (SLA-hinta)")
    db = await get_pool()
    old = await db.fetchval("SELECT associate_commission FROM instances WHERE id = $1", instance_id)
    if old is None:
        raise HTTPException(status_code=404, detail="unknown instance")
    row = await db.fetchrow(
        "UPDATE instances SET associate_commission = $2 WHERE id = $1 RETURNING id, associate_commission",
        instance_id,
        commission,
    )
    await audit(
        "dashboard",
        "commission_changed",
        {
            "instance": instance_id,
            "old_commission_eur": str(old),
            "new_commission_eur": str(row["associate_commission"]),
        },
        actor="admin",
    )
    log.info("commission updated via dashboard: %s commission=%s EUR", instance_id, row["associate_commission"])
    return JSONResponse({"id": row["id"], "commission": float(row["associate_commission"])})


DASHBOARD_HTML = """<!doctype html>
<html lang="fi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Verifi. Ohjauspaneeli</title>
<style>
  :root {
    color-scheme: light;
    --page: #f9f9f7; --surface: #fcfcfb;
    --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
    --grid: #e1e0d9; --baseline: #c3c2b7; --ring: rgba(11,11,11,0.10);
    --bar: #2a78d6; --bar-strong: #1c5cab;
    --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
    --good-text: #006300;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page: #0d0d0d; --surface: #1a1a19;
      --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
      --grid: #2c2c2a; --baseline: #383835; --ring: rgba(255,255,255,0.10);
      --bar: #3987e5; --bar-strong: #6da7ec;
      --good-text: #0ca30c;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--page); color: var(--ink);
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: 20px; max-width: 1080px; margin-inline: auto;
  }
  header { display: flex; align-items: baseline; gap: 12px; margin-bottom: 16px; }
  h1 { font-size: 20px; margin: 0; }
  #updated { color: var(--muted); font-size: 12px; }
  h2 { font-size: 14px; color: var(--ink-2); margin: 24px 0 8px; font-weight: 600; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; }
  .tile {
    background: var(--surface); border: 1px solid var(--ring); border-radius: 10px;
    padding: 12px 14px;
  }
  .tile .label { color: var(--ink-2); font-size: 12px; }
  .tile .value { font-size: 26px; font-weight: 650; margin-top: 2px; }
  .tile .sub { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .card {
    background: var(--surface); border: 1px solid var(--ring); border-radius: 10px;
    padding: 14px;
  }
  /* Bar chart */
  .chart { position: relative; height: 160px; }
  .gridline { position: absolute; left: 26px; right: 0; border-top: 1px solid var(--grid); }
  .gridline .tick {
    position: absolute; right: 100%; margin-right: 6px; transform: translateY(-50%);
    color: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums;
  }
  .plot {
    position: absolute; inset: 0 0 18px 26px; display: flex; align-items: flex-end;
    gap: 3px; border-bottom: 1px solid var(--baseline);
  }
  .barcol { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; height: 100%; position: relative; }
  .bar {
    background: var(--bar); border-radius: 4px 4px 0 0; min-height: 0;
    transition: height .2s ease;
  }
  .barcol:hover .bar { background: var(--bar-strong); }
  .bar-label {
    position: absolute; top: -16px; left: 50%; transform: translateX(-50%);
    font-size: 10px; color: var(--ink-2); font-variant-numeric: tabular-nums;
  }
  .day-label {
    position: absolute; top: 100%; left: 50%; transform: translateX(-50%);
    margin-top: 3px; font-size: 10px; color: var(--muted); white-space: nowrap;
  }
  #tooltip {
    position: fixed; pointer-events: none; z-index: 10; display: none;
    background: var(--ink); color: var(--page); padding: 6px 9px; border-radius: 6px;
    font-size: 12px; box-shadow: 0 2px 8px rgba(0,0,0,.25);
  }
  /* Tables */
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; color: var(--muted); font-weight: 500; font-size: 11px;
       border-bottom: 1px solid var(--grid); padding: 4px 8px; }
  td { padding: 6px 8px; border-bottom: 1px solid var(--grid); }
  tr:last-child td { border-bottom: 0; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .status { display: inline-flex; align-items: center; gap: 5px; }
  .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .muted { color: var(--muted); }
  details summary { cursor: pointer; color: var(--muted); font-size: 12px; margin-top: 6px; }
  #error { display: none; color: var(--critical); margin: 8px 0; }
  input.price {
    width: 74px; text-align: right; font: inherit; font-variant-numeric: tabular-nums;
    color: var(--ink); background: var(--page); border: 1px solid var(--grid);
    border-radius: 6px; padding: 3px 6px;
  }
  input.price:focus { outline: 2px solid var(--bar); border-color: transparent; }
  button.save {
    font: inherit; font-size: 12px; padding: 4px 10px; border-radius: 6px;
    border: 1px solid var(--grid); background: var(--surface); color: var(--ink);
    cursor: pointer;
  }
  button.save:hover { border-color: var(--bar); color: var(--bar-strong); }
  .saved-ok { color: var(--good-text); font-size: 12px; margin-left: 6px; }
  .saved-err { color: var(--critical); font-size: 12px; margin-left: 6px; }
  td.details-cell { font-size: 11px; color: var(--ink-2); max-width: 420px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
</head>
<body>
<header>
  <h1>Verifi. Ohjauspaneeli</h1>
  <span id="updated">Ladataan...</span>
</header>
<div id="error">Tietojen haku epäonnistui. Yritetään uudelleen...</div>

<div class="tiles" id="tiles"></div>

<h2>Lompakot ja kaasu</h2>
<div class="card" style="overflow-x:auto">
  <table id="walletTable"><thead><tr>
    <th>Lompakko</th><th>Osoite</th><th class="num">Saldo</th><th>Tila</th><th>Tehtävä</th>
  </tr></thead><tbody></tbody></table>
  <div id="walletNote" class="muted" style="font-size:11px;margin-top:6px"></div>
</div>

<h2>Verifyt päivittäin, viimeiset 14 päivää</h2>
<div class="card">
  <div class="chart" id="chart"></div>
  <details>
    <summary>Näytä taulukkona</summary>
    <table id="dailyTable"><thead>
      <tr><th>Päivä</th><th class="num">Verifyt</th><th class="num">Maksullisia</th></tr>
    </thead><tbody></tbody></table>
  </details>
</div>

<h2>Associatet</h2>
<div class="card" style="overflow-x:auto">
  <table id="assocTable"><thead><tr>
    <th>Nimi</th><th>Tila</th><th class="num">Vastattu</th><th class="num">Keskiaika</th>
    <th class="num">Tarkkuus</th><th class="num">Ansaittu</th><th class="num">Maksamatta</th>
  </tr></thead><tbody></tbody></table>
</div>

<h2>Viimeisimmät verifyt</h2>
<div class="card" style="overflow-x:auto">
  <table id="recentTable"><thead><tr>
    <th>#</th><th>Lompakko</th><th>Pyyntö</th><th>Tila</th><th>Sisäänpääsy</th>
    <th>Lunastus</th><th class="num">Veloitettu</th><th>Luotu</th>
  </tr></thead><tbody></tbody></table>
</div>

<h2>Ilmaiskäytöt ja krediitit</h2>
<div class="card" style="overflow-x:auto">
  <table id="entitlementTable"><thead><tr>
    <th>Aika</th><th>Lompakko</th><th>Tyyppi</th><th>Kattavuus</th>
    <th>Lähde</th><th>Käytetty ketjuun</th>
  </tr></thead><tbody></tbody></table>
</div>

<h2>Hinnoittelu (sopimus v3)</h2>
<div class="card" style="overflow-x:auto">
  <table id="termsTable"><thead><tr>
    <th>Maksuväline</th><th class="num">Sisäänpääsy</th><th class="num">SLA-lunastus</th>
    <th class="num">Grace-lunastus</th><th>Muunnos</th>
  </tr></thead><tbody></tbody></table>
  <div id="termsNote" class="muted" style="font-size:11px;margin-top:6px"></div>
</div>

<h2>Instanssit ja palkkio</h2>
<div class="card" style="overflow-x:auto">
  <table id="instTable"><thead><tr>
    <th>Instanssi</th><th>Tila</th><th class="num">Palkkio EUR / SLA-vastaus</th>
    <th class="num">Ilmaiskiintiö / osoite</th><th></th>
  </tr></thead><tbody></tbody></table>
  <div class="muted" style="font-size:11px;margin-top:6px">
    Hinnat tulevat core-api:n ympäristöstä (ADMISSION_EUR, SLA_UNLOCK_EUR, GRACE_UNLOCK_EUR) eikä niitä muuteta täältä.
    Palkkio tallentuu heti ja koskee uusia tarjouksia. Jo sisäänpäässeet ketjut pitävät palkkionsa.
    Vastaaja ansaitsee ketjun omassa maksuvälineessä: SLA-vastaus koko palkkion, grace-vastaus suhteessa grace-hintaan.
  </div>
</div>

<h2>Tapahtumaloki</h2>
<div class="card" style="overflow-x:auto">
  <table id="auditTable"><thead><tr>
    <th>Aika</th><th>Lähde</th><th>Tapahtuma</th><th>Tiedot</th>
  </tr></thead><tbody></tbody></table>
</div>

<div id="tooltip"></div>

<script>
const amt = (v, asset) => v.toFixed(2) + " " + asset;
const perAsset = (list, key) => list.length ? list.map(x => amt(x[key], x.asset)).join(" · ") : "0.00";
const ATOMIC = (s, decimals) => (Number(s) / 10 ** decimals).toFixed(2);
const secs = ms => ms == null ? "ei dataa" : (ms/1000).toFixed(1) + " s";
const STATUS = {
  admission_pending: { fi: "maksu vahvistuu", color: "var(--warning)", icon: "\\u23F3" },
  pending:  { fi: "jonossa",     color: "var(--warning)",  icon: "\\u23F3" },
  accepted: { fi: "hyväksytty",  color: "var(--good)",     icon: "\\u2705" },
  refined:  { fi: "tarkennettu", color: "var(--good)",     icon: "\\u{1F4DD}" },
  rejected: { fi: "hylätty",     color: "var(--critical)", icon: "\\u274C" },
  expired:  { fi: "vanhentunut", color: "var(--serious)",  icon: "\\u231B" },
  failed:   { fi: "epäonnistui", color: "var(--critical)", icon: "\\u274C" },
};
const esc = s => String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function statusCell(s) {
  const st = STATUS[s] || { fi: s, color: "var(--muted)", icon: "" };
  return `<span class="status"><span class="dot" style="background:${st.color}"></span>${st.icon} ${st.fi}</span>`;
}

function tile(label, value, sub) {
  return `<div class="tile"><div class="label">${label}</div>` +
         `<div class="value">${value}</div>` +
         (sub ? `<div class="sub">${sub}</div>` : "") + `</div>`;
}

const tooltip = document.getElementById("tooltip");
function showTip(e, html) {
  tooltip.innerHTML = html;
  tooltip.style.display = "block";
  tooltip.style.left = Math.min(e.clientX + 12, window.innerWidth - 160) + "px";
  tooltip.style.top = (e.clientY - 10) + "px";
}
function hideTip() { tooltip.style.display = "none"; }

function renderChart(daily) {
  const chart = document.getElementById("chart");
  const max = Math.max(1, ...daily.map(d => d.total));
  const fmtDay = iso => { const d = new Date(iso); return d.getDate() + "." + (d.getMonth() + 1) + "."; };
  const gridSteps = 3;
  let html = "";
  for (let i = 1; i <= gridSteps; i++) {
    const frac = i / gridSteps;
    html += `<div class="gridline" style="bottom:calc(18px + (100% - 18px) * ${frac.toFixed(3)})">` +
            `<span class="tick">${Math.round(max * frac)}</span></div>`;
  }
  html += `<div class="plot">`;
  const maxIdx = daily.reduce((m, d, i) => d.total > daily[m].total ? i : m, 0);
  daily.forEach((d, i) => {
    const h = (d.total / max * 100).toFixed(1);
    const showNum = d.total > 0 && (i === maxIdx || i === daily.length - 1);
    const showDay = i % 2 === (daily.length - 1) % 2;
    html += `<div class="barcol" data-i="${i}">` +
            (showNum ? `<span class="bar-label">${d.total}</span>` : "") +
            `<div class="bar" style="height:${h}%"></div>` +
            (showDay ? `<span class="day-label">${fmtDay(d.day)}</span>` : "") +
            `</div>`;
  });
  html += `</div>`;
  chart.innerHTML = html;
  chart.querySelectorAll(".barcol").forEach(col => {
    const d = daily[Number(col.dataset.i)];
    col.addEventListener("mousemove", e => showTip(e,
      `<b>${fmtDay(d.day)}</b><br>${d.total} verifyä<br>${d.paid} maksullista`));
    col.addEventListener("mouseleave", hideTip);
  });
  document.querySelector("#dailyTable tbody").innerHTML = daily.map(d =>
    `<tr><td>${fmtDay(d.day)}</td><td class="num">${d.total}</td><td class="num">${d.paid}</td></tr>`
  ).join("");
}

async function refresh() {
  let data;
  try {
    const resp = await fetch("/admin/data", { credentials: "same-origin" });
    if (!resp.ok) throw new Error(resp.status);
    data = await resp.json();
    document.getElementById("error").style.display = "none";
  } catch (e) {
    document.getElementById("error").style.display = "block";
    return;
  }
  const t = data.totals;
  document.getElementById("tiles").innerHTML =
    tile("Tänään", t.today, "verifyä") +
    tile("Tällä viikolla", t.week, "verifyä") +
    tile("Jonossa nyt", t.pending_now, t.pending_now > 0 ? "odottaa ihmistä" : "kaikki hoidettu") +
    tile("Keskivastausaika", secs(t.avg_ms_7d), "viimeiset 7 päivää") +
    tile("Tuotto tällä viikolla", perAsset(t.revenue, "week"), "kaikkiaan " + perAsset(t.revenue, "total")) +
    tile("Maksamatta associateille", perAsset(t.owed, "amount"), "/maksa botissa");
  renderChart(data.daily);
  document.querySelector("#assocTable tbody").innerHTML = data.associates.map(a => {
    const avail = a.status !== "active" ? `<span class="muted">${esc(a.status)}</span>`
      : a.available ? statusCell("accepted").replace("hyväksytty", "vapaa")
      : `<span class="status"><span class="dot" style="background:var(--muted)"></span>varattu</span>`;
    return `<tr><td>${esc(a.name)}${a.username ? ` <span class="muted">@${esc(a.username)}</span>` : ""}</td>` +
      `<td>${avail}</td><td class="num">${a.answered}</td><td class="num">${secs(a.avg_ms)}</td>` +
      `<td class="num">${(a.accuracy * 100).toFixed(0)} %</td>` +
      `<td class="num">${perAsset(a.balances, "earned")}</td><td class="num">${perAsset(a.balances, "pending")}</td></tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">Ei vielä associateja. Lisää botissa: /lisaa @nimi</td></tr>`;
  document.querySelector("#recentTable tbody").innerHTML = data.recent.map(v => {
    const wallet = v.wallet_address ? v.wallet_address.slice(0, 6) + "..." + v.wallet_address.slice(-4) : "puuttuu";
    const entry = v.entry_source === "initial_free" ? `ilmainen ${v.free_use_number}/5`
      : v.entry_source === "failure_credit" ? "krediitti" : "x402";
    const unlock = v.unlock_source || (v.status === "accepted" || v.status === "rejected" || v.status === "refined" ? "odottaa" : "ei vielä");
    return `<tr><td>#V-${v.verify_no}</td><td title="${esc(v.wallet_address)}">${esc(wallet)}</td>` +
      `<td class="details-cell" title="${esc(v.intent + ": " + v.claim)}">${esc(v.intent)}: ${esc(v.claim)}</td>` +
      `<td>${statusCell(v.status)}</td><td>${esc(entry)} (${amt(v.charged.entry, v.charged.asset)})</td>` +
      `<td>${esc(unlock)}${v.applied_window ? " " + esc(v.applied_window) : ""} (${amt(v.charged.unlock, v.charged.asset)})</td>` +
      `<td class="num">${amt(v.charged.total, v.charged.asset)}</td>` +
      `<td class="muted">${new Date(v.created_at).toLocaleString("fi-FI")}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">Ei vielä verifyjä.</td></tr>`;
  document.querySelector("#entitlementTable tbody").innerHTML = data.entitlements.map(e => {
    const wallet = e.wallet_address.slice(0, 6) + "..." + e.wallet_address.slice(-4);
    const kind = e.kind === "initial_free" ? `ilmainen ${e.free_use_number}/5` : "epäonnistumiskrediitti";
    const coverage = e.covers_unlock ? "sisäänpääsy + lunastus" : "seuraava sisäänpääsy";
    return `<tr><td class="muted">${new Date(e.granted_at).toLocaleString("fi-FI")}</td>` +
      `<td title="${esc(e.wallet_address)}">${esc(wallet)}</td><td>${esc(kind)}</td>` +
      `<td>${coverage}</td><td>${esc(e.source_verify_id || "alkukiintiö")}</td>` +
      `<td>${esc(e.consumed_by_verify_id || "käyttämättä")}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">Ei vielä ilmaiskäyttöjä tai krediittejä.</td></tr>`;
  renderInstances(data.instances);
  renderTerms(data.terms);
  document.querySelector("#auditTable tbody").innerHTML = data.audit.map(a =>
    `<tr><td class="muted" style="white-space:nowrap">${new Date(a.at).toLocaleString("fi-FI")}</td>` +
    `<td>${esc(a.source)}</td><td>${esc(a.event)}</td>` +
    `<td class="details-cell" title="${esc(JSON.stringify(a.details))}">${esc(JSON.stringify(a.details))}</td></tr>`
  ).join("") || `<tr><td colspan="4" class="muted">Ei vielä tapahtumia.</td></tr>`;
  document.getElementById("updated").textContent =
    "Päivitetty " + new Date().toLocaleTimeString("fi-FI");
}

function renderTerms(terms) {
  const tbody = document.querySelector("#termsTable tbody");
  const note = document.getElementById("termsNote");
  if (!terms) {
    tbody.innerHTML = `<tr><td colspan="5" class="muted">Ei vielä tarjousta. Ensimmäinen 402 luo sen.</td></tr>`;
    note.textContent = "";
    return;
  }
  const info = terms.info;
  const symbol = a => a.toLowerCase() === "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913" ? "USDC"
    : a.toLowerCase() === "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42" ? "EURC" : a.slice(0, 10) + "...";
  tbody.innerHTML = info.prices.map(p => {
    const conv = p.conversion
      ? `1 EUR = ${esc(p.conversion.rate)} (${esc(p.conversion.source)}, ${esc(p.conversion.as_of)}), pyöristys ylös sentteihin`
      : "euromääräinen, ei muunnosta";
    return `<tr><td>${esc(symbol(p.asset))}</td><td class="num">${ATOMIC(p.admission, 6)}</td>` +
      `<td class="num">${ATOMIC(p.sla, 6)}</td><td class="num">${p.grace ? ATOMIC(p.grace, 6) : "ei gracea"}</td>` +
      `<td class="muted" style="font-size:11px">${conv}</td></tr>`;
  }).join("");
  const w = info.windows;
  note.textContent =
    `Pohja EUR: ${info.price_basis.admission} / ${info.price_basis.sla}` +
    (info.price_basis.grace ? ` / ${info.price_basis.grace}` : "") +
    `. SLA-ikkuna ${w.sla.within_seconds / 60} min` +
    (w.grace ? `, grace ${w.grace.within_seconds / 3600} h` : ", ei grace-ikkunaa") +
    ` sisäänpääsystä. Tarjous ${terms.terms_id}, voimassa ${new Date(terms.valid_until).toLocaleTimeString("fi-FI")} asti.`;
}

function renderInstances(instances) {
  const tbody = document.querySelector("#instTable tbody");
  // Never wipe the row the admin is editing.
  if (tbody.contains(document.activeElement)) return;
  tbody.innerHTML = instances.map(i =>
    `<tr data-id="${esc(i.id)}"><td>${esc(i.name)} <span class="muted">${esc(i.id)}</span></td>` +
    `<td>${i.status === "active" ? statusCell("accepted").replace("hyväksytty", "aktiivinen")
        : `<span class="muted">${esc(i.status)}</span>`}</td>` +
    `<td class="num"><input class="price" data-f="commission" type="number" step="0.01" min="0" value="${i.commission.toFixed(2)}"></td>` +
    `<td class="num" title="${i.free_used_total} ilmaista verifyä yhteensä">${i.free_allowance} <span class="muted">(${i.free_agents} osoitetta)</span></td>` +
    `<td><button class="save">Tallenna</button><span class="save-msg"></span></td></tr>`
  ).join("");
  tbody.querySelectorAll("tr").forEach(tr => {
    const commInput = tr.querySelector('input[data-f="commission"]');
    const msg = tr.querySelector(".save-msg");
    tr.querySelector("button.save").addEventListener("click", async () => {
      const c = parseFloat(commInput.value);
      msg.className = "save-msg";
      if (!(c >= 0)) {
        msg.className = "saved-err"; msg.textContent = "palkkio ei voi olla negatiivinen";
        return;
      }
      try {
        const resp = await fetch(`/admin/instances/${tr.dataset.id}/pricing`, {
          method: "POST", credentials: "same-origin",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({commission: c}),
        });
        if (!resp.ok) throw new Error((await resp.json()).detail || resp.status);
        const r = await resp.json();
        commInput.value = r.commission.toFixed(2);
        msg.className = "saved-ok"; msg.textContent = "Tallennettu ✓";
        setTimeout(() => { msg.textContent = ""; }, 4000);
      } catch (e) {
        msg.className = "saved-err"; msg.textContent = "Virhe: " + e.message;
      }
    });
  });
}

const WSTATE = {
  ok:      { fi: "kunnossa",  color: "var(--good)",     icon: "\\u2705" },
  warning: { fi: "vähenee",   color: "var(--warning)",  icon: "\\u26A0" },
  low:     { fi: "vähissä",   color: "var(--critical)", icon: "\\u26A0" },
  unknown: { fi: "ei tietoa", color: "var(--muted)",    icon: "" },
};

async function refreshWallets() {
  let w;
  try {
    const resp = await fetch("/admin/wallets", { credentials: "same-origin" });
    if (!resp.ok) throw new Error(resp.status);
    w = await resp.json();
  } catch (e) {
    return;
  }
  const fmt = (v, unit) => v == null ? "ei tietoa" : v.toFixed(unit === "ETH" ? 5 : 2) + " " + unit;
  const rows = [["gas_wallet", "ETH"], ["receiving_wallet", "USDC"]].map(([key, unit]) => {
    const x = w[key];
    const st = WSTATE[x.state] || WSTATE.unknown;
    const bal = unit === "ETH" ? x.eth : x.usdc;
    const addr = x.address
      ? `<a href="${esc(x.explorer)}" target="_blank" rel="noopener noreferrer" title="${esc(x.address)}">` +
        `${esc(x.address.slice(0, 10))}...${esc(x.address.slice(-6))}</a>`
      : `<span class="muted">ei asetettu</span>`;
    return `<tr><td>${esc(x.label)}</td><td>${addr}</td>` +
      `<td class="num">${fmt(bal, unit)}</td>` +
      `<td><span class="status"><span class="dot" style="background:${st.color}"></span>` +
      `${st.icon} ${st.fi}</span></td>` +
      `<td class="muted" style="font-size:11px">${esc(x.purpose)}</td></tr>`;
  });
  document.querySelector("#walletTable tbody").innerHTML = rows.join("");
  const note = document.getElementById("walletNote");
  if (w.problems && w.problems.length) {
    note.textContent = "Huomiot: " + w.problems.join(". ");
    note.style.color = "var(--critical)";
  } else {
    note.textContent = w.network +
      ". Kaasu maksaa settlementit, tulot laskeutuvat vastaanottavaan osoitteeseen. " +
      "Yksityisiä avaimia ei ole tässä palvelussa eikä tässä näkymässä.";
    note.style.color = "";
  }
}

refresh();
refreshWallets();
setInterval(refresh, 10000);
setInterval(refreshWallets, 60000);
</script>
</body>
</html>
"""
