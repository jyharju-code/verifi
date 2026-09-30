#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-https://verifi.cloud}"
smoke_dir=$(mktemp -d)
trap 'rm -rf "$smoke_dir"' EXIT

request() {
    local name="$1"
    local expected="$2"
    shift 2
    local body_file="$smoke_dir/${name}.body"
    local status
    status=$(curl --silent --show-error --location --output "$body_file" --write-out '%{http_code}' "$@")
    if ! echo "$status" | grep -qE "^(${expected})$"; then
        echo "$name returned HTTP $status, expected $expected" >&2
        sed -n '1,40p' "$body_file" >&2
        exit 1
    fi
    echo "$status" > "$smoke_dir/${name}.status"
}

require_text() {
    local name="$1"
    local text="$2"
    if ! grep -Fq "$text" "$smoke_dir/${name}.body"; then
        echo "$name did not contain required text: $text" >&2
        exit 1
    fi
}

request homepage 200 "$base_url/"
require_text homepage "verifi"
require_text homepage "x402"

request docs 200 "$base_url/docs/"
# The site may still serve contract 2 while contract 3 rolls out, so accept
# either and check the prices of the one that is live.
if grep -Fq 'data-contract-version="3"' "$smoke_dir/docs.body"; then
    require_text docs "0.10 EUR"
    require_text docs "2.90 EUR"
    require_text docs "1.45 EUR"
    request well_known_x402 200 "$base_url/.well-known/x402"
    require_text well_known_x402 "https://verifi.cloud/verify"
else
    require_text docs 'data-contract-version="2"'
    require_text docs "0.10 USDC"
    require_text docs "2.90 USDC"
fi

request health 200 "$base_url/verify-api/health"
require_text health '"ok":true'

# An invalid agent_id must never enter the human queue. The exact refusal
# depends on configuration: 402 when the x402 gate answers first (the gate
# order that discovery crawlers require), 400 from the handler when payments
# are off, 503 when paid admission is not configured at all.
request invalid_verify '400|402|503' \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{"intent":"smoke","claim":"must not enter the queue","agent_id":"invalid"}' \
    "$base_url/verify"
if [ "$(cat "$smoke_dir/invalid_verify.status")" = "400" ]; then
    require_text invalid_verify "agent_id"
fi

request invalid_contact 422 \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{}' \
    "$base_url/contact"

echo "Production smoke passed for $base_url"
