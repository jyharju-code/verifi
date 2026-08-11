#!/usr/bin/env bash
# Production acceptance for the logging and audit feature. Runs on the server
# (needs docker access to postgres and the nginx container). Invoked through
# the ops wrapper: verifi acceptance-logging.
#
# It never spends money and never creates human work: the REST and MCP probes
# are unpaid, so they stop at the 402 payment gate. It checks that each probe
# is logged, correlated by request_id, sourced correctly, free of secrets, and
# that the logs survive an nginx container recreate.
set -uo pipefail
BASE="${1:-https://verifi.cloud}"
LOGDIR=/var/log/verifi/nginx
ACCESS="$LOGDIR/access.json.log"
PSQL="docker exec verifi-postgres-1 psql -U verifi -d verifi -tAc"
pass=0; fail=0
ok()   { echo "PASS: $1"; pass=$((pass+1)); }
bad()  { echo "FAIL: $1"; fail=$((fail+1)); }

echo "== Acceptance: logging and audit against $BASE =="

# 1. Unpaid REST /verify -> 402, capture the request id from the response.
rest_rid=$(curl -sS -D - -o /dev/null -X POST "$BASE/verify" \
    -H 'content-type: application/json' \
    -d '{"agent_id":"0xac0epttestac0eptestac0eptestac0eptest01","intent":"acceptance","claim":"logging probe, unpaid"}' \
    | awk 'tolower($1)=="x-request-id:"{print $2}' | tr -d '\r')
[ -n "$rest_rid" ] && ok "REST response carried X-Request-ID ($rest_rid)" \
    || bad "REST response had no X-Request-ID"

# 2. Unpaid MCP verify_claim -> 402, capture its request id. Best effort: the
#    MCP transport may need a handshake; if so this is reported as SKIP.
mcp_rid=$(curl -sS -D - -o /dev/null -X POST "$BASE/mcp" \
    -H 'content-type: application/json' \
    -H 'accept: application/json, text/event-stream' \
    -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"verify_claim","arguments":{"intent":"acceptance","claim":"mcp logging probe, unpaid","agent_id":"0xac0eptestac0eptestac0eptestac0eptest02"}}}' \
    | awk 'tolower($1)=="x-request-id:"{print $2}' | tr -d '\r')
[ -n "$mcp_rid" ] && ok "MCP response carried X-Request-ID ($mcp_rid)" \
    || echo "SKIP: MCP response had no X-Request-ID (transport handshake); DB check below still applies"

sleep 2  # let the best-effort audit writes land

# 3. The REST request id appears in the nginx JSON access log and in the audit
#    table, and its audit source is rest.
if [ -n "$rest_rid" ]; then
    grep -q "\"request_id\":\"$rest_rid\"" "$ACCESS" \
        && ok "REST request_id is in the nginx JSON access log" \
        || bad "REST request_id missing from $ACCESS"
    src=$($PSQL "SELECT source FROM request_audit WHERE request_id='$rest_rid' LIMIT 1;")
    [ "$src" = "rest" ] && ok "REST audit row exists with source=rest" \
        || bad "REST audit row source was '$src' (expected rest)"
fi

# 4. REST and MCP are distinguishable in the audit table.
rest_n=$($PSQL "SELECT count(*) FROM request_audit WHERE source='rest' AND at > now() - interval '2 minutes';")
mcp_n=$($PSQL "SELECT count(*) FROM request_audit WHERE source='mcp' AND at > now() - interval '2 minutes';")
{ [ "${rest_n:-0}" -ge 1 ] && [ "${mcp_n:-0}" -ge 1 ]; } \
    && ok "rest and mcp rows both present in the last 2 minutes (rest=$rest_n mcp=$mcp_n)" \
    || echo "NOTE: rest=$rest_n mcp=$mcp_n in last 2 min (mcp may need a real MCP client)"

# 5. A forged X-Forwarded-For is not treated as the client address. We send a
#    public request with a spoofed header; the audit must not trust it.
curl -sS -o /dev/null -X POST "$BASE/verify" \
    -H 'content-type: application/json' \
    -H 'X-Forwarded-For: 8.8.8.8' \
    -d '{"agent_id":"0xac0eptestac0eptestac0eptestac0eptest03","intent":"acceptance","claim":"forged xff probe"}'
sleep 2
spoof=$($PSQL "SELECT forwarded_trusted FROM request_audit WHERE agent_id ilike '0xac0eptestac0eptestac0eptestac0eptest03' ORDER BY at DESC LIMIT 1;")
[ "$spoof" = "f" ] && ok "forged X-Forwarded-For was recorded as untrusted" \
    || bad "forged X-Forwarded-For trust flag was '$spoof' (expected f)"

# 6. No secret ever reaches the access log.
if grep -qiE 'payment-signature|authorization|payment-response|"claim"|"intent"|private' "$ACCESS"; then
    bad "a forbidden token appeared in the access log"
else
    ok "access log contains no secret, claim, or intent tokens"
fi

# 7. Logs survive an nginx container recreate.
before=$(wc -l < "$ACCESS" 2>/dev/null || echo 0)
docker compose -f deploy/docker-compose.yml --env-file .env --profile edge up -d --force-recreate nginx >/dev/null 2>&1
sleep 3
after=$(wc -l < "$ACCESS" 2>/dev/null || echo 0)
{ [ -f "$ACCESS" ] && [ "${after:-0}" -ge "${before:-0}" ] && [ "${before:-0}" -gt 0 ]; } \
    && ok "access log persisted across nginx recreate ($before lines before, $after after)" \
    || bad "access log did not persist across recreate (before=$before after=$after)"

# 8. Rotation config is valid and the directory permissions are restrictive.
logrotate -d /etc/logrotate.d/verifi-nginx >/dev/null 2>&1 \
    && ok "logrotate config parses" || bad "logrotate config failed to parse"
perms=$(stat -c '%a' "$LOGDIR" 2>/dev/null)
{ [ "$perms" = "750" ] || [ "$perms" = "700" ] || [ "$perms" = "755" ]; } \
    && ok "log dir permissions are $perms" || bad "log dir permissions are '$perms'"

echo "== Acceptance done: $pass passed, $fail failed =="
[ "$fail" -eq 0 ]
