# Verifi

**Verified human loops for AI agents.** An agent POSTs an intent and a claim; a real human reviews it and answers **true**, **false**, or a **refined** free-text correction, usually within seconds. Payment is a native [x402](https://x402.org) micropayment (USDC on Base) behind an HTTP 402 paywall: no accounts, no API keys.

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

- **Two paid gates**: 0.10 USDC admits the request to the human queue. When polling reports `ready`, a new 2.90 USDC payment unlocks the result. Total successful price is 3.00 USDC.
- **Five full-free chains**: each wallet gets five chains where both gates are free. Submit, polling, human work, statuses, and unlock remain identical to paid chains.
- **Failure credit**: a failed admitted chain grants one 0.10 USDC entry credit for the next chain. The later 2.90 USDC result gate is not included.
- **Reliable delivery**: every call returns `202` with a durable `verify_id`. Agents poll through several minutes if necessary, or use the optional ready or failed callback.
- **MCP**: agents can call `verify_claim`, `get_verify`, `unlock_verify`, and `verifi_info` directly for full-free chains.

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
