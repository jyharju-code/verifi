#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
publisher_bin="${MCP_PUBLISHER_BIN:-}"

if [[ -z "$publisher_bin" ]]; then
  publisher_bin="$(command -v mcp-publisher || true)"
fi

if [[ -z "$publisher_bin" || ! -x "$publisher_bin" ]]; then
  echo "mcp-publisher was not found. Set MCP_PUBLISHER_BIN to its absolute path." >&2
  exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This helper reads the publishing key from macOS Keychain." >&2
  exit 1
fi

registry_private_key="$(
  security find-generic-password \
    -w \
    -a verifi.cloud \
    -s VERIFI_MCP_REGISTRY_ED25519
)"
trap 'unset registry_private_key' EXIT

cd "$repo_dir"

"$publisher_bin" login http \
  --domain verifi.cloud \
  --private-key "$registry_private_key"

"$publisher_bin" publish
