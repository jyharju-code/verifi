# Verifi

**Verified human loops for AI agents.** An agent POSTs an intent and a claim; a real human reviews it and answers **true**, **false**, or a **refined** free-text correction. Payment is a native [x402](https://x402.org) micropayment (USDC or EURC on Base) behind an HTTP 402 paywall: no accounts, no API keys.

Live at **[verifi.cloud](https://verifi.cloud)** · [API docs](https://verifi.cloud/docs/) · [llms.txt](https://verifi.cloud/llms.txt) · MCP endpoint at `https://verifi.cloud/mcp`

Official MCP Registry name: `cloud.verifi/human-verification`. Publication and
domain ownership details are documented in [docs/MCP_REGISTRY.md](docs/MCP_REGISTRY.md).

## Why

Every agent framework hits the same wall: the agent cannot trust its own output. Verifi gives the agent a durable human workflow, paid the way agents pay (x402), and designed to remain reliable when a good answer takes several minutes.

## How it works

```text
agent ──POST /verify──▶ verify-api ──▶ core engine ──▶ Telegram bot ──▶ human
  ▲                        │x402           │ route,        │ buttons:      │
  │                        │verify+settle  │ audit         │ ✅ ❌ ✏️       │
  └──────── answer ◀───────┴───────────────┴───────────────┴───────────────┘
```

- **Three gates, priced in euros**: 0.10 EUR admits the request to the human queue. When the human answers within 60 minutes of admission, the result unlocks for 2.90 EUR; a later answer, within 24 hours, unlocks for 1.45 EUR. The price is fixed by when the human answered, never by when the agent unlocks. Pay in EURC, or in USDC converted at the ECB reference rate and rounded up to the cent. The admission asset binds the chain, and every price is in the first `402`.
- **No free human work**: every chain pays both gates. Human time is the scarce resource and wallet addresses are free to mint, so a free tier would let anyone flood the queue unpaid. Free without a human: the docs, MCP discovery, and the `402` response that lets an agent test the connection and read the price.
- **Failure credit**: a chain whose admission was actually paid and nobody answers within 24 hours grants one free admission for the next chain, in the same asset. The unlock is not included, and credits cannot be farmed for free work.
- **Reliable delivery**: every call returns `202` with a durable `verify_id`. Agents poll for as long as the human needs, up to 24 hours, or use the optional ready or failed callback.
- **MCP**: agents can call `verify_claim`, `get_verify`, `unlock_verify`, and `verifi_info` directly.

## Repository layout

```text
core/          Python core engine
  api/         internal FastAPI + admin dashboard + public /contact
  bot/         Telegram bot for human responders (associates)
  mcp/         MCP server (Streamable HTTP)
  routing/     associate selection
  payments/    earnings, settlements, payouts
  db/          PostgreSQL schema + migrations
instances/
  verify-api/  public Verify API (Node.js + @x402/express)
  ask-this-finn/  second instance (placeholder)
deploy/        docker-compose, nginx, facilitator config, static site
scripts/       ops wrapper (lock + audit + turn-taking)
```

One VPS runs everything: PostgreSQL (+pgvector), the core engine, the bot, the Verify API, the x402 facilitator, the MCP server, and nginx.

## Running it yourself

```bash
cp deploy/env.server.example .env   # fill in tokens and keys
docker compose -f deploy/docker-compose.yml --env-file .env up -d postgres
docker compose -f deploy/docker-compose.yml --env-file .env up -d --build \
  core-api verify-api mcp
docker compose -f deploy/docker-compose.yml --env-file .env --profile bot up -d bot
docker compose -f deploy/docker-compose.yml --env-file .env --profile payments up -d facilitator
docker compose -f deploy/docker-compose.yml --env-file .env --profile edge up -d nginx
```

You need: a Telegram bot token, a receiving wallet address (`X402_PAY_TO`), and a small gas wallet for the facilitator (`FACILITATOR_PRIVATE_KEY`, a few euros of ETH on Base). See [docs/WALLETS.md](docs/WALLETS.md) for the money architecture and [docs/DECISIONS.md](docs/DECISIONS.md) for design decisions.

## API in 20 seconds

```bash
curl -X POST https://verifi.cloud/verify \
  -H "Content-Type: application/json" \
  -d '{"intent": "send_email",
       "claim": "The user opted in via double opt-in on 2026-07-15.",
       "agent_id": "0xYourWalletAddress"}'
```

Every admitted response starts with `202` and `status: "processing"`. Poll until `ready` or `failed`; a ready result is retrieved through the separate unlock endpoint. The canonical repository reference is [docs/API.md](docs/API.md), rendered at [verifi.cloud/docs](https://verifi.cloud/docs/).

## License

MIT
