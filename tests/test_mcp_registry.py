import json
import re
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class McpRegistryMetadataTests(unittest.TestCase):
    def test_remote_server_metadata_is_canonical(self):
        metadata = json.loads((REPO_ROOT / "server.json").read_text())

        self.assertEqual(
            metadata["$schema"],
            "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        )
        self.assertEqual(metadata["name"], "cloud.verifi/human-verification")
        self.assertEqual(metadata["title"], "Verifi")
        self.assertLessEqual(len(metadata["description"]), 100)
        self.assertRegex(metadata["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(
            metadata["remotes"],
            [
                {
                    "type": "streamable-http",
                    "url": "https://verifi.cloud/mcp",
                }
            ],
        )

    def test_http_domain_proof_contains_only_a_public_key(self):
        proof_path = (
            REPO_ROOT
            / "deploy"
            / "nginx"
            / "html"
            / ".well-known"
            / "mcp-registry-auth"
        )
        proof = proof_path.read_text().strip()

        self.assertRegex(
            proof,
            re.compile(r"^v=MCPv1; k=ed25519; p=[A-Za-z0-9+/]{43}=$"),
        )
        self.assertNotIn("PRIVATE", proof.upper())

    def test_nginx_serves_the_exact_registry_proof_path(self):
        nginx_config = (
            REPO_ROOT / "deploy" / "nginx" / "conf.d" / "verifi-ssl.conf"
        ).read_text()

        self.assertIn(
            "location = /.well-known/mcp-registry-auth {",
            nginx_config,
        )


if __name__ == "__main__":
    unittest.main()
