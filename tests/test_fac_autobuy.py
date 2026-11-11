from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.fac_autobuy import (
    CREDIT_OPTION,
    choose_plan,
    classify_url,
    invoice_state,
    listed_category,
    money_amounts,
    name_matches,
    parse_money,
    public_wait,
    trigger_is_public,
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


class InvoiceTest(unittest.TestCase):
    # Text of the real unpaid test invoice (2026-11-08, $0 balance).
    UNPAID = (
        "Total\n$1.020,00\nInvoice Status\nPayment Pending\nAwaiting payment\n"
        "Total Due\n$1.020,00\n\nRemaining\n$1.020,00\nPay Invoice\n"
    )

    def test_invoice_state(self):
        self.assertEqual(invoice_state(self.UNPAID), ("Payment Pending", "$1.020,00"))
        self.assertEqual(invoice_state("Invoice Status\nPaid\n"), ("Paid", None))
        self.assertEqual(invoice_state("nothing here"), ("?", None))

    def test_credit_option_never_matches_a_gateway(self):
        for text in ("Account credit", "Pay with credits", "使用餘額付款", "余额支付", "Balance ($300,00)"):
            self.assertTrue(CREDIT_OPTION.search(text), text)
        for text in ("PayPal", "USDT (TRC20)", "Alipay (CNY)", "WeChat Pay (CNY)", "Credit Card", "Pay Now"):
            self.assertFalse(CREDIT_OPTION.search(text), text)


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
        # An announcement link arms the sale category.
        self.assertEqual(
            trigger_targets({"urls": ["https://fachost.cloud/announcements/9"]}, BASE, DEFAULTS),
            DEFAULTS,
        )
        # Old queue files may still carry keyword_hit; it no longer arms anything extra.
        self.assertEqual(
            trigger_targets({"urls": [product], "keyword_hit": True}, BASE, DEFAULTS),
            [product + "/checkout"],
        )


class PublicTest(unittest.TestCase):
    MENU = [
        "https://fachost.cloud/products/tw-tbc-vds",
        "https://fachost.cloud/products/tw-seednet-vds",
        "https://fachost.cloud/products/tw-seednet-vds/seednet-vds-1",
    ]

    def test_listed_only_when_the_category_is_in_the_menu(self):
        sale = "https://fachost.cloud/products/special-regulations"
        self.assertFalse(listed_category(sale, self.MENU, BASE))
        self.assertFalse(listed_category(sale + "/seednet-bf-2026/checkout", self.MENU, BASE))
        self.assertTrue(listed_category(sale, self.MENU + [sale], BASE))
        self.assertTrue(listed_category(sale + "/seednet-bf-2026/checkout", self.MENU + [sale + "?x=1"], BASE))
        self.assertTrue(listed_category(
            "https://fachost.cloud/products/tw-seednet-vds/seednet-vds-1/checkout", self.MENU, BASE))
        self.assertFalse(listed_category("https://fachost.cloud/announcements", self.MENU, BASE))

    def test_public_wait_counts_from_the_last_hidden_check(self):
        # Never seen hidden (TG, or listed at the first look): order at once.
        self.assertEqual(public_wait(4, None, 100.0), 0.0)
        # Last check that still saw it hidden was 2 minutes ago: order at once.
        self.assertEqual(public_wait(4, 0.0, 120.0), 0.0)
        # It was still hidden 1 s ago: wait the remaining 3 s.
        self.assertAlmostEqual(public_wait(4, 99.0, 100.0), 3.0)

    def test_only_telegram_triggers_are_public(self):
        self.assertTrue(trigger_is_public({"source": "tg-channel"}))
        self.assertTrue(trigger_is_public({"source": "tg-group"}))
        self.assertFalse(trigger_is_public({"source": "fachost_sale"}))
        self.assertFalse(trigger_is_public({}))


class TriggerQueueTest(unittest.TestCase):
    def test_round_trip_and_stale_drop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(post_trigger(tmp, "tg-channel", [], "hello"))
            self.assertIsNone(post_trigger(None, "tg-channel", ["https://fachost.cloud/"]))
            post_trigger(tmp, "tg-channel", ["https://fachost.cloud/products/x"], "黑五")
            post_trigger(tmp, "tg-group", ["https://fachost.cloud/products/y"], "年付")
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

    def test_only_trusted_links_queue_a_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            watch = self.watch(tmp)
            product = "https://fachost.cloud/products/a/b"
            watch.trigger("tg-group", "看這個", [product, "https://t.me/x"], False)  # member: ignored
            watch.trigger("tg-group", "黑五年付來了", [], True)  # admin, keyword only: ignored
            watch.trigger("tg-group", "黑五年付開賣", [product], True)
            watch.trigger("tg-channel", "黑五年付 11/20 開賣", [], True)  # no link: ignored
            watch.trigger("tg-channel", "開賣", ["https://fachost.cloud/announcements/3"], True)
            items = take_triggers(tmp)
            self.assertEqual(
                [(i["source"], i["urls"]) for i in items],
                [("tg-group", [product]), ("tg-channel", ["https://fachost.cloud/announcements/3"])],
            )


if __name__ == "__main__":
    unittest.main()
