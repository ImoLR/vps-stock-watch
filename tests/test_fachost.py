from __future__ import annotations

import html
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.app import WatcherApp
from watcher.models import Product
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.fachost import FachostProvider, FetchedDocument


def category_html(slug, categories, products):
    category_ids = [item[2] for item in categories]
    category_id = next(item[2] for item in categories if item[0] == slug)
    product_ids = [item["id"] for item in products]
    snapshot = {
        "data": {
            "products": [None, {"keys": product_ids}],
            "categories": [None, {"keys": category_ids}],
            "childCategories": [None, {"keys": []}],
            "category": [None, {"key": category_id}],
        },
        "memo": {"path": "products/%s" % slug},
    }
    currency = {
        "data": {"currentCurrency": "USD"},
        "memo": {"path": "products/%s" % slug},
    }
    navigation = "".join(
        '<a href="https://fachost.cloud/products/%s"><span>%s</span></a>'
        % (category_slug, label)
        for category_slug, label, _category_id in categories
    )
    cards = []
    for item in products:
        stock_text = item.get("stock_text", "Sold out")
        soldout = "sold out" in stock_text.casefold()
        buy = (
            ""
            if soldout
            else '<a class="fh-product-buy" href="/products/%s/%s/checkout">Order now</a>'
            % (slug, item["product_slug"])
        )
        cards.append(
            """
            <article class="fh-product-card%s">
              <div class="fh-product-top">
                <div><span class="fh-product-type">KVM VIRTUAL SERVER</span>
                <h2>%s</h2></div><span class="fh-stock">%s</span>
              </div>
              <div class="fh-product-price"><strong>%s</strong><span>/ month</span></div>
              <dl class="fh-product-specs">
                <div><dt>CPU</dt><dd>2 vCore</dd></div>
                <div><dt>Memory</dt><dd>2 GB</dd></div>
                <div><dt>Storage</dt><dd>20 GB SSD</dd></div>
                <div><dt>Bandwidth</dt><dd>500 Mbps Shared</dd></div>
                <div><dt>Traffic</dt><dd>5 TB / week</dd></div>
                <div><dt>Network</dt><dd>1× IPv4+1× IPv6</dd></div>
              </dl>
              <a class="fh-details-link" href="/products/%s/%s">View plan details</a>
              %s
            </article>
            """
            % (
                " is-sold-out" if soldout else "",
                item["name"],
                stock_text,
                item.get("price", "$37,99"),
                slug,
                item["product_slug"],
                buy,
            )
        )
    return """
    <html><body>
      <div wire:name="components.currency-switch" wire:snapshot="%s"></div>
      <div wire:name="products" wire:snapshot="%s" class="fh-shop-layout">
        <aside class="fh-category-panel"><nav>%s</nav></aside>
        <main class="fh-shop-main">
          <header class="fh-shop-header"><span class="fh-kicker">TAIWAN INFRASTRUCTURE</span>
          <h1>%s</h1><div class="fh-category-description">Public KVM plans</div></header>
          <div class="fh-product-grid">%s</div>
        </main>
      </div>
    </body></html>
    """ % (
        html.escape(json.dumps(currency), quote=True),
        html.escape(json.dumps(snapshot), quote=True),
        navigation,
        next(item[1] for item in categories if item[0] == slug),
        "".join(cards),
    )


def checkout_html(product_id=22, category_id=1, name="Hinet-VDS-Lite"):
    snapshot = {
        "data": {
            "product": [None, {"key": product_id}],
            "category": [None, {"key": category_id}],
            "plan": [None, {"key": 71}],
            "plan_id": 71,
            "total": [
                {
                    "currency": {"code": "USD"},
                    "formatted": {"price": "$37,99", "total": "$40,99"},
                },
                {"s": "price"},
            ],
        },
        "memo": {"path": "products/tw-hinet-vds/hinet-vds-lite/checkout"},
    }
    return """
    <html><body>
      <div wire:name="products.checkout" wire:snapshot="%s">
        <h1>%s</h1>
        <article class="prose"><ul>
          <li><p><strong>CPU</strong>：2 vCore</p></li>
          <li><p><strong>Memory</strong>：2 GB</p></li>
          <li><p><strong>Network</strong>：1× IPv4+1× IPv6</p></li>
        </ul></article>
        <select id="plan_id"><option value="71">月付 - $37,99</option></select>
        <button wire:click="checkout">Checkout</button>
      </div>
    </body></html>
    """ % (html.escape(json.dumps(snapshot), quote=True), name)


def fachost_product(product_id="1", stock=0, available=False, price="$10 / month", name="Plan"):
    return Product(
        provider="fachost",
        product_id=product_id,
        name=name,
        category="TW-Test-VDS",
        region="Taiwan · Test",
        price=price,
        billing_cycle="monthly",
        stock=stock,
        available=available,
        url="https://fachost.cloud/products/tw-test-vds/plan-%s" % product_id,
    )


class FakeTelegram:
    chat_id = "123"

    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}


class FachostProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = FachostProvider(
            {"name": "fachost", "type": "fachost", "retry_backoff_seconds": 0}
        )
        self.categories = [
            ("tw-test-vds", "TW-Test-VDS", 10),
            ("hk-test-vds", "HK-Test-VDS", 20),
        ]

    def test_parses_real_product_id_numeric_stock_price_specs_and_checkout(self):
        page = category_html(
            "tw-test-vds",
            self.categories,
            [
                {
                    "id": 43,
                    "name": "TEST Plan",
                    "product_slug": "test-plan",
                    "stock_text": "9 available",
                }
            ],
        )
        products = self.provider.parse_category(
            page,
            "https://fachost.cloud/products/tw-test-vds",
            "tw-test-vds",
        )
        self.assertEqual(len(products), 1)
        item = products[0]
        self.assertEqual(item.key, "fachost:43")
        self.assertEqual(item.stock, 9)
        self.assertTrue(item.available)
        self.assertEqual(item.price, "$37,99 / month")
        self.assertEqual(item.billing_cycle, "monthly")
        self.assertEqual(item.metadata["currency"], "USD")
        self.assertEqual(item.metadata["category_id"], "10")
        self.assertEqual(item.metadata["product_slug"], "test-plan")
        self.assertEqual(item.specs["cpu"], "2 vCore")
        self.assertEqual(item.specs["ram"], "2 GB")
        self.assertEqual(item.specs["disk"], "20 GB SSD")
        self.assertEqual(item.specs["ipv4"], "1× IPv4")
        self.assertEqual(item.specs["ipv6"], "1× IPv6")
        self.assertEqual(
            item.url,
            "https://fachost.cloud/products/tw-test-vds/test-plan/checkout",
        )

    def test_boolean_only_inventory_is_not_fabricated(self):
        page = category_html(
            "tw-test-vds",
            self.categories,
            [
                {
                    "id": 44,
                    "name": "Unlimited Plan",
                    "product_slug": "unlimited-plan",
                    "stock_text": "Available",
                }
            ],
        )
        item = self.provider.parse_category(
            page,
            "https://fachost.cloud/products/tw-test-vds",
            "tw-test-vds",
        )[0]
        self.assertTrue(item.available)
        self.assertIsNone(item.stock)

    def test_sold_out_is_boolean_without_inventing_zero(self):
        page = category_html(
            "tw-test-vds",
            self.categories,
            [{"id": 45, "name": "Plan", "product_slug": "plan"}],
        )
        item = self.provider.parse_category(
            page,
            "https://fachost.cloud/products/tw-test-vds",
            "tw-test-vds",
        )[0]
        self.assertFalse(item.available)
        self.assertIsNone(item.stock)
        self.assertEqual(item.metadata["stock_source"], "category_card.sold_out")

    def test_public_hidden_checkout_uses_real_product_id_and_boolean_stock(self):
        item = self.provider.parse_checkout(
            checkout_html(),
            "https://fachost.cloud/products/tw-hinet-vds/hinet-vds-lite/checkout",
            {"tw-hinet-vds": {"labels": ["TW-Hinet-VDS"]}},
        )
        self.assertEqual(item.product_id, "22")
        self.assertEqual(item.name, "Hinet-VDS-Lite")
        self.assertEqual(item.category, "TW-Hinet-VDS")
        self.assertEqual(item.price, "$37,99")
        self.assertEqual(item.billing_cycle, "monthly")
        self.assertTrue(item.available)
        self.assertIsNone(item.stock)
        self.assertTrue(item.metadata["hidden_public"])
        self.assertEqual(
            item.metadata["discovery"],
            {
                "type": "extra_url",
                "hidden": True,
                "source": "extra_product_urls",
            },
        )
        self.assertEqual(item.specs["ipv4"], "1× IPv4")
        self.assertEqual(item.specs["ipv6"], "1× IPv6")

    def test_full_discovery_follows_new_categories_and_deduplicates_real_id(self):
        home = '<a href="/products/tw-test-vds">TW-Test-VDS</a>'
        first = category_html(
            "tw-test-vds",
            self.categories,
            [{"id": 43, "name": "Plan 43", "product_slug": "plan-43"}],
        )
        second = category_html(
            "hk-test-vds",
            self.categories,
            [
                {"id": 43, "name": "Plan 43", "product_slug": "plan-43"},
                {"id": 44, "name": "Plan 44", "product_slug": "plan-44"},
            ],
        )
        documents = {
            self.provider.BASE_URL: FetchedDocument(
                self.provider.BASE_URL, self.provider.BASE_URL, home
            ),
            "https://fachost.cloud/products/tw-test-vds": FetchedDocument(
                "https://fachost.cloud/products/tw-test-vds",
                "https://fachost.cloud/products/tw-test-vds",
                first,
            ),
            "https://fachost.cloud/products/hk-test-vds": FetchedDocument(
                "https://fachost.cloud/products/hk-test-vds",
                "https://fachost.cloud/products/hk-test-vds",
                second,
            ),
        }
        with patch.object(
            self.provider, "_fetch_document", side_effect=lambda url: documents[url]
        ):
            products = self.provider.fetch_products()
        self.assertEqual([item.product_id for item in products], ["43", "44"])
        self.assertEqual(self.provider.last_scan_stats["categories"], 2)
        self.assertEqual(self.provider.last_scan_stats["duplicate_product_ids"], 1)
        self.assertEqual(
            {item["slug"] for item in products[0].metadata["categories"]},
            {"tw-test-vds", "hk-test-vds"},
        )

    def test_incomplete_id_to_card_mapping_is_rejected(self):
        page = category_html(
            "tw-test-vds",
            self.categories,
            [{"id": 43, "name": "Plan", "product_slug": "plan"}],
        ).replace("<article class=", "<section class=", 1)
        with self.assertRaises(ParseError):
            self.provider.parse_category(
                page,
                "https://fachost.cloud/products/tw-test-vds",
                "tw-test-vds",
            )

    def test_provider_factory_builds_fachost(self):
        provider = build_provider({"name": "fachost", "type": "fachost"})
        self.assertIsInstance(provider, FachostProvider)

    def test_removed_extra_checkout_404_does_not_invalidate_visible_catalog(self):
        provider = FachostProvider(
            {
                "name": "fachost",
                "type": "fachost",
                "retry_backoff_seconds": 0,
                "extra_product_urls": [
                    "https://fachost.cloud/products/tw-hinet-vds/hinet-vds-lite/checkout"
                ],
            }
        )
        categories = [("tw-test-vds", "TW-Test-VDS", 10)]
        home = '<a href="/products/tw-test-vds">TW-Test-VDS</a>'
        category = category_html(
            "tw-test-vds",
            categories,
            [{"id": 43, "name": "Visible", "product_slug": "visible"}],
        )

        def fetch(url):
            if url == provider.BASE_URL:
                return FetchedDocument(url, url, home)
            if url.endswith("/tw-test-vds"):
                return FetchedDocument(url, url, category)
            raise FetchError("HTTP 404", status=404)

        with patch.object(provider, "_fetch_document", side_effect=fetch):
            products = provider.fetch_products()
        self.assertEqual([item.product_id for item in products], ["43"])
        self.assertEqual(provider.last_scan_stats["hidden_public_products"], 0)


class FachostLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.directory.name) / "state.json")

    def tearDown(self):
        self.directory.cleanup()

    def app(self):
        app = WatcherApp(
            {
                "state_path": self.state_path,
                "poll_interval_seconds": 60,
                "providers": [
                    {"name": "fachost", "type": "fachost", "interval_seconds": 60}
                ],
            },
            notifications=False,
        )
        app.telegram = FakeTelegram()
        return app

    def exercise(self, old, new):
        app = self.app()
        provider = app.providers[0]
        snapshots = iter((old, new))
        provider.fetch_products = lambda: next(snapshots)
        self.assertTrue(app.check(provider))
        self.assertEqual(app.telegram.sent, [])
        self.assertTrue(app.check(provider))
        return app

    def test_first_complete_snapshot_is_silent_baseline(self):
        app = self.app()
        provider = app.providers[0]
        provider.fetch_products = lambda: [fachost_product("1"), fachost_product("2")]
        self.assertTrue(app.check(provider))
        self.assertEqual(app.telegram.sent, [])
        self.assertEqual(len(app.store.provider("fachost")["products"]), 2)

    def test_zero_to_available_restock_notifies(self):
        app = self.exercise(
            [fachost_product(stock=0, available=False)],
            [fachost_product(stock=3, available=True)],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("🔥 FACHOST 补货", app.telegram.sent[0][0])

    def test_available_to_zero_sold_out_notifies(self):
        app = self.exercise(
            [fachost_product(stock=3, available=True)],
            [fachost_product(stock=0, available=False)],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("🔴 FACHOST 售罄", app.telegram.sent[0][0])

    def test_arbitrary_numeric_stock_change_notifies(self):
        app = self.exercise(
            [fachost_product(stock=5, available=True)],
            [fachost_product(stock=2, available=True)],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("📦 FACHOST 库存变化", app.telegram.sent[0][0])

    def test_new_public_product_notifies_even_when_sold_out(self):
        app = self.exercise(
            [fachost_product("1")],
            [fachost_product("1"), fachost_product("2", name="TEST temporary VPS")],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("🆕 FACHOST 发现新品", app.telegram.sent[0][0])
        self.assertIn("疑似测试商品", app.telegram.sent[0][0])

    def test_price_change_notifies(self):
        app = self.exercise(
            [fachost_product(price="$10 / month")],
            [fachost_product(price="$12 / month")],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("💰 FACHOST 价格变化", app.telegram.sent[0][0])

    def test_removed_product_after_complete_snapshot_notifies(self):
        app = self.exercise(
            [fachost_product("1"), fachost_product("2")],
            [fachost_product("1")],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("🗑 FACHOST 商品下架", app.telegram.sent[0][0])

    def test_incomplete_catalog_preserves_state_without_removed_notification(self):
        app = self.app()
        provider = app.providers[0]
        provider.fetch_products = lambda: [fachost_product("1"), fachost_product("2")]
        self.assertTrue(app.check(provider))
        provider.fetch_products = lambda: (_ for _ in ()).throw(
            FetchError("category request timed out")
        )
        self.assertFalse(app.check(provider))
        self.assertEqual(app.telegram.sent, [])
        self.assertEqual(
            set(app.store.provider("fachost")["products"]),
            {"fachost:1", "fachost:2"},
        )

    def test_interval_persists_and_status_and_menu_include_fachost(self):
        app = self.app()
        app.set_provider_interval("fachost", 17)
        restarted = self.app()
        self.assertEqual(restarted.provider_interval("fachost"), 17)
        restarted.store.record_success("fachost", [fachost_product("1")])
        restarted.store.save()
        restarted.send_status_menu()
        status = restarted.telegram.sent[-1][0]
        self.assertIn("<b>FACHOST</b>", status)
        self.assertIn("商品：1", status)
        self.assertIn("扫描间隔：17 秒", status)
        restarted._send_interval_menu()
        menu, markup = restarted.telegram.sent[-1]
        self.assertIn("FACHOST：17 秒", menu)
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "FACHOST")


if __name__ == "__main__":
    unittest.main()
