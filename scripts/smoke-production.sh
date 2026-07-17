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
    if [ "$status" != "$expected" ]; then
        echo "$name returned HTTP $status, expected $expected" >&2
        sed -n '1,40p' "$body_file" >&2
        exit 1
    fi
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
require_text docs 'data-contract-version="2"'
require_text docs "0.10 USDC"
require_text docs "2.90 USDC"

request health 200 "$base_url/verify-api/health"
require_text health '"ok":true'

request invalid_verify 400 \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{"intent":"smoke","claim":"must not enter the queue","agent_id":"invalid"}' \
    "$base_url/verify"
require_text invalid_verify "agent_id"

request invalid_contact 422 \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{}' \
    "$base_url/contact"

echo "Production smoke passed for $base_url"
