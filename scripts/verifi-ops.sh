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
    KEY="$1"; VALUE="$2"
    chattr -i .env 2>/dev/null || true
    if grep -q "^${KEY}=" .env; then
        sed -i "s|^${KEY}=.*|${KEY}=${VALUE}|" .env
    else
        echo "${KEY}=${VALUE}" >> .env
    fi
    chattr +i .env 2>/dev/null || true
    audit "env_changed" "{\"key\": \"$KEY\"}"
    echo "OK: $KEY updated. Remember: verifi deploy <services that use the value>"
    ;;
  backup)
    audit "manual_backup" "{}"
    /etc/cron.daily/verifi-pg-backup
    ls -la /root/backups/ | tail -3
    ;;
  *)
    echo "Verifi ops. Usage: verifi status|turn|deploy|restart|logs|env-set|backup"
    echo "Actor: VERIFI_ACTOR=claude|hermes|juhana. Mutating commands require the turn (verifi turn)."
    exit 1
    ;;
esac
