# Where Verifi is listed, and what each listing needs

Checked 2026-10-01. Rule from the discovery plan: nothing is submitted, posted
or opened in someone else's repo without Juhana's yes for that specific action.
Copy comes from `launch-kit.md`.

## Status

| Target | Status | Next step | Who |
| --- | --- | --- | --- |
| Official MCP Registry | Listed, 3.0.0 latest | Publish 3.0.1 (icons, website, repo) after the site deploy | Claude, on yes |
| Glama | Listed, healthy, unclaimed: https://glama.ai/mcp/connectors/cloud.verifi/human-verification | Claim: sign in, "claim ownership", copy the `glama_claim_...` token. Claude publishes it at `/.well-known/glama.json` | Juhana gets the token, Claude publishes |
| x402scan | Not registered | Register `https://verifi.cloud` at https://www.x402scan.com/resources/register ("Add Server") | Juhana |
| punkpeye/awesome-remote-mcp-servers | Not listed | Star the repo (required by its CONTRIBUTING), then open the PR with the entry below | Juhana stars, PR on yes |
| Smithery | Not listed | `smithery auth login`, then `smithery mcp publish "https://verifi.cloud/mcp" -n <namespace>/human-verification` | Juhana logs in and picks the namespace |
| Circle Agent Marketplace | Not listed | Google Form (sign-in): endpoint, payout wallet, description. Manual review | Juhana |
| mcpservers.org (feeds wong2/awesome-mcp-servers) | Not listed | https://mcpservers.org/submit, tick "remote". Free queue about 2 weeks | Juhana |
| mcp.so | Not listed | https://mcp.so/submit, type "Remote Server" | Juhana |
| MCP Market | Not listed | https://mcpmarket.com/submit, "Remote MCP" | Juhana |
| xpaysh/awesome-x402 | Not listed | PR with the entry below. Many open PRs, slow merges | PR on yes |
| PulseMCP | Submissions paused | Picks up the official registry when it reopens | Nobody |
| Agentic.Market / CDP Bazaar | Not possible today | Lists only services settling through Coinbase's CDP facilitator. Business decision (plan item 2.2) | Juhana decides |
| x402.org ecosystem page | Gone (sunset 2026-07, points to x402scan) | None | Nobody |

Not pursued: appcypher list (archived), the main punkpeye list (sends hosted
servers to the remote list), Cline marketplace (installable servers), A2A agent
card (the spec requires every A2A operation), agents.txt (no standard),
x402scan ownership proof (needs a signature from the payout wallet, and the
current payout address is an exchange deposit address).

## Entries in each target's format

### punkpeye/awesome-remote-mcp-servers

Section "Agreements & Coordination", alphabetical:

```markdown
- [Verifi](https://verifi.cloud) `https://verifi.cloud/mcp`
  [![Verifi MCP connector](https://glama.ai/mcp/connectors/cloud.verifi/human-verification/badges/score.svg)](https://glama.ai/mcp/connectors/cloud.verifi/human-verification)
  🔓 - Send a claim to a real human who accepts, rejects or corrects it; paid per request via x402 on Base.
```

### xpaysh/awesome-x402

End of the "Model Context Protocol (MCP)" section:

```markdown
- [Verifi](https://verifi.cloud) - Human-in-the-loop for AI agents: a real person verifies, decides or improves the agent's work. Remote MCP and HTTP, paid per review with x402 on Base.
```

### mcpservers.org

- Server name: Verifi
- Category: closest to "Productivity" or "Other"
- Short description: the 25-word line in `launch-kit.md`
- Website: https://verifi.cloud, Docs: https://verifi.cloud/docs/, Repository: https://github.com/jyharju-code/verifi
- Official MCP Registry name: cloud.verifi/human-verification
- Remote: yes

### mcp.so (form or GitHub issue on chatmcp/mcpso)

- Name: Verifi
- Type: Remote Server
- URL: https://verifi.cloud/mcp
- Tagline: Human judgment for AI agents.
- Short description: the 25-word line
- Blurb: the 60-word description

### Circle Agent Marketplace

See "Circle Agent Marketplace intake" in `launch-kit.md`. Circle's docs speak
of USDC only, so list USDC.

### Smithery

No static server card is needed: `/mcp` answers the scan without auth. Pick a
namespace first, for example `@verifi`.

## Before Show HN

The HN guidelines ask that a Show HN be easy to try. Today trying Verifi costs
real money after the free 402. Options: a short demo video, or a small number
of free first reviews per wallet with a hard daily cap. That is a product
decision, not a listing step.

## Do first

Confirm EURC on the Coinbase receiving address, or change the docs to say EURC
comes later. Directories copy the docs, and they currently promise EURC while
production offers USDC only.
