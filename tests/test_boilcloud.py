from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.app import WatcherApp
from watcher.models import Change, ChangeType, Product
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.boilcloud import BoilcloudProvider, FetchedDocument


def category_html(categories, products, current=None, empty=False, group_id=None):
    links = []
    options = []
    for slug, label in categories:
        active = " active" if slug == current else ""
        gid = ' data-gid="%s"' % group_id if slug == current and group_id else ""
        selected = " selected" if slug == current else ""
        links.append(
            '<a class="list-group-item%s" menuitemname="%s" href="/store/%s"%s>%s</a>'
            % (active, label, slug, gid, label)
        )
        options.append(
            '<option value="/store/%s"%s>%s</option>' % (slug, selected, label)
        )
    cards = []
    for item in products:
        cards.append(
            """
            <div class="tt-single-product" id="product{pid}">
              <div class="tt-product-name"><h5 id="product{pid}-name">{name}</h5></div>
              <div class="product-pricing"><span class="price">{price}</span>
                <span data-key="monthly">月繳</span></div>
              <span class="qty">{stock} <span data-key="available">可用</span></span>
              <div id="product{pid}-description">
                <strong>{region}</strong><br>
                <strong>{bandwidth}</strong><br>
                <span>2核 | 2G 內存 | 16G SSD</span><br>
                <span>5TB 流量</span><br>
                <span>IPv4 ×1・IPv6 /64</span>
              </div>
              <a class="btn btn-order-now" href="/store/{category}/product-{pid}">立即購買</a>
            </div>
            """.format(
                pid=item["pid"],
                name=item.get("name", "Plan %s" % item["pid"]),
                price=item.get("price", "$10.00 USD"),
                stock=item.get("stock", 0),
                region=item.get("region", "香港"),
                bandwidth=item.get("bandwidth", "500Mbps 共享頻寬"),
                category=current or "catalog",
            )
        )
    empty_text = "產品群組不包含任何可見的產品" if empty else ""
    return """
    <html><body>
      <div id="order-standard_cart">
        <div class="cart-sidebar"><div menuitemname="Categories">{links}</div></div>
        <select>{options}</select>
        <h2 class="font-size-22">Public VPS Products</h2>
        <div class="products">{cards}{empty_text}</div>
      </div>
    </body></html>
    """.format(
        links="".join(links),
        options="".join(options),
        cards="".join(cards),
        empty_text=empty_text,
    )


def boil_product(pid="1", stock=0, price="$10.00 USD", name="Public VPS"):
    return Product(
        provider="boilcloud",
        product_id=pid,
        name=name,
        category="Public",
        price=price,
        billing_cycle="monthly",
        stock=stock,
        available=stock > 0,
        url="https://cloud.boil.network/cart.php?a=add&pid=%s" % pid,
    )


class FakeTelegram:
    chat_id = "123"

    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}


class BoilcloudProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = BoilcloudProvider(
            {"name": "boilcloud", "type": "boilcloud", "retry_backoff_seconds": 0}
        )

    def test_parses_pid_integer_stock_price_specs_and_product_url(self):
        html = category_html(
            [("taiwan-b", "B區台灣家寬")],
            [{"pid": "381", "name": "TEST HiNet VPS", "stock": 3, "region": "台灣"}],
            current="taiwan-b",
            group_id="27",
        )
        products = self.provider.parse_category(
            html,
            "https://cloud.boil.network/store/taiwan-b",
            "taiwan-b",
            "B區台灣家寬",
            "27",
        )
        self.assertEqual(len(products), 1)
        item = products[0]
        self.assertEqual(item.key, "boilcloud:381")
        self.assertEqual(item.stock, 3)
        self.assertTrue(item.available)
        self.assertEqual(item.price, "$10.00 USD")
        self.assertEqual(item.billing_cycle, "monthly")
        self.assertEqual(item.metadata["currency"], "USD")
        self.assertEqual(item.metadata["category_slug"], "taiwan-b")
        self.assertEqual(item.metadata["category_group_id"], "27")
        self.assertEqual(item.specs["cpu"], "2核")
        self.assertEqual(item.specs["ram"], "2G 內存")
        self.assertEqual(item.specs["disk"], "16G SSD")
        self.assertEqual(item.specs["traffic"], "5TB 流量")
        self.assertEqual(item.url, "https://cloud.boil.network/cart.php?a=add&pid=381")
        self.assertTrue(self.provider.should_notify(Change(ChangeType.NEW, item)))

    def test_full_discovery_uses_navigation_sidebar_and_sitemap_and_deduplicates_pid(self):
        categories = [("alpha", "Alpha"), ("beta", "Beta")]
        home = '<html><header><a href="/store/alpha">Alpha</a></header></html>'
        alpha = category_html(categories, [{"pid": "10", "stock": 1}], current="alpha")
        beta = category_html(
            categories,
            [{"pid": "10", "stock": 1}, {"pid": "11", "stock": 0}],
            current="beta",
        )
        sitemap = """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://cloud.boil.network/store/alpha</loc></url>
          <url><loc>https://cloud.boil.network/store/beta</loc></url>
        </urlset>"""
        documents = {
            self.provider.BASE_URL: FetchedDocument(
                self.provider.BASE_URL, self.provider.BASE_URL, home
            ),
            self.provider.STORE_URL: FetchedDocument(
                self.provider.STORE_URL,
                "https://cloud.boil.network/store/alpha",
                alpha,
            ),
            self.provider.SITEMAP_URL: FetchedDocument(
                self.provider.SITEMAP_URL, self.provider.SITEMAP_URL, sitemap
            ),
            "https://cloud.boil.network/store/beta": FetchedDocument(
                "https://cloud.boil.network/store/beta",
                "https://cloud.boil.network/store/beta",
                beta,
            ),
        }

        with patch.object(
            self.provider, "_fetch_document", side_effect=lambda url: documents[url]
        ):
            products = self.provider.fetch_products()

        self.assertEqual([item.product_id for item in products], ["10", "11"])
        self.assertEqual(self.provider.last_scan_stats["categories"], 2)
        self.assertEqual(self.provider.last_scan_stats["duplicate_pids"], 1)
        self.assertEqual(
            {item["slug"] for item in products[0].metadata["categories"]},
            {"alpha", "beta"},
        )

    def test_empty_category_is_valid_only_with_explicit_whmcs_marker(self):
        valid = category_html([("empty", "Empty")], [], current="empty", empty=True)
        self.assertEqual(
            self.provider.parse_category(
                valid, "https://cloud.boil.network/store/empty", "empty", "Empty"
            ),
            [],
        )
        with self.assertRaises(ParseError):
            self.provider.parse_category(
                "<html><body>unexpected layout</body></html>",
                "https://cloud.boil.network/store/broken",
                "broken",
                "Broken",
            )

    def test_failure_of_one_discovered_category_aborts_the_catalog(self):
        categories = [("alpha", "Alpha"), ("beta", "Beta")]
        alpha = category_html(categories, [{"pid": "10", "stock": 1}], current="alpha")
        documents = {
            self.provider.BASE_URL: FetchedDocument(
                self.provider.BASE_URL,
                self.provider.BASE_URL,
                '<html><header><a href="/store/alpha">Alpha</a></header></html>',
            ),
            self.provider.STORE_URL: FetchedDocument(
                self.provider.STORE_URL,
                "https://cloud.boil.network/store/alpha",
                alpha,
            ),
            self.provider.SITEMAP_URL: FetchedDocument(
                self.provider.SITEMAP_URL,
                self.provider.SITEMAP_URL,
                """<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                  <url><loc>https://cloud.boil.network/store/alpha</loc></url>
                  <url><loc>https://cloud.boil.network/store/beta</loc></url>
                </urlset>""",
            ),
        }

        def fetch(url):
            if url.endswith("/store/beta"):
                raise FetchError("beta timeout")
            return documents[url]

        with patch.object(self.provider, "_fetch_document", side_effect=fetch):
            with self.assertRaises(FetchError):
                self.provider.fetch_products()

    def test_provider_factory_builds_boilcloud(self):
        provider = build_provider({"name": "boilcloud", "type": "boilcloud"})
        self.assertIsInstance(provider, BoilcloudProvider)


class BoilcloudLifecycleTests(unittest.TestCase):
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
                    {"name": "boilcloud", "type": "boilcloud", "interval_seconds": 120}
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

    def test_first_snapshot_is_silent_baseline(self):
        app = self.app()
        provider = app.providers[0]
        provider.fetch_products = lambda: [boil_product("1", 0), boil_product("2", 1)]
        self.assertTrue(app.check(provider))
        self.assertEqual(app.telegram.sent, [])
        self.assertEqual(len(app.store.provider("boilcloud")["products"]), 2)

    def test_zero_to_one_restock_notifies(self):
        app = self.exercise([boil_product(stock=0)], [boil_product(stock=1)])
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("🔥 BOILCLOUD 补货", app.telegram.sent[0][0])

    def test_one_to_zero_sold_out_notifies(self):
        app = self.exercise([boil_product(stock=1)], [boil_product(stock=0)])
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("🔴 BOILCLOUD 售罄", app.telegram.sent[0][0])

    def test_numeric_stock_change_notifies(self):
        app = self.exercise([boil_product(stock=5)], [boil_product(stock=4)])
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("📦 BOILCLOUD 库存变化", app.telegram.sent[0][0])

    def test_new_pid_even_when_sold_out_notifies(self):
        app = self.exercise(
            [boil_product("1", 1)],
            [boil_product("1", 1), boil_product("2", 0, name="TEST temporary VPS")],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("🆕 BOILCLOUD 发现新品", app.telegram.sent[0][0])
        self.assertIn("疑似测试商品", app.telegram.sent[0][0])

    def test_price_change_notifies(self):
        app = self.exercise(
            [boil_product(price="$10.00 USD")],
            [boil_product(price="$12.00 USD")],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("💰 BOILCLOUD 价格变化", app.telegram.sent[0][0])

    def test_removed_product_after_complete_snapshot_notifies(self):
        app = self.exercise(
            [boil_product("1", 1), boil_product("2", 0)],
            [boil_product("1", 1)],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("🗑 BOILCLOUD 商品下架", app.telegram.sent[0][0])

    def test_failed_category_scan_preserves_snapshot_and_never_reports_removed(self):
        app = self.app()
        provider = app.providers[0]
        provider.fetch_products = lambda: [boil_product("1", 1), boil_product("2", 0)]
        self.assertTrue(app.check(provider))
        provider.fetch_products = lambda: (_ for _ in ()).throw(
            FetchError("category beta timed out")
        )
        self.assertFalse(app.check(provider))
        self.assertEqual(app.telegram.sent, [])
        self.assertEqual(
            set(app.store.provider("boilcloud")["products"]),
            {"boilcloud:1", "boilcloud:2"},
        )

    def test_interval_persists_and_status_and_menu_include_boilcloud(self):
        app = self.app()
        app.set_provider_interval("boilcloud", 17)
        restarted = self.app()
        self.assertEqual(restarted.provider_interval("boilcloud"), 17)
        restarted.store.record_success("boilcloud", [boil_product("1", 1)])
        restarted.store.save()
        restarted.send_status_menu()
        status = restarted.telegram.sent[-1][0]
        self.assertIn("<b>BOILCLOUD</b>", status)
        self.assertIn("商品：1", status)
        self.assertIn("扫描间隔：17 秒", status)
        restarted._send_interval_menu()
        menu, markup = restarted.telegram.sent[-1]
        self.assertIn("BOILCLOUD：17 秒", menu)
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "BOILCLOUD")


if __name__ == "__main__":
    unittest.main()
