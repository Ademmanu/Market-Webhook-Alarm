import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import core


class StepTests(unittest.TestCase):
    def test_first_observation_never_fires(self):
        self.assertEqual(core.step("above", 100, None, 105, True), (False, True))

    def test_cross_above_fires_once_then_disarms(self):
        self.assertEqual(core.step("above", 100, 99, 101, True), (True, False))
        self.assertEqual(core.step("above", 100, 101, 102, False), (False, False))

    def test_rearm_after_returning(self):
        self.assertEqual(core.step("above", 100, 101, 99, False), (False, True))
        self.assertEqual(core.step("above", 100, 99, 100, True), (True, False))  # touching target counts

    def test_cooldown_blocks_rearm(self):
        self.assertEqual(core.step("above", 100, 101, 99, False, can_rearm=False), (False, False))

    def test_below(self):
        self.assertEqual(core.step("below", 100, 101, 100, True), (True, False))
        self.assertEqual(core.step("below", 100, 99, 101, False), (False, True))

    def test_no_fire_without_crossing(self):
        self.assertEqual(core.step("above", 100, 90, 95, True), (False, True))


class FormatTests(unittest.TestCase):
    EV = {"event": "price_target_hit", "symbol": "XAUUSD", "direction": "above", "target": 2350.5,
          "price": 2350.62, "bid": 2350.52, "ask": 2350.72, "mid": 2350.62, "label": "Gold"}

    def test_fmt_num(self):
        self.assertEqual(core.fmt_num(2350.5), "2350.5")
        self.assertEqual(core.fmt_num(100.0), "100")
        self.assertEqual(core.fmt_num(0), "0")
        self.assertEqual(core.fmt_num(0.00001234), "0.00001234")
        self.assertEqual(core.fmt_num(None), "–")

    def test_json_passthrough(self):
        self.assertIs(core.build_body("json", self.EV), self.EV)

    def test_discord_slack_telegram(self):
        self.assertEqual(core.build_body("discord", self.EV)["content"], "XAUUSD crossed above 2350.5 (now 2350.62)")
        self.assertEqual(core.build_body("slack", self.EV)["text"], "XAUUSD crossed above 2350.5 (now 2350.62)")
        self.assertIn("text", core.build_body("telegram", self.EV))

    def test_template_and_unknown_placeholders(self):
        out = core.build_body("slack", self.EV, "{label}: {symbol} @ {price} {nope}")
        self.assertEqual(out["text"], "Gold: XAUUSD @ 2350.62 {nope}")

    def test_template_cannot_reach_attributes(self):
        out = core.build_body("slack", self.EV, "{symbol.__class__} {0}")
        self.assertNotIn("class 'str'", out["text"])

    def test_test_event_prefix(self):
        self.assertTrue(core.build_body("slack", {**self.EV, "event": "test"})["text"].startswith("TEST: "))

    def test_signature_matches_reference(self):
        # Reference value computed independently with the openssl CLI semantics of HMAC-SHA256.
        import hashlib, hmac
        raw = b'{"a":1}'
        self.assertEqual(core.sign("k", raw), "sha256=" + hmac.new(b"k", raw, hashlib.sha256).hexdigest())

    def test_backoff(self):
        self.assertEqual([core.backoff(n, 5) for n in (1, 2, 3, 4)], [5, 10, 20, 40])
        self.assertEqual(core.backoff(20, 5), 300)


class UrlTests(unittest.TestCase):
    def test_requires_https(self):
        with self.assertRaises(ValueError): core.check_webhook_url("http://example.com/x")
        with self.assertRaises(ValueError): core.check_webhook_url("ftp://example.com/x")
        with self.assertRaises(ValueError): core.check_webhook_url("https:///nohost")

    def test_blocks_private_and_loopback(self):
        for bad in ("https://127.0.0.1/x", "https://10.1.2.3/x", "https://192.168.0.5/x",
                    "https://169.254.169.254/latest/meta-data", "https://[::1]/x"):
            with self.assertRaises(ValueError, msg=bad): core.check_webhook_url(bad)

    def test_allows_public_ip(self):
        self.assertEqual(core.check_webhook_url("https://93.184.216.34/hook"), "https://93.184.216.34/hook")

    def test_dev_mode_relaxes(self):
        core.check_webhook_url("http://127.0.0.1:9000/x", dev=True)

    def test_symbols(self):
        for ok in ("EURUSD", "US30.CASH", "BTCUSD#", "XAU/USD"):
            self.assertTrue(core.SYMBOL_RE.fullmatch(ok), ok)
        for bad in ("", "eur usd", "A" * 41, "EUR;DROP"):
            self.assertFalse(core.SYMBOL_RE.fullmatch(bad), bad)


if __name__ == "__main__":
    unittest.main()
