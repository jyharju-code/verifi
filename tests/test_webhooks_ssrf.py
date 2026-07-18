"""SSRF guard for outbound callback delivery.

The callback target is pinned to a validated public IP so the address cannot
change between the safety check and the request (DNS rebinding).
"""
import unittest
from unittest.mock import patch

from core import webhooks


def _fake_getaddrinfo(ip):
    def inner(host, port, *args, **kwargs):
        family = 10 if ":" in ip else 2  # AF_INET6 / AF_INET
        return [(family, 1, 6, "", (ip, port))]

    return inner


class ResolvePinnedTests(unittest.TestCase):
    def test_rejects_non_https(self):
        ok, reason, *_ = webhooks._resolve_pinned("http://example.com/hook")
        self.assertFalse(ok)
        self.assertEqual(reason, "https required")

    def test_rejects_non_default_port(self):
        ok, reason, *_ = webhooks._resolve_pinned("https://example.com:8443/hook")
        self.assertFalse(ok)
        self.assertEqual(reason, "port 443 only")

    def test_rejects_embedded_credentials(self):
        ok, reason, *_ = webhooks._resolve_pinned("https://user:pw@example.com/hook")
        self.assertFalse(ok)
        self.assertEqual(reason, "invalid host")

    def test_rejects_private_resolution(self):
        with patch("core.webhooks.socket.getaddrinfo", _fake_getaddrinfo("10.0.0.5")):
            ok, reason, *_ = webhooks._resolve_pinned("https://internal.example.com/hook")
        self.assertFalse(ok)
        self.assertEqual(reason, "resolves to non-public address")

    def test_rejects_link_local_metadata_endpoint(self):
        with patch("core.webhooks.socket.getaddrinfo", _fake_getaddrinfo("169.254.169.254")):
            ok, reason, *_ = webhooks._resolve_pinned("https://metadata.example.com/hook")
        self.assertFalse(ok)
        self.assertEqual(reason, "resolves to non-public address")

    def test_pins_public_ip_and_preserves_host_and_sni(self):
        with patch("core.webhooks.socket.getaddrinfo", _fake_getaddrinfo("93.184.216.34")):
            ok, reason, pinned, host, sni = webhooks._resolve_pinned(
                "https://hooks.example.com/path?x=1"
            )
        self.assertTrue(ok)
        self.assertEqual(pinned, "https://93.184.216.34:443/path?x=1")
        self.assertEqual(host, "hooks.example.com")
        self.assertEqual(sni, "hooks.example.com")

    def test_pins_ipv6_with_brackets(self):
        with patch("core.webhooks.socket.getaddrinfo", _fake_getaddrinfo("2606:2800:220:1:248:1893:25c8:1946")):
            ok, _reason, pinned, host, sni = webhooks._resolve_pinned("https://v6.example.com/h")
        self.assertTrue(ok)
        self.assertEqual(pinned, "https://[2606:2800:220:1:248:1893:25c8:1946]:443/h")
        self.assertEqual(host, "v6.example.com")


if __name__ == "__main__":
    unittest.main()
