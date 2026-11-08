from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.fac_autobuy import (
    choose_plan,
    classify_url,
    money_amounts,
    name_matches,
    parse_money,
    trigger_targets,
)
from watcher.fac_trigger import post_trigger, take_triggers
from watcher.providers.fachost_sale import FachostSaleProvider

from tests.test_fachost_sale import PLANS, URL, open_page, response


BASE = "https://fachost.cloud/"
DEFAULTS = ["https://fachost.cloud/products/special-regulations"]


class MoneyTest(unittest.TestCase):
    def test_fac_format_dot_thousands_comma_cents(self):
        self.assertEqual(parse_money("$1.020,00"), 1020.0)
        self.assertEqual(parse_money("$246,99"), 246.99)
        self.assertEqual(parse_money("$28,49 / month"), 28.49)

    def test_other_formats(self):
        self.assertEqual(parse_money("$1,020.00"), 1020.0)
        self.assertEqual(parse_money("$990.5"), 990.5)
        self.assertEqual(parse_money("$1.020"), 1020.0)
        self.assertEqual(parse_money("$280"), 280.0)
        self.assertIsNone(parse_money("free"))

    def test_plan_label_amounts(self):
        label = "年付 - $990,00\n      + $30,00 Setup fee"
        self.assertEqual(money_amounts(label), [990.0, 30.0])


class MatchTest(unittest.TestCase):
    def test_seednet_vds_matches_nat_does_not(self):
        include, exclude = ["seednet"], ["nat"]
        self.assertTrue(name_matches("Seednet-VDS-1 Black Friday 2025", include, exclude))
        self.assertTrue(name_matches("SeedNet VDS 年付特惠", include, exclude))
        self.assertFalse(name_matches("Seednet-NAT-1", include, exclude))
        self.assertFalse(name_matches("SeedNet NAT機 年付", include, exclude))
        self.assertFalse(name_matches("Hinet-VDS-1", include, exclude))
        # "nat" inside a longer Latin word is not NAT.
        self.assertTrue(name_matches("Seednet Signature VDS", include, ["nat"]))
        self.assertTrue(name_matches("Seednet International", include, ["nat"]))

    def test_choose_annual_plan(self):
        options = [
            ("30", "月付 - $99,00\n + $30,00 Setup fee"),
            ("31", "年付 - $990,00\n + $30,00 Setup fee"),
            ("32", "季付 - $285,00"),
        ]
        keywords = ["年付", "annual", "every year"]
        self.assertEqual(choose_plan(options, keywords), ("31", "年付 - $990,00 + $30,00 Setup fee"))
        self.assertIsNone(choose_plan(options[:1] + options[2:], keywords))
        self.assertEqual(choose_plan([("7", "Annually - $246,99")], ["annual"])[0], "7")


class UrlTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(
            classify_url("https://fachost.cloud/products/special-regulations", BASE),
            ("category", "https://fachost.cloud/products/special-regulations"),
        )
        self.assertEqual(
            classify_url("https://www.fachost.cloud/products/special-regulations/seednet-vds-1?x=1", BASE),
            ("checkout", "https://fachost.cloud/products/special-regulations/seednet-vds-1/checkout"),
        )
        self.assertEqual(
            classify_url("https://fachost.cloud/products/custom/tw-mobiledata/checkout?plan=30", BASE),
            ("checkout", "https://fachost.cloud/products/custom/tw-mobiledata/checkout"),
        )
        self.assertEqual(classify_url("https://fachost.cloud/announcements/12", BASE), ("other", None))
        self.assertEqual(classify_url("https://example.com/products/a/b", BASE), ("other", None))

    def test_trigger_targets(self):
        product = "https://fachost.cloud/products/special-regulations/seednet-bf-2026"
        self.assertEqual(
            trigger_targets({"urls": [product]}, BASE, DEFAULTS),
            [product + "/checkout"],
        )
        # An announcement link or a keyword-only post arms the sale category.
        self.assertEqual(
            trigger_targets({"urls": ["https://fachost.cloud/announcements/9"]}, BASE, DEFAULTS),
            DEFAULTS,
        )
        self.assertEqual(trigger_targets({"urls": [], "keyword_hit": True}, BASE, DEFAULTS), DEFAULTS)
        both = trigger_targets({"urls": [product], "keyword_hit": True}, BASE, DEFAULTS)
        self.assertEqual(both, [product + "/checkout"] + DEFAULTS)


class TriggerQueueTest(unittest.TestCase):
    def test_round_trip_and_stale_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(post_trigger(tmp, "tg-channel", [], "hello"))
            self.assertIsNone(post_trigger(None, "tg-channel", ["https://fachost.cloud/"]))
            post_trigger(tmp, "tg-channel", ["https://fachost.cloud/products/x"], "黑五")
            post_trigger(tmp, "tg-group", [], "年付", keyword_hit=True)
            stale = Path(tmp) / "0000000001.000000-old.json"
            stale.write_text(json.dumps({"source": "x", "urls": ["u"], "at": 1}), encoding="utf-8")
            items = take_triggers(tmp)
            self.assertEqual([i["source"] for i in items], ["tg-channel", "tg-group"])
            self.assertEqual(list(Path(tmp).iterdir()), [])
            self.assertEqual(take_triggers(tmp), [])
            self.assertEqual(take_triggers(str(Path(tmp) / "missing")), [])

    def test_sale_provider_triggers_only_when_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = FachostSaleProvider(
                {"name": "fachost_sale", "type": "fachost_sale", "request_spacing_seconds": 0,
                 "autobuy_trigger_dir": tmp}
            )
            with patch.object(item.session, "get", return_value=response(404, "Not found")):
                item.fetch_products()
            self.assertEqual(take_triggers(tmp), [])
            checkout = "%s/seednet-vds-black-friday-2026/checkout" % URL
            pages = [response(200, open_page()), response(200, PLANS, checkout)]
            with patch.object(item.session, "get", side_effect=pages):
                item.fetch_products()
            items = take_triggers(tmp)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["source"], "fachost_sale")
            self.assertEqual(items[0]["urls"], [URL])


class TelegramHookTest(unittest.TestCase):
    def watch(self, tmp):
        from watcher.fac_tg_watch import DEFAULT_KEYWORDS, FacTelegramWatch

        watch = FacTelegramWatch.__new__(FacTelegramWatch)
        watch.trigger_dir = tmp
        watch.keywords = list(DEFAULT_KEYWORDS)
        return watch

    def test_links_or_trusted_keywords_queue_a_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = self.watch(tmp)
            watch.trigger("tg-group", "看這個", ["https://fachost.cloud/products/a/b", "https://t.me/x"], False)
            watch.trigger("tg-group", "黑五年付來了", [], False)  # not an admin: ignored
            watch.trigger("tg-channel", "黑五年付 11/20 開賣", [], True)
            watch.trigger("tg-channel", "例行維護通知", [], True)
            items = take_triggers(tmp)
            self.assertEqual(
                [(i["source"], i["urls"], i["keyword_hit"]) for i in items],
                [("tg-group", ["https://fachost.cloud/products/a/b"], False), ("tg-channel", [], True)],
            )


if __name__ == "__main__":
    unittest.main()
