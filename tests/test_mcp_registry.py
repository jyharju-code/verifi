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

    def test_well_known_mcp_names_the_endpoint_it_advertises(self):
        """mcpub.dev refuses a submission without this file, content free.

        Its submit tool answered well_known_missing with required_url
        https://verifi.cloud/.well-known/mcp.json. The file content is
        unconstrained, so the only failure that matters is naming an endpoint
        that does not exist: a directory would list a server that never answers.
        The URL is read from the same file the tests already trust rather than
        repeated here, so a move cannot leave the two disagreeing.
        """
        well_known = (
            REPO_ROOT / "deploy" / "nginx" / "html" / ".well-known" / "mcp.json"
        )
        document = json.loads(well_known.read_text())

        self.assertEqual(document["name"], "verifi")
        self.assertEqual(document["mcp"], "https://verifi.cloud/mcp")
        self.assertTrue(document["description"].strip())

    def test_well_known_mcp_is_static_beside_the_files_nginx_already_serves(self):
        """No location block is needed, and adding one would be the bug.

        nginx serves this directory from root with a default type, which is how
        402index-verify.txt and glama.json answer 200 today. A file placed
        anywhere else in the tree would deploy cleanly and 404 forever, which is
        exactly the state this task exists to leave.
        """
        directory = REPO_ROOT / "deploy" / "nginx" / "html" / ".well-known"
        served = {path.name for path in directory.iterdir()}

        self.assertIn("mcp.json", served)
        # The precedent this relies on.
        self.assertIn("402index-verify.txt", served)
        self.assertIn("glama.json", served)


if __name__ == "__main__":
    unittest.main()
