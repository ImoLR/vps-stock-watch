from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from watcher.app import WatcherApp
from watcher.models import Change, ChangeType, Product
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.leikwanhost import FetchedDocument, LeikwanhostProvider


def category_html(categories, products, current="alpha", empty=False):
    links = []
    options = []
    for index, (slug, label) in enumerate(categories, start=1):
        active = " active" if slug == current else ""
        selected = " selected" if slug == current else ""
        links.append(
            '<a id="Secondary_Sidebar-Categories-%s" class="list-group-item%s" '
            'href="/index.php?rp=/store/%s">%s</a>'
            % (slug, active, slug, label)
        )
        options.append(
            '<option value="/index.php?rp=/store/%s"%s>%s</option>'
            % (slug, selected, label)
        )
    cards = []
    for item in products:
        stock_text = item.get("stock_text")
        stock = '<span class="lk-cart-stock">%s</span>' % stock_text if stock_text is not None else ""
        disabled = bool(
            stock_text
            and (stock_text.casefold() == "sold out" or stock_text.startswith("0 "))
        )
        disabled_attrs = (
            ' is-disabled" aria-disabled="true" style="pointer-events:none"'
            if disabled
            else '"'
        )
        setup = item.get("setup", "HK$11.50 Setup Fee")
        setup_text = " · %s" % setup if setup else ""
        cards.append(
            """
            <div class="lk-cart-card%s" id="product%s">
              <div class="lk-cart-card-head"><h3 id="product%s-name">%s</h3>%s</div>
              <p class="lk-cart-desc" id="product%s-description">
                上海入口<br>2*vCPU EPYC<br>4GB RAM<br>20 GB NVME SSD<br>
                2 TiB@300Mbps/Bi-Direction<br>1* IPv6 /64
              </p>
              <div class="lk-cart-price" id="product%s-price">
                <small>Starting from</small><span class="price">%s</span>
                <small>%s%s</small>
              </div>
              <a class="lk-cart-btn%s href="/index.php?rp=/store/%s/plan-%s"
                 id="product%s-order-button">Order Now</a>
            </div>
            """
            % (
                " is-disabled" if disabled else "",
                item["pid"],
                item["pid"],
                item.get("name", "Plan %s" % item["pid"]),
                stock,
                item["pid"],
                item["pid"],
                item.get("price", "HK$128.00HKD"),
                item.get("cycle", "Monthly"),
                setup_text,
                disabled_attrs,
                current,
                item["pid"],
                item["pid"],
            )
        )
    empty_text = "Product group does not contain any visible products" if empty else ""
    group_id = next(
        str(index) for index, (slug, _label) in enumerate(categories, start=1) if slug == current
    )
    return """
    <html><body><div id="order-standard_cart" class="lk-cart">
      <div class="list-group">%s</div><select>%s</select>
      <h1>Public products</h1><div id="products">%s%s</div>
      <form method="post" action="/cart.php?gid=%s"></form>
    </div></body></html>
    """ % ("".join(links), "".join(options), "".join(cards), empty_text, group_id)


def leikwan_product(
    pid="1",
    stock=None,
    available=True,
    price="HK$10.00HKD",
    cycle="monthly",
    name="Plan",
):
    return Product(
        provider="leikwanhost",
        product_id=pid,
        name=name,
        category="上海 VPS",
        region="上海",
        price=price,
        billing_cycle=cycle,
        stock=stock,
        available=available,
        url="https://buy.leikwanhost.com/cart.php?a=add&pid=%s" % pid,
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


class LeiKwanHostProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = LeikwanhostProvider(
            {
                "name": "leikwanhost",
                "type": "leikwanhost",
                "retry_backoff_seconds": 0,
            }
        )
        self.categories = [("alpha", "上海 VPS"), ("beta", "Beta Products")]

    def parse(self, item):
        html = category_html(self.categories, [item], current="alpha")
        return self.provider.parse_category(
            html,
            "https://buy.leikwanhost.com/index.php?rp=/store/alpha",
            "alpha",
            "上海 VPS",
            "1",
        )[0]

    def test_parses_real_pid_numeric_stock_price_cycle_specs_and_direct_url(self):
        item = self.parse({"pid": "57", "name": "CT-Beta", "stock_text": "3 Available"})
        self.assertEqual(item.key, "leikwanhost:57")
        self.assertEqual(item.stock, 3)
        self.assertTrue(item.available)
        self.assertEqual(item.price, "HK$128.00HKD + HK$11.50 Setup Fee")
        self.assertEqual(item.billing_cycle, "monthly")
        self.assertEqual(item.metadata["currency"], "HKD")
        self.assertEqual(item.metadata["category_group_id"], "1")
        self.assertEqual(item.metadata["discovery"]["hidden"], False)
        self.assertEqual(item.specs["cpu"], "2*vCPU")
        self.assertEqual(item.specs["ram"], "4GB RAM")
        self.assertEqual(item.url, "https://buy.leikwanhost.com/cart.php?a=add&pid=57")

    def test_boolean_inventory_is_not_fabricated(self):
        item = self.parse({"pid": "19", "stock_text": None})
        self.assertIsNone(item.stock)
        self.assertTrue(item.available)
        self.assertEqual(item.metadata["stock_source"], "category_card.order_button")

    def test_sold_out_is_boolean_without_inventing_zero(self):
        item = self.parse({"pid": "27", "stock_text": "Sold out"})
        self.assertIsNone(item.stock)
        self.assertFalse(item.available)

    def test_zero_available_preserves_exact_zero(self):
        item = self.parse({"pid": "27", "stock_text": "0 Available"})
        self.assertEqual(item.stock, 0)
        self.assertFalse(item.available)

    def test_default_notification_policy_does_not_silence_isp_or_metal_names(self):
        for name in ("ISP Line Test", "Bare metal Test"):
            item = self.parse({"pid": "57", "name": name, "stock_text": "3 Available"})
            self.assertTrue(self.provider.should_notify(Change(ChangeType.NEW, item)))

    def test_empty_category_requires_explicit_whmcs_marker(self):
        valid = category_html(self.categories, [], current="alpha", empty=True)
        self.assertEqual(
            self.provider.parse_category(
                valid,
                "https://buy.leikwanhost.com/index.php?rp=/store/alpha",
                "alpha",
                "Alpha",
            ),
            [],
        )
        with self.assertRaises(ParseError):
            self.provider.parse_category(
                "<html><body>unexpected layout</body></html>",
                "https://buy.leikwanhost.com/index.php?rp=/store/alpha",
                "alpha",
                "Alpha",
            )

    def test_full_discovery_follows_new_categories_and_deduplicates_pid(self):
        alpha = category_html(
            self.categories, [{"pid": "10", "stock_text": "2 Available"}], "alpha"
        )
        beta = category_html(
            self.categories,
            [
                {"pid": "10", "stock_text": "2 Available"},
                {"pid": "11", "stock_text": None},
            ],
            "beta",
        )
        documents = {
            self.provider.BASE_URL: FetchedDocument(
                self.provider.BASE_URL,
                self.provider.BASE_URL,
                '<html><nav><a href="/index.php?rp=/store/alpha">Alpha</a></nav></html>',
            ),
            self.provider.STORE_URL: FetchedDocument(
                self.provider.STORE_URL,
                "https://buy.leikwanhost.com/index.php?rp=/store/alpha",
                alpha,
            ),
            "https://buy.leikwanhost.com/index.php?rp=/store/beta": FetchedDocument(
                "https://buy.leikwanhost.com/index.php?rp=/store/beta",
                "https://buy.leikwanhost.com/index.php?rp=/store/beta",
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

    def test_incomplete_navigation_aborts_complete_catalog(self):
        alpha = category_html(self.categories, [{"pid": "10"}], "alpha")
        beta = category_html([("beta", "Beta Products")], [{"pid": "11"}], "beta")
        documents = {
            self.provider.BASE_URL: FetchedDocument(
                self.provider.BASE_URL,
                self.provider.BASE_URL,
                '<html><nav><a href="/index.php?rp=/store/alpha">Alpha</a></nav></html>',
            ),
            self.provider.STORE_URL: FetchedDocument(
                self.provider.STORE_URL,
                "https://buy.leikwanhost.com/index.php?rp=/store/alpha",
                alpha,
            ),
            "https://buy.leikwanhost.com/index.php?rp=/store/beta": FetchedDocument(
                "https://buy.leikwanhost.com/index.php?rp=/store/beta",
                "https://buy.leikwanhost.com/index.php?rp=/store/beta",
                beta,
            ),
        }
        with patch.object(
            self.provider, "_fetch_document", side_effect=lambda url: documents[url]
        ):
            with self.assertRaises(ParseError):
                self.provider.fetch_products()

    def test_failure_of_one_category_aborts_complete_catalog(self):
        alpha = category_html(self.categories, [{"pid": "10"}], "alpha")

        def fetch(url):
            if "beta" in url:
                raise FetchError("beta timeout")
            if url == self.provider.BASE_URL:
                return FetchedDocument(
                    self.provider.BASE_URL,
                    self.provider.BASE_URL,
                    '<html><nav><a href="/index.php?rp=/store/alpha">Alpha</a></nav></html>',
                )
            return FetchedDocument(
                self.provider.STORE_URL,
                "https://buy.leikwanhost.com/index.php?rp=/store/alpha",
                alpha,
            )

        with patch.object(self.provider, "_fetch_document", side_effect=fetch):
            with self.assertRaises(FetchError):
                self.provider.fetch_products()

    def test_provider_factory_builds_leikwanhost(self):
        provider = build_provider({"name": "leikwanhost", "type": "leikwanhost"})
        self.assertIsInstance(provider, LeikwanhostProvider)


class LeiKwanHostLifecycleTests(unittest.TestCase):
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
                    {
                        "name": "leikwanhost",
                        "type": "leikwanhost",
                        "interval_seconds": 120,
                    }
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
        provider.fetch_products = lambda: [
            leikwan_product("1", stock=2),
            leikwan_product("2", available=False),
        ]
        self.assertTrue(app.check(provider))
        self.assertEqual(app.telegram.sent, [])
        self.assertEqual(len(app.store.provider("leikwanhost")["products"]), 2)

    def test_boolean_restock_notifies(self):
        app = self.exercise(
            [leikwan_product(available=False)], [leikwan_product(available=True)]
        )
        self.assertIn("🔥 LeiKwanHost 补货", app.telegram.sent[0][0])

    def test_boolean_sold_out_notifies(self):
        app = self.exercise(
            [leikwan_product(available=True)], [leikwan_product(available=False)]
        )
        self.assertIn("🔴 LeiKwanHost 售罄", app.telegram.sent[0][0])

    def test_arbitrary_numeric_stock_change_notifies(self):
        app = self.exercise(
            [leikwan_product(stock=5)], [leikwan_product(stock=3)]
        )
        self.assertIn("📦 LeiKwanHost 库存变化", app.telegram.sent[0][0])

    def test_new_pid_even_when_sold_out_notifies(self):
        app = self.exercise(
            [leikwan_product("1")],
            [leikwan_product("1"), leikwan_product("2", available=False, name="TEST plan")],
        )
        self.assertIn("🆕 LeiKwanHost 发现新品", app.telegram.sent[0][0])
        self.assertIn("疑似测试商品", app.telegram.sent[0][0])

    def test_price_and_billing_cycle_changes_notify(self):
        app = self.exercise(
            [leikwan_product(price="HK$10.00HKD", cycle="monthly")],
            [leikwan_product(price="HK$12.00HKD", cycle="quarterly")],
        )
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn("💰 LeiKwanHost 价格变化", app.telegram.sent[0][0])
        self.assertIn("付款周期变化：monthly → quarterly", app.telegram.sent[0][0])

    def test_removed_product_after_complete_snapshot_notifies(self):
        app = self.exercise(
            [leikwan_product("1"), leikwan_product("2")],
            [leikwan_product("1")],
        )
        self.assertIn("🗑 LeiKwanHost 商品下架", app.telegram.sent[0][0])

    def test_incomplete_catalog_preserves_state_without_removed_notification(self):
        app = self.app()
        provider = app.providers[0]
        provider.fetch_products = lambda: [leikwan_product("1"), leikwan_product("2")]
        self.assertTrue(app.check(provider))
        provider.fetch_products = lambda: (_ for _ in ()).throw(
            FetchError("category failed")
        )
        self.assertFalse(app.check(provider))
        self.assertEqual(app.telegram.sent, [])
        self.assertEqual(
            set(app.store.provider("leikwanhost")["products"]),
            {"leikwanhost:1", "leikwanhost:2"},
        )

    def test_interval_status_and_stock_are_integrated_and_state_only(self):
        app = self.app()
        app.set_provider_interval("leikwanhost", 17)
        app.store.record_success("leikwanhost", [leikwan_product("57", stock=3)])
        app.store.save()
        restarted = self.app()
        restarted.telegram = FakeTelegram()
        self.assertEqual(restarted.provider_interval("leikwanhost"), 17)
        restarted.send_status_menu()
        self.assertIn("<b>LeiKwanHost</b>", restarted.telegram.sent[-1][0])
        provider = restarted.providers[0]
        provider.fetch_products = Mock(side_effect=AssertionError("network scan called"))
        restarted.send_stock_provider("leikwanhost", refresh_disk=True)
        text = restarted.telegram.sent[-1][0]
        self.assertIn("LeiKwanHost 当前可购买库存", text)
        self.assertIn("🟢 正常库存", text)
        self.assertIn("库存：3", text)
        self.assertIn("cart.php?a=add&amp;pid=57", text)
        provider.fetch_products.assert_not_called()


if __name__ == "__main__":
    unittest.main()
