from __future__ import annotations

import os
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

from watcher.models import Change, ChangeType, Product
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.highend import parse_feed, parse_price, parse_specs, parse_store_page


CHALLENGE = "<html><title>Just a moment...</title><script src='https://challenges.cloudflare.com/x'></script>"


DESCRIPTION = (
    "1 vCore<br />1 G RAM<br />10 GB SSD<br />Unlimited Traffic<br />"
    "300Mbps Shared Network Port<br /><br /><br/><strong>Home broadband, no SLA "
    "guarantee, best-effort basis.<br /></strong>"
)


def feed(value):
    return Mock(status_code=200, text="document.write('%s');" % value)


def fixture(name):
    with open(os.path.join(os.path.dirname(__file__), "fixtures", name), encoding="utf-8") as handle:
        return handle.read()


def store_page(products):
    """A minimal WHMCS category page for {pid: (name, qty or None, link)}."""
    blocks = []
    for pid, (name, qty, link) in products.items():
        qty_html = '<span class="qty">%d Available</span>' % qty if qty is not None else ""
        blocks.append(
            '<div class="product" id="product%s"><header><span id="product%s-name">%s</span>%s</header>'
            '<footer><a href="%s" class="btn" id="product%s-order-button">Order Now</a></footer></div>'
            % (pid, pid, name, qty_html, link, pid)
        )
    return '<div class="products" id="products">%s</div>' % "".join(blocks)


class FakeSite:
    """Answers productsinfo.php from a {pid: (name, {cycle: price})} table,
    and index.php?rp= from ``pages`` {path: html} and ``direct`` {path: in_stock}."""

    def __init__(self, products):
        self.products = products
        self.pages = {}
        self.direct = {}
        self.calls = []

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        self.calls.append(dict(params))
        if "rp" in params:
            path = params["rp"]
            if path in self.pages:
                return Mock(status_code=200, text=self.pages[path], headers={})
            if self.direct.get(path):
                return Mock(
                    status_code=302,
                    text="",
                    headers={"Location": "https://x/cart.php?a=confproduct&i=0"},
                )
            return Mock(status_code=200, text=fixture("highend_out_of_stock.html"), headers={})
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


class WatchTests(unittest.TestCase):
    def baseline(self, site, **overrides):
        options = dict(watch_keywords=["hinet", "seednet"], sweep_batch=10, discovery_misses=20)
        options.update(overrides)
        instance = provider(site, **options)
        instance.fetch_products()
        site.calls.clear()
        return instance

    def test_all_pids_reread_but_only_watched_priced(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        for _ in range(4):
            instance.fetch_products()
        swept = {int(c["pid"]) for c in site.calls if c["get"] == "name" and int(c["pid"]) <= 20}
        priced = {int(c["pid"]) for c in site.calls if c["get"] == "price"}
        self.assertEqual({2, 3, 19, 20}, swept)
        self.assertEqual({19, 20}, priced)

    def test_new_and_removed_alert_for_any_product(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        site.products[21] = ("HKG.P0.New", {"monthly": "$9.00 USD"})
        instance.fetch_products()
        new = instance._catalog["21"]
        self.assertEqual({"monthly": "$9.00 USD"}, new.pricing)
        self.assertTrue(instance.should_notify(Change(ChangeType.NEW, new)))
        self.assertTrue(instance.should_notify(Change(ChangeType.REMOVED, new, new)))

    def test_price_stock_name_alert_only_for_watched(self):
        instance = provider(FakeSite({}), watch_keywords=["hinet", "seednet"])
        hk = Product("highend", "3", "HKG.P0.Micro", stock=1, available=True, pricing={"m": "$1"})
        hk2 = Product("highend", "3", "HKG.P0.Micro", stock=0, available=False, pricing={"m": "$2"})
        tw = Product("highend", "21", "Hinet.Sencha", stock=5, available=True, pricing={"m": "$1"})
        tw2 = Product("highend", "21", "Hinet.Sencha", stock=4, available=True, pricing={"m": "$2"})
        peony = Product("highend", "22", "So-Net.Peony", stock=0, available=False)
        peony2 = Product("highend", "22", "So-Net.Peony", stock=3, available=True)
        for kind in (ChangeType.SOLD_OUT, ChangeType.PRICE, ChangeType.NAME):
            self.assertFalse(instance.should_notify(Change(kind, hk2, hk)))
        self.assertFalse(instance.should_notify(Change(ChangeType.RESTOCK, peony2, peony)))
        self.assertTrue(instance.should_notify(Change(ChangeType.STOCK, tw2, tw, ["stock"])))
        self.assertTrue(instance.should_notify(Change(ChangeType.PRICE, tw2, tw, ["pricing"])))
        renamed = Product("highend", "2", "Seednet.Matcha")
        self.assertTrue(instance.should_notify(Change(ChangeType.NAME, renamed, Product("highend", "2", "test"))))

    def test_watched_only_limits_every_alert_and_the_sweep(self):
        site = FakeSite(catalog())
        instance = self.baseline(site, watch_keywords=["seednet"], watched_only=True)
        site.products[21] = ("Hinet.Sencha", {"annually": "$400.00 USD"})
        site.products[22] = ("Seednet.Hojicha", {"annually": "$200.00 USD"})
        for _ in range(4):
            instance.fetch_products()
        swept = {int(c["pid"]) for c in site.calls if c["get"] == "name" and int(c["pid"]) <= 20}
        priced = {int(c["pid"]) for c in site.calls if c["get"] == "price"}
        self.assertEqual({2, 19, 20}, swept)
        self.assertNotIn(21, priced)
        self.assertIn(22, priced)
        hinet, seednet = instance._catalog["21"], instance._catalog["22"]
        for kind in (ChangeType.NEW, ChangeType.REMOVED):
            self.assertFalse(instance.should_notify(Change(kind, hinet, hinet)))
            self.assertTrue(instance.should_notify(Change(kind, seednet, seednet)))
        listing = Change(ChangeType.NEW, hinet, hinet, ["listing"])
        self.assertFalse(instance.should_notify(listing))

    def test_rename_reclassifies(self):
        site = FakeSite(catalog())
        instance = self.baseline(site)
        site.products[2] = ("So-Net.Peony", {})
        instance.fetch_products()
        self.assertEqual("TW", instance._catalog["2"].region)
        self.assertEqual("So-Net", instance._catalog["2"].category)

    def test_unpriced_product_does_not_starve_the_rest(self):
        site = FakeSite(catalog())
        site.products[21] = ("Hinet.Pending", {})
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


class StoreTests(unittest.TestCase):
    PROMO = "/store/promotion"
    TPE = "/store/tpe-home-broadband"
    MATCHA = "/store/promotion/tpe-seednet-matcha"

    def site(self):
        site = FakeSite(catalog())
        site.products[21] = ("Hinet.Sencha", {"annually": "$400.00 USD"})
        site.pages[self.PROMO] = store_page({21: ("Hinet.Sencha", 5, "/store/promotion/tpe-hinet-sencha")})
        site.pages[self.TPE] = store_page({19: ("Seednet.Lavender", 0, "/store/tpe-home-broadband/tpe-seednet-lavender")})
        return site

    def provider(self, site, **overrides):
        options = dict(
            watch_keywords=["hinet", "seednet"],
            discovery_misses=20,
            store_pages=[self.TPE, self.PROMO],
            direct_links={20: self.MATCHA},
            feed_every_rounds=5,
        )
        options.update(overrides)
        return provider(site, **options)

    def test_parses_real_pages(self):
        promo = parse_store_page(fixture("highend_promotion.html"))
        self.assertEqual({"21", "22"}, set(promo))
        self.assertEqual(5, promo["21"]["stock"])
        self.assertEqual("Hinet.Sencha", promo["21"]["name"])
        self.assertEqual("/store/promotion/tpe-hinet-sencha", promo["21"]["link"])
        self.assertEqual(0, promo["22"]["stock"])
        tpe = parse_store_page(fixture("highend_tpe.html"))
        self.assertEqual({"16", "17", "18", "19"}, set(tpe))
        self.assertTrue(all(item["stock"] == 0 for item in tpe.values()))
        with self.assertRaises(ParseError):
            parse_store_page(fixture("highend_out_of_stock.html"))

    def test_stock_from_pages_and_direct_link(self):
        site = self.site()
        instance = self.provider(site)
        products = {p.product_id: p for p in instance.fetch_products()}
        self.assertEqual((5, True), (products["21"].stock, products["21"].available))
        self.assertEqual(self.PROMO, products["21"].metadata["listed_in"])
        self.assertEqual(
            "https://billing.highendnetwork.com/store/promotion/tpe-hinet-sencha", products["21"].url
        )
        self.assertEqual((0, False), (products["19"].stock, products["19"].available))
        self.assertEqual((None, False), (products["20"].stock, products["20"].available))
        self.assertIsNone(products["3"].available)
        site.direct[self.MATCHA] = True
        products = {p.product_id: p for p in instance.fetch_products()}
        self.assertTrue(products["20"].available)

    def test_store_every_round_feed_every_fifth(self):
        site = self.site()
        instance = self.provider(site)
        instance.fetch_products()
        rounds = []
        for _ in range(5):
            site.calls.clear()
            instance.fetch_products()
            rounds.append(
                (sum("rp" in c for c in site.calls), sum("pid" in c for c in site.calls))
            )
        self.assertTrue(all(store == 3 for store, _ in rounds))
        self.assertEqual([0, 0, 0, 0], [feed for _, feed in rounds[:4]])
        self.assertGreater(rounds[4][1], 0)

    def test_new_pid_on_store_page_is_picked_up_at_once(self):
        site = self.site()
        instance = self.provider(site)
        instance.fetch_products()
        site.products[23] = ("Seednet.Hojicha", {"annually": "$200.00 USD"})
        site.pages[self.PROMO] = store_page({
            21: ("Hinet.Sencha", 5, "/store/promotion/tpe-hinet-sencha"),
            23: ("Seednet.Hojicha", 2, "/store/promotion/tpe-seednet-hojicha"),
        })
        products = {p.product_id: p for p in instance.fetch_products()}
        self.assertEqual(2, products["23"].stock)
        self.assertEqual({"annually": "$200.00 USD"}, products["23"].pricing)
        self.assertEqual(23, instance._high_water)

    def test_listing_moves_alert(self):
        site = self.site()
        instance = self.provider(site)
        before = instance.fetch_products()
        state = {"products": {p.key: p.to_dict() for p in before}}
        site.pages[self.PROMO] = store_page({
            3: ("HKG.P0.Micro", 1, "/store/promotion/hkg-micro"),
        })
        after = instance.fetch_products()
        changes = {(c.product.product_id, c.type) for c in instance.additional_changes(after, state)}
        self.assertEqual({("3", ChangeType.NEW), ("21", ChangeType.REMOVED)}, changes)
        for change in instance.additional_changes(after, state):
            self.assertEqual(["listing"], change.fields)
            self.assertTrue(instance.should_notify(change))

    def test_no_listing_alerts_on_first_store_round(self):
        site = self.site()
        instance = self.provider(site)
        old = Product("highend", "21", "Hinet.Sencha")
        state = {"products": {old.key: old.to_dict()}}
        after = instance.fetch_products()
        self.assertEqual([], instance.additional_changes(after, state))

    def test_challenged_store_page_is_a_block(self):
        site = self.site()
        instance = self.provider(site)
        instance.fetch_products()
        site.pages[self.PROMO] = CHALLENGE
        with self.assertRaises(FetchError) as caught:
            instance.fetch_products()
        self.assertTrue(caught.exception.blocked)

    def test_unexpected_direct_reply_fails_round(self):
        site = self.site()
        instance = self.provider(site)
        instance.fetch_products()
        site.pages[self.MATCHA] = "<html>maintenance</html>"
        with self.assertRaises(ParseError):
            instance.fetch_products()


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
