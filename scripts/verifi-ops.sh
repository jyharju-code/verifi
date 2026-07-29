#!/bin/bash
# Verifi ops wrapper. Installed as /usr/local/bin/verifi on the server.
#
# Three mechanisms keep multiple agents from colliding (see AGENTS.md):
#   1. An operation lock: two commands never run at the same time.
#   2. A turn (vuoroveto): mutating commands run only for the turn holder.
#   3. .env is immutable (chattr +i) outside env-set.
set -euo pipefail
cd /root/verifi

ACTOR="${VERIFI_ACTOR:-unknown}"
CMD="${1:-status}"
shift || true
COMPOSE="docker compose -f deploy/docker-compose.yml --env-file .env --profile bot --profile edge --profile payments"
TURN_FILE=/root/verifi/.turn

audit() {
    local event="$1" details="$2"
    docker exec verifi-postgres-1 psql -U verifi -d verifi -q \
        -c "INSERT INTO audit_log (source, event, actor, details) VALUES ('ops', '$event', '$ACTOR', '$details'::jsonb)" \
        2>/dev/null || true
}

turn_holder() { cat "$TURN_FILE" 2>/dev/null || echo "vapaa"; }

# Missing keys are an expected answer, so a no-match grep must not trip
# set -o pipefail. The || true keeps the function truthful and quiet.
get_env() { grep -s "^$1=" .env | head -1 | cut -d= -f2- || true; }

# The single writer for .env. Lifts the immutability bit, writes one key,
# restores the bit, and audits the key name only, never the value.
env_write() {
    local KEY="$1" VALUE="$2"
    chattr -i .env 2>/dev/null || true
    if grep -q "^${KEY}=" .env; then
        sed -i "s|^${KEY}=.*|${KEY}=${VALUE}|" .env
    else
        echo "${KEY}=${VALUE}" >> .env
    fi
    chattr +i .env 2>/dev/null || true
    audit "env_changed" "{\"key\": \"$KEY\"}"
}

require_turn() {
    local holder
    holder=$(turn_holder)
    if [ "$holder" != "vapaa" ] && [ "$holder" != "$ACTOR" ]; then
        echo "STOP: the turn belongs to '$holder', you are '$ACTOR'."
        echo "Mutating commands require the turn. Check: verifi turn"
        echo "Take the turn (agree with the operator first): VERIFI_ACTOR=$ACTOR verifi turn $ACTOR"
        exit 1
    fi
}

exec 9>/root/verifi/.ops.lock
if ! flock -w 300 9; then
    echo "Another operation holds the lock (over 5 min). Check: verifi status"
    exit 1
fi

case "$CMD" in
  status)
    echo "Turn: $(turn_holder)"
    docker ps --format 'table {{.Names}}\t{{.Status}}' | sort
    ;;
  turn)
    if [ $# -eq 0 ]; then
        echo "Turn: $(turn_holder)"
    else
        NEW="$1"
        case "$NEW" in claude|hermes|juhana|vapaa) ;; *)
            echo "Usage: verifi turn [claude|hermes|juhana|vapaa]"; exit 1 ;;
        esac
        OLD=$(turn_holder)
        echo "$NEW" > "$TURN_FILE"
        audit "turn_changed" "{\"from\": \"$OLD\", \"to\": \"$NEW\"}"
        echo "Turn changed: $OLD -> $NEW"
    fi
    ;;
  deploy)
    require_turn
    audit "deploy_started" "{\"services\": \"${*:-all}\"}"
    $COMPOSE up -d --build "$@"
    audit "deploy_finished" "{\"services\": \"${*:-all}\"}"
    ;;
  restart)
    require_turn
    [ $# -ge 1 ] || { echo "Usage: verifi restart <service...>"; exit 1; }
    audit "restart" "{\"services\": \"$*\"}"
    $COMPOSE up -d --force-recreate "$@"
    ;;
  logs)
    [ $# -ge 1 ] || { echo "Usage: verifi logs <service>"; exit 1; }
    docker logs "verifi-$1-1" --tail "${2:-100}"
    ;;
  env-set)
    require_turn
    [ $# -ge 2 ] || { echo "Usage: verifi env-set KEY value"; exit 1; }
    env_write "$1" "$2"
    echo "OK: $1 updated. Remember: verifi deploy <services that use the value>"
    ;;
  payments-setup)
    # One command from FACILITATOR MISMATCH to settled mainnet payments.
    # Creates the gas wallet when one does not exist, points verify-api at
    # the self-hosted facilitator, deploys both, and verifies the result.
    # Idempotent: an existing key is never overwritten, so this is safe to
    # rerun after funding the wallet or after any config change.
    require_turn
    audit "payments_setup_started" "{}"

    if [ -z "$(get_env X402_PAY_TO)" ]; then
        echo "WARNING: X402_PAY_TO is empty, so the paid gates stay off regardless."
        echo "Set the revenue wallet first: verifi env-set X402_PAY_TO 0x..."
    fi

    PK=$(get_env FACILITATOR_PRIVATE_KEY)
    ADDR=$(get_env FACILITATOR_ADDRESS)
    if [ -z "$PK" ]; then
        echo "No gas wallet in .env. Generating one inside the verify-api image."
        PAIR=$($COMPOSE run --rm --no-deps verify-api node --input-type=module -e '
            import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
            const pk = generatePrivateKey();
            console.log(pk, privateKeyToAccount(pk).address);' | tail -1)
        PK=$(echo "$PAIR" | awk '{print $1}')
        ADDR=$(echo "$PAIR" | awk '{print $2}')
        case "$PK" in
            0x????????????????????????????????????????????????????????????????) ;;
            *) echo "FAILED: wallet generation returned nothing usable."; exit 1 ;;
        esac
        env_write FACILITATOR_PRIVATE_KEY "$PK"
        env_write FACILITATOR_ADDRESS "$ADDR"
        audit "gas_wallet_created" "{\"address\": \"$ADDR\"}"
        echo "New gas wallet: $ADDR"
        echo "The key exists only in .env. Copy it to the operator keychain now:"
        echo "  grep '^FACILITATOR_PRIVATE_KEY=' .env"
    else
        if [ -z "$ADDR" ]; then
            ADDR=$($COMPOSE run --rm --no-deps -e SETUP_PK="$PK" verify-api node --input-type=module -e '
                import { privateKeyToAccount } from "viem/accounts";
                console.log(privateKeyToAccount(process.env.SETUP_PK).address);' | tail -1)
            env_write FACILITATOR_ADDRESS "$ADDR"
        fi
        echo "Existing gas wallet: $ADDR"
    fi

    env_write FACILITATOR_URL "http://facilitator:8080"
    echo "FACILITATOR_URL -> http://facilitator:8080 (docker network only)"

    NETWORK=$(get_env X402_NETWORK)
    NETWORK=${NETWORK:-eip155:8453}

    # The facilitator first, so verify-api's startup check sees the truth.
    $COMPOSE up -d --build facilitator
    echo "Waiting for the facilitator to serve exact on $NETWORK"
    SUPPORTED=""
    for _ in $(seq 1 30); do
        SUP=$(curl -s --max-time 3 http://127.0.0.1:8402/supported || true)
        if echo "$SUP" | grep -q "$NETWORK"; then SUPPORTED=1; break; fi
        sleep 2
    done
    if [ -z "$SUPPORTED" ]; then
        echo "FAILED: the facilitator does not list $NETWORK. Its log:"
        docker logs verifi-facilitator-1 --tail 30
        audit "payments_setup_failed" "{\"reason\": \"facilitator_supported\"}"
        exit 1
    fi

    $COMPOSE up -d --build verify-api
    sleep 5
    echo "verify-api says:"
    docker logs verifi-verify-api-1 --tail 50 2>&1 | grep -i "facilitator\|x402 two-gate" || true

    # Balance of the gas wallet, so the one remaining human step is visible.
    RPC=$(get_env BASE_RPC_URL)
    RPC=${RPC:-https://mainnet.base.org}
    HEX=$(curl -s --max-time 10 -X POST "$RPC" -H 'content-type: application/json' \
        -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getBalance\",\"params\":[\"$ADDR\",\"latest\"]}" \
        | grep -o '"result" *: *"0x[0-9a-fA-F]*"' | grep -o '0x[0-9a-fA-F]*' || true)
    if [ -n "$HEX" ] && [ ${#HEX} -le 17 ]; then
        WEI=$((HEX))
        echo "Gas balance: $(awk -v w="$WEI" 'BEGIN {printf "%.6f", w / 1e18}') ETH on Base"
        if [ "$WEI" -eq 0 ]; then
            echo ""
            echo "ACTION NEEDED: send a few euros of ETH on Base (chain id 8453) to"
            echo "  $ADDR"
            echo "Settlement submits transactions from this wallet and cannot run on 0."
            echo "No rerun is needed afterwards: the next paid verify simply works."
        fi
    else
        echo "Balance check skipped (RPC unreachable). Fund $ADDR with ETH on Base."
    fi
    audit "payments_setup_finished" "{\"address\": \"$ADDR\", \"network\": \"$NETWORK\"}"
    ;;
  backup)
    audit "manual_backup" "{}"
    /etc/cron.daily/verifi-pg-backup
    ls -la /root/backups/ | tail -3
    ;;
  *)
    echo "Verifi ops. Usage: verifi status|turn|deploy|restart|logs|env-set|payments-setup|backup"
    echo "Actor: VERIFI_ACTOR=claude|hermes|juhana. Mutating commands require the turn (verifi turn)."
    exit 1
    ;;
esac
