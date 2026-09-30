import unittest
from html.parser import HTMLParser
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HTML_ROOT = REPO_ROOT / "deploy" / "nginx" / "html"


class StrictHtmlParser(HTMLParser):
    def error(self, message):
        raise AssertionError(message)


class GetStartedPageTests(unittest.TestCase):
    def setUp(self):
        self.landing = (HTML_ROOT / "index.html").read_text()
        self.page = (HTML_ROOT / "docs" / "get-started" / "index.html").read_text()

    def test_landing_links_directly_to_get_started(self):
        self.assertIn('href="/docs/get-started/">Get started</a>', self.landing)

    def test_page_explains_a_general_x402_wallet(self):
        expected = [
            "Give your agent <span>buying power.</span>",
            "other compatible online tools",
            "Make the wallet ready to use Verifi and other x402 services.",
            "BASE MAINNET",
            "USDC or EURC, not ETH",
        ]
        for text in expected:
            with self.subTest(text=text):
                self.assertIn(text, self.page)

    def test_page_keeps_verifi_pricing_accurate(self):
        self.assertIn("Test the connection free", self.page)
        self.assertIn("An unpaid request returns a 402 with every price", self.page)
        self.assertIn("0.10 EUR enters the queue", self.page)
        self.assertIn("2.90 EUR unlocks an answer given within 60 minutes", self.page)
        self.assertIn("1.45 EUR one given later, within 24 hours", self.page)
        self.assertIn("Pay in EURC, or in USDC", self.page)
        for stale in ("3 USDC per completed review", "2.90 USDC unlocks the answer"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, self.page)

    def test_page_stays_simple_for_the_reader(self):
        for implementation_detail in (
            "@x402/",
            "ExactEvmScheme",
            "privateKeyToAccount",
            "EIP-3009",
            "50 free",
        ):
            with self.subTest(implementation_detail=implementation_detail):
                self.assertNotIn(implementation_detail, self.page)

    def test_page_is_parseable_html_without_forbidden_long_dashes(self):
        parser = StrictHtmlParser()
        parser.feed(self.page)
        parser.close()
        self.assertNotIn("\u2013", self.page)
        self.assertNotIn("\u2014", self.page)


if __name__ == "__main__":
    unittest.main()
