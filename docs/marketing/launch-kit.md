# Verifi launch kit (contract v3)

Copy for directories, launch posts and outreach. Every claim here matches the
live service on 2026-10-01: check `docs/API.md` before reusing a number. No
em-dashes or en-dashes, per the repo convention.

Facts to keep straight:

- What it is: an API and MCP server that sends an AI agent's question, claim or
  draft to a real person, who accepts, rejects or improves it with a short
  explanation.
- Price: 0.10 EUR to enter the human queue, then 2.90 EUR if the human answered
  within 60 minutes of admission, or 1.45 EUR within 24 hours. The unlock price
  is fixed by when the human answered, never by when the agent collects it.
- Paid with x402 on Base. USDC today (converted from euros at the ECB reference
  rate, shown in every 402). EURC is ready in code and switches on once the
  receiving address is confirmed.
- No signup, no API key, no account. The agent's wallet is its identity.
- Free: docs, MCP discovery, the status endpoint and the 402 itself, so an agent
  can read every price before it pays.
- If no human answers within 24 hours, the next admission for that wallet is
  free.
- Endpoints: `POST https://verifi.cloud/verify`, MCP at
  `https://verifi.cloud/mcp`, OpenAPI at `https://verifi.cloud/openapi.json`,
  MCP Registry `cloud.verifi/human-verification`.
- Operator: Potamoi Group Oy, Finland.

Do not claim: instant answers, a fixed response time, a number of reviewers,
enterprise compliance, or anything about Coinbase or Circle endorsing Verifi.

## Names and lines

| Use | Text |
| --- | --- |
| Name | Verifi |
| Category | Human-in-the-loop, human verification, AI agent tools, x402 paid API |
| Tagline (6 words) | Human judgment for AI agents. |
| One line (under 100 characters, MCP Registry `description`) | Get a real human to verify, decide, or improve an AI agent's work. Pay per review via x402 on Base. |
| Short (25 words) | Verifi gives AI agents access to a real person. Send a claim or draft, get a verdict or a better answer. Pay per review with x402, no signup. |
| Keywords | human in the loop, human review, human verification, fact check, approval, AI agent, MCP, x402, Base, USDC, EURC, pay per use, no API key |

### 60-word description

Verifi is a human-in-the-loop API for AI agents. Your agent sends a question,
claim or draft with its intent. A real person reviews it and accepts, rejects
or writes a better answer with an explanation. Pay per review with x402 on
Base: 0.10 EUR to ask, then 2.90 EUR for an answer within an hour or 1.45 EUR
within a day. No signup or API key.

### 150-word description

AI agents are fast, but some decisions still need a person: a customer message
that could promise too much, a fact the model is unsure about, a draft that has
to sound right. Verifi gives agents that person through one API and an MCP
server.

The agent sends what it wants to do and what it proposes. A human responder
reads the full context and returns accept, reject, or a refined answer with an
explanation. The agent is woken by a callback, or polls for free, and unlocks
the answer when it is ready.

Payment is native to agents: x402 on Base, paid from the agent's own wallet,
with no signup and no API key. Every price is in the first 402 response before
anything is paid. Asking costs 0.10 EUR. The answer costs 2.90 EUR if the human
replied within 60 minutes, or 1.45 EUR within 24 hours. If nobody answers in a
day, the next request is free.

## Use cases (with request examples)

Each is a real `intent` and `claim` pair an agent could send.

1. Customer messages before they go out.
   intent: "Send this outage update to 3,000 customers."
   claim: "We restored service at 14:10 and every affected user gets a full month free."
2. Facts the model is unsure about.
   intent: "Use this in a sales email to a Finnish municipality."
   claim: "Finnish municipalities must publish procurement decisions within 14 days."
3. Approvals with a human name behind them.
   intent: "Book the venue if the terms are acceptable."
   claim: "The cancellation clause allows a full refund up to 30 days before the event."
4. Taste and tone.
   intent: "Pick the subject line for the launch email."
   claim: "Option B, 'Your agent now has a human on call', is the strongest."
5. Local knowledge.
   intent: "Recommend a lunch spot near Helsinki central station for a client."
   claim: "Restaurant X is open on Mondays and takes reservations for six."

## Why the pricing works this way (for posts and FAQs)

- Two gates stop spam without accounts: the small admission keeps the human
  queue for real work, and nobody pays the big part before a human has answered.
- A fast answer is worth more, so it costs more. A slow answer is still useful
  and costs half.
- The price is fixed by when the human answered, so an agent that polls slowly
  never loses the fast price, and one that polls quickly never gains it.
- Everything is known up front: the first 402 carries the full terms, the window
  lengths and the exchange rate used.

## Launch posts

### X / Twitter thread

1. AI agents can now ask a real person before they act.
   Verifi: send a claim or draft, get back accept, reject, or a better answer.
   No signup, no API key. The agent pays with its own wallet via x402 on Base.
   https://verifi.cloud
2. How it works: POST /verify with what the agent wants to do and what it
   proposes. A human reviews it. A callback says "ready". The agent unlocks the
   answer.
3. Pricing in one line: 0.10 EUR to ask. 2.90 EUR if a human answers within an
   hour, 1.45 EUR within a day. Fixed by when the human answered, never by when
   you poll.
4. Every price is in the first 402, before anything is paid. Agents can probe
   for free.
5. MCP too: https://verifi.cloud/mcp (cloud.verifi/human-verification in the MCP
   Registry). Docs and OpenAPI: https://verifi.cloud/docs/

### Farcaster (/base, /x402)

Verifi is live on Base: human judgment as an x402 API. An agent sends a claim,
a person accepts, rejects or improves it. 0.10 EUR to ask, 2.90 EUR for an
answer within an hour, 1.45 EUR within a day, all in the first 402. No signup,
just a wallet. https://verifi.cloud

### LinkedIn (English)

We built the missing step in many AI agent workflows: asking a person.

Verifi lets an AI agent send a claim, a customer message or a decision to a
real human, who accepts it, rejects it or writes a better version with an
explanation. The agent pays per review from its own wallet using x402 on Base,
so there is no signup and no API key.

The pricing rewards speed: 0.10 EUR to ask, then 2.90 EUR if a human answers
within an hour, or 1.45 EUR within a day. Every price is visible before the
agent pays anything.

If you build agents that send messages, make purchases or state facts, I would
like to hear which decisions you would never let them make alone.
https://verifi.cloud

### LinkedIn (suomeksi)

Rakensimme puuttuvan askeleen moneen tekoälyagentin työnkulkuun: kysy ihmiseltä.

Verifin kautta tekoälyagentti voi lähettää väitteen, asiakasviestin tai
päätöksen oikealle ihmiselle, joka hyväksyy sen, hylkää sen tai kirjoittaa
paremman version perusteluineen. Agentti maksaa jokaisen tarkistuksen omasta
lompakostaan x402-maksuna Base-verkossa, joten rekisteröitymistä tai
API-avainta ei tarvita.

Hinnoittelu palkitsee nopeutta: 0,10 euroa kysymisestä, sitten 2,90 euroa jos
ihminen vastaa tunnin sisällä tai 1,45 euroa vuorokauden sisällä. Kaikki hinnat
näkyvät ennen kuin agentti maksaa mitään.

https://verifi.cloud

### Show HN (draft, post only with a live demo ready)

Title: Show HN: Verifi, a human-in-the-loop API that AI agents pay for with x402

Body: Verifi lets an AI agent ask a real person to accept, reject or improve
what it is about to do. It is an HTTP API and an MCP server. Payment uses x402
on Base, so the agent pays from its own wallet without signup or API keys. An
unpaid request returns a 402 with every price: 0.10 EUR to enter the queue, then
2.90 EUR if a human answered within 60 minutes or 1.45 EUR within 24 hours. The
price is fixed by when the human answered, not by when the agent polls. I would
love feedback on the pricing model and on what you would route to a human.
Docs: https://verifi.cloud/docs/

### Product Hunt

- Tagline: Human judgment for AI agents, paid per review
- First comment: the 150-word description, plus "Try it free: send an unpaid
  POST to /verify and read the 402."

## Directory entries

### Circle Agent Marketplace intake

- Service name: Verifi
- Category: Human verification / human-in-the-loop
- Description: the 60-word description
- Endpoint: `POST https://verifi.cloud/verify` (returns 402 with x402
  requirements)
- OpenAPI: https://verifi.cloud/openapi.json
- Network and asset: Base mainnet (eip155:8453), USDC; EURC planned
- Price: 0.10 EUR admission, then 2.90 or 1.45 EUR unlock (shown converted in
  the 402)
- Payout wallet: the `X402_PAY_TO` receiving address (Juhana confirms)
- Contact: Juhana Harju, Potamoi Group Oy

### x402scan

Register `https://verifi.cloud` at https://www.x402scan.com/resources/register
("Add Server"). Discovery reads `/openapi.json`, which already declares
`x-payment-info` (0.10 EUR, x402) on `POST /verify`.

### Awesome lists

Entry text (adapt to each list's line format):
`Verifi: human-in-the-loop for AI agents. A real person verifies, decides or improves the agent's work. Remote MCP and x402 pay per review on Base.`

## What to measure

The request audit already records every `/verify` call with its source (REST
or MCP), user agent and outcome. Track weekly:

- unpaid 402 probes (discovery working),
- admitted chains by source,
- answer time against the 60 minute window,
- unlock rate (ready chains that are unlocked).
