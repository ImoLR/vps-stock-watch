from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watcher.app import WatcherApp
from watcher.models import Change, ChangeType, Product
from watcher.providers.blossom import BlossomProvider
from watcher.telegram import format_status


class FakeTelegram:
    chat_id = "123"

    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}


def product(
    family,
    product_id="plan-1",
    stock=0,
    available=False,
    price="$10 USD",
    provider="blossom",
):
    return Product(
        provider=provider,
        product_id=product_id,
        name="Plan",
        category=family,
        stock=stock,
        available=available,
        price=price,
        metadata={"family": family},
    )


class NotificationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.directory.cleanup()

    def app(self, provider_type="blossom"):
        config = {
            "state_path": str(Path(self.directory.name) / (provider_type + ".json")),
            "poll_interval_seconds": 60,
            "providers": [
                {"name": provider_type, "type": provider_type, "interval_seconds": 60}
            ],
        }
        app = WatcherApp(config, notifications=False)
        app.telegram = FakeTelegram()
        return app

    def exercise(self, old, new):
        app = self.app()
        provider = app.providers[0]
        snapshots = iter((old, new))
        provider.fetch_products = lambda: next(snapshots)
        self.assertTrue(app.check(provider))
        self.assertTrue(app.check(provider))
        return app

    def assert_silent(self, old, new):
        app = self.exercise(old, new)
        self.assertEqual(app.telegram.sent, [])
        return app

    def assert_one_sent(self, old, new, text):
        app = self.exercise(old, new)
        self.assertEqual(len(app.telegram.sent), 1)
        self.assertIn(text, app.telegram.sent[0][0])

    def test_isp_restock_is_silent(self):
        self.assert_silent(
            [product("isp", stock=0, available=False)],
            [product("isp", stock=1, available=True)],
        )

    def test_isp_sold_out_is_silent(self):
        self.assert_silent(
            [product("isp", stock=1, available=True)],
            [product("isp", stock=0, available=False)],
        )

    def test_isp_numeric_stock_change_is_silent(self):
        self.assert_silent(
            [product("isp", stock=5, available=True)],
            [product("isp", stock=4, available=True)],
        )

    def test_isp_price_change_is_silent(self):
        self.assert_silent(
            [product("isp", price="$10 USD")],
            [product("isp", price="$12 USD")],
        )

    def test_isp_new_sku_notifies(self):
        self.assert_one_sent(
            [product("isp")],
            [product("isp"), product("isp", product_id="future-isp")],
            "Blossom Host 发现新品",
        )

    def test_isp_removal_from_snapshot_written_while_muted_notifies(self):
        # State saved before ISP was unmuted still carries the old flag.
        old = product("isp", product_id="old-isp")
        old.metadata["notification_suppressed"] = True
        self.assert_one_sent([product("isp"), old], [product("isp")], "Blossom Host")

    def test_metal_restock_is_silent(self):
        self.assert_silent(
            [product("metal", stock=0, available=False)],
            [product("metal", stock=2, available=True)],
        )

    def test_metal_sold_out_is_silent(self):
        self.assert_silent(
            [product("metal", stock=2, available=True)],
            [product("metal", stock=0, available=False)],
        )

    def test_metal_new_product_is_silent(self):
        self.assert_silent(
            [product("metal")],
            [product("metal"), product("metal", product_id="new-physical-server")],
        )

    def test_metal_price_change_is_silent(self):
        self.assert_silent(
            [product("metal", price="$100 USD")],
            [product("metal", price="$120 USD")],
        )

    def test_silent_products_still_update_state_and_status_totals(self):
        app = self.assert_silent(
            [product("metal"), product("metal", product_id="metal-1")],
            [
                product("metal", stock=1, available=True),
                product("metal", product_id="metal-1", stock=2, available=True),
            ],
        )
        products = app.store.provider("blossom")["products"]
        self.assertEqual(products["blossom:plan-1"]["stock"], 1)
        self.assertEqual(products["blossom:metal-1"]["stock"], 2)
        status = format_status(app.store.snapshot())
        self.assertIn("商品数量：2", status)
        self.assertIn("当前有货：2", status)

    def test_other_blossom_family_restock_is_silent(self):
        self.assert_silent(
            [product("residential", stock=0, available=False)],
            [product("residential", stock=1, available=True)],
        )

    def test_other_blossom_family_new_offer_notifies(self):
        self.assert_one_sent(
            [product("residential")],
            [product("residential"), product("residential", product_id="offer-2")],
            "Blossom Host 发现新品",
        )

    def test_only_new_and_removed_notify_and_metal_never(self):
        provider = BlossomProvider({"name": "blossom", "type": "blossom"})
        for family in ("metal", "isp", "residential"):
            for change_type in ChangeType:
                expected = family != "metal" and change_type in (
                    ChangeType.NEW,
                    ChangeType.REMOVED,
                )
                with self.subTest(family=family, change_type=change_type):
                    self.assertEqual(
                        provider.should_notify(Change(change_type, product(family))),
                        expected,
                    )

    def test_nexkr_and_dmit_remain_notifiable(self):
        for provider_type in ("nexkr", "dmit"):
            with self.subTest(provider=provider_type):
                app = self.app(provider_type)
                provider = app.providers[0]
                item = product(
                    "isp",
                    provider=provider_type,
                    stock=1,
                    available=True,
                )
                self.assertTrue(provider.should_notify(Change(ChangeType.RESTOCK, item)))


if __name__ == "__main__":
    unittest.main()
