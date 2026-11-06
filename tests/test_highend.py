from __future__ import annotations

import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

from watcher.models import Change, ChangeType, Product
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.highend import parse_feed, parse_price, parse_specs


CHALLENGE = "<html><title>Just a moment...</title><script src='https://challenges.cloudflare.com/x'></script>"


DESCRIPTION = (
    "1 vCore<br />1 G RAM<br />10 GB SSD<br />Unlimited Traffic<br />"
    "300Mbps Shared Network Port<br /><br /><br/><strong>Home broadband, no SLA "
    "guarantee, best-effort basis.<br /></strong>"
)


def feed(value):
    return Mock(status_code=200, text="document.write('%s');" % value)


class FakeSite:
    """Answers productsinfo.php from a {pid: (name, {cycle: price})} table."""

    def __init__(self, products):
        self.products = products
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params))
        pid = int(params["pid"])
        if pid not in self.products:
            return feed("Product ID Not Found")
        name, prices = self.products[pid]
        if params["get"] == "name":
            return feed(name)
        if params["get"] == "description":
            return feed(DESCRIPTION)
        return feed(prices.get(params["billingcycle"], "$-1.00 USD"))


def provider(site, **overrides):
    config = {
        "name": "highend",
        "type": "highend",
        "request_spacing_seconds": 5,
        "discovery_misses": 3,
        "sweep_batch": 2,
        "probe_ahead": 2,
        "far_probe_span": 0,
        "price_batch": 1,
    }
    config.update(overrides)
    instance = build_provider(config)
    instance._sleep = Mock()
    instance.session = site
    return instance


def catalog():
    return {
        2: ("test", {}),
        3: ("HKG.P0.Micro", {"monthly": "$4.99 USD", "annually": "$49.90 USD"}),
        19: ("Seednet.Lavender ", {"quarterly": "$75.00 USD"}),
        20: ("Seednet.Matcha", {"quarterly": "$66.00 USD"}),
    }


class FeedParsingTests(unittest.TestCase):
    def test_unwraps_document_write(self):
        self.assertEqual("Seednet.Matcha", parse_feed("document.write('Seednet.Matcha');"))
        self.assertEqual('a "b"', parse_feed("document.write('a \\\"b\\\"');"))

    def test_rejects_other_replies(self):
        with self.assertRaises(ParseError):
            parse_feed("<html>error</html>")

    def test_disabled_cycle_is_none(self):
        self.assertIsNone(parse_price("$-1.00 USD"))
        self.assertEqual("$75.00 USD", parse_price("$75.00  USD"))


    def test_specs_from_real_description(self):
        self.assertEqual(
            {
                "cpu": "1 vCore",
                "ram": "1 G RAM",
                "disk": "10 GB SSD",
                "traffic": "Unlimited Traffic",
                "bandwidth": "300Mbps Shared Network Port",
            },
            parse_specs(DESCRIPTION),
        )


class DiscoveryTests(unittest.TestCase):
    def test_first_run_walks_until_misses(self):
        site = FakeSite(catalog())
        products = provider(site, discovery_misses=20).fetch_products()

        self.assertEqual(["2", "3", "19", "20"], [p.product_id for p in products])
        lavender = products[2]
        self.assertEqual("Seednet.Lavender", lavender.name)
        self.assertEqual("Seednet", lavender.category)
        self.assertEqual("TW", lavender.region)
        self.assertIsNone(lavender.available)
        self.assertEqual(
            "https://billing.highendnetwork.com/cart.php?a=add&pid=19", lavender.url
        )
        # Names only on the first walk; stops after 20 empty PIDs past 20.
        self.assertTrue(all(call["get"] == "name" for call in site.calls))
        self.assertEqual(40, len(site.calls))

    def test_spaces_requests(self):
        site = FakeSite(catalog())
        instance = provider(site, discovery_misses=20)
        clock = iter(range(0, 1000, 1))
        instance._clock = lambda: next(clock)
        instance.fetch_products()
        waits = [call.args[0] for call in instance._sleep.call_args_list]
        self.assertTrue(waits)
        self.assertTrue(all(wait >= 3 for wait in waits))

    def test_too_few_products_fails(self):
        with self.assertRaises(ParseError):
            provider(FakeSite({}), minimum_products=1).fetch_products()


class IncrementalTests(unittest.TestCase):
    def baseline(self, site):
        instance = provider(site, discovery_misses=20)
        instance.fetch_products()
        site.calls.clear()
        return instance

    def test_new_pid_above_high_water_gets_prices(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        site.products[21] = ("Seednet.Hojicha", {"quarterly": "$59.00 USD", "annually": "$199.00 USD"})

        products = {p.product_id: p for p in instance.fetch_products()}

        new = products["21"]
        self.assertEqual("Seednet.Hojicha", new.name)
        self.assertEqual("$59.00 USD", new.price)
        self.assertEqual("quarterly", new.billing_cycle)
        self.assertEqual({"quarterly": "$59.00 USD", "annually": "$199.00 USD"}, new.pricing)
        self.assertEqual("300Mbps Shared Network Port", new.specs["bandwidth"])
        self.assertEqual(21, instance._high_water)
        self.assertEqual(["21"], instance.last_scan_stats["new_pids"])

    def test_round_request_budget(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        instance.fetch_products()
        # 2 swept names + 2 probes + description + 4 price cycles for one product.
        self.assertEqual(9, len(site.calls))
        for _ in range(3):
            instance.fetch_products()
        site.calls.clear()
        instance.fetch_products()
        # Every product's specs were read once; later refreshes skip them.
        self.assertEqual(8, len(site.calls))

    def test_rotation_covers_every_pid(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        seen = set()
        for _ in range(2):
            site.calls.clear()
            instance.fetch_products()
            seen |= {c["pid"] for c in site.calls if c["get"] == "name" and int(c["pid"]) <= 20}
        self.assertEqual({"2", "3", "19", "20"}, seen)

    def test_rename_is_picked_up(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        site.products[2] = ("Seednet.Matcha.BF", {})
        instance.fetch_products()
        self.assertEqual("Seednet.Matcha.BF", instance._catalog["2"].name)

    def test_removal_needs_two_misses(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        del site.products[2]
        instance.fetch_products()
        self.assertIn("2", instance._catalog)
        instance.fetch_products()
        self.assertNotIn("2", instance._catalog)

    def test_block_keeps_previous_catalog(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        before = dict(instance._catalog)
        site.get = Mock(return_value=Mock(status_code=403, text=CHALLENGE))
        with self.assertRaises(FetchError) as caught:
            instance.fetch_products()
        self.assertTrue(caught.exception.blocked)
        self.assertEqual(1, site.get.call_count)
        self.assertEqual(before, instance._catalog)

    def test_unexpected_reply_fails_round(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        site.get = Mock(return_value=Mock(status_code=200, text="<html>maintenance</html>"))
        with self.assertRaises(ParseError):
            instance.fetch_products()


class FocusRegionTests(unittest.TestCase):
    def baseline(self, site):
        instance = provider(site, focus_regions=["TW"], sweep_batch=10, discovery_misses=20)
        instance.fetch_products()
        site.calls.clear()
        return instance

    def test_only_focus_region_is_reread_and_priced(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        for _ in range(4):
            instance.fetch_products()
        swept = {int(c["pid"]) for c in site.calls if c["get"] == "name" and int(c["pid"]) <= 20}
        priced = {int(c["pid"]) for c in site.calls if c["get"] == "price"}
        self.assertEqual({2, 19, 20}, swept)
        self.assertEqual({19, 20}, priced)

    def test_new_pid_outside_focus_is_silent_and_unpriced(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        site.products[21] = ("HKG.P0.New", {"monthly": "$9.00 USD"})
        site.products[22] = ("Hinet.Sencha", {"annually": "$400.00 USD"})
        instance.fetch_products()
        priced = {int(c["pid"]) for c in site.calls if c["get"] == "price"}
        self.assertNotIn(21, priced)
        self.assertIn(22, priced)
        self.assertFalse(instance.should_notify(Change(ChangeType.NEW, instance._catalog["21"])))
        self.assertTrue(instance.should_notify(Change(ChangeType.NEW, instance._catalog["22"])))

    def test_unclassified_new_pid_still_notifies(self):
        instance = provider(FakeSite({}), focus_regions=["TW"])
        self.assertTrue(instance.should_notify(Change(ChangeType.NEW, Product("highend", "30", "test"))))

    def test_rename_into_focus_reclassifies(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        site.products[2] = ("So-Net.Peony", {})
        instance.fetch_products()
        self.assertEqual("TW", instance._catalog["2"].region)
        self.assertEqual("So-Net", instance._catalog["2"].category)

    def test_unpriced_product_does_not_starve_the_rest(self):
        site = FakeSite(catalog())
        site.products[21] = ("So-Net.Peony", {})
        instance = self.baseline(site)
        picked = []
        for _ in range(6):
            site.calls.clear()
            instance.fetch_products()
            picked.append(next(int(c["pid"]) for c in site.calls if c["get"] == "price"))
        # Rounds 0-1 price the never-priced 19/20; then 21 (still no price)
        # gets every even round and the others the odd ones.
        self.assertEqual([19, 20, 21], picked[:3])
        self.assertEqual(21, picked[4])
        self.assertNotIn(21, (picked[3], picked[5]))


class StateTests(unittest.TestCase):
    def test_restore_state_skips_first_walk(self):
        site = FakeSite(catalog())
        saved = {
            "initialized": True,
            "high_water_pid": 25,
            "products": {
                "highend:20": Product("highend", "20", "Seednet.Matcha").to_dict(),
            },
        }
        instance = provider(site)
        instance.restore_state(saved)
        instance.fetch_products()
        self.assertEqual(25, instance._high_water)
        probed = [int(c["pid"]) for c in site.calls if c["get"] == "name"]
        self.assertIn(26, probed)
        self.assertNotIn(1, probed)

    def test_high_water_saved(self):
        site = FakeSite(catalog())
        instance = provider(site, discovery_misses=20)
        products = instance.fetch_products()
        entry = {}
        instance.update_state_after_success(entry, products)
        self.assertEqual({"high_water_pid": 20}, entry)

    def test_first_price_fill_is_silent(self):
        instance = provider(FakeSite({}))
        old = Product("highend", "3", "HKG.P0.Micro")
        new = Product("highend", "3", "HKG.P0.Micro", price="$4.99 USD", pricing={"monthly": "$4.99 USD"})
        self.assertFalse(instance.should_notify(Change(ChangeType.PRICE, new, old, ["price", "pricing"])))
        newer = Product("highend", "3", "HKG.P0.Micro", price="$3.99 USD", pricing={"monthly": "$3.99 USD"})
        self.assertTrue(instance.should_notify(Change(ChangeType.PRICE, newer, new, ["price", "pricing"])))
        self.assertTrue(instance.should_notify(Change(ChangeType.NEW, new)))

    def test_price_appearing_on_read_unpriced_product_alerts(self):
        instance = provider(FakeSite({}))
        pending = Product("highend", "22", "So-Net.Peony", metadata={"priced_at": 1})
        opened = Product(
            "highend", "22", "So-Net.Peony", price="$240.00 USD", pricing={"annually": "$240.00 USD"}
        )
        self.assertTrue(instance.should_notify(Change(ChangeType.PRICE, opened, pending, ["price", "pricing"])))


if __name__ == "__main__":
    unittest.main()
