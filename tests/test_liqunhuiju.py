from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.app import WatcherApp
from watcher.models import Product
from watcher.providers import build_provider
from watcher.providers.base import ParseError
from watcher.providers.liqunhuiju import LiqunHuijuProvider
from watcher.telegram import format_status, format_stock_menu, format_stock_provider


def homepage_html(catalog_link=""):
    link = '<a href="%s">Products</a>' % catalog_link if catalog_link else ""
    return """
    <html><head><title>會聚 · LeiKwan Bridge | 利群主機</title></head>
    <body class="pg-public bridge-home-page"><div class="public-shell">
      <a href="/login">登入</a><a href="/knowledgebase">知識庫</a>%s
    </div></body></html>
    """ % link


def catalog_html(products=None, empty=False):
    cards = []
    for item in products or []:
        stock = item.get("stock")
        stock_attr = ' data-stock="%s"' % stock if stock is not None else ""
        cards.append(
            """
            <article data-product-id="%s" data-category="%s"%s>
              <h3 class="product-name">%s</h3>
              <span class="price">%s</span>
              <span class="billing-cycle">monthly</span>
              <a class="order-button" href="/buy/%s">Buy</a>
            </article>
            """
            % (
                item["id"],
                item.get("category", "Bridge"),
                stock_attr,
                item.get("name", "Plan %s" % item["id"]),
                item.get("price", "HK$10.00"),
                item["id"],
            )
        )
    marker = '<div data-catalog-empty="true">当前没有公开商品</div>' if empty else ""
    return "<html><body><main class=public-catalog>%s%s</main></body></html>" % (
        "".join(cards),
        marker,
    )


def product(product_id="123", stock=2, price="HK$10.00"):
    return Product(
        provider="liqunhuiju",
        product_id=product_id,
        name="Bridge Plan",
        category="Bridge",
        price=price,
        billing_cycle="monthly",
        stock=stock,
        available=stock > 0,
        url="https://v3.leikwanhost.com/buy/%s" % product_id,
        metadata={
            "discovery": {
                "type": "catalog",
                "hidden": False,
                "source": "public_catalog",
            }
        },
    )


class FakeTelegram:
    chat_id = "123"

    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}


class LiqunHuijuProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = LiqunHuijuProvider(
            {"name": "liqunhuiju", "type": "liqunhuiju"}
        )

    def test_current_recognized_landing_page_is_a_valid_empty_catalog(self):
        with patch.object(
            self.provider, "_fetch", return_value=homepage_html()
        ) as fetch:
            products = self.provider.fetch_products()
        self.assertEqual(products, [])
        self.assertEqual(self.provider.last_scan_stats["empty_mode"], "landing_only")
        fetch.assert_called_once_with(self.provider.BASE_URL)

    def test_explicit_empty_catalog_is_valid_but_unknown_empty_layout_fails(self):
        self.assertEqual(
            self.provider.parse_catalog(
                catalog_html(empty=True), "https://v3.leikwanhost.com/products"
            ),
            [],
        )
        with self.assertRaises(ParseError):
            self.provider.parse_catalog(
                "<html><body>unexpected layout</body></html>",
                "https://v3.leikwanhost.com/products",
            )

    def test_product_uses_independent_namespace_numeric_stock_and_direct_url(self):
        parsed = self.provider.parse_catalog(
            catalog_html([{"id": "123", "stock": 3}]),
            "https://v3.leikwanhost.com/products",
        )[0]
        self.assertEqual(parsed.key, "liqunhuiju:123")
        self.assertNotEqual(parsed.key, "leikwanhost:123")
        self.assertEqual(parsed.stock, 3)
        self.assertTrue(parsed.available)
        self.assertEqual(parsed.url, "https://v3.leikwanhost.com/buy/123")

    def test_http_200_unknown_homepage_schema_is_not_a_valid_empty_catalog(self):
        with patch.object(
            self.provider, "_fetch", return_value="<html><body>error page</body></html>"
        ):
            with self.assertRaises(ParseError):
                self.provider.fetch_products()

    def test_provider_factory_builds_liqunhuiju(self):
        built = build_provider({"name": "liqunhuiju", "type": "liqunhuiju"})
        self.assertIsInstance(built, LiqunHuijuProvider)


class LiqunHuijuLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.directory.name) / "state.json")
        self.app = WatcherApp(
            {
                "state_path": self.state_path,
                "poll_interval_seconds": 60,
                "providers": [
                    {
                        "name": "liqunhuiju",
                        "type": "liqunhuiju",
                        "interval_seconds": 60,
                    }
                ],
            },
            notifications=False,
        )
        self.app.telegram = FakeTelegram()
        self.provider = self.app.providers[0]

    def tearDown(self):
        self.directory.cleanup()

    def test_zero_baseline_then_first_product_notifies_as_new(self):
        self.provider.fetch_products = lambda: []
        self.provider.last_scan_stats = {"empty_mode": "landing_only"}
        self.assertTrue(self.app.check(self.provider))
        state = self.app.store.provider("liqunhuiju")
        self.assertTrue(state["initialized"])
        self.assertEqual(state["products"], {})
        self.assertEqual(self.app.telegram.sent, [])

        self.provider.fetch_products = lambda: [product()]
        self.provider.last_scan_stats = {"empty_mode": "explicit_catalog"}
        self.assertTrue(self.app.check(self.provider))
        self.assertIn("🆕 利群汇聚 发现新品", self.app.telegram.sent[-1][0])

    def test_landing_only_empty_after_products_preserves_state_without_removed(self):
        self.app.store.record_success("liqunhuiju", [])
        self.app.store.record_success("liqunhuiju", [product()])
        self.app.store.save()
        self.provider.fetch_products = lambda: []
        self.provider.last_scan_stats = {"empty_mode": "landing_only"}
        self.assertFalse(self.app.check(self.provider))
        self.assertIn("liqunhuiju:123", self.app.store.provider("liqunhuiju")["products"])
        self.assertEqual(self.app.telegram.sent, [])

    def test_explicit_complete_catalog_can_remove_last_product(self):
        self.app.store.record_success("liqunhuiju", [])
        self.app.store.record_success("liqunhuiju", [product()])
        self.app.store.save()
        self.provider.fetch_products = lambda: []
        self.provider.last_scan_stats = {"empty_mode": "explicit_catalog"}
        self.assertTrue(self.app.check(self.provider))
        self.assertEqual(self.app.store.provider("liqunhuiju")["products"], {})
        self.assertIn("🗑 利群汇聚 商品下架", self.app.telegram.sent[-1][0])

    def test_status_stock_and_interval_support_healthy_zero_products(self):
        self.app.store.record_success("liqunhuiju", [])
        self.app.store.save()
        self.app.set_provider_interval("liqunhuiju", 17)
        state = self.app.store.snapshot()
        status = format_status(state, self.app.provider_intervals())
        self.assertIn("<b>利群汇聚</b>", status)
        self.assertIn("商品：0", status)
        self.assertIn("有货：0", status)
        self.assertIn("扫描间隔：17 秒", status)
        self.assertIn("利群汇聚：0", format_stock_menu(state, ["liqunhuiju"]))
        stock = format_stock_provider(state, "liqunhuiju", 17)[0]
        self.assertIn("当前暂无可购买库存", stock)
        self.assertIn("监控商品：0", stock)


if __name__ == "__main__":
    unittest.main()
