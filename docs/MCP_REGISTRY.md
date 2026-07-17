# MCP Registry publication

Verifi is published to the official MCP Registry as:

```text
cloud.verifi/human-verification
```

The canonical metadata is the repository root `server.json`. The public remote
endpoint is `https://verifi.cloud/mcp` over Streamable HTTP.

## Namespace ownership

The Registry namespace `cloud.verifi` is the reverse DNS form of
`verifi.cloud`. Ownership is proven with an Ed25519 public key at:

```text
https://verifi.cloud/.well-known/mcp-registry-auth
```

Only the public key is committed and deployed. The matching 32 byte private
key is stored in macOS Keychain with these identifiers:

```text
service: VERIFI_MCP_REGISTRY_ED25519
account: verifi.cloud
```

Do not copy the private key into the repository, the VPS, shell history, or a
CI log.

## Publishing an update

Registry versions are immutable. Before every future publication, update the
`version` in `server.json` to a unique semantic version that matches the remote
API contract version.

Install a verified release of the official `mcp-publisher`, then run:

```bash
MCP_PUBLISHER_BIN=/absolute/path/to/mcp-publisher \
  ./scripts/publish-mcp-registry.sh
```

The helper reads the private key from Keychain, authenticates the
`verifi.cloud` domain over HTTP, and publishes the root `server.json`.

After publishing, verify both the exact server name and the latest version in
the Registry API:

```bash
curl -fsS \
  'https://registry.modelcontextprotocol.io/v0.1/servers?search=cloud.verifi%2Fhuman-verification'
```

The Registry is metadata-only. Production deployment of the MCP server and
the public ownership file remains part of the normal Verifi release process.
