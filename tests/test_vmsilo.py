from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.app import WatcherApp
from watcher.models import Change, ChangeType, Product
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.vmsilo import FetchedDocument, VmsiloProvider
from watcher.telegram import format_status, format_stock_menu, format_stock_provider


def category_html(categories, products, current="gid=1", layout=True):
    tabs = "".join(
        '<a href="/cart.php?%s"%s>%s</a>'
        % (ref, ' aria-current="page"' if ref == current else "", label)
        for ref, label in categories
    )
    slug = dict(categories).get(current, current).split()[0].casefold()
    cards = []
    for index, item in enumerate(products, 1):
        product_slug = item["slug"]
        stock_text = item.get("stock_text")
        stock = "<p>%s</p>" % stock_text if stock_text else ""
        if stock_text == "Sold Out" and item.get("drop_link"):
            button = '<span class="btn btn-default">Out of Stock</span>'
        else:
            button = '<a href="/store/%s/%s" class="btn btn-primary%s">%s</a>' % (
                slug,
                product_slug,
                " disabled" if stock_text == "Sold Out" else "",
                "Sold Out" if stock_text == "Sold Out" else "Configure",
            )
        cards.append(
            """
            <article class="bv-plan" id="product%s">
              <header><h3>%s</h3></header>
              <div class="bv-plan-desc"><div><strong>Default configuration</strong><br />
                CPU Cores：1<br />Amount Of RAM：512M<br />Disk Space：5G<br />
                IPV4：0<br />IPV6：1<br />Bandwidth：200G<br />Network Rate：50Mbps<br />
              </div><ul></ul></div>%s
              <footer class="bv-plan-price"><div><small>Base configuration</small><br>
                <span class="price">¥%s CNY</span><small>%s</small></div>%s</footer>
            </article>
            """
            % (
                item.get("pid", index),
                item.get("name", "Plan %s" % product_slug),
                stock,
                item.get("price", "10.00"),
                item.get("cycle", "/ month"),
                button,
            )
        )
    head = '<div class="bv-products-head"><div><h2>%s</h2></div></div>' % (
        dict(categories).get(current, current)
    )
    return """
    <html><body><div id="order-standard_cart">
      <nav class="bv-group-tabs" aria-label="VPS categories">%s</nav>
      %s
      <div class="bv-plan-grid">%s</div>
    </div></body></html>
    """ % (tabs, head if layout else "", "".join(cards))


def vmsilo_product(
    slug="plan-a", stock=None, available=True, price="¥10.00", name="Plan A"
):
    return Product(
        provider="vmsilo",
        product_id=slug,
        name=name,
        category="IEPL",
        price=price,
        billing_cycle="monthly",
        stock=stock,
        available=available,
        url="https://portal.vmsilo.com/store/iepl/%s" % slug,
        metadata={
            "discovery": {
                "type": "catalog",
                "hidden": False,
                "source": "store_navigation",
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


class VmsiloProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = VmsiloProvider(
            {
                "name": "vmsilo",
                "type": "vmsilo",
                "retry_backoff_seconds": 0,
                "minimum_categories": 2,
            }
        )
        self.categories = [("gid=1", "Alpha Plans"), ("gid=2", "Beta Plans")]
        self.base = self.provider.BASE_URL

    def document(self, requested, final, html):
        return FetchedDocument(requested, self.base + final, html)

    def test_parses_stable_slug_numeric_and_boolean_stock_and_direct_url(self):
        html = category_html(
            self.categories,
            [
                {"slug": "numeric-plan", "stock_text": "3 Available"},
                {"slug": "boolean-plan"},
                {"slug": "sold-plan", "stock_text": "Sold Out"},
                {"slug": "once-plan", "price": "0.00", "cycle": "One-time"},
            ],
        )
        items = self.provider.parse_category(
            html,
            "https://portal.vmsilo.com/store/alpha",
            "alpha",
            "Alpha Plans",
        )
        by_id = {item.product_id: item for item in items}
        self.assertEqual(by_id["numeric-plan"].key, "vmsilo:numeric-plan")
        self.assertEqual(by_id["numeric-plan"].stock, 3)
        self.assertTrue(by_id["numeric-plan"].available)
        self.assertIsNone(by_id["boolean-plan"].stock)
        self.assertTrue(by_id["boolean-plan"].available)
        self.assertIsNone(by_id["sold-plan"].stock)
        self.assertFalse(by_id["sold-plan"].available)
        self.assertEqual(by_id["numeric-plan"].price, "¥10.00")
        self.assertEqual(by_id["numeric-plan"].billing_cycle, "monthly")
        self.assertEqual(by_id["once-plan"].price, "¥0.00")
        self.assertEqual(by_id["once-plan"].billing_cycle, "onetime")
        self.assertEqual(
            by_id["numeric-plan"].url,
            "https://portal.vmsilo.com/store/alpha/numeric-plan",
        )
        self.assertEqual(by_id["numeric-plan"].specs["cpu"], "1")
        self.assertEqual(by_id["numeric-plan"].specs["bandwidth"], "50Mbps")

    def test_sold_out_card_without_link_keeps_learned_slug(self):
        url = "https://portal.vmsilo.com/store/alpha"
        listed = category_html(self.categories, [{"slug": "plan-a", "pid": 7}])
        self.provider.parse_category(listed, url, "alpha", "Alpha Plans")
        sold = category_html(
            self.categories,
            [{"slug": "plan-a", "pid": 7, "stock_text": "Sold Out", "drop_link": True}],
        )
        [item] = self.provider.parse_category(sold, url, "alpha", "Alpha Plans")
        self.assertEqual(item.product_id, "plan-a")
        self.assertFalse(item.available)
        self.assertEqual(item.url, url)

    def test_empty_category_requires_recognized_layout(self):
        valid = category_html(self.categories, [], current="gid=2")
        self.assertEqual(
            self.provider.parse_category(
                valid,
                "https://portal.vmsilo.com/store/beta",
                "beta",
                "Beta Plans",
            ),
            [],
        )
        with self.assertRaises(ParseError):
            self.provider.parse_category(
                "<html><body>empty</body></html>",
                "https://portal.vmsilo.com/store/beta",
                "beta",
                "Beta Plans",
            )
        with self.assertRaises(ParseError):
            self.provider.parse_category(
                category_html(self.categories, [], current="gid=2", layout=False),
                "https://portal.vmsilo.com/store/beta",
                "beta",
                "Beta Plans",
            )

    def test_full_discovery_uses_group_tabs_and_marks_extra_category_hidden(self):
        self.provider.config["extra_category_urls"] = ["/store/gamma"]
        alpha = category_html(
            self.categories, [{"slug": "plan-a", "name": "Plan A"}], "gid=1"
        )
        beta = category_html(
            self.categories, [{"slug": "plan-b", "name": "Plan B"}], "gid=2"
        )
        gamma = category_html(
            self.categories, [{"slug": "hidden-plan", "name": "Hidden Plan"}], "none"
        ).replace("/store/none/", "/store/gamma/")
        store = self.provider.STORE_URL
        documents = {
            store: self.document(store, "store/alpha", alpha),
            self.base + "cart.php?gid=2": self.document(
                self.base + "cart.php?gid=2", "store/beta", beta
            ),
            self.base + "store/gamma": self.document(
                self.base + "store/gamma", "store/gamma", gamma
            ),
        }
        with patch.object(
            self.provider, "_fetch_document", side_effect=lambda url: documents[url]
        ):
            products = self.provider.fetch_products()
        by_id = {item.product_id: item for item in products}
        self.assertEqual(set(by_id), {"plan-a", "plan-b", "hidden-plan"})
        self.assertTrue(by_id["hidden-plan"].metadata["discovery"]["hidden"])
        self.assertFalse(by_id["plan-a"].metadata["discovery"]["hidden"])
        self.assertEqual(by_id["plan-b"].category, "Beta Plans")
        self.assertEqual(by_id["plan-b"].metadata["category_slug"], "beta")
        self.assertEqual(self.provider.last_scan_stats["categories"], 3)
        self.assertEqual(self.provider.last_scan_stats["requests"], 0)
        self.assertEqual(
            self.provider.last_scan_stats["category_routes"],
            {"gid=1": "alpha", "gid=2": "beta", "store/gamma": "gamma"},
        )

    def test_too_few_categories_aborts_catalog(self):
        store = self.provider.STORE_URL
        alpha = category_html([("gid=1", "Alpha Plans")], [{"slug": "plan-a"}])
        with patch.object(
            self.provider,
            "_fetch_document",
            return_value=self.document(store, "store/alpha", alpha),
        ):
            with self.assertRaises(ParseError):
                self.provider.fetch_products()

    def test_incomplete_navigation_aborts_catalog(self):
        alpha = category_html(self.categories, [{"slug": "plan-a"}], "gid=1")
        beta = category_html([("gid=2", "Beta Plans")], [], "gid=2")
        store = self.provider.STORE_URL
        documents = {
            store: self.document(store, "store/alpha", alpha),
            self.base + "cart.php?gid=2": self.document(
                self.base + "cart.php?gid=2", "store/beta", beta
            ),
        }
        with patch.object(
            self.provider, "_fetch_document", side_effect=lambda url: documents[url]
        ):
            with self.assertRaises(ParseError):
                self.provider.fetch_products()

    def test_wrong_active_tab_aborts_catalog(self):
        alpha = category_html(self.categories, [{"slug": "plan-a"}], "gid=1")
        store = self.provider.STORE_URL
        documents = {
            store: self.document(store, "store/alpha", alpha),
            self.base + "cart.php?gid=2": self.document(
                self.base + "cart.php?gid=2", "store/alpha", alpha
            ),
        }
        with patch.object(
            self.provider, "_fetch_document", side_effect=lambda url: documents[url]
        ):
            with self.assertRaises(ParseError):
                self.provider.fetch_products()

    def test_fetch_failure_aborts_catalog(self):
        with patch.object(
            self.provider,
            "_fetch_document",
            side_effect=FetchError("blocked", status=403, blocked=True),
        ):
            with self.assertRaises(FetchError):
                self.provider.fetch_products()

    def test_provider_factory_builds_vmsilo(self):
        built = build_provider({"name": "vmsilo", "type": "vmsilo"})
        self.assertIsInstance(built, VmsiloProvider)

    def test_default_notification_policy_does_not_inherit_blossom_silencing(self):
        item = vmsilo_product(name="ISP Line Bare metal")
        self.assertTrue(self.provider.should_notify(Change(ChangeType.NEW, item)))


class VmsiloLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.directory.name) / "state.json")
        self.app = WatcherApp(
            {
                "state_path": self.state_path,
                "poll_interval_seconds": 60,
                "providers": [
                    {
                        "name": "vmsilo",
                        "type": "vmsilo",
                        "interval_seconds": 120,
                        "minimum_categories": 2,
                    }
                ],
            },
            notifications=False,
        )
        self.app.telegram = FakeTelegram()
        self.provider = self.app.providers[0]

    def tearDown(self):
        self.directory.cleanup()

    def scan(self, products):
        self.provider.fetch_products = lambda: products
        return self.app.check(self.provider)

    def test_first_snapshot_is_silent_then_all_core_changes_notify(self):
        self.assertTrue(self.scan([vmsilo_product(stock=1)]))
        self.assertEqual(self.app.telegram.sent, [])

        self.assertTrue(self.scan([vmsilo_product(stock=3)]))
        self.assertIn("📦 VMSILO 库存变化", self.app.telegram.sent[-1][0])
        self.assertTrue(self.scan([vmsilo_product(stock=0, available=False)]))
        self.assertIn("🔴 VMSILO 售罄", self.app.telegram.sent[-1][0])
        self.assertTrue(self.scan([vmsilo_product(stock=2)]))
        self.assertIn("🔥 VMSILO 补货", self.app.telegram.sent[-1][0])
        self.assertTrue(self.scan([vmsilo_product(stock=2, price="¥12.00")]))
        self.assertIn("💰 VMSILO 价格变化", self.app.telegram.sent[-1][0])
        self.assertTrue(
            self.scan(
                [
                    vmsilo_product(stock=2, price="¥12.00"),
                    vmsilo_product("plan-b", stock=None, name="Plan B"),
                ]
            )
        )
        self.assertIn("🆕 VMSILO 发现新品", self.app.telegram.sent[-1][0])
        self.assertTrue(self.scan([vmsilo_product("plan-b", stock=None, name="Plan B")]))
        self.assertIn("🗑 VMSILO 商品下架", self.app.telegram.sent[-1][0])

    def test_incomplete_scan_preserves_previous_state(self):
        self.assertTrue(self.scan([vmsilo_product()]))
        self.provider.fetch_products = lambda: (_ for _ in ()).throw(
            ParseError("incomplete catalog")
        )
        self.assertFalse(self.app.check(self.provider))
        self.assertIn("vmsilo:plan-a", self.app.store.provider("vmsilo")["products"])
        self.assertEqual(self.app.telegram.sent, [])

    def test_status_stock_compact_category_and_interval_are_integrated(self):
        self.app.store.record_success("vmsilo", [vmsilo_product()])
        self.app.store.save()
        self.app.set_provider_interval("vmsilo", 19)
        state = self.app.store.snapshot()
        status = format_status(state, self.app.provider_intervals())
        self.assertIn("<b>VMSILO</b>", status)
        self.assertIn("扫描间隔：19 秒", status)
        self.assertIn("VMSILO：1", format_stock_menu(state, ["vmsilo"]))
        stock = "\n".join(format_stock_provider(state, "vmsilo", 19))
        self.assertIn("【IEPL】", stock)
        self.assertIn("Plan A · ¥10.00/月", stock)
        self.assertNotIn("ID / PID", stock)
        self.assertNotIn("地区：", stock)


if __name__ == "__main__":
    unittest.main()
