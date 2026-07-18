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

from core import config
from core.audit import audit
from core.db.database import get_pool

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
    money = await db.fetchrow(
        """
        SELECT COALESCE((SELECT sum(entry_charged_usdc + unlock_charged_usdc)
                         FROM verifies), 0) AS revenue_total,
               COALESCE((SELECT sum(entry_charged_usdc + unlock_charged_usdc)
                         FROM verifies
                         WHERE created_at >= date_trunc('week', now())), 0) AS revenue_week,
               COALESCE((SELECT sum(earnings - paid_total) FROM associates
                         WHERE status <> 'removed'), 0) AS owed
        """
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
        SELECT a.name, a.username, a.status, a.available, a.accuracy,
               a.earnings, a.earnings - a.paid_total AS pending_balance,
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
               x402_payment_tx, x402_unlock_tx, response_time_ms, created_at
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
                "revenue_week": float(money["revenue_week"]),
                "revenue_total": float(money["revenue_total"]),
                "owed": float(money["owed"]),
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
                    "earnings": float(r["earnings"]),
                    "pending_balance": float(r["pending_balance"]),
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
                    "price": float(r["price_per_verify"]),
                    "commission": float(r["associate_commission"]),
                    "status": r["status"],
                    "free_allowance": r["free_allowance"],
                    "free_agents": r["free_agents"],
                    "free_used_total": r["free_used_total"],
                }
                for r in instances
            ],
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


class PricingIn(BaseModel):
    price: float
    commission: float


@router.post("/admin/instances/{instance_id}/pricing")
async def set_pricing(instance_id: str, body: PricingIn, request: Request) -> JSONResponse:
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    price = round(body.price, 2)
    commission = round(body.commission, 2)
    if not (0 <= commission <= price <= 1000):
        raise HTTPException(
            status_code=422,
            detail="vaatimus: 0 <= palkkio <= hinta <= 1000",
        )
    db = await get_pool()
    old = await db.fetchrow(
        "SELECT price_per_verify, associate_commission FROM instances WHERE id = $1", instance_id
    )
    if old is None:
        raise HTTPException(status_code=404, detail="unknown instance")
    row = await db.fetchrow(
        """
        UPDATE instances SET price_per_verify = $2, associate_commission = $3
        WHERE id = $1
        RETURNING id, price_per_verify, associate_commission
        """,
        instance_id,
        Decimal(str(price)),
        Decimal(str(commission)),
    )
    await audit(
        "dashboard",
        "price_changed",
        {
            "instance": instance_id,
            "old_price": str(old["price_per_verify"]),
            "new_price": str(row["price_per_verify"]),
            "old_commission": str(old["associate_commission"]),
            "new_commission": str(row["associate_commission"]),
        },
        actor="admin",
    )
    log.info(
        "pricing updated via dashboard: %s price=%s commission=%s",
        instance_id, row["price_per_verify"], row["associate_commission"],
    )
    return JSONResponse(
        {
            "id": row["id"],
            "price": float(row["price_per_verify"]),
            "commission": float(row["associate_commission"]),
            "platform_share": round(float(row["price_per_verify"]) - float(row["associate_commission"]), 2),
        }
    )


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

<h2>Instanssit ja hinnoittelu</h2>
<div class="card" style="overflow-x:auto">
  <table id="instTable"><thead><tr>
    <th>Instanssi</th><th>Tila</th><th class="num">Hinta $</th><th class="num">Palkkio $</th>
    <th class="num">Alustalle $</th><th class="num">Ilmaiskiintiö / osoite</th><th></th>
  </tr></thead><tbody></tbody></table>
  <div class="muted" style="font-size:11px;margin-top:6px">
    Hinta ja palkkio tallentuvat kantaan heti. Alustalle = hinta miinus palkkio.
    Sopimushinnan muutos vaatii lisäksi X402_ENTRY_PRICE ja X402_UNLOCK_PRICE päivitykset sekä uuden API-sopimusversion.
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
const money = v => "$" + v.toFixed(2);
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
    tile("Tuotto tällä viikolla", money(t.revenue_week), "kaikkiaan " + money(t.revenue_total)) +
    tile("Maksamatta associateille", money(t.owed), "/maksa botissa");
  renderChart(data.daily);
  document.querySelector("#assocTable tbody").innerHTML = data.associates.map(a => {
    const avail = a.status !== "active" ? `<span class="muted">${esc(a.status)}</span>`
      : a.available ? statusCell("accepted").replace("hyväksytty", "vapaa")
      : `<span class="status"><span class="dot" style="background:var(--muted)"></span>varattu</span>`;
    return `<tr><td>${esc(a.name)}${a.username ? ` <span class="muted">@${esc(a.username)}</span>` : ""}</td>` +
      `<td>${avail}</td><td class="num">${a.answered}</td><td class="num">${secs(a.avg_ms)}</td>` +
      `<td class="num">${(a.accuracy * 100).toFixed(0)} %</td>` +
      `<td class="num">${money(a.earnings)}</td><td class="num">${money(a.pending_balance)}</td></tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">Ei vielä associateja. Lisää botissa: /lisaa @nimi</td></tr>`;
  document.querySelector("#recentTable tbody").innerHTML = data.recent.map(v => {
    const wallet = v.wallet_address ? v.wallet_address.slice(0, 6) + "..." + v.wallet_address.slice(-4) : "puuttuu";
    const entry = v.entry_source === "initial_free" ? `ilmainen ${v.free_use_number}/5`
      : v.entry_source === "failure_credit" ? "krediitti" : "x402";
    const unlock = v.unlock_source || (v.status === "accepted" || v.status === "rejected" || v.status === "refined" ? "odottaa" : "ei vielä");
    return `<tr><td>#V-${v.verify_no}</td><td title="${esc(v.wallet_address)}">${esc(wallet)}</td>` +
      `<td class="details-cell" title="${esc(v.intent + ": " + v.claim)}">${esc(v.intent)}: ${esc(v.claim)}</td>` +
      `<td>${statusCell(v.status)}</td><td>${esc(entry)} (${money(v.entry_charged_usdc)})</td>` +
      `<td>${esc(unlock)} (${money(v.unlock_charged_usdc)})</td>` +
      `<td class="num">${money(v.total_charged_usdc)}</td>` +
      `<td class="muted">${new Date(v.created_at).toLocaleString("fi-FI")}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">Ei vielä verifyjä.</td></tr>`;
  document.querySelector("#entitlementTable tbody").innerHTML = data.entitlements.map(e => {
    const wallet = e.wallet_address.slice(0, 6) + "..." + e.wallet_address.slice(-4);
    const kind = e.kind === "initial_free" ? `ilmainen ${e.free_use_number}/5` : "epäonnistumiskrediitti";
    const coverage = e.covers_unlock ? "0,10 + 2,90 USDC" : "0,10 USDC";
    return `<tr><td class="muted">${new Date(e.granted_at).toLocaleString("fi-FI")}</td>` +
      `<td title="${esc(e.wallet_address)}">${esc(wallet)}</td><td>${esc(kind)}</td>` +
      `<td>${coverage}</td><td>${esc(e.source_verify_id || "alkukiintiö")}</td>` +
      `<td>${esc(e.consumed_by_verify_id || "käyttämättä")}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="muted">Ei vielä ilmaiskäyttöjä tai krediittejä.</td></tr>`;
  renderInstances(data.instances);
  document.querySelector("#auditTable tbody").innerHTML = data.audit.map(a =>
    `<tr><td class="muted" style="white-space:nowrap">${new Date(a.at).toLocaleString("fi-FI")}</td>` +
    `<td>${esc(a.source)}</td><td>${esc(a.event)}</td>` +
    `<td class="details-cell" title="${esc(JSON.stringify(a.details))}">${esc(JSON.stringify(a.details))}</td></tr>`
  ).join("") || `<tr><td colspan="4" class="muted">Ei vielä tapahtumia.</td></tr>`;
  document.getElementById("updated").textContent =
    "Päivitetty " + new Date().toLocaleTimeString("fi-FI");
}

function renderInstances(instances) {
  const tbody = document.querySelector("#instTable tbody");
  // Never wipe the row the admin is editing.
  if (tbody.contains(document.activeElement)) return;
  tbody.innerHTML = instances.map(i =>
    `<tr data-id="${esc(i.id)}"><td>${esc(i.name)} <span class="muted">${esc(i.id)}</span></td>` +
    `<td>${i.status === "active" ? statusCell("accepted").replace("hyväksytty", "aktiivinen")
        : `<span class="muted">${esc(i.status)}</span>`}</td>` +
    `<td class="num"><input class="price" data-f="price" type="number" step="0.01" min="0" value="${i.price.toFixed(2)}"></td>` +
    `<td class="num"><input class="price" data-f="commission" type="number" step="0.01" min="0" value="${i.commission.toFixed(2)}"></td>` +
    `<td class="num platform-share">${money(i.price - i.commission)}</td>` +
    `<td class="num" title="${i.free_used_total} ilmaista verifyä yhteensä">${i.free_allowance} <span class="muted">(${i.free_agents} osoitetta)</span></td>` +
    `<td><button class="save">Tallenna</button><span class="save-msg"></span></td></tr>`
  ).join("");
  tbody.querySelectorAll("tr").forEach(tr => {
    const priceInput = tr.querySelector('input[data-f="price"]');
    const commInput = tr.querySelector('input[data-f="commission"]');
    const share = tr.querySelector(".platform-share");
    const msg = tr.querySelector(".save-msg");
    const recompute = () => {
      const p = parseFloat(priceInput.value) || 0;
      const c = parseFloat(commInput.value) || 0;
      share.textContent = money(p - c);
      share.style.color = c > p ? "var(--critical)" : "";
    };
    priceInput.addEventListener("input", recompute);
    commInput.addEventListener("input", recompute);
    tr.querySelector("button.save").addEventListener("click", async () => {
      const p = parseFloat(priceInput.value);
      const c = parseFloat(commInput.value);
      msg.className = "save-msg";
      if (!(c >= 0 && p >= c)) {
        msg.className = "saved-err"; msg.textContent = "palkkio ei voi ylittää hintaa";
        return;
      }
      try {
        const resp = await fetch(`/admin/instances/${tr.dataset.id}/pricing`, {
          method: "POST", credentials: "same-origin",
          headers: {"content-type": "application/json"},
          body: JSON.stringify({price: p, commission: c}),
        });
        if (!resp.ok) throw new Error((await resp.json()).detail || resp.status);
        const r = await resp.json();
        priceInput.value = r.price.toFixed(2);
        commInput.value = r.commission.toFixed(2);
        share.textContent = money(r.platform_share);
        msg.className = "saved-ok"; msg.textContent = "Tallennettu ✓";
        setTimeout(() => { msg.textContent = ""; }, 4000);
      } catch (e) {
        msg.className = "saved-err"; msg.textContent = "Virhe: " + e.message;
      }
    });
  });
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
