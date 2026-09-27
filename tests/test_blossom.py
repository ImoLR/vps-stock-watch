from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.models import Change, ChangeType, Product
from watcher.providers.base import FetchError, ParseError
from watcher.providers.blossom import BlossomProvider
from watcher.state import StateStore
from watcher.telegram import format_change, format_status


FIXTURE = Path(__file__).parent / "fixtures" / "blossom_catalog.json"


def blossom(product_id: str = "isp-xs", **values) -> Product:
    defaults = {
        "provider": "blossom",
        "product_id": product_id,
        "name": "ISP-XS",
        "region": "Washington, DC",
        "price": "$19 USD",
        "billing_cycle": "monthly",
        "stock": 0,
        "available": False,
    }
    defaults.update(values)
    return Product(**defaults)


class BlossomProviderTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.provider = BlossomProvider({"name": "blossom", "type": "blossom"})

    def test_maps_complete_catalog_and_keeps_same_name_locations_separate(self):
        products = self.provider.parse_catalog(self.payload)
        self.assertEqual(len(products), 4)
        washington, seattle = products[:2]
        self.assertEqual(washington.name, seattle.name)
        self.assertNotEqual(washington.product_id, seattle.product_id)
        self.assertEqual(washington.region, "Washington, DC")
        self.assertEqual(seattle.region, "Seattle, WA")
        self.assertEqual(washington.stock, 0)
        self.assertEqual(seattle.stock, 3)
        self.assertFalse(washington.available)
        self.assertTrue(seattle.available)
        self.assertEqual(washington.price, "$19 USD")
        self.assertEqual(washington.specs["disk"], "20 GB + 40 GB HDD")
        self.assertEqual(seattle.url, "https://blossomhost.us/#/buy/isp-seattle-xs")
        residential = products[3]
        self.assertEqual(residential.product_id, "residential-offer-2")
        self.assertEqual(residential.stock, 0)
        self.assertFalse(residential.available)
        self.assertIsNone(residential.price)
        self.assertEqual(residential.specs["ram"], "3 GB")
        self.assertTrue(washington.metadata["notification_suppressed"])
        self.assertTrue(products[2].metadata["notification_suppressed"])
        self.assertFalse(residential.metadata["notification_suppressed"])
        self.assertFalse(
            self.provider.should_notify(Change(ChangeType.RESTOCK, washington))
        )
        self.assertTrue(
            self.provider.should_notify(Change(ChangeType.RESTOCK, residential))
        )

    def test_fetch_uses_public_json_catalog(self):
        with patch.object(self.provider, "get_json", return_value=self.payload) as get_json:
            products = self.provider.fetch_products()
        get_json.assert_called_once_with("https://blossomhost.us/api/catalog")
        self.assertEqual(len(products), 4)

    def test_missing_numeric_stock_stays_none(self):
        del self.payload["plans"][0]["stock_count"]
        product = self.provider.parse_catalog(self.payload)[0]
        self.assertIsNone(product.stock)
        self.assertFalse(product.available)

    def test_schema_failure_rejects_incomplete_snapshot(self):
        del self.payload["plans"][0]["in_stock"]
        with self.assertRaises(ParseError):
            self.provider.parse_catalog(self.payload)


class BlossomLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.directory.name) / "state.json"))

    def tearDown(self):
        self.directory.cleanup()

    def record(self, products):
        return self.store.record_success("blossom", products)

    def test_first_blossom_snapshot_is_baseline_without_notifications(self):
        first, changes = self.record([blossom()])
        self.assertTrue(first)
        self.assertEqual(changes, [])

    def test_new_sku_is_detected(self):
        self.record([blossom()])
        _, changes = self.record([blossom(), blossom("isp-new", name="ISP-NEW")])
        self.assertEqual([change.type for change in changes], [ChangeType.NEW])

    def test_restock_is_detected_with_stock_change(self):
        self.record([blossom(stock=0, available=False)])
        _, changes = self.record([blossom(stock=3, available=True)])
        self.assertEqual([change.type for change in changes], [ChangeType.RESTOCK])
        self.assertEqual(changes[0].fields, ["available", "stock"])

    def test_sold_out_is_detected_with_stock_change(self):
        self.record([blossom(stock=1, available=True)])
        _, changes = self.record([blossom(stock=0, available=False)])
        self.assertEqual([change.type for change in changes], [ChangeType.SOLD_OUT])
        self.assertEqual(changes[0].fields, ["available", "stock"])

    def test_any_numeric_stock_change_is_detected(self):
        self.record([blossom(stock=5, available=True)])
        _, changes = self.record([blossom(stock=4, available=True)])
        self.assertEqual([change.type for change in changes], [ChangeType.STOCK])

    def test_price_change_is_detected(self):
        self.record([blossom(price="$19 USD")])
        _, changes = self.record([blossom(price="$22 USD")])
        self.assertEqual([change.type for change in changes], [ChangeType.PRICE])

    def test_removed_product_is_detected_after_success(self):
        self.record([blossom(), blossom("isp-s", name="ISP-S")])
        _, changes = self.record([blossom()])
        self.assertEqual([change.type for change in changes], [ChangeType.REMOVED])

    def test_provider_failure_preserves_blossom_snapshot(self):
        self.record([blossom()])
        before = self.store.snapshot()["providers"]["blossom"]["products"]
        self.store.record_failure("blossom", str(FetchError("HTTP 503")))
        self.assertEqual(self.store.provider("blossom")["products"], before)

    def test_boolean_only_stock_does_not_invent_numeric_change(self):
        self.record([blossom(stock=None, available=False)])
        _, changes = self.record([blossom(stock=None, available=True)])
        self.assertEqual([change.type for change in changes], [ChangeType.RESTOCK])
        self.assertEqual(changes[0].fields, ["available"])

    def test_notifications_and_status_use_blossom_host_name(self):
        message = format_change(
            type("ChangeLike", (), {"product": blossom(), "old": None, "type": ChangeType.NEW})(),
            [],
        )
        self.assertIn("Blossom Host 发现新品", message)
        self.record([blossom()])
        status = format_status(self.store.snapshot())
        self.assertIn("<b>Blossom Host</b>", status)
        self.assertIn("Provider 数量：1", status)
        self.assertIn("商品数量：1", status)


if __name__ == "__main__":
    unittest.main()
