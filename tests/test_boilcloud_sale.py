from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from watcher.diff import compare_products
from watcher.fac_trigger import take_triggers
from watcher.models import Change, ChangeType
from watcher.providers import build_provider
from watcher.providers.base import FetchError
from watcher.providers.boilcloud_sale import BoilcloudSaleProvider
from watcher.telegram import format_change

from tests.test_boilcloud import category_html


SLUG = "festive-offer"
URL = "https://cloud.boil.network/store/%s" % SLUG
FEED = "https://cloud.boil.network/feeds/productsinfo.php"
PUBLIC = [("hong-kong-shared-bandwidth", "香港家寬"), ("taiwan-shared-bandwidth", "台灣家寬")]


def response(status, text="", url=URL):
    return SimpleNamespace(status_code=status, text=text, url=url)


def feed(value):
    return response(200, "document.write('%s');" % value, FEED)


def empty_page():
    return category_html(PUBLIC, [], current=None, empty=True)


def open_page(stock=3, listed=False):
    categories = PUBLIC + ([(SLUG, "節日特別提供")] if listed else [])
    return category_html(
        categories,
        [{"pid": 540, "name": "2026 黑五 HiNet 500Mbps", "stock": stock, "cycle": ("annually", "年繳")}],
        current=SLUG,
    )


def provider(**extra):
    config = {
        "name": "boilcloud_sale",
        "type": "boilcloud_sale",
        "feed_start_pid": 528,
        "feed_every_rounds": 3,
        "probe_ahead": 2,
    }
    config.update(extra)
    item = BoilcloudSaleProvider(config)
    item._sleep = lambda seconds: None
    return item


def not_found():
    return feed("Product ID Not Found")


class BoilcloudSaleProviderTest(unittest.TestCase):
    def test_factory_builds_provider(self):
        self.assertIsInstance(
            build_provider({"name": "boilcloud_sale", "type": "boilcloud_sale"}),
            BoilcloudSaleProvider,
        )

    def test_empty_group_is_one_closed_pseudo_product_plus_feed_probe(self):
        item = provider()
        with patch.object(
            item.session, "get", side_effect=[response(200, empty_page()), not_found(), not_found()]
        ) as get:
            products = item.fetch_products()
        self.assertEqual([p.product_id for p in products], ["category:%s" % SLUG])
        self.assertIs(products[0].available, False)
        self.assertEqual(item.last_scan_stats["requests"], 3)
        self.assertEqual(
            [call.kwargs["params"]["pid"] for call in get.call_args_list[1:]], ["529", "530"]
        )
        # Rounds 2 and 3 read only the group page.
        for _ in range(2):
            with patch.object(item.session, "get", return_value=response(200, empty_page())):
                item.fetch_products()
            self.assertEqual(item.last_scan_stats["requests"], 1)

    def test_opening_alerts_with_stock_and_menu_state(self):
        item = provider(feed_every_rounds=100)
        with patch.object(item.session, "get", side_effect=[response(200, empty_page()), not_found(), not_found()]):
            closed = item.fetch_products()
        with patch.object(item.session, "get", return_value=response(200, open_page())):
            opened = item.fetch_products()
        by_id = {p.product_id: p for p in opened}
        group = by_id["category:%s" % SLUG]
        self.assertIs(group.available, True)
        self.assertIs(group.metadata["listed_in_menu"], False)
        self.assertEqual(by_id["540"].stock, 3)
        self.assertIs(by_id["540"].metadata["discovery"]["hidden"], True)

        changes = compare_products(closed, opened)
        types = {(c.type, c.product.product_id) for c in changes}
        self.assertIn((ChangeType.RESTOCK, "category:%s" % SLUG), types)
        self.assertIn((ChangeType.NEW, "540"), types)
        opened_alert = next(c for c in changes if c.product.product_id.startswith("category:"))
        text = format_change(opened_alert, [])
        self.assertIn("活动分类已开放", text)
        self.assertIn("未列出（隐藏分组", text)

    def test_menu_listing_marks_the_group_public(self):
        item = provider(feed_every_rounds=100)
        item._rounds = 1
        with patch.object(item.session, "get", return_value=response(200, open_page(listed=True))):
            products = item.fetch_products()
        by_id = {p.product_id: p for p in products}
        self.assertIs(by_id["category:%s" % SLUG].metadata["listed_in_menu"], True)
        self.assertIs(by_id["540"].metadata["discovery"]["hidden"], False)

    def test_every_stock_change_notifies(self):
        item = provider(feed_every_rounds=100)
        item._rounds = 1
        with patch.object(item.session, "get", return_value=response(200, open_page(3))):
            before = item.fetch_products()
        with patch.object(item.session, "get", return_value=response(200, open_page(0))):
            after = item.fetch_products()
        changes = compare_products(before, after)
        self.assertEqual([(c.type, c.product.product_id) for c in changes], [(ChangeType.SOLD_OUT, "540")])
        # Not a SeedNet / annual name filter like the catalog provider.
        self.assertTrue(item.should_notify(changes[0]))

    def test_new_pid_is_reported_once_with_prices(self):
        item = provider()
        pages = [
            response(200, empty_page()),
            feed("黑五 SeedNet 年付"),
            feed("$-1.00 USD"),
            feed("$-1.00 USD"),
            feed("$-1.00 USD"),
            feed("$199.00 USD"),
            not_found(),
        ]
        with patch.object(item.session, "get", side_effect=pages):
            products = item.fetch_products()
        by_id = {p.product_id: p for p in products}
        found = by_id["pid:529"]
        self.assertEqual(found.pricing, {"annually": "$199.00 USD"})
        self.assertEqual(found.url, "https://cloud.boil.network/cart.php?a=add&pid=529")
        self.assertEqual(item.last_scan_stats["high_water_pid"], 529)
        text = format_change(Change(ChangeType.NEW, found), [])
        self.assertIn("隐藏新品", text)
        self.assertIn("$199.00 USD", text)

        # A restart restores the found PID and the high-water mark.
        state = {"products": {p.key: p.to_dict() for p in products}}
        item.update_state_after_success(state, products)
        again = provider()
        again.restore_state(state)
        with patch.object(
            again.session, "get", side_effect=[response(200, empty_page()), not_found(), not_found()]
        ) as get:
            later = again.fetch_products()
        self.assertEqual(
            [call.kwargs["params"]["pid"] for call in get.call_args_list[1:]], ["530", "531"]
        )
        self.assertEqual(compare_products(products, later), [])

    def test_block_page_fails_the_round(self):
        item = provider()
        with patch.object(item.session, "get", return_value=response(429, "slow down")):
            with self.assertRaises(FetchError):
                item.fetch_products()

    def test_feed_block_keeps_the_old_probe_state(self):
        item = provider()
        with patch.object(
            item.session, "get", side_effect=[response(200, empty_page()), feed("新品"), response(403, "no")]
        ):
            with self.assertRaises(FetchError):
                item.fetch_products()
        self.assertEqual(item._new_pids, {})
        self.assertEqual(item._high_water, 528)

    def test_redirect_away_counts_as_closed(self):
        item = provider(feed_every_rounds=100)
        item._rounds = 1
        with patch.object(
            item.session, "get", return_value=response(200, "<html></html>", "https://cloud.boil.network/store")
        ):
            products = item.fetch_products()
        self.assertIs(products[0].available, False)

    def test_requests_are_spaced(self):
        item = provider()
        waits = []
        item._sleep = waits.append
        item._clock = lambda: 100.0
        with patch.object(
            item.session, "get", side_effect=[response(200, empty_page()), not_found(), not_found()]
        ):
            item.fetch_products()
        self.assertEqual(waits, [5.0, 5.0])


class BoilTelegramRelayTest(unittest.TestCase):
    def watch(self, tmp):
        from watcher.fac_tg_watch import FacTelegramWatch, Relay, RelayState

        base = Path(tmp)
        fac = Relay(
            "FAC",
            {"channel_id": -1002582634785, "group_id": -1002306171240,
             "autobuy_trigger_dir": "fac_triggers"},
            base,
            primary=True,
        )
        boil = Relay(
            "BOIL",
            {
                "name": "BOIL",
                "channel_id": -1002036810571,
                "channel_username": "BoilsHomelabChannel",
                "group_id": -1002302355078,
                "link_hosts": ["cloud.boil.network"],
                "staff_usernames": ["@zby88zby", "BoilCloud"],
                "keywords": ["黑五", "盲盒"],
            },
            base,
        )
        watch = FacTelegramWatch.__new__(FacTelegramWatch)
        watch.relays = [fac, boil]
        watch.by_channel = {r.channel_id: r for r in watch.relays}
        watch.by_group = {r.group_id: r for r in watch.relays}
        watch.trigger_dir = fac.trigger_dir
        watch.admin_refresh_seconds = 3600
        watch.state = RelayState(base / "relay_state.json")
        watch._lock = asyncio.Lock()
        watch.sent = []

        async def notify(text):
            watch.sent.append(text)

        async def refresh_admins(relay, force=False):
            return None

        watch.notify = notify
        watch.refresh_admins = refresh_admins
        boil.admins = {1}
        return watch, fac, boil

    @staticmethod
    def message(chat_id, msg_id, text, sender_id=0, sender="someone"):
        async def get_sender():
            return SimpleNamespace(first_name=sender, last_name=None, username=None, title=None)

        return SimpleNamespace(
            chat_id=chat_id, id=msg_id, raw_text=text, entities=[], buttons=None, fwd_from=None,
            sender_id=sender_id, date=datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc),
            get_sender=get_sender,
        )

    def test_staff_filter_ignores_community_admins_and_bots(self):
        from watcher.fac_tg_watch import Relay

        with tempfile.TemporaryDirectory() as tmp:
            _, fac, boil = self.watch(tmp)
            staff = SimpleNamespace(username="zby88zby", bot=False)
            helper = SimpleNamespace(username="DAdaGia", bot=False)
            self.assertTrue(boil.is_admin(staff))
            self.assertFalse(boil.is_admin(helper))
            # FAC keeps every human admin and only its own fac_* bots.
            self.assertTrue(fac.is_admin(helper))
            self.assertFalse(fac.is_admin(SimpleNamespace(username="Lottery8Bot", bot=True)))
            self.assertTrue(fac.is_admin(SimpleNamespace(username="fac_bot", bot=True)))
            self.assertIsInstance(boil, Relay)

    def test_boil_channel_post_is_relayed_once_under_its_own_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch, _, _ = self.watch(tmp)
            post = self.message(
                -1002036810571, 720,
                "🎄 聖誕盲盒 https://cloud.boil.network/store/festive-offer/2026-xmas",
            )
            asyncio.run(watch.handle_channel(post))
            asyncio.run(watch.handle_channel(post))  # catch-up after a reconnect
            self.assertEqual(len(watch.sent), 1)
            self.assertIn("📣 BOIL 公告频道", watch.sent[0])
            self.assertIn("BOIL 链接", watch.sent[0])
            self.assertIn("https://t.me/BoilsHomelabChannel/720", watch.sent[0])
            self.assertEqual(watch.state.data["relays"]["BOIL"]["channel_last_id"], 720)
            # FAC's top-level state is untouched; no buyer trigger for BOIL.
            self.assertIsNone(watch.state.data["channel_last_id"])
            self.assertEqual(take_triggers(str(Path(tmp) / "fac_triggers")), [])

    def test_boil_group_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch, _, _ = self.watch(tmp)
            group = -1002302355078
            for msg_id, text, sender in [
                (1, "黑五有活动吗", 2),  # member, keyword only
                (2, "看 https://fachost.cloud/products/x", 2),  # another vendor's link
                (3, "早安", 1),  # staff, no keyword
                (4, "這個 https://cloud.boil.network/store/hong-kong-zone38", 2),  # member link
                (5, "黑五盲盒今晚開", 1),  # staff + keyword
            ]:
                asyncio.run(watch.handle_group(self.message(group, msg_id, text, sender)))
            self.assertEqual(len(watch.sent), 2)
            self.assertIn("含 BOIL 链接", watch.sent[0])
            self.assertIn("管理员 + 活动关键词", watch.sent[1])
            self.assertTrue(all("💬 BOIL 群消息" in text for text in watch.sent))

    def test_old_state_file_keeps_fac_keys(self):
        import json

        from watcher.fac_tg_watch import RelayState

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "relay_state.json"
            path.write_text(json.dumps({"channel_last_id": 41, "digests": {"41": "x"}}), "utf-8")
            state = RelayState(path)
            self.assertEqual(state.relay(None)["channel_last_id"], 41)
            self.assertIsNone(state.relay("BOIL")["channel_last_id"])
            state.save()
            self.assertEqual(json.loads(path.read_text("utf-8"))["channel_last_id"], 41)


if __name__ == "__main__":
    unittest.main()
