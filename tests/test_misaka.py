from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watcher.app import WatcherApp
from watcher.diff import compare_products
from watcher.models import Change, ChangeType, Product
from watcher.providers import build_provider
from watcher.providers.base import FetchError, ParseError
from watcher.providers.misaka import MisakaProvider
from watcher.telegram import format_change, format_status, format_stock_provider


def region(slug, country_code, name):
    return {
        "id": slug,
        "slug": slug,
        "name": name,
        "country_code": country_code,
        "country": name,
        "facility": "Test DC",
        "type": "core",
        "available": True,
    }


def plan(
    plan_id,
    name="Small",
    available=True,
    monthly=10.0,
    stock_marker=False,
    **extra
):
    value = {
        "id": plan_id,
        "slug": name.lower().replace(" ", "-"),
        "name": name,
        "memory": 1024,
        "vcores": 1,
        "disk": 16384,
        "transfer": 1024,
        "network_billing_model": "HigherDirection",
        "nvme": False,
        "routing_profile": "Premium",
        "price_monthly": monthly,
        "price_semiannual": monthly * 5.94,
        "price_annual": monthly * 11,
        "available": available,
        "unavailable_reason": "out_of_stock",
        "tags": ["Standard"],
    }
    if stock_marker:
        value["stock"] = 1 if available else 0
    value.update(extra)
    return value


class FakeTelegram:
    chat_id = "123"

    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}


class MisakaProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = MisakaProvider(
            {
                "name": "misaka",
                "type": "misaka",
                "focus_regions": ["HK", "TW", "JP"],
                "focus_region_labels": {
                    "HK": "Hong Kong",
                    "TW": "Taiwan",
                    "JP": "Japan",
                },
                "minimum_regions": 1,
                "minimum_global_products": 1,
                "retry_backoff_seconds": 0,
            }
        )

    def fetch(self, regions, plans):
        def get(url):
            if url.endswith("/regions"):
                return regions
            slug = url.split("/regions/", 1)[1].split("/", 1)[0]
            return plans[slug]

        self.provider._get_json = get
        return self.provider.fetch_products()

    def test_uses_real_region_and_plan_ids_and_keeps_sold_out_focus_plan(self):
        regions = [
            region("HKG12", "HK", "Hong Kong"),
            region("TPE01", "TW", "Taipei"),
            region("NRT04", "JP", "Tokyo"),
            region("IAD01", "US", "Ashburn"),
        ]
        products = self.fetch(
            regions,
            {
                "HKG12": [plan(572, available=False)],
                "TPE01": [plan(592)],
                "NRT04": [plan(693, available=False)],
                "IAD01": [plan(85)],
            },
        )
        by_id = {item.product_id: item for item in products}
        hk = by_id["HKG12:572"]
        self.assertFalse(hk.available)
        self.assertIsNone(hk.stock)
        self.assertEqual(hk.category, "Hong Kong")
        self.assertEqual(hk.metadata["country_code"], "HK")
        self.assertEqual(
            hk.url, "https://app.misaka.io/iaas/vm/create/HKG12/small"
        )
        us = by_id["IAD01:85"]
        self.assertIsNone(us.available)
        self.assertTrue(us.metadata["catalog_only"])
        self.assertTrue(us.metadata["catalog_available"])

    def test_numeric_stock_is_preserved_only_for_focus_regions(self):
        products = self.fetch(
            [
                region("HKG12", "HK", "Hong Kong"),
                region("TPE01", "TW", "Taipei"),
                region("NRT04", "JP", "Tokyo"),
                region("IAD01", "US", "Ashburn"),
            ],
            {
                "HKG12": [plan(1, stock_marker=True)],
                "TPE01": [plan(2, available=False, stock_marker=True)],
                "NRT04": [plan(3)],
                "IAD01": [plan(4, stock_marker=True)],
            },
        )
        by_id = {item.product_id: item for item in products}
        self.assertEqual(by_id["HKG12:1"].stock, 1)
        self.assertEqual(by_id["TPE01:2"].stock, 0)
        self.assertIsNone(by_id["IAD01:4"].stock)

    def test_explicit_pricing_and_promotion_fields_are_separate(self):
        products = self.fetch(
            [
                region("HKG12", "HK", "Hong Kong"),
                region("TPE01", "TW", "Taipei"),
                region("NRT04", "JP", "Tokyo"),
            ],
            {
                "HKG12": [
                    plan(
                        1,
                        original_price_monthly=12,
                        sale_price_monthly=9,
                        discount_amount=3,
                        discount_percentage=25,
                        promotion="Launch sale",
                    )
                ],
                "TPE01": [plan(2)],
                "NRT04": [plan(3)],
            },
        )
        item = products[0]
        self.assertEqual(item.price, "$9.00 USD")
        self.assertEqual(item.pricing["semiannual"], "$59.40 USD")
        self.assertEqual(item.original_price, "$12.00 USD")
        self.assertEqual(item.sale_price, "$9.00 USD")
        self.assertEqual(item.discount_amount, "$3.00 USD")
        self.assertEqual(item.discount_percentage, "25%")
        self.assertEqual(item.promotion, "Launch sale")

    def test_catalog_only_notification_policy_only_allows_new(self):
        item = Product(
            "misaka",
            "IAD01:1",
            "Small",
            metadata={"catalog_only": True},
        )
        for kind in (
            ChangeType.RESTOCK,
            ChangeType.SOLD_OUT,
            ChangeType.STOCK,
            ChangeType.PRICE,
            ChangeType.PROMOTION,
            ChangeType.NAME,
            ChangeType.REMOVED,
        ):
            self.assertFalse(self.provider.should_notify(Change(kind, item, old=item)))
        self.assertTrue(self.provider.should_notify(Change(ChangeType.NEW, item)))

    def test_incomplete_catalog_retention_guard(self):
        self.provider.last_scan_stats = {"global_regions": 4, "global_products": 4}
        self.provider._catalog_regions = {"HKG12": {}}
        with self.assertRaises(ParseError):
            self.provider.validate_snapshot(
                [Product("misaka", "HKG12:1", "Small")],
                {
                    "catalog_stats": {
                        "global_regions": 10,
                        "global_products": 20,
                    },
                    "products": {},
                },
            )

    def test_provider_factory_builds_misaka(self):
        self.assertIsInstance(
            build_provider({"name": "misaka", "type": "misaka"}),
            MisakaProvider,
        )


class MisakaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = WatcherApp(
            {
                "state_path": str(Path(self.directory.name) / "state.json"),
                "poll_interval_seconds": 60,
                "providers": [
                    {
                        "name": "misaka",
                        "type": "misaka",
                        "interval_seconds": 60,
                        "focus_regions": ["HK", "TW", "JP"],
                        "focus_region_labels": {
                            "HK": "Hong Kong",
                            "TW": "Taiwan",
                            "JP": "Japan",
                        },
                        "minimum_regions": 1,
                        "minimum_global_products": 1,
                    }
                ],
            },
            notifications=False,
        )
        self.app.telegram = FakeTelegram()
        self.provider = self.app.providers[0]

    def tearDown(self):
        self.directory.cleanup()

    def set_catalog(self, regions, plans):
        def get(url):
            if url.endswith("/regions"):
                return regions
            slug = url.split("/regions/", 1)[1].split("/", 1)[0]
            return plans[slug]

        self.provider._get_json = get

    @staticmethod
    def base_regions():
        return [
            region("HKG12", "HK", "Hong Kong"),
            region("TPE01", "TW", "Taipei"),
            region("NRT04", "JP", "Tokyo"),
            region("IAD01", "US", "Ashburn"),
        ]

    @staticmethod
    def base_plans():
        return {
            "HKG12": [plan(1, available=False)],
            "TPE01": [plan(2)],
            "NRT04": [plan(3)],
            "IAD01": [plan(4)],
        }

    def test_first_global_and_focus_baselines_are_silent_and_complete(self):
        self.set_catalog(self.base_regions(), self.base_plans())
        self.assertTrue(self.app.check(self.provider))
        self.assertEqual(self.app.telegram.sent, [])
        entry = self.app.store.provider("misaka")
        self.assertEqual(len(entry["catalog_regions"]), 4)
        self.assertEqual(len(entry["products"]), 4)
        self.assertFalse(entry["products"]["misaka:HKG12:1"]["available"])
        self.assertIn("first_seen_at", entry["products"]["misaka:IAD01:4"]["metadata"])

    def test_new_region_and_nonfocus_product_notify_but_later_changes_do_not(self):
        self.set_catalog(self.base_regions(), self.base_plans())
        self.app.check(self.provider)
        regions = self.base_regions() + [region("SYD01", "AU", "Sydney")]
        plans = self.base_plans()
        plans["SYD01"] = [plan(5, name="Australia Small")]
        self.set_catalog(regions, plans)
        self.app.check(self.provider)
        rendered = "\n".join(text for text, _ in self.app.telegram.sent)
        self.assertEqual(len(self.app.telegram.sent), 2)
        self.assertIn("🌏 Misaka 发现新地区", rendered)
        self.assertIn("Region ID：SYD01", rendered)
        self.assertIn("🆕 Misaka 发现新品", rendered)
        self.assertIn("状态：有货", rendered)

        self.app.telegram.sent = []
        plans["SYD01"] = [plan(5, name="Australia Small", available=False, monthly=12)]
        self.app.check(self.provider)
        self.assertEqual(self.app.telegram.sent, [])
        plans["SYD01"] = []
        self.app.check(self.provider)
        self.assertEqual(self.app.telegram.sent, [])

    def test_focus_new_product_is_not_duplicated(self):
        self.set_catalog(self.base_regions(), self.base_plans())
        self.app.check(self.provider)
        plans = self.base_plans()
        plans["HKG12"].append(plan(6, name="New HK Size"))
        self.set_catalog(self.base_regions(), plans)
        self.app.check(self.provider)
        self.assertEqual(len(self.app.telegram.sent), 1)
        self.assertIn("🆕 Misaka 发现新品", self.app.telegram.sent[0][0])

    def test_failed_scan_preserves_global_and_focus_baselines(self):
        self.set_catalog(self.base_regions(), self.base_plans())
        self.app.check(self.provider)
        before = self.app.store.snapshot()["providers"]["misaka"]
        self.provider.fetch_products = lambda: (_ for _ in ()).throw(
            FetchError("one region failed")
        )
        self.assertFalse(self.app.check(self.provider))
        after = self.app.store.provider("misaka")
        self.assertEqual(after["products"], before["products"])
        self.assertEqual(after["catalog_regions"], before["catalog_regions"])
        self.assertEqual(self.app.telegram.sent, [])

    def test_status_stock_and_interval_only_count_focus_inventory(self):
        self.set_catalog(self.base_regions(), self.base_plans())
        self.app.check(self.provider)
        state = self.app.store.snapshot()
        status = format_status(state, {"misaka": 60})
        self.assertIn("重点地区：HK / TW / JP", status)
        self.assertIn("重点商品：3", status)
        self.assertIn("当前有货：2", status)
        self.assertIn("全球地区：4", status)
        self.assertIn("全球 Catalog 商品：4", status)
        stock = "\n".join(format_stock_provider(state, "misaka", 60))
        self.assertNotIn("Ashburn", stock)
        self.assertNotIn("Hong Kong", stock)
        self.assertIn("【Taiwan】", stock)
        self.assertIn("【Japan】", stock)
        self.assertIn("/iaas/vm/create/TPE01/small", stock)
        self.assertEqual(self.app.provider_interval("misaka"), 60)


class MisakaDiffTests(unittest.TestCase):
    @staticmethod
    def item(**values):
        defaults = dict(
            provider="misaka",
            product_id="HKG12:1",
            name="Small",
            available=False,
            price="$10.00 USD",
            billing_cycle="monthly",
            pricing={
                "monthly": "$10.00 USD",
                "semiannual": "$59.40 USD",
                "annual": "$110.00 USD",
            },
            metadata={"focused": True, "catalog_only": False},
        )
        defaults.update(values)
        return Product(**defaults)

    def test_focus_restock_sold_out_and_numeric_stock_changes(self):
        restock = compare_products(
            [self.item(stock=0)], [self.item(stock=1, available=True)]
        )
        self.assertEqual([x.type for x in restock], [ChangeType.RESTOCK])
        soldout = compare_products(
            [self.item(stock=1, available=True)], [self.item(stock=0)]
        )
        self.assertEqual([x.type for x in soldout], [ChangeType.SOLD_OUT])
        stock = compare_products(
            [self.item(stock=5, available=True)],
            [self.item(stock=4, available=True)],
        )
        self.assertEqual([x.type for x in stock], [ChangeType.STOCK])

    def test_all_cycle_price_changes_are_detected(self):
        current = self.item()
        current.pricing = dict(current.pricing, annual="$120.00 USD")
        changes = compare_products([self.item()], [current])
        self.assertEqual([x.type for x in changes], [ChangeType.PRICE])
        self.assertEqual(changes[0].fields, ["pricing"])

    def test_promotion_start_change_and_end(self):
        normal = self.item()
        sale = self.item(
            price="$8.00 USD",
            original_price="$10.00 USD",
            sale_price="$8.00 USD",
            discount_percentage="20%",
            promotion="Sale",
        )
        started = compare_products([normal], [sale])
        self.assertEqual([x.type for x in started], [ChangeType.PROMOTION])
        changed_sale = self.item(
            price="$7.00 USD",
            original_price="$10.00 USD",
            sale_price="$7.00 USD",
            discount_percentage="30%",
            promotion="Sale",
        )
        changed = compare_products([sale], [changed_sale])
        self.assertEqual([x.type for x in changed], [ChangeType.PROMOTION])
        ended = compare_products([sale], [normal])
        self.assertEqual([x.type for x in ended], [ChangeType.PROMOTION])
        self.assertIn("🏷️ Misaka 开始促销", format_change(started[0], []))
        self.assertIn("🏷️ Misaka 折扣变化", format_change(changed[0], []))
        self.assertIn("🏷️ Misaka 促销结束", format_change(ended[0], []))

    def test_focus_removed_and_new_are_detected(self):
        self.assertEqual(
            [x.type for x in compare_products([], [self.item()])],
            [ChangeType.NEW],
        )
        self.assertEqual(
            [x.type for x in compare_products([self.item()], [])],
            [ChangeType.REMOVED],
        )


if __name__ == "__main__":
    unittest.main()
