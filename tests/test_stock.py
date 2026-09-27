from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from watcher.app import WatcherApp
from watcher.models import Change, ChangeType, Product
from watcher.providers.base import BaseProvider
from watcher.state import StateStore
from watcher.telegram import (
    BOT_COMMANDS,
    INTERVAL_MENU_CALLBACK,
    STOCK_MENU_CALLBACK,
    STOCK_PROVIDER_PREFIX,
    available_products,
    format_change,
    format_stock_provider,
    is_currently_available,
    is_hidden_inventory,
    stock_menu_markup,
    stock_provider_markup,
)


ADMIN_ID = "12345"
PROVIDER_NAMES = [
    "nexkr",
    "dmit",
    "blossom",
    "boilcloud",
    "fachost",
    "leikwanhost",
    "liqunhuiju",
    "vmsilo",
]


def product(
    provider,
    product_id,
    name=None,
    stock=None,
    available=True,
    hidden=False,
    notification_suppressed=False,
    source=None,
    category="VPS",
    price="$10 USD",
    billing_cycle="monthly",
):
    source = source or ("extra_product_urls" if hidden else "catalog")
    return Product(
        provider=provider,
        product_id=str(product_id),
        name=name or "%s plan %s" % (provider, product_id),
        category=category,
        region="Test Region",
        price=price,
        billing_cycle=billing_cycle,
        stock=stock,
        available=available,
        url="https://example.test/buy/%s/%s" % (provider, product_id),
        metadata={
            "discovery": {
                "type": "extra_url" if hidden else "catalog",
                "hidden": hidden,
                "source": source,
            },
            "notification_suppressed": notification_suppressed,
        },
    )


class FakeTelegram:
    def __init__(self):
        self.chat_id = ADMIN_ID
        self.sent = []
        self.answered = []
        self.registered_commands = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_query_id, text=None, show_alert=False):
        self.answered.append((callback_query_id, text, show_alert))

    def commands(self, _offset):
        return []

    def set_commands(self, commands):
        self.registered_commands = list(commands)


def message(text, chat_id=ADMIN_ID, user_id=ADMIN_ID):
    return {
        "message": {
            "from": {"id": int(user_id)},
            "chat": {"id": int(chat_id)},
            "text": text,
        }
    }


def callback(data, chat_id=ADMIN_ID, user_id=ADMIN_ID):
    return {
        "callback_query": {
            "id": "stock-callback",
            "from": {"id": int(user_id)},
            "message": {"chat": {"id": int(chat_id)}, "message_id": 7},
            "data": data,
        }
    }


class StockQueryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.directory.name) / "state.json")
        self.app = WatcherApp(
            {
                "state_path": self.state_path,
                "poll_interval_seconds": 60,
                "providers": [
                    {"name": name, "type": name, "interval_seconds": 60}
                    for name in PROVIDER_NAMES
                ],
            },
            notifications=False,
        )
        self.app.telegram = FakeTelegram()

    def tearDown(self):
        self.directory.cleanup()

    def save_products(self, provider, products):
        self.app.store.record_success(provider, products)
        self.app.store.save()

    def seed_all_providers(self):
        for name in PROVIDER_NAMES:
            self.save_products(
                name,
                [
                    product(name, "1", stock=3),
                    product(name, "2", stock=0, available=False),
                ],
            )

    def test_stock_command_is_registered_without_removing_status(self):
        self.assertEqual(
            BOT_COMMANDS,
            [
                {"command": "status", "description": "查看监控状态"},
                {"command": "stock", "description": "查询当前可购买库存"},
            ],
        )
        self.app.configure_bot_commands()
        self.assertEqual(self.app.telegram.registered_commands, BOT_COMMANDS)

    def test_stock_home_shows_eight_providers_with_live_available_counts(self):
        self.seed_all_providers()
        self.app.process_update(message("/stock"))
        text, markup = self.app.telegram.sent[-1]
        self.assertIn("📦 当前可购买库存", text)
        labels = [row[0]["text"] for row in markup["inline_keyboard"]]
        self.assertEqual(
            labels,
            [
                "NexKr · 1",
                "DMIT · 1",
                "Blossom Host · 1",
                "BOILCLOUD · 1",
                "FACHOST · 1",
                "LeiKwanHost · 1",
                "利群汇聚 · 1",
                "VMSILO · 1",
                "🔄 刷新",
            ],
        )

    def test_status_and_interval_menu_show_all_eight_providers(self):
        self.seed_all_providers()
        self.app.process_update(message("/status"))
        status, _markup = self.app.telegram.sent[-1]
        self.assertIn("Provider 数量：8", status)
        self.assertIn("<b>利群汇聚</b>", status)
        self.assertIn("<b>VMSILO</b>", status)
        self.app.process_update(callback(INTERVAL_MENU_CALLBACK))
        text, markup = self.app.telegram.sent[-1]
        self.assertIn("利群汇聚：60 秒", text)
        self.assertIn("VMSILO：60 秒", text)
        labels = [row[0]["text"] for row in markup["inline_keyboard"]]
        self.assertEqual(
            labels,
            [
                "NexKr",
                "DMIT",
                "Blossom Host",
                "BOILCLOUD",
                "FACHOST",
                "LeiKwanHost",
                "利群汇聚",
                "VMSILO",
                "返回",
            ],
        )

    def test_available_filter_uses_real_stock_and_never_invents_one(self):
        self.assertTrue(is_currently_available(product("x", "1", stock=3).to_dict()))
        self.assertFalse(
            is_currently_available(product("x", "2", stock=0, available=True).to_dict())
        )
        self.assertFalse(
            is_currently_available(product("x", "3", stock=3, available=False).to_dict())
        )
        boolean_only = product("x", "4", stock=None, available=True).to_dict()
        self.assertTrue(is_currently_available(boolean_only))
        state = {
            "providers": {
                "x": {
                    "last_success_at": "2026-09-27T12:00:00Z",
                    "products": {"x:4": boolean_only},
                }
            }
        }
        messages = format_stock_provider(
            state,
            "x",
            60,
            now=datetime(2026, 9, 27, 12, 0, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(len(messages), 1)
        self.assertIn("x plan 4 · $10 USD/月", messages[0])
        self.assertNotIn("库存：", messages[0])

    def test_provider_list_is_compact_grouped_and_keeps_hidden_after_normal(self):
        self.save_products(
            "fachost",
            [
                product("fachost", "1", stock=3, category="Cloud VPS"),
                product(
                    "fachost",
                    "22",
                    name="Hinet-VDS-Lite",
                    hidden=True,
                    category="TW-Hinet-VDS",
                ),
            ],
        )
        self.app.send_stock_provider("fachost")
        text, _markup = self.app.telegram.sent[-1]
        self.assertEqual(text.count("🟢 正常库存"), 1)
        self.assertIn("🟢 正常库存", text)
        self.assertIn("🕵️ 隐藏库存", text)
        self.assertLess(text.index("🟢 正常库存"), text.index("🕵️ 隐藏库存"))
        self.assertIn("【Cloud VPS】", text)
        self.assertIn("【TW-Hinet-VDS】", text)
        self.assertIn("Hinet-VDS-Lite", text)
        self.assertIn('href="https://example.test/buy/fachost/22"', text)
        self.assertIn("🕵️ 其中隐藏库存：1", text)
        for forbidden in ("地区：", "ID / PID", "discovery"):
            self.assertNotIn(forbidden, text)
        self.assertNotIn("\n库存：", text)

    def test_hidden_detection_is_provenance_based_not_pid_or_name(self):
        normal_22 = product("fachost", "22", name="hidden old lite", hidden=False).to_dict()
        hidden_999 = product("fachost", "999", name="Ordinary Plan", hidden=True).to_dict()
        self.assertFalse(is_hidden_inventory(normal_22))
        self.assertTrue(is_hidden_inventory(hidden_999))

    def test_boilcloud_navigation_only_catalog_is_normal_inventory(self):
        item = product("boilcloud", "381", hidden=False).to_dict()
        item["metadata"]["navigation_only"] = True
        item["metadata"]["discovery"]["source"] = "homepage_navigation"
        self.assertFalse(is_hidden_inventory(item))

    def test_blossom_silent_isp_and_metal_are_still_visible_as_normal(self):
        isp = product(
            "blossom",
            "isp-xs",
            stock=3,
            notification_suppressed=True,
            category="🏡 ISP Line — Washington/Verizon or Seattle/Astound",
        )
        isp.metadata["family"] = "isp"
        metal = product(
            "blossom",
            "metal-1",
            stock=2,
            notification_suppressed=True,
            category="Bare metal — dedicated physical servers",
        )
        metal.metadata["family"] = "metal"
        self.save_products("blossom", [isp, metal])
        shown = available_products(self.app.store.provider("blossom"))
        self.assertEqual({item["product_id"] for item in shown}, {"isp-xs", "metal-1"})
        self.assertTrue(all(not is_hidden_inventory(item) for item in shown))
        rendered = "\n".join(
            format_stock_provider(self.app.store.snapshot(), "blossom", 60)
        )
        self.assertIn("【🏡 ISP Line — Washington/Verizon or Seattle/Astound】", rendered)
        self.assertIn("【Bare metal — dedicated physical servers】", rendered)
        self.assertNotIn("🕵️ 隐藏库存", rendered)
        provider = next(item for item in self.app.providers if item.name == "blossom")
        self.assertFalse(provider.should_notify(Change(ChangeType.STOCK, isp, old=isp)))
        self.assertFalse(provider.should_notify(Change(ChangeType.STOCK, metal, old=metal)))

    def test_available_products_sort_normal_inventory_before_hidden(self):
        self.save_products(
            "fachost",
            [
                product("fachost", "1", name="AAA normal"),
                product("fachost", "2", name="ZZZ hidden", hidden=True),
            ],
        )
        shown = available_products(self.app.store.provider("fachost"))
        self.assertEqual([item["product_id"] for item in shown], ["1", "2"])

    def test_long_catalog_is_sent_as_consecutive_chunks_with_only_final_back_button(self):
        products = [
            product(
                "dmit",
                str(index),
                name="Plan %02d with a deliberately descriptive product name" % index,
                category="Category %02d" % ((index - 1) // 3),
            )
            for index in range(1, 16)
        ]
        self.save_products("dmit", products)
        with patch("watcher.app.format_stock_provider") as formatter:
            formatter.return_value = ["chunk one (1/2)", "chunk two (2/2)"]
            self.app.send_stock_provider("dmit")
        self.assertEqual(
            self.app.telegram.sent,
            [
                ("chunk one (1/2)", None),
                ("chunk two (2/2)", stock_provider_markup()),
            ],
        )

    def test_chunker_keeps_categories_whole_before_splitting_product_lines(self):
        products = [
            product("dmit", "1", name="A one", category="Alpha"),
            product("dmit", "2", name="A two", category="Alpha"),
            product("dmit", "3", name="B one", category="Beta"),
            product("dmit", "4", name="B two", category="Beta"),
        ]
        self.save_products("dmit", products)
        messages = format_stock_provider(
            self.app.store.snapshot(), "dmit", 60, max_length=500
        )
        self.assertEqual(len(messages), 2)
        rendered = "\n".join(messages)
        self.assertEqual(rendered.count("【Alpha】"), 1)
        self.assertEqual(rendered.count("【Beta】"), 1)
        self.assertNotIn("· 续】", rendered)
        self.assertIn("(1/2)", messages[0])
        self.assertIn("(2/2)", messages[1])
        self.assertNotIn("上一页", rendered)
        self.assertNotIn("下一页", rendered)
        self.assertNotIn("第 ", rendered)
        self.assertTrue(all(len(message) <= 500 for message in messages))

    def test_oversized_category_splits_only_between_lines_and_repeats_continuation(self):
        products = [
            product(
                "leikwanhost",
                str(index),
                name="Long plan %02d with enough text to force another message" % index,
                category="One Real Category",
            )
            for index in range(1, 9)
        ]
        self.save_products("leikwanhost", products)
        messages = format_stock_provider(
            self.app.store.snapshot(), "leikwanhost", 60, max_length=430
        )
        self.assertGreater(len(messages), 1)
        rendered = "\n".join(messages)
        self.assertIn("【One Real Category · 续】", rendered)
        for index, message_text in enumerate(messages, 1):
            self.assertIn("(%d/%d)" % (index, len(messages)), message_text)
        for index in range(1, 9):
            self.assertEqual(rendered.count("Long plan %02d" % index), 1)
        self.assertTrue(all(len(message) <= 430 for message in messages))

    def test_all_eight_providers_use_their_saved_category_labels(self):
        for index, name in enumerate(PROVIDER_NAMES, 1):
            category = "Saved category %d" % index
            self.save_products(
                name,
                [product(name, str(index), category=category, stock=1)],
            )
            rendered = "\n".join(
                format_stock_provider(self.app.store.snapshot(), name, 60)
            )
            self.assertIn("【%s】" % category, rendered)

    def test_provider_page_callbacks_and_return_work(self):
        self.seed_all_providers()
        self.app.process_update(callback(STOCK_PROVIDER_PREFIX + "fachost:0"))
        self.assertIn("FACHOST 当前库存", self.app.telegram.sent[-1][0])
        self.app.process_update(callback(STOCK_MENU_CALLBACK))
        self.assertIn("当前可购买库存", self.app.telegram.sent[-1][0])

    def test_refresh_reloads_state_file(self):
        self.save_products("nexkr", [product("nexkr", "1")])
        external = StateStore(self.state_path)
        external.record_success(
            "nexkr", [product("nexkr", "1"), product("nexkr", "2")]
        )
        external.save()
        self.app.process_update(callback(STOCK_MENU_CALLBACK))
        _text, markup = self.app.telegram.sent[-1]
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "NexKr · 2")

    def test_stock_query_never_fetches_provider_or_starts_browser(self):
        self.seed_all_providers()
        fetches = []
        for provider in self.app.providers:
            provider.fetch_products = Mock(side_effect=AssertionError("network fetch called"))
            fetches.append(provider.fetch_products)
        with patch.object(
            BaseProvider,
            "_get_text_browser",
            side_effect=AssertionError("browser started"),
        ) as browser:
            self.app.process_update(message("/stock"))
            self.app.process_update(callback(STOCK_PROVIDER_PREFIX + "dmit:0"))
            self.app.process_update(callback(STOCK_MENU_CALLBACK))
        self.assertTrue(all(not fetch.called for fetch in fetches))
        browser.assert_not_called()

    def test_non_admin_message_and_callback_are_rejected(self):
        self.seed_all_providers()
        self.app.process_update(message("/stock", user_id="99999"))
        self.assertEqual(self.app.telegram.sent, [])
        self.app.process_update(
            callback(STOCK_PROVIDER_PREFIX + "fachost:0", user_id="99999")
        )
        self.assertEqual(self.app.telegram.sent, [])
        self.assertEqual(
            self.app.telegram.answered[-1],
            ("stock-callback", "无权限", True),
        )

    def test_stale_warning_uses_twice_the_provider_interval(self):
        item = product("nexkr", "1").to_dict()
        state = {
            "providers": {
                "nexkr": {
                    "last_success_at": "2026-09-27T12:00:00Z",
                    "products": {"nexkr:1": item},
                }
            }
        }
        fresh = format_stock_provider(
            state,
            "nexkr",
            60,
            now=datetime(2026, 9, 27, 12, 1, 59, tzinfo=timezone.utc),
        )[0]
        stale = format_stock_provider(
            state,
            "nexkr",
            60,
            now=datetime(2026, 9, 27, 12, 2, 1, tzinfo=timezone.utc),
        )[0]
        self.assertNotIn("数据可能已过期", fresh)
        self.assertIn("⚠️ 数据可能已过期", stale)

    def test_single_page_has_only_back_button(self):
        markup = stock_provider_markup()
        self.assertEqual(
            markup,
            {
                "inline_keyboard": [
                    [{"text": "← 返回网站列表", "callback_data": STOCK_MENU_CALLBACK}]
                ]
            },
        )

    def test_menu_markup_does_not_depend_on_notification_policy(self):
        silent = product(
            "blossom",
            "isp-xs",
            stock=3,
            notification_suppressed=True,
        ).to_dict()
        state = {
            "providers": {
                "blossom": {"products": {"blossom:isp-xs": silent}}
            }
        }
        markup = stock_menu_markup(state, ["blossom"])
        self.assertEqual(markup["inline_keyboard"][0][0]["text"], "Blossom Host · 1")

    def test_active_change_notification_format_remains_detailed(self):
        item = product("nexkr", "42", stock=3)
        item.specs = {"cpu": "2 cores"}
        text = format_change(Change(ChangeType.STOCK, item, fields=["stock"]), [])
        self.assertIn("📦 NexKr 库存变化", text)
        self.assertIn("ID / PID：42", text)
        self.assertIn("地区：Test Region", text)
        self.assertIn("CPU：2 cores", text)
        self.assertIn("库存：3", text)


if __name__ == "__main__":
    unittest.main()
