"""The public edge must pass the x402 PAYMENT-REQUIRED header through.

Contract v3 puts the full terms and their schemas in that header. With
nginx's default 4 KB header buffer the first 402 became a 502 in production.
"""
import re
import unittest

from tests.dbutil import REPO

CONFS = [
    REPO / "deploy" / "nginx" / "conf.d" / "verifi-ssl.conf",
    REPO / "deploy" / "nginx" / "verifi-ssl.conf.example",
]


def verify_location(text: str) -> str:
    start = text.index("location /verify {")
    return text[start:text.index("\n    }\n", start)]


class PaymentHeaderBufferTests(unittest.TestCase):
    def test_the_verify_location_has_room_for_the_402_header(self):
        for conf in CONFS:
            with self.subTest(conf=conf.name):
                block = verify_location(conf.read_text())
                match = re.search(r"proxy_buffer_size\s+(\d+)k;", block)
                self.assertIsNotNone(match, "proxy_buffer_size missing in location /verify")
                self.assertGreaterEqual(int(match.group(1)), 16)
