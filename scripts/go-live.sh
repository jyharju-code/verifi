#!/usr/bin/env bash
# One-shot go-live. Run on the server, no git setup needed:
#   curl -fsSL https://raw.githubusercontent.com/jyharju-code/verifi/main/scripts/go-live.sh | bash
# Force-syncs /root/verifi to origin/main (.env is gitignored, so untouched),
# installs the ops wrapper, deploys, and turns on payments. Idempotent.
set -uo pipefail
export VERIFI_ACTOR=juhana
cd /root/verifi || { echo "FAIL: /root/verifi not found"; exit 1; }

release_turn() { /usr/local/bin/verifi turn vapaa 2>/dev/null || true; }
trap release_turn EXIT

echo "== 1/6 Pointing origin at the public HTTPS repo =="
git remote set-url origin https://github.com/jyharju-code/verifi.git || true

echo "== 2/6 Fetching the merged code =="
git fetch origin main || { echo "FAIL: cannot reach GitHub from the server. Check network."; exit 1; }

echo "== 3/6 Force-syncing to origin/main (.env is gitignored, so it is untouched) =="
git reset --hard origin/main || { echo "FAIL: git reset."; exit 1; }

echo "== 4/6 Installing the ops wrapper =="
cp scripts/verifi-ops.sh /usr/local/bin/verifi && chmod +x /usr/local/bin/verifi

echo "== 5/6 Deploying (this stops the 500 errors) =="
/usr/local/bin/verifi turn juhana
/usr/local/bin/verifi deploy

echo "== 6/6 Turning on payments =="
/usr/local/bin/verifi payments-setup

echo ""
echo "=================================================================="
echo " DONE. Scroll up for:"
echo "   facilitator serves exact on eip155:8453   (payments work)"
echo "   New/Existing gas wallet: 0x...            (fund this address)"
echo "=================================================================="
