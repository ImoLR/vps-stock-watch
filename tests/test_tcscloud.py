from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock

from watcher.diff import compare_products
from watcher.models import Change, ChangeType
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.tcscloud import (
    parse_category,
    parse_order_page,
    parse_specs,
    region_of,
)


FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://billing.tcscloud.net/"
CATEGORIES = {
    "taiwan-vps-dynamic": "tcscloud_dynamic.html",
    "taiwan-vps-fixed": "tcscloud_fixed.html",
    "taipei-premium": "tcscloud_premium.html",
    "taipei-eco": "tcscloud_eco.html",
}
CONFPRODUCT = (FIXTURES / "tcscloud_confproduct.html").read_text()
OUT_OF_STOCK = (FIXTURES / "tcscloud_out_of_stock.html").read_text()


def page(text, url, status=200):
    return Mock(status_code=status, text=text, url=url)


class FakeSite:
    """Serves the saved store pages, a feed table and per-PID order pages."""

    def __init__(self, feed=None, orderable=(), pages=None, retired=(), groups=None, cart_only=()):
        self.pages = {slug: (FIXTURES / name).read_text() for slug, name in CATEGORIES.items()}
        self.pages.update(pages or {})
        self.feed = dict(feed or {})
        self.orderable = set(orderable)
        self.retired = set(retired)
        self.cart_only = set(cart_only)
        self.groups = dict(groups or {})
        self.calls = []
        self.cookies = Mock()

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if "rp=/store/" in url:
            slug = url.split("rp=/store/", 1)[1]
            if slug not in self.pages:
                return page("not found", url, 404)
            return page(self.pages[slug], url)
        if url.endswith("feeds/productsinfo.php"):
            pid = int(params["pid"])
            entry = self.feed.get(pid)
            if entry is None:
                return page("document.write('Product ID Not Found');", url)
            if params["get"] == "name":
                return page("document.write('%s');" % entry[0], url)
            if params["get"] == "description":
                return page("document.write('CPU | 1 vCore<br />\\nRAM | 1 GB<br />');", url)
            return page("document.write('%s');" % entry[1].get(params["billingcycle"], "$-1.00 USD"), url)
        if "cart.php?a=add&pid=" in url:
            pid = int(url.rsplit("=", 1)[1])
            if pid in self.cart_only:
                return page("<html>cart</html>", BASE + "cart.php?a=view")
            if pid in self.orderable:
                return page(CONFPRODUCT, BASE + "cart.php?a=confproduct&i=0")
            if pid in self.retired:
                return page(self.pages["taiwan-vps-dynamic"], BASE + "index.php?rp=/store/taiwan-vps-dynamic")
            group = self.groups.get(pid, "taiwan-vps-dynamic")
            return page(OUT_OF_STOCK, BASE + "index.php?rp=/store/%s/p%d" % (group, pid))
        raise AssertionError("unexpected url %s" % url)

    def feed_calls(self):
        return [params for url, params in self.calls if url.endswith("productsinfo.php")]

    def order_calls(self):
        return [url for url, _ in self.calls if "a=add" in url]


def provider(site, **overrides):
    config = {
        "name": "tcscloud",
        "type": "tcscloud",
        "request_spacing_seconds": 5,
        "feed_start_pid": 100,
        "discovery_misses": 3,
        "probe_ahead": 2,
        "far_probe_span": 0,
        "name_batch": 1,
        "order_check_batch": 2,
        "focus_regions": ["TW"],
        "minimum_categories": 4,
        "minimum_products": 5,
    }
    config.update(overrides)
    instance = build_provider(config)
    instance._sleep = Mock()
    instance._now = Mock(return_value=1_000_000.0)
    instance.session = site
    return instance


def by_id(products):
    return {product.product_id: product for product in products}


class ParsingTests(unittest.TestCase):
    def test_category_cards_carry_stock_count_price_and_specs(self):
        cards, routes = parse_category((FIXTURES / "tcscloud_dynamic.html").read_text(), "taiwan-vps-dynamic")
        cards = {card["pid"]: card for card in cards}
        self.assertEqual(["108", "110", "111", "117"], sorted(cards))
        self.assertEqual(9, cards["110"]["stock"])
        self.assertEqual(0, cards["108"]["stock"])
        self.assertEqual("$59.99 USD", cards["110"]["price"])
        self.assertTrue(cards["110"]["price_from"])
        self.assertEqual("月付", cards["110"]["billing_cycle"])
        self.assertEqual(
            {"taiwan-vps-dynamic", "taiwan-vps-fixed", "taipei-premium", "taipei-eco"},
            set(routes),
        )
        self.assertEqual("台灣VPS - 固定IP", routes["taiwan-vps-fixed"])
        specs = parse_specs(cards["110"]["description"])
        self.assertEqual("2 vCore", specs["cpu"])
        self.assertEqual("HiNet 1000M shared (via 10G XGSPON)", specs["bandwidth"])

    def test_untracked_stock_has_no_count(self):
        cards, _ = parse_category((FIXTURES / "tcscloud_premium.html").read_text(), "taipei-premium")
        self.assertEqual({"109": None, "116": None}, {card["pid"]: card["stock"] for card in cards})

    def test_empty_group_is_a_valid_empty_category(self):
        cards, routes = parse_category((FIXTURES / "tcscloud_eco.html").read_text(), "taipei-eco")
        self.assertEqual([], cards)
        self.assertIn("taiwan-vps-dynamic", routes)

    def test_out_of_stock_page_is_an_error_page_not_an_empty_category(self):
        with self.assertRaises(ParseError):
            parse_category(OUT_OF_STOCK, "taiwan-vps-dynamic")

    def test_order_page_states(self):
        available = parse_order_page(CONFPRODUCT, BASE + "cart.php?a=confproduct&i=0")
        self.assertEqual("available", available["status"])
        self.assertEqual(
            {"月付": "1 個月價格 - $9.99 USD", "年付": "12 個月價格 - $8.33 USD"},
            available["pricing"],
        )
        sold_out = parse_order_page(OUT_OF_STOCK, BASE + "index.php?rp=/store/a/b")
        self.assertEqual("sold_out", sold_out["status"])
        self.assertEqual("a", sold_out["group"])
        retired = parse_order_page(
            (FIXTURES / "tcscloud_dynamic.html").read_text(),
            BASE + "index.php?rp=/store/taiwan-vps-dynamic",
        )
        self.assertEqual("retired", retired["status"])
        cart = parse_order_page("<html>cart</html>", BASE + "cart.php?a=view")
        self.assertEqual("available", cart["status"])
        unknown = parse_order_page("<html>login</html>", BASE + "clientarea.php")
        self.assertEqual("unknown", unknown["status"])

    def test_region_defaults_to_taiwan(self):
        self.assertEqual("TW", region_of("DH2-HINET-1000M DYN 台灣VPS - 動態IP"))
        self.assertEqual("TW", region_of("TPE-P1 B Taipei Premium"))
        self.assertEqual("HK", region_of("HKG-P1 Lite"))
        self.assertEqual("TW", region_of("TPE-PLUS"))


class ScanTests(unittest.TestCase):
    def feed(self):
        return {
            107: ("HINET-DYN-600M", {}),
            108: ("DH1-HINET-500M DYN", {}),
            119: ("DA1-APTG-40M DYN", {"monthly": "$15.99 USD"}),
        }

    def test_first_run_reads_every_category_and_walks_the_feed(self):
        site = FakeSite(self.feed(), orderable={109, 119})
        watcher = provider(site)
        products = by_id(watcher.fetch_products())
        self.assertEqual(
            {"107", "108", "109", "110", "111", "112", "114", "115", "116", "117", "119"},
            set(products),
        )
        self.assertEqual(9, products["110"].stock)
        self.assertTrue(products["110"].available)
        self.assertFalse(products["108"].available)
        self.assertEqual("台灣VPS - 動態IP", products["110"].category)
        self.assertEqual("TW", products["119"].region)
        self.assertTrue(products["119"].metadata["discovery"]["hidden"])
        self.assertFalse(products["110"].metadata["discovery"]["hidden"])
        self.assertEqual(BASE + "cart.php?a=add&pid=119", products["119"].url)
        # Feed walk: 100..106 empty (3 misses stop only past the listed top),
        # listed PIDs skipped, 107/119 found, then 3 misses past 119.
        names = [int(p["pid"]) for p in site.feed_calls() if p["get"] == "name"]
        self.assertEqual([100, 101, 102, 103, 104, 105, 106, 107, 113, 118, 119, 120, 121, 122], names)
        self.assertEqual(122 - 3, watcher.last_scan_stats["high_water_pid"])
        # Every request was spaced.
        self.assertEqual(len(site.calls) - 1, watcher._sleep.call_count)

    def test_order_page_sets_stock_for_untracked_and_unlisted_products(self):
        site = FakeSite(self.feed(), orderable={109, 110, 119})
        watcher = provider(site, order_check_batch=10)
        products = by_id(watcher.fetch_products())
        self.assertTrue(products["109"].available)
        self.assertIsNone(products["109"].stock)
        self.assertEqual("order_page", products["109"].metadata["stock_source"])
        self.assertTrue(products["119"].available)
        self.assertEqual("$9.99 USD", products["119"].price)
        self.assertFalse(products["107"].available)
        self.assertEqual("$59.99 USD", products["110"].price)
        self.assertIn("年付", products["110"].pricing)

    def test_unlisted_product_gets_a_feed_price_even_when_sold_out(self):
        site = FakeSite(self.feed(), orderable=())
        watcher = provider(site)
        watcher.fetch_products()
        products = by_id(watcher.fetch_products())
        self.assertEqual("$15.99 USD", products["119"].price)
        self.assertEqual("月付", products["119"].billing_cycle)
        self.assertEqual("1 vCore", products["119"].specs["cpu"])

    def test_new_direct_link_pid_is_found_and_checked_the_same_round(self):
        site = FakeSite(self.feed(), orderable={109})
        watcher = provider(site)
        before = watcher.fetch_products()
        site.calls.clear()
        site.feed[120] = ("DH3-HINET-PROMO", {"annually": "$199.00 USD"})
        site.orderable.add(120)
        after = watcher.fetch_products()
        self.assertEqual(["120"], watcher.last_scan_stats["new_pids"])
        new = by_id(after)["120"]
        self.assertTrue(new.available)
        self.assertIn(BASE + "cart.php?a=add&pid=120", site.order_calls())
        changes = compare_products(before, after)
        self.assertIn(("120", ChangeType.NEW), [(c.product.product_id, c.type) for c in changes])

    def test_stock_changes_from_the_card(self):
        site = FakeSite(self.feed())
        watcher = provider(site)
        before = watcher.fetch_products()
        site.pages["taiwan-vps-dynamic"] = site.pages["taiwan-vps-dynamic"].replace(
            "0 可用", "3 可用", 1
        )
        after = watcher.fetch_products()
        changes = {(c.product.product_id, c.type) for c in compare_products(before, after)}
        self.assertIn(("108", ChangeType.RESTOCK), changes)

    def test_delisted_product_still_in_feed_becomes_direct_link_only(self):
        site = FakeSite(self.feed())
        watcher = provider(site)
        watcher.fetch_products()
        html = site.pages["taiwan-vps-dynamic"]
        start = html.index('<div class="product clearfix" id="product108">')
        end = html.index('<div class="product clearfix" id="product110">')
        site.pages["taiwan-vps-dynamic"] = html[:start] + html[end:]
        products = by_id(watcher.fetch_products())
        self.assertIn("108", products)
        self.assertFalse(products["108"].metadata["listed"])
        self.assertEqual("1 vCore", products["108"].specs["cpu"])

    def test_unlisted_pid_needs_two_misses_before_removal(self):
        site = FakeSite(self.feed())
        watcher = provider(site, probe_ahead=1)
        watcher.fetch_products()
        del site.feed[107]
        del site.feed[119]
        seen = []
        for _ in range(4):
            seen.append(set(by_id(watcher.fetch_products())))
        self.assertIn("107", seen[0])
        self.assertNotIn("107", seen[-1])
        self.assertNotIn("119", seen[-1])

    def test_feed_work_can_skip_rounds(self):
        site = FakeSite(self.feed())
        watcher = provider(site, feed_every_rounds=2)
        watcher.fetch_products()
        site.calls.clear()
        watcher.fetch_products()  # round 1: categories only
        self.assertEqual([], [p for p in site.feed_calls() if p["get"] == "name"])

    def test_focus_regions_drop_other_markets(self):
        feed = self.feed()
        feed[120] = ("HKG-P1 Lite", {})
        watcher = provider(FakeSite(feed))
        self.assertNotIn("120", by_id(watcher.fetch_products()))

    def test_ignored_pid_is_never_tracked(self):
        feed = self.feed()
        feed[119] = ("TCP-BBR Managed Service", {})
        watcher = provider(FakeSite(feed), ignore_pids=[119])
        self.assertNotIn("119", by_id(watcher.fetch_products()))

    def test_restart_restores_catalog_without_rewalking(self):
        site = FakeSite(self.feed())
        first = provider(site)
        products = first.fetch_products()
        state = {"products": {p.key: p.to_dict() for p in products}}
        first.update_state_after_success(state, products)
        site.calls.clear()
        second = provider(site)
        second.restore_state(state)
        second.fetch_products()
        names = [int(p["pid"]) for p in site.feed_calls() if p["get"] == "name"]
        self.assertLessEqual(len(names), 3)
        self.assertEqual(119, second.last_scan_stats["high_water_pid"])

    def test_block_is_not_retried(self):
        site = FakeSite(self.feed())
        site.pages["taiwan-vps-dynamic"] = "<title>Just a moment...</title> challenges.cloudflare.com"
        watcher = provider(site)
        with self.assertRaises(FetchError) as caught:
            watcher.fetch_products()
        self.assertTrue(caught.exception.blocked)
        self.assertEqual(1, len(site.calls))

    def test_too_few_listed_products_is_rejected(self):
        site = FakeSite(self.feed())
        watcher = provider(site, minimum_products=50)
        with self.assertRaises(ParseError):
            watcher.fetch_products()

    def test_retired_pid_is_not_orderable_and_rechecked_rarely(self):
        site = FakeSite(self.feed(), retired={107})
        watcher = provider(site, order_check_batch=10)
        products = by_id(watcher.fetch_products())
        self.assertFalse(products["107"].available)
        self.assertEqual("retired", products["107"].metadata["order_page"])
        site.calls.clear()
        watcher._now.return_value += 1800
        watcher.fetch_products()
        self.assertNotIn(BASE + "cart.php?a=add&pid=107", site.order_calls())
        watcher._now.return_value += 21600
        site.retired.clear()
        site.orderable.add(107)
        before = watcher._products["107"]
        after = by_id(watcher.fetch_products())["107"]
        self.assertTrue(after.available)
        self.assertFalse(before.available)

    def test_hidden_group_is_learned_from_the_sold_out_redirect(self):
        feed = self.feed()
        feed[88] = ("HINET-FIX-600M", {})
        feed[94] = ("KBT-FIX-2.5G", {})
        site = FakeSite(
            feed,
            pages={"legacy": (FIXTURES / "tcscloud_legacy.html").read_text()},
            groups={88: "legacy", 94: "legacy"},
        )
        watcher = provider(site, feed_start_pid=85, order_check_batch=10)
        first = by_id(watcher.fetch_products())
        self.assertFalse(first["88"].metadata["listed"])
        self.assertEqual(["legacy"], watcher.last_scan_stats["hidden_groups"])
        second = by_id(watcher.fetch_products())
        self.assertTrue(second["88"].metadata["listed"])
        self.assertEqual(0, second["88"].stock)
        self.assertEqual("$120.00 USD", second["94"].price)
        self.assertEqual("legacy（隐藏分组）", second["94"].category)
        self.assertTrue(second["94"].metadata["discovery"]["hidden"])
        self.assertNotIn("88", watcher._unlisted)

    def test_dead_hidden_group_is_dropped_not_fatal(self):
        site = FakeSite(self.feed())
        watcher = provider(site, extra_categories=["gone"])
        watcher.fetch_products()
        self.assertEqual([], watcher.last_scan_stats["hidden_groups"])

    def test_product_without_options_goes_straight_to_the_cart(self):
        site = FakeSite(self.feed(), cart_only={116})
        watcher = provider(site, order_check_batch=10)
        self.assertTrue(by_id(watcher.fetch_products())["116"].available)

    def seednet_only(self, site, **overrides):
        return provider(
            site,
            stock_watch_keywords=["seednet", "seed-", "fet-", "-fet"],
            read_hidden_groups=False,
            order_check_batch=10,
            **overrides
        )

    def test_only_seednet_products_are_watched(self):
        feed = self.feed()
        feed[105] = ("SEEDNET-DYN-300M", {})
        site = FakeSite(feed, orderable={109, 119}, retired={105, 107})
        watcher = self.seednet_only(site, feed_start_pid=104)
        products = by_id(watcher.fetch_products())
        watched = {pid for pid, p in products.items() if p.metadata["stock_watched"]}
        # DS1-SEEDNET (slug s1-fet-...), FS1-SEEDNET and the retired SEEDNET PID.
        self.assertEqual({"105", "111", "112"}, watched)
        self.assertIn(BASE + "cart.php?a=add&pid=105", site.order_calls())
        for pid in ("107", "109", "116", "119"):
            self.assertNotIn(BASE + "cart.php?a=add&pid=%s" % pid, site.order_calls())
        self.assertIsNone(products["109"].available)
        # Card stock is free, so it stays readable for every product.
        self.assertEqual(9, products["110"].stock)

    def test_unwatched_stock_and_price_changes_are_silent(self):
        site = FakeSite(self.feed())
        watcher = self.seednet_only(site)
        before = watcher.fetch_products()
        html = site.pages["taiwan-vps-dynamic"]
        html = html.replace("9 可用", "8 可用", 1).replace("$59.99 USD", "$49.99 USD")
        # DS1-SEEDNET-300M restocks.
        start = html.index('id="product111"')
        html = html[:start] + html[start:].replace("0 可用", "2 可用", 1)
        site.pages["taiwan-vps-dynamic"] = html
        after = watcher.fetch_products()
        sent = {
            (c.product.product_id, c.type)
            for c in compare_products(before, after)
            if watcher.should_notify(c)
        }
        self.assertEqual({("111", ChangeType.RESTOCK)}, sent)

    def test_new_and_removed_products_always_alert(self):
        site = FakeSite(self.feed())
        watcher = self.seednet_only(site)
        before = watcher.fetch_products()
        site.feed[120] = ("DH9-HINET-PROMO", {"monthly": "$9.00 USD"})
        site.orderable.add(120)
        after = watcher.fetch_products()
        changes = [c for c in compare_products(before, after) if watcher.should_notify(c)]
        new = [c for c in changes if c.type == ChangeType.NEW]
        self.assertEqual(["120"], [c.product.product_id for c in new])
        self.assertTrue(new[0].product.available)
        self.assertEqual("$9.00 USD", new[0].product.price)
        removed = Change(ChangeType.REMOVED, before[0], before[0])
        self.assertTrue(watcher.should_notify(removed))

    def test_store_front_moves_alert_for_every_product(self):
        site = FakeSite(self.feed())
        watcher = self.seednet_only(site)
        before = watcher.fetch_products()
        state = {"products": {p.key: p.to_dict() for p in before}}
        html = site.pages["taiwan-vps-dynamic"]
        start = html.index('<div class="product clearfix" id="product110">')
        end = html.index('<div class="product clearfix" id="product117">')
        site.pages["taiwan-vps-dynamic"] = html[:start] + html[end:]
        site.feed[110] = ("DH2-HINET-1000M DYN", {})
        after = watcher.fetch_products()
        moves = watcher.additional_changes(after, state)
        self.assertEqual([("110", ChangeType.REMOVED)], [(c.product.product_id, c.type) for c in moves])
        self.assertEqual(["listing"], moves[0].fields)
        self.assertTrue(watcher.should_notify(moves[0]))
        self.assertIn("110", by_id(after))

    def test_first_order_page_prices_do_not_alert(self):
        site = FakeSite(self.feed(), orderable={109})
        watcher = provider(site, order_check_batch=10)
        before = by_id(watcher.fetch_products())
        old = before["110"]
        old.pricing = {}
        new = by_id(watcher.fetch_products())["110"]
        new.pricing = {"月付": "1 個月價格 - $59.99 USD"}
        change = Change(ChangeType.PRICE, new, old, ["pricing"])
        self.assertFalse(watcher.should_notify(change))


if __name__ == "__main__":
    unittest.main()
