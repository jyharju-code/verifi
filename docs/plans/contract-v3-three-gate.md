# Contract v3: three gates, euro pricing, EURC. Implementation plan

Status: **approved 2026-09-30.** Juhana accepted every recommendation in
section 9. The decisions are recorded in section 12, the reconciliation with the
spec in section 13, and three new conflicts found in the spec and the discovery
listings plan in section 14.
Branch: `feat/contract-v3-three-gate`, in a separate worktree
(`../verifi-v3`), created from `origin/main` at `b76ec60`. Upstream tracking is
removed so a bare `git push` cannot reach `main`.

Prepared 2026-09-30 against the DECISIONS block in the task (D1 to D15).

---

## 0. Blockers and housekeeping

**B1. Resolved.** The spec is now at `docs/specs/extension-service-windows.md`.
Four em and en dashes were replaced with plain punctuation to satisfy the
repository rule. No wording changed. Section 5 now follows the spec's shapes.

**B2. Local `main` is 10 commits behind `origin/main`**, and it carries your
uncommitted Potamoi Group Oy footer edits in `deploy/nginx/html/index.html`,
`docs/index.html`, and `docs/get-started/index.html`. I did not touch them.
v3 edits the pricing sections of the same three files, so I suggest committing
the footer separately first. Otherwise the two will conflict at merge time.

---

## 1. Verified findings that shape the design

### 1.1 The x402 library can do all of this (installed `@x402/*` 2.18.0)

Read from the installed type definitions and source, not from memory:

| Need | Library support |
|---|---|
| Two assets in one 402 | `RouteConfig.accepts: PaymentOption \| PaymentOption[]` |
| Per-request price | `price: Price \| DynamicPrice`, where `DynamicPrice = (ctx: HTTPRequestContext) => Price`. The context carries `paymentHeader` and `adapter.getQueryParam()` |
| EURC with its own EIP-712 domain | `Price = Money \| AssetAmount`, `AssetAmount = { asset, amount, extra }`. Tested: `ExactEvmScheme.parsePrice` and `enhancePaymentRequirements` pass an EURC `AssetAmount` through unchanged, custom `extra` keys included |
| Dynamic `extensions["service-windows"]` | `resourceServer.registerExtension({ key, enrichPaymentRequiredResponse })`. Static `RouteConfig.extensions` only declares it |
| Bazaar discovery | `declareDiscoveryExtension({ bodyType: "json", inputSchema, ... })` plus `bazaarResourceServerExtension` from `@x402/extensions/bazaar` |

### 1.2 How a payment is matched to a requirement (this decides the design)

`paymentRequirementsMatchAccepted` in `@x402/core/server`:

- `scheme, network, asset, amount, payTo, maxTimeoutSeconds` must be **deep equal**.
- The server's `extra` must be a **subset** of the payer's `extra`.

When nothing matches, the middleware answers a fresh 402 and never settles.
Two requirements of the task fall out of this for free:

- **Asset binding at unlock.** The unlock 402 offers one entry. A payment in the
  other asset cannot match, so it is refused before settlement, with no charge.
- **Expired quote.** `terms_id` travels in each requirement's `extra`. The price
  function reads `ctx.paymentHeader`: if the paid `terms_id` is still valid it
  rebuilds exactly that quote, so the payment matches. If it has expired it
  returns the current quote, nothing matches, and the payer gets a fresh 402.

### 1.3 v2 clients: two traps, both avoidable

- **Extension echo.** `validateExtensions`: *"When the client omits extensions
  entirely, validation passes."* It checks only extensions a client echoes. A v2
  client that ignores `service-windows` is unaffected.
- **Which asset a client picks.** The default selector in `@x402/core/client` is
  `(version, accepts) => accepts[0]`, after filtering to schemes it supports. A
  v2 agent registers `ExactEvmScheme` for `eip155:*`, which supports both assets.
  **If EURC were listed first, every existing USDC agent would sign an EURC
  payment it cannot fund.** So `accepts` is ordered **USDC first, EURC second**.
  The terms extension still states the euro basis. See question Q11.

### 1.4 Runtime 402 versus docs: the docs were wrong

A real 402 from a local verify-api against the public testnet facilitator:
`x402Version: 2`, `accepts[0]` keys `amount, asset, extra, maxTimeoutSeconds,
network, payTo, scheme`, `amount: "100000"`, no `maxAmountRequired`.

**The runtime is correct. The docs are wrong:** `maxAmountRequired` appears 3
times in `deploy/nginx/html/openapi.json` and once in `llms-full.txt`. I wrote
those in PR #10. They are fixed in this work.

### 1.5 Token metadata, read on-chain (Base mainnet, block ~51,994,568)

Read-only `eth_call`s. The EIP-712 domain separator was recomputed from
`name`, `version`, chain id 8453 and the address, and compared with the
contract's own `DOMAIN_SEPARATOR()`. They match, which is the only reliable
proof that the name and version are right.

| | EURC | USDC |
|---|---|---|
| Address | `0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| `name()` | `EURC` | `USD Coin` |
| `version()` | `2` | `2` |
| `decimals()` | `6` | `6` |
| `DOMAIN_SEPARATOR()` matches recomputed | yes | yes |
| EIP-3009 `TRANSFER_WITH_AUTHORIZATION_TYPEHASH` | `0x7c7c6cdb...2267`, equal to the standard hash | same |
| `authorizationState()` callable | yes | yes |
| Implementation (Blockscout) | `FiatTokenV2_2` | `FiatTokenV2_2` |

EURC has 6 decimals, so the D8 amounts **100000 / 2900000 / 1450000** are right.
`@x402/evm` knows only USDC as the Base default, so EURC is always passed as an
explicit `AssetAmount` with `extra: { name: "EURC", version: "2" }`.

### 1.6 Facilitator (x402-rs, read at tag v2.0.2, which `:latest` builds from)

- **No token allowlist.** The v2 exact EIP-3009 path builds the contract straight
  from `accepted.asset`. **EURC needs no configuration change.**
- The EIP-712 domain comes from `extra.name` and `extra.version`, which are
  required on the v2 path.
- Unknown `extra` keys such as `terms_id` are silently ignored, so the
  facilitator does not enforce them. The resource server does (section 1.2).
- **`/supported` lists scheme and network only, never assets.** The task's
  "`/supported` must list both assets" cannot be met by configuration. See Q7.

### 1.7 ECB reference rate

- Daily XML `eurofxref-daily.xml` carries the reference date
  (`<Cube time='2026-09-30'> ... rate='1.1355'`). Published around 16:00 CET on
  TARGET working days only, so it is stale over weekends and holidays (up to
  about 4 days at Easter).
- Reuse is free with attribution ("Source: ECB") and without modification.
- **The ECB states: "The reference rates are published for information purposes
  only. Using the rates for transaction purposes is strongly discouraged."**
  No SLA. This is the "unsuitable source" case D9 asks me to raise. See Q4.

### 1.8 x402scan discovery

- `x-payment-info.price = { mode: "fixed" | "dynamic", currency, amount }` or
  `{ mode: "dynamic", currency, min, max }`, plus `protocols: [{ "x402": {} }]`.
- The validator x402scan runs (`@agentcash/discovery` 1.7.5) accepts any ISO 4217
  code, so **`EUR` validates**. But x402scan's own integration spec says the
  amount "is decimal USD", and every example is USD. See Q5.
- `/.well-known/x402` is now **legacy**: the library only notes that it exists
  (`LEGACY_WELL_KNOWN_FOUND`). `openapi.json` is what gets read.
- A 402 without a bazaar input schema is marked non-invocable.

---

## 2. Design

### 2.1 One pricing function, in core

New `core/pricing.py`:

- `load_config()` reads D12 settings and **validates at startup**:
  `GRACE_SECONDS > SLA_SECONDS` when grace is enabled, and
  `GRACE_UNLOCK_EUR <= SLA_UNLOCK_EUR`. Refuses to start otherwise.
- `quote_terms(now) -> Terms` is **the** pricing function (D13). It returns
  windows, basis prices, per-asset atomic amounts, and the conversion object.
  No load or time-of-day logic now, but it is the only place that would change.
- All money math uses `Decimal`, never float. USDC amount =
  `ceil_to_cent(eur_price * rate)`. With rate 1.17 this gives 0.12 / 3.40 / 1.70,
  that is 120000 / 3400000 / 1700000, as the task expects.

Quotes live in PostgreSQL (no in-memory state):

- `pricing_terms` holds one row per quote: `terms_id`, `issued_at`,
  `valid_until`, and the full terms as JSON.
- A quote is reused while it has more than half its validity left, so crawler
  probes do not write a row each. Expired quotes that no chain references are
  pruned.
- `fx_rates` caches ECB rates by `(source, rate_date)`. A core loop fetches
  hourly and inserts only when the reference date changes: "once per
  publication". If the latest rate is older than 7 days the operator is alerted,
  but **USDC is still quoted from the latest rate**, because dropping USDC would
  break v2 agents (D15).

New internal endpoints, guarded by `CORE_INTERNAL_SECRET` like the rest:
`GET /internal/terms/current` and `GET /internal/terms/{terms_id}`.

**Token metadata is configuration, checked against the chain.** Addresses,
names, versions and decimals are config values set from section 1.5. At startup
core recomputes each domain separator and compares it with the contract. A
mismatch is logged loudly and audited, like the gas watch. It does not block
boot, so a public RPC hiccup cannot take the API down.

### 2.2 Gate 1: `POST /verify`

- The paid route's `accepts` becomes a two-entry list whose prices are
  `DynamicPrice` functions. With no payment header they return the current
  quote. With a payment header they decode `accepted.extra.terms_id` and rebuild
  that quote if it is still valid (section 1.2).
- Each requirement's `extra` = `{ name, version, terms_id }`.
- **Order: USDC first, EURC second** (section 1.3).
- `extensions["service-windows"]` is declared on the route and filled per
  response by a registered extension whose `enrichPaymentRequiredResponse` reads
  `terms_id` from the requirements it is given. The extension and the
  requirements therefore always describe the same quote.
- `extensions.bazaar` via `declareDiscoveryExtension`, with a JSON Schema for
  `intent`, `claim`, `agent_id`, `callback_url`.
- **Binding.** After the middleware has verified the payment, verify-api reads
  `accepted` from the `PAYMENT-SIGNATURE` header. It is trustworthy at that point
  because the middleware has just matched it against a server requirement.
  It passes `terms_id` and the paid asset to `create_verify`, which copies the
  quote into the row: the bound asset and network, and all three bound atomic
  amounts for that asset.
- **Admission follows settlement, unchanged.** In `_apply_settlement(entry)` core
  sets `admitted_at`, `sla_deadline = admitted_at + SLA`,
  `grace_deadline = admitted_at + GRACE`, and `expires_at = grace_deadline`.
  A slow settlement therefore never eats into the SLA window (D5).
- **Credit-funded admissions** do not pass through x402. They bind the current
  quote at admission. Which asset binds gates 2 and 3 is open: see Q3.
- The 202 body adds both deadlines, the bound terms, and `contract_version: 3`.

### 2.3 When the human answers (the price is decided here)

In the bot's `_resolve`, one `UPDATE` sets `ready_at = now()` and computes, in
SQL, with the same transaction timestamp:

```
applied_window = CASE WHEN now() <= sla_deadline   THEN 'sla'
                      WHEN now() <= grace_deadline THEN 'grace' END
unlock_amount_atomic = the bound amount for that window
```

- `<=` makes a boundary instant belong to the earlier, dearer window (D6).
- The same `UPDATE` refuses when `now() > grace_deadline`, so an answer that
  races the 60 second expiry loop cannot create a result that should have
  expired. v2 rows get the equivalent guard on `expires_at`.
- **`responded_at` is not reused as `ready_at`.** It is also written on expiry
  and on abandoned admissions, so it does not mean "the answer was recorded".
  A dedicated `ready_at` column keeps D6 unambiguous. `responded_at` stays as is.
- After this, the price is only ever read. The exchange rate is never read again.

### 2.4 Status: `GET /verify/{id}`

- `processing`: both deadlines, so agents can plan polling.
- `ready`: the `service_window` object (`applied`, deadlines, `network`,
  `asset`, `unlock_amount`), and the unlock price fields set to the stored
  amount.
- `expires_at` equals `grace_deadline` for v3 chains.

### 2.5 Gates 2 and 3: `POST /verify-unlock`

- The existing pre-check handler is kept: no payment before `ready`, and a
  completed chain returns its answer with no new charge.
- The paid route's `DynamicPrice` reads the verify by `?id=` and returns **one**
  requirement: the bound asset at the stored `unlock_amount_atomic`, with
  `extra.terms_id`. A payment in the other asset cannot match (section 1.2).
- `extensions["service-windows"].info` repeats `applied`, `admitted_at`,
  `ready_at`, `terms_id`.
- `_apply_settlement(unlock)` records the stored amount, not a constant.

### 2.6 Expiry job

- v3 rows: `status = 'pending' AND grace_deadline < now()`. Strict `<`, so the
  boundary instant is still grace (D6). v2 rows keep `expires_at` (60 minutes).
  Because v3 sets `expires_at = grace_deadline`, one predicate on `expires_at`
  covers both.
- **Status: reuse `expired` with reason `human_timeout`,** exactly as v2. Agents
  already parse it, and the task allows it.
- The failure-credit logic and its ledger links are unchanged, including the
  case where a credit-funded entry returns its credit.

### 2.7 Human side

- **Card:** shows the SLA deadline and the grace deadline in UTC, once, when the
  card is sent. No reminders, nudges, rankings or penalties.
- **Confirmation** after answering names the applied window: "answered in the
  grace window".
- A silent card edit when the SLA passes is possible but not planned. See Q10.
- **Earnings (D11):** a new append-only ledger `associate_earnings`, one row per
  answered verify, in the chain's own asset, never converted. Rule and timing:
  see Q2.
- **Payouts** gain an `asset` column (existing rows are USDC). `/payouts` and
  `/paid` work per asset. The awal command stays USDC-only. EURC payouts print a
  manual instruction. `PAYOUTS_AUTO` stays off.
- Historical `associates.earnings` and `paid_total` become a legacy USDC opening
  balance. Nothing is rewritten.

### 2.8 MCP

- `verify_claim`, `get_verify` and `unlock_verify` already pass verify-api
  responses and the decoded `PAYMENT-REQUIRED` straight through, so they gain the
  new fields and the extension object without logic changes.
- `verifi_info` today is a static local function. It will fetch current terms
  from a new verify-api `GET /terms`, which is reachable on the docker network
  but **not** exposed through nginx.
- `MCP_CONTRACT_VERSION` becomes `3.0.0`, together with `server.json`, because a
  test requires them to match.

### 2.9 Other places with hard-coded prices

- `core/webhooks.py`: the ready payload has `price_usdc: "2.90"`. It gets the
  stored price and `service_window`.
- `core/api/dashboard.py` (Finnish operator UI): revenue and owed per asset.
  The editable `price_per_verify` field would contradict D12, which makes env
  the price source. It becomes a read-only view of the current terms. See Q12.

### 2.10 Docs and discovery, one story

`docs/API.md`, `docs/api-contract.json`, `deploy/nginx/html/docs/index.html`
(API.md's own rule requires it in the same commit), `llms.txt`,
`llms-full.txt`, `openapi.json`, the landing page and get-started pricing,
`README.md`, `docs/WALLETS.md`, `docs/DECISIONS.md` (new entry "Three gates,
euro pricing, EURC"), and the pricing sentence in `CLAUDE.md`.

- "usually within seconds" is removed wherever it appears.
- `openapi.json`: `x-payment-info` on both paid operations; `maxAmountRequired`
  becomes `amount`; examples for both assets; new `human_timeout` and
  `expires_at` descriptions.
- `/.well-known/x402`: a static file with `{ "version": 1, "resources":
  ["https://verifi.cloud/verify"] }`, plus an nginx location that repeats the
  security headers, as the other `.well-known` location does.
- `api-contract.json` still says `"fullFreeChainsPerWallet": 5`, although free
  chains were removed in #16. It is corrected here.

---

## 3. Database migration

One idempotent file, `core/db/migrations/2026-09-30-contract-v3-three-gate.sql`,
mirrored in the base schema files and in `deploy/initdb/00-schema.sh`.

New tables:

| Table | Purpose |
|---|---|
| `pricing_terms` | One row per quote: `terms_id` (PK), `issued_at`, `valid_until`, `terms` (jsonb) |
| `fx_rates` | `source`, `base`, `quote`, `rate` (numeric), `rate_date`, `fetched_at`; unique on `(source, base, quote, rate_date)` |
| `associate_earnings` | Append-only: `associate_id`, `verify_id` (unique), `asset`, `amount_atomic`, `applied_window`, `rule`, `created_at` |

New `verifies` columns, all nullable, so existing rows are untouched:

| Column | Meaning |
|---|---|
| `contract_version` | `2` for existing rows via the column default, `3` for new |
| `terms_id` | FK to `pricing_terms` |
| `bound_terms` | jsonb snapshot: windows, basis prices, bound asset and network, the three bound atomic amounts, and the conversion rate, source and date when USDC |
| `bound_asset`, `bound_network` | For cheap filtering and reporting |
| `entry_amount_atomic` | The admission amount in the bound asset |
| `sla_deadline`, `grace_deadline` | Set at admission from `admitted_at` |
| `ready_at` | Set only when the human's answer is recorded |
| `applied_window` | `sla`, `grace`, or null |
| `unlock_amount_atomic` | Decided at `ready_at`, then only read |

Also: `payouts.asset` (default `USDC`), and a `CHECK` that `applied_window` is
`sla` or `grace`.

**Existing rows keep v2 semantics.** Nothing is re-priced and no historical row
is rewritten. `audit_log` stays append-only, with new events `terms_quoted`,
`terms_bound`, `window_applied`, and `earnings_recorded`.

---

## 4. Facilitator change

**None to `facilitator-config.json`.** x402-rs has no asset allowlist
(section 1.6). What changes:

- A documented local check that an EURC `transferWithAuthorization` verifies
  against an anvil fork of Base, with a throwaway key whose EURC balance is set
  on the fork. No mainnet transaction. The public Base RPC rate-limits heavily
  (I hit it during the metadata reads), so the fork may need a keyed RPC. If it
  cannot be made to work locally I will say so and describe the manual check.
- Suggested, not required: pin the image to `ghcr.io/x402-rs/x402-facilitator:v2.0.2`
  instead of `:latest`, so a new release cannot change settlement unannounced.

---

## 5. API fields (shapes follow the spec, see section 13)

| Where | Field | Change |
|---|---|---|
| All responses | `contract_version` | New, `3` |
| Gate 1 402 | `accepts` | Two entries, USDC then EURC. `extra` adds `terms_id` |
| Gate 1 402 | `extensions["service-windows"]` | New: `terms_id`, `terms_valid_until`, `windows`, `price_basis`, `prices` (with `conversion` for USDC), `asset_binding: "admission_asset"`, `expiry`, `cancellation: "none"` |
| Gate 1 402 | `extensions.bazaar` | New: input JSON Schema |
| 202 and status | `sla_deadline`, `grace_deadline` | New |
| 202 and status | `terms` (bound) | New |
| Status, ready | `service_window` | New: `applied`, deadlines, `network`, `asset`, `unlock_amount` |
| Status, ready | `unlock.price_usdc` | Kept, correct for USDC chains. For an EURC chain it is `null` |
| Status, ready | `unlock.asset`, `unlock.amount_atomic`, `unlock.amount_decimal` | New, asset-neutral |
| `funding.*_usdc` | | Kept and correct for USDC chains. `null` on EURC chains rather than a misleading number |
| `funding` | `asset`, `*_amount_atomic`, `*_amount_decimal` | New |
| `expires_at` | | Equals `grace_deadline` for v3 chains |
| Unlock 402 | `accepts` | One entry: bound asset, applied amount |
| Unlock 402 | `extensions["service-windows"].info` | `applied`, `admitted_at`, `ready_at`, `terms_id` |

---

## 6. What v2 clients see

- Same endpoints, same statuses, same `verify_id` flow.
- The gate 1 402 lists USDC first, so a v2 `@x402/fetch` agent picks USDC, signs,
  and is admitted with no code change. It ignores the extension, and echo
  validation passes when a client echoes nothing.
- On a USDC chain every `_usdc` field keeps its meaning. Only the value changes:
  it is the converted amount (for example 3.40 instead of 2.90).
- **Behaviour change a v2 agent will notice:** a chain can stay `processing` for
  up to 24 hours instead of 60 minutes, and the unlock price can be the grace
  price. Neither needs code changes, and the 402 always carries the exact amount.
- Chains created before the migration finish under v2 rules.

---

## 7. Files that will change

**Core (Python):** `core/pricing.py` (new), `core/fx.py` (new),
`core/config.py`, `core/api/server.py`, `core/api/dashboard.py`,
`core/bot/handlers/verify_buttons.py`, `core/bot/handlers/associate.py`,
`core/bot/admin.py`, `core/bot/payments.py`, `core/payments/settlement.py`,
`core/payments/payout.py`, `core/notify.py`, `core/webhooks.py`,
`core/wallets.py`, `core/mcp/server.py`.

**Database:** `core/db/migrations/2026-09-30-contract-v3-three-gate.sql` (new),
`core/db/verifies.sql`, `core/db/associates.sql`, `core/db/pricing.sql` (new),
`deploy/initdb/00-schema.sh`.

**verify-api (Node):** `instances/verify-api/server.js`,
`instances/verify-api/routes/verify.js`, `instances/verify-api/routes/terms.js`
(new), `instances/verify-api/routes/verify.test.js`,
`instances/verify-api/routes/pricing.test.js` (new).

**Deploy and site:** `deploy/docker-compose.yml`, `deploy/env.server.example`,
`.env.example`, `deploy/nginx/conf.d/verifi-ssl.conf`,
`deploy/nginx/verifi-ssl.conf.example`,
`deploy/nginx/html/.well-known/x402` (new), `deploy/nginx/html/index.html`,
`deploy/nginx/html/docs/index.html`, `deploy/nginx/html/docs/get-started/index.html`,
`deploy/nginx/html/llms.txt`, `deploy/nginx/html/llms-full.txt`,
`deploy/nginx/html/openapi.json`.

**Docs:** `docs/API.md`, `docs/api-contract.json`, `docs/DECISIONS.md`,
`docs/WALLETS.md`, `docs/local-checks/eurc-facilitator.md` (new), `README.md`,
`CLAUDE.md`, `server.json`.

**Tests:** `tests/test_pricing.py` (new), `tests/test_windows.py` (new),
`tests/test_earnings.py` (new), `tests/test_mcp_payments.py`,
`tests/test_get_started.py`, `tests/test_wallets.py` (move one class above the
`__main__` guard, a leftover of mine), `scripts/smoke-production.sh`.

---

## 8. Tests

Every test the task lists, plus the ones this design needs. DB-backed tests run
against a disposable local PostgreSQL, as I did for v0.4.4. CI has no database,
so those are marked and skipped there, and the pure-logic tests carry CI.

| Test | Covers |
|---|---|
| Ready exactly at `sla_deadline` gives SLA price | D6 boundary |
| Ready 1 ms after `sla_deadline` gives grace price | D6 |
| Ready exactly at `grace_deadline` gives grace price | D6 |
| After `grace_deadline`: expired, `_resolve` refuses, not unlockable | D3, D4 |
| Ready in SLA, unlocked 20 h later: still SLA price | D6 |
| Slow settlement: windows start at `admitted_at` | D5 |
| Changing config or the rate after admission changes nothing on an existing item | D9, binding |
| Rate 1.17: USDC 120000 / 3400000 / 1700000; EURC 100000 / 2900000 / 1450000 | D8, D9 |
| Decimal rounding never goes down, including exact cents | D9 |
| EURC-admitted item: unlock 402 offers EURC only; a USDC payment does not match | D10 |
| The reverse, USDC-admitted | D10 |
| Payment against an expired `terms_id`: no match, fresh 402, no settlement | Quotes |
| Gate 1 402 holds full terms for both assets, USDC first | Gate 1, D15 |
| Unlock 402 holds the applied window and the stored amount | Gates 2, 3 |
| No cancellation route exists, and terms say `cancellation: "none"` | D7 |
| Earnings: SLA and grace answers give the right amount in the bound asset | D11 |
| v2 rows created before the migration keep v2 behaviour | D15 |
| A v2-style USDC client completes a v3 chain unchanged | D15 |
| Expiry grants the credit as before, and a credit-funded entry returns its credit | D4 |
| Config rejects `GRACE_SECONDS <= SLA_SECONDS` and `GRACE_UNLOCK_EUR > SLA_UNLOCK_EUR` | D12 |
| Grace disabled: expiry at SLA deadline, single window | D12 |
| MCP tools return the same fields as HTTP | MCP |
| Extension and requirements in one 402 always carry the same `terms_id` | Consistency |
| Token metadata self-check flags a wrong name or version | 1.5 |
| EURC `transferWithAuthorization` verifies on a local fork | Facilitator (documented check) |
| `@agentcash/discovery` finds `/verify` as a paid resource with an input schema | Discovery |

---

## 9. Conflicts and questions (answers needed before code)

**Q1. The spec file** (B1). Please add `docs/specs/extension-service-windows.md`.

**Q2. Earnings rule and timing (D11).** Today every answered paid verify earns
the flat `instances.associate_commission` (0.50) **when the human answers**,
whether or not the agent ever unlocks. It does not depend on price at all, and
`week_earnings` recomputes history from the current commission. My proposal:

- **Amount:** `earn_atomic = floor(unlock_amount_atomic * commission / SLA_UNLOCK_EUR)`,
  in the bound asset. The commission is snapshotted into the bound terms at
  admission. This follows the price actually charged and needs no exchange rate:
  - EURC: SLA 0.50, grace 0.25.
  - USDC at 1.17: SLA 0.586206, grace 0.293103.
- **Timing:** keep accrual at answer time. The human did the work, and the
  platform carries the risk of an agent that never unlocks.

  The stricter reading of "the price actually paid" would accrue at unlock
  instead. Which do you want?

**Q3. Asset binding for credit-funded admissions (D10 gap).** A chain admitted
on a failure credit paid no asset at gate 1, so "same asset as gate 1" is
undefined. Proposal: bind to the asset of the chain that earned the credit.
Credits minted before v3 bind to USDC. The alternative is to offer both assets
at unlock for credit-funded chains only.

**Q4. ECB as the rate source (D9).** Licence is fine with attribution, and our
terms disclose the rate, its source and date verbatim. But the ECB "strongly
discourages" transactional use, publishes no SLA, and goes stale over holidays.
Our use sets our own published list price with a markup; nobody's funds convert
at the ECB rate, so I think it is defensible. Proceed with ECB, or should I
evaluate an alternative first (for example Chainlink's EUR/USD feed on Base, read
on-chain, whose address I would verify rather than assume)?

**Q5. `x-payment-info` currency.** `EUR` passes the validator x402scan uses, so
by your rule I would use EUR: `fixed 0.10` for admission, and `dynamic
1.45 to 2.90` for unlock. But x402scan's integration spec calls the amount
"decimal USD", so its UI may show euros as dollars. Use EUR anyway, or USD bands
with the euro prices in the descriptions?

**Q6. No-grace deployments versus D12 validation.** D12 requires rejecting
`GRACE_SECONDS <= SLA_SECONDS` **and** allowing no grace window. Both hold only
if "no grace" has its own value. Proposal: `GRACE_SECONDS=0` means disabled.

**Q7. `/supported` cannot list assets** (x402-rs never lists them). Proposal:
accept that, and rely on the startup on-chain token check (section 2.1), which is
a stronger guarantee than `/supported` could give.

**Q8. Queue caps with 24 hour items** (not changed, only flagged).

- The per-wallet block already ends as soon as a human answers, because only
  `admission_pending` and `pending` count as active. An **unanswered** item now
  blocks its wallet for up to 24 hours instead of 60 minutes.
- Unanswered items also stay 24 times longer in `MAX_PENDING_TOTAL` (25), so the
  global cap fills much sooner.

Options: (a) keep both as they are; (b) count only items still inside their SLA
window as active, for both caps; (c) allow a few concurrent chains per wallet;
(d) raise the global cap.

**Q9. D14, unlock deadline for a ready result:** open. Current behaviour kept:
a ready result can be unlocked at any time, at its bound price.

**Q10. Showing the grace window to the responder.** Default: both deadlines on
the card when it is sent, and the applied window in the answer confirmation.
Optional: silently edit the card text when the SLA passes. An edit sends no
notification, but it needs a scheduled job. Default only, or both?

**Q11. USDC first in `accepts`.** Needed so existing agents keep working
(section 1.3), even though the price basis is the euro. Confirm.

**Q12. The dashboard's editable price field** conflicts with D12. Proposal:
replace it with a read-only view of the current terms. Keep the commission
field, which Q2 uses.

**Q13. CI production smoke runs against the live site on every PR.** If this PR
changes `smoke-production.sh` to expect v3 text, that CI job fails until the
deploy. Proposal: make the smoke accept either the v2 or the v3 contract during
the transition, and tighten it to v3 after deploy.

**Q14. `QUOTE_VALID_SECONDS`.** Proposal: **600**. Some MCP clients stop for a
human to approve a spend, and 300 seconds is tight for that. The ECB rate
changes once a day, so a longer quote adds almost no rate risk.

**Q15. `/.well-known/x402` is legacy** for the current discovery library, which
reports it with a warning. I will add it as asked. It stays useful to older
crawlers.

---

## 10. Commit sequence (after approval)

Each step runs the full suite before the next.

1. Spike: the gate 1 402 with a dynamic two-asset price and the extension,
   locally against the testnet facilitator. This proves the approach.
2. Migration and base schema.
3. `core/pricing.py`, `core/fx.py`, config validation, internal terms endpoints.
4. Gate 1: the paid route, binding, deadlines at settlement.
5. Answer time: `ready_at`, the window decision, and the grace guard in `_resolve`.
6. Status view and gates 2 and 3.
7. Expiry job.
8. Earnings ledger, payouts per asset, bot texts.
9. MCP, webhooks, dashboard.
10. Docs, discovery metadata, `.well-known`, nginx, `server.json` 3.0.0.
11. Local facilitator fork check and the discovery tool run.
12. Final report, then stop. No deploy, no push to `main`, no registry publish.

## 11. Not in scope

- No deploy, no MCP Registry publish, no push to `main`, no production data, no
  on-chain transaction, no private key or keychain access.
- No load-based or time-of-day pricing (D13).
- No cancellation endpoint (D7).
- No change to the queue caps (Q8 only flags them).

---

## 12. Approved decisions (2026-09-30)

Juhana accepted every recommendation in section 9:

| # | Decision |
|---|---|
| Q1 | Spec added at `docs/specs/extension-service-windows.md` |
| Q2 | Earnings = `floor(unlock_amount_atomic * commission / SLA_UNLOCK_EUR)` in the bound asset, commission snapshotted at admission, accrued when the human answers |
| Q3 | A credit-funded chain binds to the asset of the chain that earned the credit. Credits minted before v3 bind to USDC |
| Q4 | ECB daily reference rate, with attribution, rate, source and date disclosed in the terms |
| Q5 | `x-payment-info` currency `EUR` |
| Q6 | `GRACE_SECONDS=0` disables the grace window |
| Q7 | `/supported` stays scheme and network only. A startup on-chain token check replaces it |
| Q8 | Queue caps unchanged, flagged in docs |
| Q9 | D14 open: a ready result can be unlocked at any time, at its bound price |
| Q10 | Both deadlines on the card, applied window in the confirmation. No scheduled card edits |
| Q11 | `accepts` lists USDC first, EURC second |
| Q12 | Dashboard price field becomes a read-only view of the current terms. The commission stays editable |
| Q13 | The production smoke accepts either contract during the transition |
| Q14 | `QUOTE_VALID_SECONDS=600` |
| Q15 | `/.well-known/x402` is added although it is legacy for the current discovery library |

## 13. Reconciliation with the spec

The implementation follows the spec's `info` object exactly:

```json
{
  "version": 1,
  "terms_id": "...",
  "terms_valid_until": "...",
  "windows": { "sla": { "within_seconds": 3600 }, "grace": { "within_seconds": 86400 } },
  "price_basis": { "currency": "EUR", "admission": "0.10", "sla": "2.90", "grace": "1.45" },
  "prices": [
    { "network": "eip155:8453", "asset": "<USDC>", "admission": "...", "sla": "...", "grace": "...",
      "conversion": { "from": "EUR", "rate": "...", "source": "ECB euro reference rate",
                      "as_of": "YYYY-MM-DD", "rounding": "up_to_cent" } },
    { "network": "eip155:8453", "asset": "<EURC>", "admission": "100000", "sla": "2900000", "grace": "1450000" }
  ],
  "asset_binding": "admission_asset",
  "expiry": { "admission_credit": "next_admission" },
  "cancellation": "none",
  "status_url_template": "https://verifi.cloud/verify/{work_id}",
  "unlock_url_template": "https://verifi.cloud/verify-unlock?id={work_id}"
}
```

with `schema` from the spec's Appendix A next to `info`.

Where Verifi follows the spec but adds or deviates, and why:

- **No grace window:** `grace` is omitted everywhere, as the spec requires (Q6).
- **Ready status:** the spec requires `work_id`, `admitted_at`, `ready_at`,
  `service_window` and `unlock_url`. Verifi adds `work_id` as an alias of
  `verify_id` and a top-level `unlock_url` next to the existing `unlock.url`.
  Both are additive, so v2 clients are unaffected.
- **Order:** the spec's example lists EURC first. The example is not normative,
  and Verifi lists USDC first for v2 compatibility (Q11). `prices` uses the same
  order as `accepts`.
- **`offer-and-receipt`** (SHOULD) is not implemented in this branch.
- **Discovery metadata for windows** (spec: "Open: field names") is not
  implemented beyond the bazaar input schema.

## 14. New conflicts found in the spec and the discovery listings plan

These surfaced after approval. None of them blocks the core work, so
implementation proceeds and these wait for an answer before the docs step.

**N1. The spec says something the code does not do.** Security considerations:
"Verifi binds both to the requester wallet", meaning the unlock payer and the
admission payer. The code records `entry_payer` and `unlock_payer` but checks
neither against `agent_id`. Options:

- (a) correct the spec sentence to "Verifi records both payers";
- (b) enforce payer equals `agent_id` at both gates for v3 chains. A refusal
  after the gate costs nothing, but agents that pay from a different wallet than
  the one they name would be refused, which v2 allows.

Recommendation: (a) now, (b) as a follow-up.

**N2. `x-payment-info` on `POST /verify-unlock`.** The task asks for it. The
discovery listings plan (item 1.2, step 5) says not to list `/verify-unlock` as
discoverable: a cold probe without a verify id gets 400 from the pre-check, not
a 402, so x402scan would probe it and mark it broken. Recommendation:
`x-payment-info` on `POST /verify` only, with the unlock band (1.45 to 2.90 EUR)
stated in its description and in the terms.

**N3. Public status of an expired work item.** The spec names the state
`expired`. Verifi keeps the public status `failed` with
`failure.reason: "human_timeout"`, so v2 agents that stop on `failed` keep
working (D15). Recommendation: keep it, and consider allowing it in the spec.
