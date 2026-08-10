#!/usr/bin/env bash
# One-shot go-live: pull the merged code, install the ops wrapper, deploy, and
# turn on mainnet payments. Run once on the server:
#   cd /root/verifi && git pull && bash scripts/go-live.sh
# Idempotent: safe to run again. Releases the ops turn even if a step fails.
set -uo pipefail
export VERIFI_ACTOR=juhana
cd /root/verifi || { echo "FAIL: /root/verifi not found"; exit 1; }

release_turn() { /usr/local/bin/verifi turn vapaa 2>/dev/null || true; }
trap release_turn EXIT

echo "== 1/5 Fetching the merged code =="
git pull || { echo "FAIL: git pull. Check network or local changes."; exit 1; }

echo "== 2/5 Installing the ops wrapper =="
cp scripts/verifi-ops.sh /usr/local/bin/verifi && chmod +x /usr/local/bin/verifi

echo "== 3/5 Taking the ops turn =="
/usr/local/bin/verifi turn juhana

echo "== 4/5 Deploying (this stops the 500 errors) =="
/usr/local/bin/verifi deploy

echo "== 5/5 Turning on payments =="
/usr/local/bin/verifi payments-setup

echo ""
echo "=================================================================="
echo " DONE. Scroll up and look for these two lines:"
echo "   facilitator serves exact on eip155:8453   (payments work)"
echo "   New/Existing gas wallet: 0x...            (fund this address)"
echo "=================================================================="
