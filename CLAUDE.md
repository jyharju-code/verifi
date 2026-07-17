# Verifi development guide

Verifi is a platform that connects AI agents with real humans for verification.
The core engine (associate pool, routing, payments, Telegram bot) powers
product instances; the Verify API is the first instance, Ask This Finn the
second. Think Shopify: the platform powers many stores.

## Conventions

- Code, comments, commit messages, and logs: English.
- The Telegram bot speaks English; Finnish command aliases are kept for the
  original operator (/vapaa, /saldoni, ...). The admin dashboard is Finnish
  by design (operator UI).
- No em-dashes and no en-dashes anywhere. Use periods, colons, commas.
- API responses to agents: English.

## Architecture rules

- All state lives in PostgreSQL. No in-memory state; every process must
  survive a restart.
- Money events always write to the append-only audit_log table.
- Internal services bind to the docker network or localhost only; nginx is
  the single public surface. Docker published ports bypass UFW, so
  localhost binding is the real firewall.
- The x402 middleware settles during the response: never treat a paid verify
  as paid at creation time. unlock_paid flips when the settlement is
  recorded.
- Schema changes ship as idempotent migrations in core/db/migrations/ and
  are reflected in the base schema files.

## Operations

- Deployments go through the `verifi` ops wrapper on the server (lock,
  audit trail, turn-taking). Never run raw docker compose on the server.
- .env on the server is immutable (chattr) outside `verifi env-set`.
- Update the public site (deploy/nginx/html/) whenever behavior changes:
  the site must always match the code.
