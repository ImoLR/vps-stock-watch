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


def category_html(categories, products, current="alpha", count=None):
    navigation = []
    for slug, label in categories:
        active = " active" if slug == current else ""
        navigation.append(
            '<a id="Secondary_Sidebar-Categories-%s" class="product__cart__sidepanel__item%s" '
            'href="/store/%s">%s</a>' % (slug, active, slug, label)
        )
    cards = []
    for item in products:
        slug = item["slug"]
        stock_text = item.get("stock_text")
        stock = '<span class="stock">%s</span>' % stock_text if stock_text else ""
        disabled = " disabled" if stock_text == "Sold Out" else ""
        cards.append(
            """
            <div class="pricing__plans__standard__item">
              <div class="pricing-plans-special-header"><h5>%s</h5></div>
              <p>%s</p>%s
              <div class="pricing">¥ <span data-row-price-min="%s">%s</span> 按月</div>
              <a class="btn-order-now%s" href="/store/%s/%s">%s</a>
              <ul class="pricing__plans__special__body">
                CPU Cores：1<br>Amount Of RAM：512M<br>Disk Space：5G<br>
                IPV4：0<br>IPV6：1<br>Bandwidth：200G<br>Network Rate：50Mbps
              </ul>
            </div>
            """
            % (
                item.get("name", "Plan %s" % slug),
                item.get("category", current.title()),
                stock,
                item.get("price", "10.00"),
                item.get("price", "10.00"),
                disabled,
                current,
                slug,
                "Sold Out" if stock_text == "Sold Out" else "立即订购",
            )
        )
    expected = len(products) if count is None else count
    return """
    <html><body>
      <div class="product__cart__sidepanel">%s</div>
      <div class="standard__cart__slider__layout__options__header standard__cart__%dproducts">%s</div>
      %s
    </body></html>
    """ % ("".join(navigation), expected, current, "".join(cards))


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
        self.categories = [("alpha", "Alpha Plans"), ("beta", "Beta Plans")]

    def test_parses_stable_slug_numeric_and_boolean_stock_and_direct_url(self):
        html = category_html(
            self.categories,
            [
                {"slug": "numeric-plan", "stock_text": "3 Available"},
                {"slug": "boolean-plan"},
                {"slug": "sold-plan", "stock_text": "Sold Out"},
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
        self.assertEqual(
            by_id["numeric-plan"].url,
            "https://portal.vmsilo.com/store/alpha/numeric-plan",
        )
        self.assertEqual(by_id["numeric-plan"].specs["cpu"], "1")

    def test_zero_product_category_requires_explicit_count_marker(self):
        valid = category_html(self.categories, [], current="beta")
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
                category_html(self.categories, [], current="beta", count=2),
                "https://portal.vmsilo.com/store/beta",
                "beta",
                "Beta Plans",
            )

    def test_full_discovery_deduplicates_and_marks_homepage_only_category_hidden(self):
        alpha = category_html(
            self.categories,
            [{"slug": "plan-a", "name": "Shared Plan"}],
            "alpha",
        )
        beta = category_html(
            self.categories,
            [{"slug": "plan-a", "name": "Shared Plan"}],
            "beta",
        )
        gamma = category_html(
            self.categories,
            [{"slug": "hidden-plan", "name": "Hidden Plan"}],
            "gamma",
        )
        base = self.provider.BASE_URL
        store = self.provider.STORE_URL
        documents = {
            base: FetchedDocument(
                base,
                base,
                '<html><a href="/store/alpha">Alpha</a><a href="/store/gamma">Gamma</a>'
                '<a href="/store/legacy">Legacy</a></html>',
            ),
            store: FetchedDocument(store, base + "store/alpha", alpha),
            base + "store/beta": FetchedDocument(
                base + "store/beta", base + "store/beta", beta
            ),
            base + "store/gamma": FetchedDocument(
                base + "store/gamma", base + "store/gamma", gamma
            ),
            base + "store/legacy": FetchedDocument(
                base + "store/legacy", base + "store/alpha", alpha
            ),
        }
        with patch.object(
            self.provider, "_fetch_document", side_effect=lambda url: documents[url]
        ):
            products = self.provider.fetch_products()
        self.assertEqual({item.product_id for item in products}, {"plan-a", "hidden-plan"})
        hidden = next(item for item in products if item.product_id == "hidden-plan")
        self.assertTrue(hidden.metadata["discovery"]["hidden"])
        normal = next(item for item in products if item.product_id == "plan-a")
        self.assertFalse(normal.metadata["discovery"]["hidden"])
        self.assertEqual(set(normal.metadata["categories"]), {"alpha", "beta"})
        self.assertEqual(self.provider.last_scan_stats["categories"], 3)
        self.assertEqual(self.provider.last_scan_stats["duplicate_products"], 1)
        self.assertEqual(self.provider.last_scan_stats["aliases"], {"legacy": "alpha"})

    def test_incomplete_navigation_aborts_catalog(self):
        alpha = category_html(self.categories, [{"slug": "plan-a"}], "alpha")
        beta = category_html([("beta", "Beta Plans")], [], "beta")
        documents = {
            self.provider.BASE_URL: FetchedDocument(
                self.provider.BASE_URL,
                self.provider.BASE_URL,
                '<html><a href="/store/alpha">Alpha</a></html>',
            ),
            self.provider.STORE_URL: FetchedDocument(
                self.provider.STORE_URL,
                self.provider.BASE_URL + "store/alpha",
                alpha,
            ),
            self.provider.BASE_URL + "store/beta": FetchedDocument(
                self.provider.BASE_URL + "store/beta",
                self.provider.BASE_URL + "store/beta",
                beta,
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
