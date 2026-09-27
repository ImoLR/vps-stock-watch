from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.providers.dmit import DmitProvider
from watcher.providers.generic import GenericProvider
from watcher.providers.nexkr import NexKrProvider
from watcher.providers.base import FetchError, ParseError


FIXTURES = Path(__file__).parent / "fixtures"


class ProviderTests(unittest.TestCase):
    def test_browser_resources_close_when_page_content_fails(self):
        events = []

        class FakePlaywrightError(Exception):
            pass

        class FakePage:
            def __init__(self):
                self.request = types.SimpleNamespace(resource_type="image")

            def route(self, pattern, handler):
                events.append(("route", pattern))

            def goto(self, *args, **kwargs):
                return None

            def wait_for_timeout(self, _milliseconds):
                return None

            def content(self):
                raise FakePlaywrightError("renderer closed")

            def close(self):
                events.append("page.close")

        class FakeContext:
            def new_page(self):
                return FakePage()

            def close(self):
                events.append("context.close")

        class FakeBrowser:
            def new_context(self, **kwargs):
                self.context_options = kwargs
                return FakeContext()

            def close(self):
                events.append("browser.close")

        class FakeChromium:
            def launch(self, **kwargs):
                self.launch_options = kwargs
                return FakeBrowser()

        class FakePlaywright:
            chromium = FakeChromium()

        class FakePlaywrightContext:
            def __enter__(self):
                return FakePlaywright()

            def __exit__(self, *_args):
                return False

        fake_sync_api = types.ModuleType("playwright.sync_api")
        fake_sync_api.Error = FakePlaywrightError
        fake_sync_api.sync_playwright = lambda: FakePlaywrightContext()
        provider = DmitProvider({"name": "dmit", "type": "dmit"})
        with patch.dict(sys.modules, {"playwright.sync_api": fake_sync_api}):
            with self.assertRaises(FetchError):
                provider._get_text_browser("https://example.test/")
        self.assertEqual(events[-3:], ["page.close", "context.close", "browser.close"])

    def test_nexkr_json_mapping(self):
        payload = json.loads((FIXTURES / "nexkr_groups.json").read_text(encoding="utf-8"))
        provider = NexKrProvider({"name": "nexkr", "type": "nexkr"})
        with patch.object(provider, "get_json", return_value=payload):
            products = provider.fetch_products()
        self.assertEqual(len(products), 1)
        item = products[0]
        self.assertEqual(item.product_id, "1")
        self.assertEqual(item.price, "$38.00 USD")
        self.assertTrue(item.available)
        self.assertIsNone(item.stock)
        self.assertEqual(item.specs["cpu"], "1 dedicated vCPU")
        self.assertEqual(item.specs["ram"], "2 GB")
        self.assertIn("1Gbps", item.specs["bandwidth"])

    def test_nexkr_null_product_zones_mean_no_zone(self):
        payload = json.loads((FIXTURES / "nexkr_groups.json").read_text(encoding="utf-8"))
        payload["groups"][0]["products"][0]["zones"] = None
        provider = NexKrProvider({"name": "nexkr", "type": "nexkr"})
        with patch.object(provider, "get_json", return_value=payload):
            products = provider.fetch_products()
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0].region, "KR · KT")
        self.assertEqual(products[0].metadata["zone_ids"], [])

    def test_nexkr_rejects_non_list_product_zones(self):
        payload = json.loads((FIXTURES / "nexkr_groups.json").read_text(encoding="utf-8"))
        payload["groups"][0]["products"][0]["zones"] = "1"
        provider = NexKrProvider({"name": "nexkr", "type": "nexkr"})
        with patch.object(provider, "get_json", return_value=payload):
            with self.assertRaises(ParseError):
                provider.fetch_products()

    def test_dmit_html_mapping(self):
        html = (FIXTURES / "dmit_catalog.html").read_text(encoding="utf-8")
        provider = DmitProvider({"name": "dmit", "type": "dmit"})
        products = provider.parse_catalog(html)
        self.assertEqual(len(products), 2)
        available, soldout = products
        self.assertEqual(available.product_id, "265")
        self.assertEqual(available.region, "Hong Kong")
        self.assertEqual(available.category, "GENERAL")
        self.assertEqual(available.price, "$39.90 USD")
        self.assertEqual(available.billing_cycle, "monthly")
        self.assertTrue(available.available)
        self.assertIsNone(available.stock)
        self.assertEqual(available.specs["traffic"], "500GB")
        self.assertEqual(available.specs["bandwidth"], "1Gbps; Unmetered 4Mbps")
        self.assertFalse(soldout.available)

    def test_dmit_pid_probe_signals(self):
        provider = DmitProvider({"name": "dmit", "type": "dmit", "fetch_mode": "http"})
        with patch.object(provider, "get_text", return_value="<h1>Out of Stock</h1>"):
            self.assertFalse(provider.probe_pid("183"))
        with patch.object(provider, "get_text", return_value="<h1>Product Configuration</h1>"):
            self.assertTrue(provider.probe_pid("999"))

    def test_generic_json_provider(self):
        provider = GenericProvider(
            {
                "name": "example",
                "type": "generic",
                "catalog_url": "https://example.test/api/products",
                "format": "json",
                "item_rule": {"type": "jsonpath", "expression": "$.products[*]"},
                "fields": {
                    "product_id": {"type": "jsonpath", "expression": "$.sku"},
                    "name": {"type": "jsonpath", "expression": "$.title"},
                    "stock": {
                        "type": "jsonpath",
                        "expression": "$.inventory",
                        "transforms": ["int"],
                    },
                    "url": {"type": "jsonpath", "expression": "$.url"},
                },
            }
        )
        payload = '{"products":[{"sku":"a1","title":"Plan A","inventory":"3","url":"/buy/a1"}]}'
        with patch.object(provider, "get_text", return_value=payload):
            products = provider.fetch_products()
        self.assertEqual(products[0].product_id, "a1")
        self.assertEqual(products[0].stock, 3)
        self.assertTrue(products[0].available)
        self.assertEqual(products[0].url, "https://example.test/buy/a1")


if __name__ == "__main__":
    unittest.main()
