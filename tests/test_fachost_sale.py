from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from watcher.diff import compare_products
from watcher.fac_tg_watch import (
    fac_links,
    format_relay,
    group_relay_reason,
    message_urls,
)
from watcher.models import ChangeType
from watcher.providers import build_provider
from watcher.providers.base import FetchError
from watcher.providers.fachost_sale import FachostSaleProvider
from watcher.telegram import format_change

from tests.test_fachost import category_html


SLUG = "special-regulations"
URL = "https://fachost.cloud/products/%s" % SLUG


def response(status, text="", url=URL):
    return SimpleNamespace(status_code=status, text=text, url=url)


def open_page(stock_text="2 available"):
    return category_html(
        SLUG,
        [(SLUG, "Special Regulations", 11), ("tw-seednet-vds", "TW-SeedNet-VDS", 12)],
        [
            {
                "id": 50,
                "name": "Seednet VDS Black Friday 2026",
                "product_slug": "seednet-vds-black-friday-2026",
                "stock_text": stock_text,
                "price": "$28,49",
            }
        ],
    )


PLANS = """
<html><body><select id="plan_id">
  <option value="1"> 月付 - $28,49 </option>
  <option value="2"> 年付 - $246,99 </option>
</select></body></html>
"""


def provider():
    return FachostSaleProvider(
        {"name": "fachost_sale", "type": "fachost_sale", "request_spacing_seconds": 0}
    )


class FachostSaleProviderTest(unittest.TestCase):
    def test_factory_builds_provider(self):
        self.assertIsInstance(
            build_provider({"name": "fachost_sale", "type": "fachost_sale"}),
            FachostSaleProvider,
        )

    def test_hidden_category_is_one_closed_pseudo_product(self):
        item = provider()
        with patch.object(item.session, "get", return_value=response(404, "Not found")):
            products = item.fetch_products()
        self.assertEqual([p.product_id for p in products], ["category:%s" % SLUG])
        self.assertIs(products[0].available, False)
        self.assertEqual(item.last_scan_stats["requests"], 1)

    def test_opening_alerts_and_reads_cards_and_billing_plans(self):
        item = provider()
        with patch.object(item.session, "get", return_value=response(404, "Not found")):
            closed = item.fetch_products()
        checkout = "%s/seednet-vds-black-friday-2026/checkout" % URL
        pages = [response(200, open_page()), response(200, PLANS, checkout)]
        with patch.object(item.session, "get", side_effect=pages):
            opened = item.fetch_products()
        by_id = {p.product_id: p for p in opened}
        self.assertIs(by_id["category:%s" % SLUG].available, True)
        self.assertEqual(by_id["50"].name, "Seednet VDS Black Friday 2026")
        self.assertEqual(by_id["50"].pricing, {"月付": "$28,49", "年付": "$246,99"})

        changes = compare_products(closed, opened)
        types = {(c.type, c.product.product_id) for c in changes}
        self.assertIn((ChangeType.RESTOCK, "category:%s" % SLUG), types)
        self.assertIn((ChangeType.NEW, "50"), types)
        opened_alert = next(c for c in changes if c.product.product_id.startswith("category:"))
        self.assertIn("活动分类已开放", format_change(opened_alert, []))
        new_alert = next(c for c in changes if c.product.product_id == "50")
        self.assertIn("年付：$246,99", format_change(new_alert, []))

    def test_sold_out_keeps_known_plans(self):
        item = provider()
        checkout = "%s/seednet-vds-black-friday-2026/checkout" % URL
        with patch.object(
            item.session,
            "get",
            side_effect=[response(200, open_page()), response(200, PLANS, checkout)],
        ):
            before = item.fetch_products()
        with patch.object(item.session, "get", return_value=response(200, open_page("Sold out"))):
            after = item.fetch_products()
        changes = compare_products(before, after)
        self.assertEqual(
            [(c.type, c.product.product_id) for c in changes],
            [(ChangeType.SOLD_OUT, "50")],
        )

    def test_block_page_fails_the_round(self):
        item = provider()
        with patch.object(item.session, "get", return_value=response(429, "slow down")):
            with self.assertRaises(FetchError):
                item.fetch_products()

    def test_redirect_away_counts_as_closed(self):
        item = provider()
        with patch.object(
            item.session, "get", return_value=response(200, "<html></html>", "https://fachost.cloud/")
        ):
            products = item.fetch_products()
        self.assertIs(products[0].available, False)


class FacTelegramRelayTest(unittest.TestCase):
    def test_collects_text_entity_and_button_links(self):
        urls = message_urls(
            "下單：https://fachost.cloud/products/tw-nat",
            [SimpleNamespace(url="https://fachost.cloud/products/special-regulations")],
            [[SimpleNamespace(url="https://t.me/fachost_cloud")]],
        )
        self.assertEqual(
            fac_links(urls),
            [
                "https://fachost.cloud/products/tw-nat",
                "https://fachost.cloud/products/special-regulations",
            ],
        )

    def test_group_relay_rules(self):
        keywords = ["黑五", "年付"]
        link = ["https://fachost.cloud/products/x"]
        self.assertEqual(group_relay_reason("看这个", link, False, keywords), "含 FAC 链接")
        self.assertEqual(
            group_relay_reason("黑五要来了", [], True, keywords), "管理员 + 活动关键词"
        )
        self.assertIsNone(group_relay_reason("黑五要来了", [], False, keywords))
        self.assertIsNone(group_relay_reason("早安", [], True, keywords))

    def test_relay_message_escapes_text_and_lists_links(self):
        text = format_relay(
            "channel",
            datetime(2026, 11, 19, 12, 27, tzinfo=timezone.utc),
            "<b>黑五</b> https://fachost.cloud/products/special-regulations",
            ["https://fachost.cloud/products/special-regulations"],
            "https://t.me/fachost_cloud/40",
        )
        self.assertIn("11-19 20:27", text)
        self.assertIn("&lt;b&gt;黑五&lt;/b&gt;", text)
        self.assertIn('href="https://fachost.cloud/products/special-regulations"', text)
        self.assertIn("打开原消息", text)


if __name__ == "__main__":
    unittest.main()
