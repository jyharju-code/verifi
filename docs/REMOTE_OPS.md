# Remote ops: the route from a Claude session to the server

The Server ops workflow lets a Claude Code session, or anyone with write
access to this repository, run whitelisted `verifi` commands on the server
without opening a terminal there. The chain is:

    session or browser -> GitHub Actions -> SSH -> scripts/verifi-remote.sh -> verifi

Every link is constrained. The workflow only offers a fixed choice of
commands. The SSH key is locked to `scripts/verifi-remote.sh` by a forced
command, so even a leaked key cannot open a shell or run anything outside
the whitelist: `status`, `deploy`, `payments-setup`, and `logs <service>`.
Mutating commands fast-forward the checkout to `origin/main` first, take the
ops turn as actor `claude`, run through the same `verifi` wrapper as a human
operator (lock, audit trail), and release the turn afterwards. Automation
never takes the turn from a human holder: if `hermes` or `juhana` holds it,
the dispatch refuses and says so.

A push to `main` dispatches `deploy` automatically, so a merged pull request
is a deployed pull request. Until the secrets below exist, that job skips
with a notice instead of failing.

## Bootstrap, once, on the server

This is the one part no session can do, because it mints the credential and
the server must consent to it. Three commands on the server:

```
cd /root/verifi && git pull
ssh-keygen -t ed25519 -N '' -C verifi-ops-dispatch -f /root/.ssh/verifi_gha
printf 'command="/root/verifi/scripts/verifi-remote.sh",restrict %s\n' \
    "$(cat /root/.ssh/verifi_gha.pub)" >> /root/.ssh/authorized_keys
```

Then in the browser, GitHub repository Settings, Secrets and variables,
Actions, add three repository secrets:

| Secret | Value |
| --- | --- |
| `OPS_SSH_KEY` | Contents of `/root/.ssh/verifi_gha` (the private key) |
| `OPS_SSH_HOST` | The server's hostname or IP |
| `OPS_SSH_USER` | `root` |

After pasting the key, remove it from the server so it exists only as a
GitHub secret: `rm /root/.ssh/verifi_gha /root/.ssh/verifi_gha.pub`.

## Using the route

From the GitHub UI: Actions, Server ops, Run workflow, pick the command.
From a Claude session: ask it to dispatch the workflow; the GitHub MCP
tools can trigger `server-ops.yml` and read the run's logs.

Revoking the route is one line on the server: delete the forced-command
entry from `/root/.ssh/authorized_keys`. Rotating it is the bootstrap again.

## What this deliberately cannot do

The whitelist has no `env-set`, no `turn`, no shell. Money settings and turn
stealing stay human-only on the server. The one financial step, funding the
gas wallet with ETH on Base, is not automatable by design: `payments-setup`
prints the address and the current balance, and a human sends the funds.
