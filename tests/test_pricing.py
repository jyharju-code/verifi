"""Contract v3 pricing: the terms of a work item, built by one function."""
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal

from core import pricing
from core.fx import FxParseError, parse_ecb_daily
from core.pricing import FxRate, PricingConfigError, PricingUnavailable

USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EURC = "0x60a3E35Cc302bFA44Cb288Bc5a4F316Fdb1adb42"
ISSUED = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
RATE_117 = FxRate(rate=Decimal("1.17"), as_of=date(2026, 9, 30), source="ECB euro reference rate")


def terms(env=None, fx=RATE_117):
    cfg = pricing.load_config(env or {})
    return pricing.build_terms(
        cfg, fx, commission_eur=Decimal("0.50"), terms_id="st_test", issued_at=ISSUED
    )


def price_of(t, asset):
    return next(p for p in t["prices"] if p["asset"] == asset)


class ConversionTests(unittest.TestCase):
    def test_usdc_at_rate_117_rounds_up_to_the_cent(self):
        t, _ = terms()
        usdc = price_of(t, USDC)
        self.assertEqual((usdc["admission"], usdc["sla"], usdc["grace"]), ("120000", "3400000", "1700000"))

    def test_eurc_takes_the_euro_prices_unchanged(self):
        t, _ = terms()
        eurc = price_of(t, EURC)
        self.assertEqual((eurc["admission"], eurc["sla"], eurc["grace"]), ("100000", "2900000", "1450000"))
        self.assertNotIn("conversion", eurc)

    def test_conversion_is_disclosed_for_the_converted_asset(self):
        t, _ = terms()
        self.assertEqual(
            price_of(t, USDC)["conversion"],
            {"from": "EUR", "rate": "1.17", "source": "ECB euro reference rate",
             "as_of": "2026-09-30", "rounding": "up_to_cent"},
        )

    def test_an_exact_cent_is_not_rounded_up(self):
        self.assertEqual(pricing.convert(Decimal("2.90"), Decimal("1")), Decimal("2.90"))
        self.assertEqual(pricing.convert(Decimal("0.10"), Decimal("1.2")), Decimal("0.12"))

    def test_rounding_never_goes_down(self):
        for rate in ("1.0001", "1.1355", "1.17", "0.9999", "1.2345678"):
            for eur in ("0.10", "1.45", "2.90"):
                exact = Decimal(eur) * Decimal(rate)
                self.assertGreaterEqual(pricing.convert(Decimal(eur), Decimal(rate)), exact)

    def test_money_math_never_uses_floats(self):
        # 0.10 * 1.17 in binary floating point is 0.11699999999999999.
        self.assertEqual(pricing.convert(Decimal("0.10"), Decimal("1.17")), Decimal("0.12"))

    def test_atomic_conversion_refuses_to_round(self):
        self.assertEqual(pricing.to_atomic(Decimal("2.90"), 6), 2_900_000)
        with self.assertRaises(ValueError):
            pricing.to_atomic(Decimal("0.1234567"), 6)


class TermsShapeTests(unittest.TestCase):
    """The object is the spec's service-windows `info` (Appendix A)."""

    def test_required_fields_and_fixed_values(self):
        t, _ = terms()
        for key in ("version", "terms_id", "windows", "prices", "asset_binding", "expiry", "cancellation"):
            self.assertIn(key, t)
        self.assertEqual(t["version"], 1)
        self.assertEqual(t["asset_binding"], "admission_asset")
        self.assertEqual(t["cancellation"], "none")
        self.assertEqual(t["expiry"], {"admission_credit": "next_admission"})
        self.assertEqual(t["windows"], {"sla": {"within_seconds": 3600}, "grace": {"within_seconds": 86400}})
        self.assertEqual(t["price_basis"], {"currency": "EUR", "admission": "0.10", "sla": "2.90", "grace": "1.45"})

    def test_terms_valid_until_follows_quote_validity(self):
        t, _ = terms()
        self.assertEqual(t["terms_valid_until"], "2026-10-01T09:10:00.000Z")

    def test_usdc_is_offered_first(self):
        t, _ = terms()
        self.assertEqual([p["asset"] for p in t["prices"]], [USDC, EURC])

    def test_amounts_are_atomic_strings(self):
        t, _ = terms()
        for p in t["prices"]:
            for k in ("admission", "sla", "grace"):
                self.assertRegex(p[k], r"^[0-9]+$")

    def test_grace_never_exceeds_sla_for_any_asset(self):
        t, _ = terms()
        for p in t["prices"]:
            self.assertLessEqual(int(p["grace"]), int(p["sla"]))

    def test_url_templates_use_the_public_base(self):
        t, _ = terms({"PUBLIC_BASE_URL": "https://verifi.cloud/"})
        self.assertEqual(t["unlock_url_template"], "https://verifi.cloud/verify-unlock?id={work_id}")

    def test_internal_carries_token_metadata_and_commission_only(self):
        t, internal = terms()
        self.assertEqual(internal["assets"][EURC.lower()]["name"], "EURC")
        self.assertEqual(internal["assets"][USDC.lower()]["name"], "USD Coin")
        self.assertEqual(internal["responder_commission_eur"], "0.50")
        self.assertNotIn("responder_commission_eur", t)


class NoGraceTests(unittest.TestCase):
    def test_zero_disables_grace_and_omits_it_everywhere(self):
        t, _ = terms({"GRACE_SECONDS": "0"})
        self.assertNotIn("grace", t["windows"])
        self.assertNotIn("grace", t["price_basis"])
        for p in t["prices"]:
            self.assertNotIn("grace", p)


class AvailabilityTests(unittest.TestCase):
    def test_without_a_rate_only_the_basis_asset_is_offered(self):
        t, _ = terms(fx=None)
        self.assertEqual([p["asset"] for p in t["prices"]], [EURC])

    def test_nothing_priceable_is_an_error_not_an_empty_offer(self):
        with self.assertRaises(PricingUnavailable):
            terms({"X402_ASSETS": "USDC"}, fx=None)


class ConfigValidationTests(unittest.TestCase):
    def test_defaults_are_the_decided_prices(self):
        cfg = pricing.load_config({})
        self.assertEqual((cfg.admission, cfg.sla_unlock, cfg.grace_unlock),
                         (Decimal("0.10"), Decimal("2.90"), Decimal("1.45")))
        self.assertEqual((cfg.sla_seconds, cfg.grace_seconds, cfg.quote_valid_seconds), (3600, 86400, 600))

    def test_rejects_grace_not_longer_than_sla(self):
        for grace in ("3600", "1800"):
            with self.subTest(grace=grace), self.assertRaises(PricingConfigError):
                pricing.load_config({"GRACE_SECONDS": grace})

    def test_rejects_grace_price_above_sla_price(self):
        with self.assertRaises(PricingConfigError):
            pricing.load_config({"GRACE_UNLOCK_EUR": "2.91"})

    def test_grace_price_equal_to_sla_price_is_allowed(self):
        pricing.load_config({"GRACE_UNLOCK_EUR": "2.90"})

    def test_rejects_non_euro_basis_and_bad_amounts(self):
        for env in ({"PRICE_BASIS": "USD"}, {"ADMISSION_EUR": "0"}, {"SLA_UNLOCK_EUR": "abc"},
                    {"SLA_UNLOCK_EUR": "2.905"}, {"QUOTE_VALID_SECONDS": "5"},
                    {"X402_ASSETS": "USDC,DAI"}, {"EURC_ADDRESS": "0x123"}):
            with self.subTest(env=env), self.assertRaises(PricingConfigError):
                pricing.load_config(env)


class FingerprintTests(unittest.TestCase):
    def test_changes_with_config_rate_and_commission(self):
        cfg = pricing.load_config({})
        base = pricing.fingerprint(cfg, RATE_117, Decimal("0.50"))
        self.assertEqual(base, pricing.fingerprint(pricing.load_config({}), RATE_117, Decimal("0.50")))
        other_rate = FxRate(rate=Decimal("1.18"), as_of=RATE_117.as_of, source=RATE_117.source)
        self.assertNotEqual(base, pricing.fingerprint(cfg, other_rate, Decimal("0.50")))
        self.assertNotEqual(base, pricing.fingerprint(pricing.load_config({"SLA_UNLOCK_EUR": "3.00"}),
                                                      RATE_117, Decimal("0.50")))
        self.assertNotEqual(base, pricing.fingerprint(cfg, RATE_117, Decimal("0.40")))


ECB_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                 xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
  <gesmes:subject>Reference rates</gesmes:subject>
  <gesmes:Sender><gesmes:name>European Central Bank</gesmes:name></gesmes:Sender>
  <Cube>
    <Cube time='2026-09-30'>
      <Cube currency='USD' rate='1.1355'/>
      <Cube currency='JPY' rate='168.20'/>
    </Cube>
  </Cube>
</gesmes:Envelope>"""


class EcbParseTests(unittest.TestCase):
    def test_reads_date_and_usd_rate(self):
        as_of, rate = parse_ecb_daily(ECB_SAMPLE)
        self.assertEqual(as_of, date(2026, 9, 30))
        self.assertEqual(rate, Decimal("1.1355"))

    def test_rejects_a_file_without_usd(self):
        with self.assertRaises(FxParseError):
            parse_ecb_daily(ECB_SAMPLE.replace("USD", "GBP"))

    def test_rejects_garbage(self):
        for bad in ("", "<html>maintenance</html>", ECB_SAMPLE.replace("1.1355", "-1")):
            with self.subTest(bad=bad[:20]), self.assertRaises(FxParseError):
                parse_ecb_daily(bad)
