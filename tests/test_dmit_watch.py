import unittest
from unittest import mock

from watcher.models import Change, ChangeType, Product
from watcher.providers.dmit import DmitProvider, keyword_hit
from watcher.providers.tgchannel import TgChannelProvider
from watcher.telegram import format_change

# Trimmed from real pages saved 2026-11-10 (pid 126 in stock, pid 186 sold out).
IN_STOCK = """<html><body><div id="order-standard_cart">
<div class="product-title">HKG.AN5.Pro.MICRO</div>
<select name="billingcycle" id="inputBillingcycle">
<option value="monthly" selected="">$199.90 USD Monthly</option>
<option value="annually">$2398.80 USD Annually</option>
</select></div></body></html>"""
SOLD_OUT = """<html><body><h1>Out of Stock</h1><p>We are currently out of stock on this
item so orders for it have been suspended until more stock is available.</p></body></html>"""
CONFPRODUCT = "https://www.dmit.io/cart.php?a=confproduct&i=0"

WATCH = {
    "name": "dmit",
    "type": "dmit",
    "watch_pids": [
        {"pid": 126, "name": "HKG.Pro.MICRO", "keywords": ["micro"]},
        {"pid": 186, "name": "LAX.Pro.MALIBU", "keywords": ["malibu"]},
        {"pid": 138, "name": "TYO.Pro.TINY", "keywords": ["tyo+tiny"]},
    ],
}


class DmitWatchTest(unittest.TestCase):
    def provider(self, pages):
        provider = DmitProvider(dict(WATCH))
        provider._browser_pages = mock.Mock(return_value=pages)
        return provider

    def test_order_page_signals(self):
        self.assertEqual(
            DmitProvider.parse_order_page(IN_STOCK, CONFPRODUCT),
            (True, "HKG.AN5.Pro.MICRO", {"monthly": "$199.90 USD", "annually": "$2398.80 USD"}),
        )
        self.assertEqual(DmitProvider.parse_order_page(SOLD_OUT, "x?a=add&pid=186")[0], False)
        with self.assertRaises(Exception):
            DmitProvider.parse_order_page("<html><body>Just a moment...</body></html>", "")

    def test_keyword_parts(self):
        self.assertTrue(keyword_hit("TYO.AS3.Pro.TINY", ["tyo+tiny"]))
        self.assertFalse(keyword_hit("HKG.Pro.Tiny", ["tyo+tiny"]))

    def test_watched_products_and_reused_pid(self):
        renamed = IN_STOCK.replace("HKG.AN5.Pro.MICRO", "HKG.AN5.Pro.GIANT")
        provider = self.provider([(CONFPRODUCT, renamed), ("u", SOLD_OUT), (CONFPRODUCT, IN_STOCK.replace("HKG.AN5.Pro.MICRO", "TYO.AS3.Pro.TINY"))])
        micro, malibu, tiny = provider.fetch_products()
        self.assertFalse(micro.available)  # in stock, but another product now
        self.assertTrue(micro.metadata["pid_reused"])
        self.assertFalse(malibu.available)
        self.assertTrue(tiny.available)
        self.assertEqual(tiny.price, "$2398.80 USD")
        changes = provider.additional_changes([micro, malibu, tiny], {"products": {}})
        self.assertEqual([(c.type, c.product.name) for c in changes], [(ChangeType.NAME, "HKG.AN5.Pro.GIANT")])
        self.assertTrue(provider.should_notify(changes[0]))
        self.assertIn("换成了别的产品", format_change(changes[0], []))
        # Reported once, not every round.
        state = {"products": {micro.key: micro.to_dict()}}
        self.assertEqual(provider.additional_changes([micro], state), [])

    def test_only_restocks_are_notified(self):
        provider = DmitProvider(dict(WATCH))
        product = Product("dmit", "186", "LAX.Pro.MALIBU", available=True)
        self.assertTrue(provider.should_notify(Change(ChangeType.RESTOCK, product)))
        self.assertTrue(provider.should_notify(Change(ChangeType.NEW, product)))
        self.assertFalse(provider.should_notify(Change(ChangeType.SOLD_OUT, product)))
        self.assertFalse(provider.should_notify(Change(ChangeType.REMOVED, product)))
        self.assertFalse(provider.should_notify(Change(ChangeType.PRICE, product)))
        self.assertFalse(provider.should_notify(Change(ChangeType.NEW, Product("dmit", "1", "x", available=False))))

    def test_sold_out_prices_are_kept(self):
        provider = self.provider([(CONFPRODUCT, IN_STOCK), ("u", SOLD_OUT), ("u", SOLD_OUT)])
        provider.fetch_products()
        provider._browser_pages.return_value = [("u", SOLD_OUT)] * 3
        micro = provider.fetch_products()[0]
        self.assertFalse(micro.available)
        self.assertEqual(micro.pricing["annually"], "$2398.80 USD")


CHANNEL = """<html><body>
<div class="tgme_widget_message" data-post="dmitnews/477"><div class="tgme_widget_message_text">IP Transit circuit maintenance</div>
<time datetime="2026-11-01T01:03:48+00:00"></time></div>
<div class="tgme_widget_message" data-post="dmitnews/479"><div class="tgme_widget_message_text">日本GIA补货<br/>PVM.TYO.Pro.Shinagawa 年付 $239.90<br/>
<a href="https://act.dmithost.net/dmittyoshinagawa">购买</a> <a href="https://act.dmithost.net/newplan">新</a></div>
<time datetime="2026-11-10T01:00:00+00:00"></time></div>
</body></html>"""


class TgChannelTest(unittest.TestCase):
    def provider(self):
        provider = TgChannelProvider(
            {
                "name": "dmitnews",
                "type": "tg_channel",
                "channel": "dmitnews",
                "keywords": {"品川": ["shinagawa", "品川"], "Malibu": ["malibu"]},
                "resolve_hosts": ["act.dmithost.net"],
                "resolve_spacing_seconds": 0,
                "watched_pids": [179],
            }
        )
        provider.get_text = mock.Mock(return_value=CHANNEL)
        locations = {
            "https://act.dmithost.net/dmittyoshinagawa": "https://www.dmit.io/aff.php?aff=5911&pid=179",
            "https://act.dmithost.net/newplan": "https://www.dmit.io/aff.php?aff=5911&pid=300",
        }
        provider.session.get = mock.Mock(
            side_effect=lambda url, **kw: mock.Mock(headers={"Location": locations[url]})
        )
        return provider

    def test_keyword_posts_with_resolved_pids(self):
        provider = self.provider()
        (post,) = provider.fetch_products()
        self.assertEqual(post.product_id, "479")
        self.assertEqual(post.metadata["labels"], ["品川"])
        self.assertEqual(
            [(item["pid"], item["watched"]) for item in post.metadata["links"]],
            [("179", True), ("300", False)],
        )
        text = format_change(Change(ChangeType.NEW, post), [])
        self.assertIn("PID 179", text)
        self.assertIn("新 PID", text)
        self.assertIn("https://t.me/dmitnews/479", text)
        # Links are resolved once per post.
        provider.fetch_products()
        self.assertEqual(provider.session.get.call_count, 2)

    def test_only_new_posts_notify(self):
        provider = self.provider()
        product = Product("dmitnews", "1", "x")
        self.assertTrue(provider.should_notify(Change(ChangeType.NEW, product)))
        self.assertFalse(provider.should_notify(Change(ChangeType.REMOVED, product)))
        self.assertFalse(provider.should_notify(Change(ChangeType.NAME, product)))


class VendorChannelRelayTest(unittest.TestCase):
    """DMIT channel posts pushed through fac_tg_watch (watch_channels)."""

    def watch(self, tmp):
        import asyncio
        from pathlib import Path

        from watcher.fac_tg_watch import FacTelegramWatch, RelayState, vendor_channel

        config = {
            "providers": [{"name": "dmit", "watch_pids": [{"pid": 179}, {"pid": 186}]}],
        }
        vendor = vendor_channel(
            {
                "name": "DMIT",
                "ids": [-1001986550569],
                "resolve_hosts": ["act.dmithost.net"],
                "watched_pids_from": "dmit",
                "keywords": {"品川 Shinagawa": ["shinagawa", "品川"]},
            },
            config,
        )
        self.assertEqual(vendor["watched"], {"179", "186"})
        watch = FacTelegramWatch.__new__(FacTelegramWatch)
        watch.vendor_by_id = {-1001986550569: vendor}
        watch.state = RelayState(Path(tmp) / "relay_state.json")
        watch.state.data.setdefault("vendor_last_ids", {})
        watch._lock = asyncio.Lock()
        watch._empty_logged = set()
        watch.http = mock.Mock()
        watch.http.get.return_value = mock.Mock(
            headers={"Location": "https://www.dmit.io/aff.php?aff=5911&pid=179"}
        )
        watch.sent = []

        async def notify(text):
            watch.sent.append(text)

        watch.notify = notify
        return watch

    @staticmethod
    def message(msg_id, text):
        import datetime

        async def get_chat():
            return mock.Mock(username="dmitnews", title="DMIT活动推送")

        return mock.Mock(
            chat_id=-1001986550569, id=msg_id, raw_text=text, entities=[], buttons=None,
            date=datetime.datetime(2026, 11, 10, 1, 0, tzinfo=datetime.timezone.utc),
            get_chat=get_chat,
        )

    def test_keyword_post_is_relayed_once_with_pid(self):
        import asyncio
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            watch = self.watch(tmp)
            post = self.message(480, "日本GIA补货 PVM.TYO.Pro.Shinagawa https://act.dmithost.net/dmittyoshinagawa")
            asyncio.run(watch.handle_vendor(post))
            asyncio.run(watch.handle_vendor(post))  # catch-up after a reconnect
            asyncio.run(watch.handle_vendor(self.message(481, "LAX 维护通知")))
            self.assertEqual(len(watch.sent), 1)
            self.assertIn("PID 179", watch.sent[0])
            self.assertIn("已在监控", watch.sent[0])
            self.assertIn("https://t.me/dmitnews/480", watch.sent[0])
            self.assertEqual(watch.state.data["vendor_last_ids"], {"-1001986550569": 481})

    def test_trading_channel_only_relays_restocks(self):
        import asyncio
        import tempfile

        from watcher.fac_tg_watch import vendor_channel

        with tempfile.TemporaryDirectory() as tmp:
            watch = self.watch(tmp)
            vendor = vendor_channel(
                {
                    "ids": [-1001986550569],
                    "keywords": {"Malibu": ["malibu"], "东京 TINY": ["tyo+tiny"]},
                    "require_words": ["补货", "有货"],
                    "skip_words": ["【收】", "溢价", "剩余价值"],
                },
                {},
            )
            watch.vendor_by_id = {-1001986550569: vendor}
            for msg_id, text in enumerate(
                [
                    "【收】DMIT LAX.Pro.MALIBU $49.90/年（+1500）",
                    "溢价150+剩余价值出 DMIT TYO.Pro.TINY 东京 $21.9 款",
                    "Malibu 怎么样",
                    "DMIT LAX.Pro.MALIBU 补货了",
                ],
                start=10,
            ):
                asyncio.run(watch.handle_vendor(self.message(msg_id, text)))
            self.assertEqual(len(watch.sent), 1)
            self.assertIn("补货了", watch.sent[0])


if __name__ == "__main__":
    unittest.main()
