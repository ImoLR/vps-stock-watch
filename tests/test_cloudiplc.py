from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from watcher.models import Change, ChangeType, Product
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.cloudiplc import CloudiplcProvider, parse_order_page


FIXTURES = Path(__file__).parent / "fixtures"
CART = (FIXTURES / "cloudiplc_cart.html").read_text(encoding="utf-8")
GID14 = (FIXTURES / "cloudiplc_gid14.html").read_text(encoding="utf-8")
GID8 = (FIXTURES / "cloudiplc_gid8.html").read_text(encoding="utf-8")
CONFPRODUCT = (FIXTURES / "cloudiplc_confproduct.html").read_text(encoding="utf-8")
DIRECT_CONFPRODUCT = (FIXTURES / "cloudiplc_direct_confproduct.html").read_text(encoding="utf-8")
OUT_OF_STOCK = (FIXTURES / "cloudiplc_out_of_stock.html").read_text(encoding="utf-8")
CHALLENGE = (
    '<script>var _0x49a6=["EO_Bot_Ssid"];document.cookie="__tst_status="+1+"#;";'
    "setTimeout(\"location.href=location.href\",0x4b0);</script>"
)


def response(text, status=200):
    return Mock(status_code=status, text=text, url="https://www.cloudiplc.com/")


def provider(**overrides):
    config = {
        "name": "cloudiplc",
        "type": "cloudiplc",
        "request_spacing_seconds": 5,
        "order_page_check": False,
    }
    config.update(overrides)
    instance = build_provider(config)
    instance._sleep = Mock()
    return instance


class CloudiplcCatalogTests(unittest.TestCase):
    def test_parses_real_iplc_nat_group(self):
        products = provider().parse_group(GID14, "14", "出国-沪日IPLC")

        self.assertEqual(["116", "117", "118", "119"], [p.product_id for p in products])
        first = products[0]
        self.assertEqual("IPLC TY-NAT Rev2-1 [无退款]", first.name)
        self.assertEqual("出国-沪日IPLC", first.category)
        self.assertEqual("上海->东京 [共享IP]", first.region)
        self.assertEqual("￥84.17/月(按年)", first.price)
        self.assertEqual("annually", first.billing_cycle)
        self.assertIsNone(first.available)
        self.assertEqual("https://www.cloudiplc.com/cart.php?a=add&pid=116", first.url)
        self.assertEqual("20 Mbps 峰值(非独享)", first.specs["bandwidth"])
        self.assertEqual("512 MB ECC", first.specs["ram"])
        self.assertEqual("共享IP", first.specs["ipv4"])
        self.assertIn("东京NTT", first.specs["route"])
        self.assertIn("仅开放1W+端口号", first.metadata["remarks"]["备注"])

    def test_explicitly_empty_group_is_allowed(self):
        self.assertEqual([], provider().parse_group(GID8, "8"))

    def test_page_without_rows_or_empty_marker_fails(self):
        broken = GID8.replace("产品组不包含任何可见的产品", "")
        with self.assertRaises(ParseError):
            provider().parse_group(broken, "8")

    def test_discovers_groups_including_commented_out_links(self):
        instance = provider()
        self.assertEqual({"3", "8", "13", "14"}, instance._group_ids(CART))
        self.assertEqual("3", instance._active_group(CART))

    def test_full_scan_reuses_store_page_and_spaces_requests(self):
        instance = provider(minimum_products=1)
        pages = {
            "https://www.cloudiplc.com/cart.php": CART,
            "https://www.cloudiplc.com/cart.php?gid=8": GID8,
            "https://www.cloudiplc.com/cart.php?gid=13": GID8,
            "https://www.cloudiplc.com/cart.php?gid=14": GID14,
        }
        instance.session.get = Mock(side_effect=lambda url, timeout: response(pages[url]))
        clock = iter(range(0, 1000, 1))
        instance._clock = lambda: float(next(clock))

        products = instance.fetch_products()

        requested = [call.args[0] for call in instance.session.get.call_args_list]
        self.assertEqual(4, len(requested))
        self.assertNotIn("https://www.cloudiplc.com/cart.php?gid=3", requested)
        self.assertEqual(
            ["17", "18", "19", "20", "116", "117", "118", "119"],
            [p.product_id for p in products],
        )
        self.assertEqual("中国-泉州CN2", products[0].category)
        self.assertEqual(3, instance._sleep.call_count)
        self.assertTrue(all(call.args[0] >= 4 for call in instance._sleep.call_args_list))

    def test_direct_products_join_catalog_without_extra_requests(self):
        instance = provider(
            minimum_products=1,
            direct_products=[
                {"pid": 125, "name": "限时限量", "category": "活动直链"},
                {"pid": "116", "name": "listed anyway"},
            ],
        )
        pages = {
            "https://www.cloudiplc.com/cart.php": CART,
            "https://www.cloudiplc.com/cart.php?gid=8": GID8,
            "https://www.cloudiplc.com/cart.php?gid=13": GID8,
            "https://www.cloudiplc.com/cart.php?gid=14": GID14,
        }
        instance.session.get = Mock(side_effect=lambda url, timeout: response(pages[url]))

        products = {p.product_id: p for p in instance.fetch_products()}

        self.assertEqual(4, instance.session.get.call_count)
        direct = products["125"]
        self.assertEqual("限时限量", direct.name)
        self.assertEqual("活动直链", direct.category)
        self.assertEqual("https://www.cloudiplc.com/cart.php?a=add&pid=125", direct.url)
        self.assertIsNone(direct.available)
        self.assertEqual("IPLC TY-NAT Rev2-1 [无退款]", products["116"].name)

    def test_edgeone_challenge_fails_without_retry(self):
        instance = provider(retry_attempts=3)
        instance.session.get = Mock(return_value=response(CHALLENGE))
        with self.assertRaises(FetchError) as caught:
            instance.fetch_products()
        self.assertTrue(caught.exception.blocked)
        self.assertEqual(1, instance.session.get.call_count)

    def test_rate_limit_fails_without_retry(self):
        instance = provider(retry_attempts=3)
        instance.session.get = Mock(return_value=response("busy", 429))
        with self.assertRaises(FetchError):
            instance.fetch_products()
        self.assertEqual(1, instance.session.get.call_count)


class CloudiplcOrderPageTests(unittest.TestCase):
    def test_parses_real_confproduct_page(self):
        result = parse_order_page(
            CONFPRODUCT, "https://www.cloudiplc.com/cart.php?a=confproduct&i=0"
        )
        self.assertEqual("available", result["status"])
        self.assertEqual("IPLC TY-NAT Rev2-1 [无退款]", result["name"])
        self.assertEqual("￥189.99RMB", result["total"])
        self.assertEqual(
            {
                "monthly": "￥19.99RMB 每月 + ￥100.00RMB 初装费",
                "annually": "￥239.99RMB 每年 + ￥100.00RMB 初装费",
            },
            result["pricing"],
        )

    def test_parses_real_direct_link_sale_page(self):
        result = parse_order_page(
            DIRECT_CONFPRODUCT, "https://www.cloudiplc.com/cart.php?a=confproduct&i=0"
        )
        self.assertEqual("available", result["status"])
        self.assertEqual("IPLC TY-NAT 限时限量 [无退款]", result["name"])
        self.assertEqual("￥779.99RMB", result["total"])
        self.assertEqual(
            {"semiannually": "￥59.99RMB 半年", "annually": "￥99.99RMB 每年"},
            result["pricing"],
        )

    def test_direct_product_takes_price_from_order_page(self):
        instance = provider(order_page_check=True)
        instance._clock = lambda: 0.0
        order = parse_order_page(
            DIRECT_CONFPRODUCT, "https://www.cloudiplc.com/cart.php?a=confproduct&i=0"
        )
        with patch.object(CloudiplcProvider, "_check_order_pages", return_value={"125": order}):
            products = [Product("cloudiplc", "125", "D", metadata={"direct_link": True})]
            instance._apply_order_pages(products)
        self.assertIs(True, products[0].available)
        self.assertEqual("￥779.99RMB", products[0].price)
        self.assertEqual("semiannually", products[0].billing_cycle)

        instance._order_cache["125"] = (0.0, self._order("sold_out"))
        products = [Product("cloudiplc", "125", "D", metadata={"direct_link": True})]
        instance._apply_order_pages(products)
        self.assertIs(False, products[0].available)
        self.assertIsNone(products[0].price)

    def test_direct_product_price_notifies_only_between_buyable_snapshots(self):
        instance = provider()

        def item(available, price):
            return Product("cloudiplc", "125", "D", price=price, available=available,
                           metadata={"direct_link": True})

        def notifies(old, new):
            return instance.should_notify(Change(ChangeType.PRICE, new, old, ["price"]))

        self.assertFalse(notifies(item(True, "￥779.99RMB"), item(False, None)))
        self.assertFalse(notifies(item(False, None), item(True, "￥779.99RMB")))
        self.assertFalse(notifies(item(None, None), item(True, "￥779.99RMB")))
        self.assertTrue(notifies(item(True, "￥779.99RMB"), item(True, "￥699.99RMB")))

    def test_parses_real_out_of_stock_page(self):
        result = parse_order_page(
            OUT_OF_STOCK, "https://www.cloudiplc.com/cart.php?a=add&pid=30"
        )
        self.assertEqual("sold_out", result["status"])

    def test_challenge_page_is_blocked(self):
        result = parse_order_page(CHALLENGE, "https://www.cloudiplc.com/cart.php?a=add&pid=1")
        self.assertEqual("unknown", result["status"])
        self.assertTrue(result["blocked"])

    def _catalog(self):
        return [
            Product("cloudiplc", "116", "A", price="￥84.17/月(按年)"),
            Product("cloudiplc", "117", "B", price="￥111.67/月(按年)"),
        ]

    def _order(self, status, pricing=None):
        return {"status": status, "pricing": pricing or {}, "total": "￥1", "url": "u"}

    def test_order_pages_cached_until_refresh(self):
        instance = provider(order_page_check=True, order_page_refresh_seconds=7200)
        now = [1000.0]
        instance._clock = lambda: now[0]
        checks = []

        def check(pids):
            checks.append(list(pids))
            return {pid: self._order("available", {"monthly": "x"}) for pid in pids}

        with patch.object(CloudiplcProvider, "_check_order_pages", side_effect=check):
            products = self._catalog()
            instance._apply_order_pages(products)
            self.assertTrue(all(p.available for p in products))
            self.assertEqual({"monthly": "x"}, products[0].pricing)

            now[0] += 300
            products = self._catalog() + [Product("cloudiplc", "120", "C")]
            instance._apply_order_pages(products)
            self.assertTrue(all(p.available for p in products))

            now[0] += 7200
            instance._apply_order_pages(self._catalog())

        self.assertEqual([["116", "117"], ["120"], ["116", "117"]], checks)

    def test_failed_check_keeps_last_state_and_cools_down(self):
        instance = provider(order_page_check=True, order_page_retry_seconds=1800)
        now = [0.0]
        instance._clock = lambda: now[0]
        instance._order_cache["116"] = (-9000.0, self._order("sold_out"))

        with patch.object(
            CloudiplcProvider, "_check_order_pages", side_effect=FetchError("boom")
        ) as check:
            products = self._catalog()
            instance._apply_order_pages(products)
            self.assertIs(False, products[0].available)
            self.assertIsNone(products[1].available)
            now[0] += 300
            instance._apply_order_pages(self._catalog())
            self.assertEqual(1, check.call_count)

    def test_unknown_result_keeps_previous_status(self):
        instance = provider(order_page_check=True)
        instance._clock = lambda: 99999.0
        instance._order_cache["116"] = (0.0, self._order("available"))
        with patch.object(
            CloudiplcProvider,
            "_check_order_pages",
            return_value={"116": self._order("unknown"), "117": self._order("unknown")},
        ):
            products = self._catalog()
            instance._apply_order_pages(products)
        self.assertIs(True, products[0].available)
        self.assertIsNone(products[1].available)

    def test_first_order_pricing_is_silent(self):
        instance = provider()
        old = Product("cloudiplc", "116", "A")
        new = Product("cloudiplc", "116", "A", pricing={"monthly": "x"})
        self.assertFalse(instance.should_notify(Change(ChangeType.PRICE, new, old, ["pricing"])))
        changed = Product("cloudiplc", "116", "A", pricing={"monthly": "y"})
        self.assertTrue(instance.should_notify(Change(ChangeType.PRICE, changed, new, ["pricing"])))


if __name__ == "__main__":
    unittest.main()


class CloudiplcHiddenLinkTests(unittest.TestCase):
    PAGES = {
        "https://www.cloudiplc.com/cart.php": CART,
        "https://www.cloudiplc.com/cart.php?gid=8": GID8,
        "https://www.cloudiplc.com/cart.php?gid=13": GID8,
        "https://www.cloudiplc.com/cart.php?gid=14": GID14,
    }

    def _provider(self, feed, **overrides):
        instance = provider(minimum_products=1, hidden_probe=True, hidden_probe_ahead=3, **overrides)

        def get(url, timeout):
            if "productsinfo.php" in url:
                pid = url.split("pid=")[1].split("&")[0]
                reply = feed.get(pid, "Product ID Not Found")
                if isinstance(reply, int):
                    return response("blocked", reply)
                return response("document.write('%s');" % reply)
            return response(self.PAGES[url])

        instance.session.get = Mock(side_effect=get)
        return instance

    def _feed_urls(self, instance):
        return [
            call.args[0].split("pid=")[1].split("&")[0]
            for call in instance.session.get.call_args_list
            if "productsinfo.php" in call.args[0]
        ]

    def test_probe_above_highest_pid_adds_unlisted_pid_as_hidden_link(self):
        instance = self._provider({"121": "双十二 IPLC 限时 &amp; 限量"})

        products = {p.product_id: p for p in instance.fetch_products()}

        self.assertEqual(["120", "121", "122"], self._feed_urls(instance))
        hidden = products["121"]
        self.assertEqual("双十二 IPLC 限时 & 限量", hidden.name)
        self.assertEqual("隐藏链接", hidden.category)
        self.assertEqual("https://www.cloudiplc.com/cart.php?a=add&pid=121", hidden.url)
        self.assertTrue(hidden.metadata["hidden_direct_link"])
        self.assertEqual(121, instance._high_water)
        self.assertEqual(["121"], instance.last_scan_stats["hidden_found"])
        self.assertEqual(7, instance.last_scan_stats["requests"])

    def test_probe_starts_above_baseline_pid(self):
        instance = self._provider({"150": "old", "161": "new"}, hidden_probe_baseline_pid=160)

        products = {p.product_id for p in instance.fetch_products()}

        self.assertEqual(["161", "162", "163"], self._feed_urls(instance))
        self.assertNotIn("150", products)
        self.assertIn("161", products)
        self.assertEqual(161, instance._high_water)

    def test_probe_runs_every_n_rounds_and_keeps_hidden_links(self):
        instance = self._provider({"120": "test"}, hidden_probe_every_rounds=3)

        rounds = [{p.product_id for p in instance.fetch_products()} for _ in range(4)]

        self.assertTrue(all("120" in pids for pids in rounds))
        self.assertEqual(["120", "121", "122", "121", "122", "123"], self._feed_urls(instance))

    def test_hidden_link_listed_later_is_no_longer_hidden(self):
        instance = self._provider({})
        instance._hidden["116"] = Product("cloudiplc", "116", "x", metadata={"hidden_direct_link": True})

        products = {p.product_id: p for p in instance.fetch_products()}

        self.assertNotIn("hidden_direct_link", products["116"].metadata)
        self.assertEqual({}, instance._hidden)

    def test_blocked_feed_fails_the_scan(self):
        instance = self._provider({"120": 403})
        with self.assertRaises(FetchError) as caught:
            instance.fetch_products()
        self.assertTrue(caught.exception.blocked)
        self.assertEqual(["120"], self._feed_urls(instance))

    def test_broken_feed_reply_stops_probe_but_keeps_catalog(self):
        instance = self._provider({})
        get = instance.session.get.side_effect
        instance.session.get = Mock(
            side_effect=lambda url, timeout: response("<html>oops</html>")
            if "productsinfo.php" in url
            else get(url, timeout)
        )

        products = instance.fetch_products()

        self.assertEqual(8, len(products))
        self.assertEqual(["120"], self._feed_urls(instance))

    def test_restore_state_keeps_hidden_links_and_high_water(self):
        instance = provider(hidden_probe=True)
        hidden = Product("cloudiplc", "150", "sale", metadata={"hidden_direct_link": True})
        instance.restore_state(
            {
                "high_water_pid": 152,
                "products": {
                    "cloudiplc:147": Product("cloudiplc", "147", "PCCW").to_dict(),
                    "cloudiplc:150": hidden.to_dict(),
                },
            }
        )
        self.assertEqual(152, instance._high_water)
        self.assertEqual(["150"], list(instance._hidden))
        state = {}
        instance.update_state_after_success(state, [])
        self.assertEqual({"high_water_pid": 152}, state)

    def test_notify_only_new(self):
        instance = provider(notify_only_new=True)
        product = Product("cloudiplc", "150", "sale", metadata={"hidden_direct_link": True})
        old = Product("cloudiplc", "150", "test", available=False)

        self.assertTrue(instance.should_notify(Change(ChangeType.NEW, product)))
        for kind in (ChangeType.RESTOCK, ChangeType.SOLD_OUT, ChangeType.PRICE,
                     ChangeType.NAME, ChangeType.REMOVED):
            self.assertFalse(instance.should_notify(Change(kind, product, old, [])))
