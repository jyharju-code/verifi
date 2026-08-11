#!/bin/bash
# Forced-command guard for remote ops dispatch (GitHub Actions).
#
# The dispatch SSH key is locked to this script in authorized_keys:
#
#   command="/root/verifi/scripts/verifi-remote.sh",restrict ssh-ed25519 AAAA...
#
# so a leaked key can only run the whitelisted verifi commands below, never
# a shell. Mutating commands first fast-forward the checkout to origin/main
# and refresh the installed wrapper, so what runs is always the merged code.
# The turn is taken as actor "claude" and released afterwards, and it is
# never taken from a holder other than "claude" or "vapaa": a human holding
# the turn always wins over automation. See docs/REMOTE_OPS.md.
set -euo pipefail
cd /root/verifi

CMD="${SSH_ORIGINAL_COMMAND:-status}"
case "$CMD" in
  status|deploy|payments-setup|logging-setup|acceptance-logging) ;;
  deploy\ nginx|restart\ nginx) ;;
  logs\ verify-api|logs\ facilitator|logs\ core-api|logs\ bot|logs\ nginx|logs\ postgres|logs\ mcp) ;;
  *)
    echo "refused: '$CMD' is not an allowed remote command"
    echo "allowed: status, deploy, deploy nginx, restart nginx, payments-setup, logging-setup, acceptance-logging, logs <service>"
    exit 1
    ;;
esac

export VERIFI_ACTOR=claude

# Read-only commands run as they are, against whatever is deployed.
case "$CMD" in
  status|logs\ *) exec /usr/local/bin/verifi $CMD ;;
esac

git fetch origin main
git merge --ff-only origin/main
cp scripts/verifi-ops.sh /usr/local/bin/verifi
chmod +x /usr/local/bin/verifi

HOLDER=$(cat .turn 2>/dev/null || echo vapaa)
if [ "$HOLDER" != "vapaa" ] && [ "$HOLDER" != "claude" ]; then
    echo "refused: the turn belongs to '$HOLDER', and automation never takes a human's turn."
    echo "Release it on the server first: verifi turn vapaa"
    exit 1
fi
/usr/local/bin/verifi turn claude
trap '/usr/local/bin/verifi turn vapaa' EXIT
/usr/local/bin/verifi $CMD
