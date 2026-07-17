# Verifi

**Verified human loops for AI agents.** An agent POSTs an intent and a claim; a real human reviews it and answers **true**, **false**, or a **refined** free-text correction, usually within seconds. Payment is a native [x402](https://x402.org) micropayment (USDC on Base) behind an HTTP 402 paywall: no accounts, no API keys.

Live at **[verifi.cloud](https://verifi.cloud)** · [API docs](https://verifi.cloud/docs/) · [llms.txt](https://verifi.cloud/llms.txt) · MCP endpoint at `https://verifi.cloud/mcp`

## Why

Every agent framework hits the same wall: the agent cannot trust its own output. Verifi puts a human judgment call one HTTP request away, priced for machines ($0.10), paid the way agents pay (x402), and fast enough to sit inside an agent loop (median human answer so far: well under a minute).

## How it works

```text
agent ──POST /verify──▶ verify-api ──▶ core engine ──▶ Telegram bot ──▶ human
  ▲                        │x402           │ route,        │ buttons:      │
  │                        │verify+settle  │ audit         │ ✅ ❌ ✏️       │
  └──────── answer ◀───────┴───────────────┴───────────────┴───────────────┘
```

- **Free tier**: 5 verifies per wallet address (`agent_id`). The answer is always included.
- **Paid tier**: after the free quota, the same POST returns `402 Payment Required` with x402 v2 payment instructions. Any x402 client (`@x402/fetch`) completes the handshake automatically. The buyer wallet needs zero ETH (EIP-3009), and settlement goes directly on-chain to the operator's address through a **self-hosted open-source facilitator** ([x402-rs](https://github.com/x402-rs/x402-rs)).
- **Sync + async**: the call waits up to 110 s for the human (`?wait=0..110` to shorten), then 202 + polling. Optional `callback_url` webhook on resolution.
- **MCP**: agents on Claude Code, Cursor, or any MCP client can call the tools `verify_claim`, `get_verify`, and `verifi_info` directly.

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

Response: `{"verify_id": "...", "status": "accepted", "verdict": "true", ...}` or `202` + poll `GET /verify/{id}`. Full reference: [verifi.cloud/docs](https://verifi.cloud/docs/).

## License

MIT
