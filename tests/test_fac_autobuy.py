from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.fac_autobuy import (
    CREDIT_OPTION,
    FacAutoBuyer,
    OrderResult,
    classify_url,
    invoice_state,
    listed_category,
    money_amounts,
    name_matches,
    offer_rank,
    plan_months,
    rank_plans,
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

    def test_plan_months(self):
        cases = {
            "月付 - $25,00": 1, "Monthly - $25,00": 1, "季付 - $70,00": 3, "Quarterly": 3,
            "半年付 - $140,00": 6, "Semi-Annually": 6, "年付 - $990,00": 12, "年繳": 12,
            "Annually - $246,99": 12, "黑五年付特惠 - $258,99": 12, "兩年付 - $500,00": 24,
            "两年付": 24, "Biennially": 24, "三年付": 36, "Triennially": 36, "12個月": 12,
            "3個月": 3, "2026 黑五 - $258,99": None, "Black Friday 2026": None,
        }
        for label, months in cases.items():
            self.assertEqual(plan_months(label), months, label)

    def test_annual_only_comes_first_and_takes_the_cheapest_annual(self):
        rule, plans, _ = rank_plans([
            ("41", "年付 - $299,00\n + $3,00 Setup fee"),
            ("42", "年付 - $258,99"),
            ("43", "兩年付 - $500,00"),
        ], 28.5)
        self.assertEqual(rule, "只有年付")
        self.assertEqual([p[0] for p in plans], ["42", "41"])  # never the 2-year plan
        # Annual-only is capped by the total only, not per month.
        self.assertEqual(rank_plans([("1", "年付 - $360,00")], 28.5)[1][0][3], 30.0)

    def test_several_cycles_need_28_5_a_month(self):
        # A regular restock: ~$38/month, $380/year -> nothing.
        rule, plans, why = rank_plans([
            ("30", "月付 - $37,99\n + $3,00 Setup fee"),
            ("31", "年付 - $379,90\n + $3,00 Setup fee"),
        ], 28.5)
        self.assertEqual((rule, plans), ("多周期", []))
        self.assertIn("31.91", why)
        # A sale: the cheapest per month is bought, a tie goes to the longer cycle.
        rule, plans, _ = rank_plans([
            ("30", "月付 - $27,00"),
            ("31", "季付 - $81,00"),
            ("32", "年付 - $350,00"),  # $29.17/month: too dear
        ], 28.5)
        self.assertEqual([p[0] for p in plans], ["31", "30"])

    def test_unknown_cycle_is_never_bought_and_blocks_annual_only(self):
        # $360/year would pass as annual-only; with an unknown plan beside it,
        # it is held to $28.5/month and fails.
        rule, plans, _ = rank_plans([("1", "年付 - $360,00"), ("2", "特惠 - $25,00")], 28.5)
        self.assertEqual((rule, plans), ("多周期", []))
        self.assertEqual(rank_plans([("2", "特惠 - $25,00")], 28.5)[1], [])

    def test_offer_rank(self):
        annual = {"rule": "只有年付", "plans": [("1", "年付", 12, 25.0)]}
        cheap = {"rule": "多周期", "plans": [("2", "月付", 1, 20.0)]}
        dearer = {"rule": "多周期", "plans": [("3", "月付", 1, 27.0)]}
        self.assertEqual(sorted([dearer, cheap, annual], key=offer_rank), [annual, cheap, dearer])


class CategoryOrderTest(unittest.TestCase):
    def buyer(self, offers, results):
        buyer = FacAutoBuyer.__new__(FacAutoBuyer)
        buyer.base_url, buyer.unconfirmed_checkout, buyer.bought = BASE, False, []
        cards = [("SeedNet %s" % key, "https://fachost.cloud/products/s/%s/checkout" % key) for key in offers]
        buyer.scan_category = lambda url: (cards, [name for name, _ in cards])
        buyer.read_offer = lambda url: ("", offers[url.split("/")[-2]])

        def buy(offer):
            buyer.bought.append(offer["url"])
            result, unconfirmed = results.pop(0)
            buyer.unconfirmed_checkout = unconfirmed
            return result
        buyer.buy_offer = buy
        return buyer

    def offer(self, key, rule, per_month):
        return {"url": key, "rule": rule, "plans": [("1", "x", 12, per_month)]}

    def test_annual_only_is_tried_first_then_the_cheapest(self):
        offers = {
            "a": self.offer("a", "多周期", 20.0),
            "b": self.offer("b", "只有年付", 25.0),
            "c": self.offer("c", "多周期", 27.0),
        }
        buyer = self.buyer(offers, [(OrderResult.WAIT, False), (OrderResult.BOUGHT, False)])
        self.assertEqual(buyer.attempt("https://fachost.cloud/products/special-regulations"), OrderResult.BOUGHT)
        self.assertEqual(buyer.bought, ["b", "a"])

    def test_an_unconfirmed_checkout_stops_further_orders(self):
        offers = {"a": self.offer("a", "只有年付", 25.0), "b": self.offer("b", "多周期", 20.0)}
        buyer = self.buyer(offers, [(OrderResult.WAIT, True)])
        self.assertEqual(buyer.attempt("https://fachost.cloud/products/special-regulations"), OrderResult.WAIT)
        self.assertEqual(buyer.bought, ["a"])


class FakeContext:
    def __init__(self, cookies):
        self.jar = list(cookies)

    def cookies(self, url=None):
        return list(self.jar)

    def clear_cookies(self, name=None):
        self.jar = [c for c in self.jar if not (name and name.search(c["name"]))]

    def add_cookies(self, cookies):
        self.jar.extend(cookies)


class ReloginTest(unittest.TestCase):
    def buyer(self, remember_left_h, login_works):
        now = 1_800_000_000.0
        buyer = FacAutoBuyer.__new__(FacAutoBuyer)
        buyer.base_url, buyer.cfg, buyer.sent = BASE, {"relogin_before_hours": 48}, []
        context = FakeContext([
            {"name": "paymenter_remember", "expires": now + remember_left_h * 3600},
            {"name": "paymenter_session", "expires": now + 7200},
            {"name": "cf_clearance", "expires": now + 86400},
        ])

        class Page:
            url = "https://fachost.cloud/account"
        Page.context = context

        class Chrome:
            page = Page()
        buyer.chrome = Chrome()
        buyer.goto = lambda url: None
        buyer.notify = buyer.sent.append

        def login():
            if login_works:
                context.add_cookies([{"name": "paymenter_remember", "expires": now + 30 * 86400}])
            return login_works
        buyer.ensure_login = login
        return buyer, context, now

    def test_far_from_expiry_does_nothing(self):
        buyer, context, now = self.buyer(100, True)
        with patch("watcher.fac_autobuy.time.time", return_value=now):
            self.assertTrue(buyer.check_login())
        self.assertEqual((buyer.sent, len(context.jar)), ([], 3))

    def test_near_expiry_signs_in_afresh(self):
        buyer, context, now = self.buyer(37, True)
        with patch("watcher.fac_autobuy.time.time", return_value=now):
            self.assertTrue(buyer.check_login())
        self.assertEqual(sorted(c["name"] for c in context.jar), ["cf_clearance", "paymenter_remember"])
        self.assertEqual(buyer.remember_expiry(), now + 30 * 86400)
        self.assertIn("已主动重新登录", buyer.sent[0])

    def test_failed_login_restores_the_old_cookies(self):
        buyer, context, now = self.buyer(37, False)
        with patch("watcher.fac_autobuy.time.time", return_value=now):
            self.assertFalse(buyer.check_login())
        self.assertEqual(len(context.jar), 3)
        self.assertEqual(buyer.remember_expiry(), now + 37 * 3600)
        self.assertIn("没成功", buyer.sent[0])


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

    def test_boil_promo_post_without_link_queues_a_promo_trigger(self):
        from watcher.fac_tg_watch import Relay

        with tempfile.TemporaryDirectory() as tmp:
            watch = self.watch(tmp)
            relay = Relay("BOIL", {
                "channel_id": -1, "link_hosts": ["cloud.boil.network"], "autobuy_trigger_dir": tmp,
                "promo_words": ["優惠碼"], "promo_url": "https://cloud.boil.network/",
            }, Path("/"))
            relay.trigger_dir = tmp
            watch.trigger("tg-group", "黑五優惠碼：BF2026", [], False, relay)  # member: ignored
            watch.trigger("tg-channel", "黑五優惠碼：BF2026", [], True, relay)
            watch.trigger("tg-channel", "黑五快樂", [], True, relay)  # no promo word: ignored
            items = take_triggers(tmp)
            self.assertEqual([(i["source"], i["urls"], i.get("promo")) for i in items],
                             [("tg-channel", ["https://cloud.boil.network/"], True)])


if __name__ == "__main__":
    unittest.main()
