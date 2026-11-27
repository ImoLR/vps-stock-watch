from __future__ import annotations

import argparse
import json
import re
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

from watcher import boil_autobuy as ba
from watcher.fac_trigger import post_trigger, take_triggers
from watcher.providers.boilcloud_sale import BoilcloudSaleProvider

from tests.test_boilcloud import category_html


BASE = "https://cloud.boil.network/"
CFG = dict(ba.DEFAULTS)


DYNAMIC = "300Mbps 共享頻寬 動態 PPPoE IP"


def conf_page(name="2027 羊年 SeedNet 300Mbps VPS", cycles=None, desc="遠傳 SeedNet 寬頻 動態 IP"):
    cycles = cycles or [("annually", "$288.00 USD 年繳 + $3.00 USD 設定費")]
    options = "".join('<option value="%s">%s</option>' % item for item in cycles)
    return """
    <html><body><div class="product-info"><p class="product-title">%s</p><p>%s</p></div>
    <form id="frmConfigureProduct">
      <input type="hidden" name="configure" value="true"><input type="hidden" name="i" value="0">
      <select name="billingcycle">%s</select>
      <input type="text" name="hostname" value=""><input type="password" name="rootpw" value="">
      <input type="text" name="ns1prefix" value="abcd"><input type="text" name="ns2prefix" value="bcda">
      <select name="configoption[4]"><option value="17">75 MB/s + $98.00 USD 設定費</option>
        <option value="15">37.5 MB/s (300 Mbps)</option></select>
      <select name="configoption[13]"><option value="35">Debian 12</option><option value="36">Debian 13</option></select>
      <input type="checkbox" name="addons[3]" value="on">
    </form><input type="hidden" name="token" value="tok1"></body></html>
    """ % (name, desc, options)


def checkout_page(total="$291.00 USD", balance="$300.00 USD", custtype="existing"):
    return """
    <html><body><div id="totalDueToday" class="alert">購物車金額合計 : %s</div>
    <p>您的可用信用餘額為 %s。</p>
    <form id="frmCheckout" method="post" action="/cart.php?a=checkout">
      <input type="hidden" name="token" value="tok2"><input type="hidden" name="submit" value="true">
      <input type="hidden" name="custtype" value="%s">
      <input type="radio" name="account_id" value="1973" checked><input type="radio" name="account_id" value="new">
      <input type="text" name="loginemail" value=""><input type="password" name="loginpassword" value="">
      <input type="text" name="firstname" value="imolr">
      <input type="radio" name="applycredit" value="1" checked><input type="radio" name="applycredit" value="0">
      <input type="radio" name="paymentmethod" value="epay2" checked><input type="radio" name="paymentmethod" value="epay">
      <input type="tel" name="ccnumber" value="">
      <textarea name="notes"></textarea>
      <input type="checkbox" name="accepttos">
    </form></body></html>
    """ % (total, balance, custtype)


def invoice_page(status="Paid"):
    return '<html><body><div class="invoice-status"><span class="paid">%s</span></div></body></html>' % status


def cart_view(items):
    if not items:
        return '<div class="view-cart-empty">您的購物車是空的</div>'
    lines = "".join('<div class="item">x</div>' for _ in range(items))
    return ('<div class="view-cart-items">%s</div>'
            '<form method="post" action="/cart.php?a=empty"><input type="hidden" name="token" value="tok9">'
            '<button type="submit">清空購物車</button></form>' % lines)


def resp(text="", url=BASE, status=200, headers=None):
    return SimpleNamespace(status_code=status, text=text, url=url, headers=headers or {})


class HelperTest(unittest.TestCase):
    def test_parse_usd(self):
        self.assertEqual(ba.parse_usd("購物車金額合計 : $1,066.50 USD"), 1066.5)
        self.assertIsNone(ba.parse_usd("免費"))

    def test_target_filter(self):
        def key(text):
            target = ba.match_target(text, CFG)
            return target and target["key"]

        self.assertEqual(key("2027 羊年 SeedNet 300Mbps VPS 動態 PPPoE IP"), "seednet")
        self.assertEqual(key("台中 遠傳 VPS 動態 IP"), "seednet")
        self.assertIsNone(key("2027 羊年 SeedNet 300Mbps VPS"))  # no 動態: not bought
        self.assertIsNone(key("1區NAT - SeedNet 300M 動態"))
        self.assertIsNone(key("靜態 SeedNet 500Mbps 動態 IPv6"))
        self.assertIsNone(key("聖誕盲盒（獎池含 SeedNet 年付）動態"))
        self.assertIsNone(key("2026年馬上來HiNet 248Mbps 動態 IP"))
        # "nat" only as a word.
        self.assertEqual(key("SeedNet International VPS 動態"), "seednet")
        # Japan / USA, as BOIL words them.
        self.assertEqual(key("2區 - SoftBank 1000Mbps VPS 東京練馬・日本 動態 IP・支援自主更換"), "jpus")
        self.assertEqual(key("1區 - NTT/docomo 5G Mobile VPS 東京・日本 動態 IP 位址"), "jpus")
        self.assertIsNone(key("3區 - JCOM 1000Mbps VPS 東京練馬・日本 動態 IP 位址「CGNAT」"))
        self.assertIsNone(key("1區 - 靜態 ATT 500Mbps VPS 加州・美國 靜態IPv4・動態IPv6"))
        self.assertIsNone(key("2區 - SoftBank 100Mbps VPS 東京練馬・日本 1TB 流量"))  # no 動態
        self.assertEqual(key("2027 羊年 Comcast 1000Mbps 加州・美國 動態 IP"), "jpus")
        self.assertIsNone(key("3區 - 靜態 BIGLOBE 1000Mbps VPS 日本 靜態 IP 位址「MAP-E」"))

    def test_ipv4_must_be_dynamic(self):
        def ok(text):
            return ba.ipv4_dynamic(text, CFG["require_words"], CFG["static_words"])

        # BOIL's own SeedNet text: the IPv6 is dynamic too, the PPPoE IP counts.
        self.assertTrue(ok("SeedNet 獨立 IPv4 ×1 HiNet IPv6 /64（SLAAC・動態） 動態 PPPoE IP"))
        self.assertTrue(ok("動態 IP 位址"))
        self.assertTrue(ok("動態 IP・支援自主更換"))
        # Only the IPv6 is dynamic: not enough.
        self.assertFalse(ok("SeedNet 獨立 IPv4 ×1 HiNet IPv6 /64（SLAAC・動態）"))
        self.assertFalse(ok("IPv6：動態"))
        self.assertFalse(ok("靜態IPv4・動態IPv6"))
        # Static IPv6 is fine; a static IPv4 / plain static IP is not.
        self.assertTrue(ok("動態 IPv4・靜態 IPv6 /64"))
        self.assertTrue(ok("動態 PPPoE IP，IPv6 /64（靜態）"))
        self.assertFalse(ok("動態 IPv6・靜態 IPv4"))
        self.assertFalse(ok("靜態 IP 位址 動態 IPv6"))
        self.assertFalse(ok("靜態 SeedNet 500Mbps 動態 IP"))
        # No word at all: static.
        self.assertFalse(ok("SeedNet 獨立 IPv4 ×1"))
        self.assertTrue(ok("Dynamic IPv4, static IPv6"))
        self.assertFalse(ok("Static IP, dynamic IPv6"))

    def test_plan_cycles(self):
        seednet, jpus = CFG["targets"]
        short = CFG["short_cycles"]
        annual_only = {"annually": "$288.00 USD 年繳 + $3.00 USD 設定費"}
        self.assertEqual(ba.plan_cycles(annual_only, seednet, short, False), ("只有年付", 28.5, [("annually", 291 / 12)]))
        self.assertEqual(ba.plan_cycles({"annually": "$348.00 USD 年繳"}, seednet, short, False)[2], [])
        # Japan annual-only: up to $40 a month.
        self.assertEqual(ba.plan_cycles({"annually": "$468.00 USD 年繳"}, jpus, short, False)[2], [("annually", 39.0)])
        # Several cycles: cheapest per month first, over-cap ones left out ...
        regular = {"monthly": "$38.00 USD 月繳", "quarterly": "$108.00 USD 季繳",
                   "semiannually": "$-1.00 USD", "annually": "$428.00 USD 年繳"}
        self.assertEqual(ba.plan_cycles(regular, seednet, short, False), ("多周期", 28.5, []))
        mixed = {"monthly": "$28.00 USD 月繳", "annually": "$288.00 USD 年繳"}
        self.assertEqual([c for c, _ in ba.plan_cycles(mixed, seednet, short, False)[2]], ["annually", "monthly"])
        # ... unless a promo code may bring them down.
        self.assertEqual([c for c, _ in ba.plan_cycles(regular, seednet, short, True)[2]],
                         ["annually", "quarterly", "monthly"])
        # Japan with several cycles: under $30 a month.
        self.assertEqual(ba.plan_cycles({"monthly": "$30.00 USD 月繳"}, jpus, short, False)[2], [])
        self.assertEqual(ba.plan_cycles({"monthly": "$29.00 USD 月繳"}, jpus, short, False)[2], [("monthly", 29.0)])

    def test_promo_codes(self):
        self.assertEqual(ba.promo_codes("黑五優惠碼：BF2026，年付 7 折"), ["BF2026"])
        self.assertEqual(ba.promo_codes("优惠码 `XMAS-50` 和 折扣碼「NY27」"), ["XMAS-50", "NY27"])
        self.assertEqual(ba.promo_codes("Promo code: boil88 (once per account)"), ["boil88"])
        self.assertEqual(ba.promo_codes("優惠碼明天公布"), [])
        self.assertEqual(ba.promo_codes("SeedNet 補貨了"), [])

    def test_classify_url(self):
        self.assertEqual(ba.classify_url(BASE + "cart.php?a=add&pid=540", BASE), ("pid", "540"))
        self.assertEqual(ba.classify_url(BASE + "store/festive-offer", BASE), ("group", "festive-offer"))
        self.assertEqual(
            ba.classify_url(BASE + "index.php?rp=/store/festive-offer/2026-xmas-seednet", BASE),
            ("product", BASE + "store/festive-offer/2026-xmas-seednet"),
        )
        self.assertEqual(ba.classify_url("https://ippanel.boil.network/", BASE), ("other", None))
        self.assertEqual(ba.classify_url(BASE, BASE), ("other", None))

    def test_trigger_targets(self):
        def targets(source, text, urls, **extra):
            return ba.trigger_targets(dict(extra, source=source, text=text, urls=urls), CFG)

        festive = BASE + "store/festive-offer"
        # Maintenance notice with the home page link: nothing.
        self.assertEqual(targets("tg-channel", "網站升級通知", [BASE]), [])
        # Regular HKT restock group: nothing; a SeedNet post on a group: armed.
        self.assertEqual(targets("tg-channel", "HKT 補貨", [BASE + "store/hong-kong-shared-bandwidth"]), [])
        self.assertEqual(
            targets("tg-channel", "SeedNet 補貨", [BASE + "store/taiwan-shared-bandwidth"]),
            [BASE + "store/taiwan-shared-bandwidth"],
        )
        # Festive link on TG: armed; product links always.
        self.assertEqual(targets("tg-channel", "聖誕快樂", [festive]), [festive])
        self.assertEqual(
            targets("tg-group", "看", [BASE + "cart.php?a=add&amp;pid=540".replace("&amp;", "&")]),
            [BASE + "cart.php?a=add&pid=540"],
        )
        # SeedNet sale post with only the home page: the sale group.
        self.assertEqual(targets("tg-channel", "聖誕 SeedNet 年付 今晚上架", [BASE]), [festive])
        # Site monitor: only when one card is a full target not bought yet.
        self.assertEqual(targets("boilcloud_sale", "HKT 777Mbps 半年付", [festive], public=True), [])
        self.assertEqual(targets("boilcloud_sale", "羊年 SeedNet 300M", [festive], public=False), [])
        seednet = "羊年 SeedNet 300M " + DYNAMIC
        self.assertEqual(targets("boilcloud_sale", seednet, [festive], public=False), [festive])
        self.assertEqual(
            targets("boilcloud_sale", "x", [festive], public=True, cards=["HKT 777Mbps", seednet]), [festive])
        # The 2026 Black Friday blind box names SeedNet / Japan prizes: nothing.
        box = "2026年黑五盲盒 隨機開出 SeedNet 動態 IP、日本 SoftBank 等"
        self.assertEqual(targets("boilcloud_sale", box, [festive], public=True, cards=[box]), [])
        # Several cards whose words only add up to a target together: nothing.
        self.assertEqual(targets("boilcloud_sale", "x", [festive], public=True,
                                 cards=["羊年 SeedNet 靜態 IP", "HKT " + DYNAMIC]), [])
        # SeedNet already bought: only a Japan/USA card arms it.
        self.assertEqual(ba.trigger_targets(
            {"source": "boilcloud_sale", "text": seednet, "urls": [festive], "cards": [seednet]},
            CFG, done=["seednet"]), [])
        # Site monitor on a public group (one card): only a full target.
        japan = BASE + "store/japan-shared-bandwidth"
        self.assertEqual(targets("boilcloud_sale", "靜態 SoftBank 1000Mbps 日本", [japan], public=True), [])
        self.assertEqual(targets("boilcloud_sale", "SoftBank 1000Mbps 日本 動態 IP", [japan], public=True), [japan])
        # A code-only post arms nothing (the buyer learns the code); with a link, just the link.
        self.assertEqual(targets("tg-channel", "黑五優惠碼：BF2026", [BASE], promo=True), [])
        self.assertEqual(targets("tg-channel", "聖誕快樂 優惠碼 XMAS", [festive]), [festive])
        self.assertFalse(ba.quiet_target(festive, {"source": "boilcloud_sale", "urls": [festive]}, CFG))
        self.assertTrue(ba.quiet_target(japan, {"source": "boilcloud_sale", "urls": [japan]}, CFG))
        self.assertFalse(ba.quiet_target(japan, {"source": "tg-channel", "urls": [japan]}, CFG))

    def test_public_rules(self):
        self.assertTrue(ba.trigger_is_public({"source": "tg-channel"}))
        self.assertTrue(ba.trigger_is_public({"source": "boilcloud_sale", "public": True}))
        self.assertFalse(ba.trigger_is_public({"source": "boilcloud_sale", "public": False}))
        self.assertEqual(ba.public_wait(4, None, 100), 0)
        self.assertEqual(ba.public_wait(4, 99, 100), 3)

    def test_conf_form_picks_annual_and_free_options(self):
        data, info = ba.conf_form(conf_page(), "annually", "seednet", "Pw12345678!9")
        self.assertEqual(info["name"], "2027 羊年 SeedNet 300Mbps VPS")
        self.assertEqual(data["billingcycle"], "annually")
        self.assertEqual(data["configoption[4]"], "15")  # not the paid upgrade
        self.assertEqual(data["configoption[13]"], "35")
        self.assertEqual((data["hostname"], data["rootpw"]), ("seednet", "Pw12345678!9"))
        self.assertNotIn("addons[3]", data)
        self.assertEqual(data["token"], "tok1")

    def test_conf_form_without_annual(self):
        data, info = ba.conf_form(conf_page(cycles=[("monthly", "$38 月繳")]), "annually", "h", "p")
        self.assertIsNone(data)
        self.assertIn("没有annually周期", info["why"])

    def test_checkout_form(self):
        data, info = ba.checkout_form(checkout_page())
        self.assertEqual((info["total"], info["balance"]), (291.0, 300.0))
        self.assertEqual(data["applycredit"], "1")
        self.assertEqual(data["paymentmethod"], "epay2")
        self.assertEqual(data["account_id"], "1973")
        self.assertEqual(data["accepttos"], "on")
        self.assertNotIn("ccnumber", data)
        self.assertNotIn("loginemail", data)
        data, _ = ba.checkout_form(checkout_page(), accept_tos=False)
        self.assertNotIn("accepttos", data)

    def test_invoice_helpers(self):
        self.assertEqual(ba.invoice_status(invoice_page("Paid")), "Paid")
        self.assertEqual(ba.invoice_status(invoice_page("未付款")), "Unpaid")
        self.assertEqual(ba.invoice_id(BASE + "viewinvoice.php?id=88"), "88")
        self.assertEqual(ba.invoice_id(BASE + "cart.php?a=complete", '<a href="viewinvoice.php?id=91">'), "91")

    def test_group_listed(self):
        public = [("hong-kong-shared-bandwidth", "香港"), ("taiwan-shared-bandwidth", "台灣")]
        self.assertFalse(ba.group_listed(category_html(public, [], empty=True), "festive-offer", BASE))
        listed = category_html(public + [("festive-offer", "節日特別提供")], [], empty=True)
        self.assertTrue(ba.group_listed(listed, "festive-offer", BASE))


class BurstWindowTest(unittest.TestCase):
    def window(self, *when, minutes=(0,), hours=range(8, 24)):
        return ba.burst_window(datetime(2026, 11, 27, *when), minutes, hours, 30, 60)

    def test_inside_around_the_hour(self):
        self.assertEqual(self.window(12, 59, 30), (True, 0.0))
        self.assertEqual(self.window(13, 0, 0), (True, 0.0))
        self.assertEqual(self.window(13, 1, 0), (True, 0.0))

    def test_seconds_until_the_next_window(self):
        self.assertEqual(self.window(12, 59, 0), (False, 30.0))
        self.assertEqual(self.window(13, 1, 1), (False, 3509.0))
        self.assertEqual(self.window(13, 10, 0, minutes=(0, 30)), (False, 1170.0))

    def test_hours_outside_the_list_are_skipped(self):
        # 23:00 is the last mark; the next one is 08:00 the next morning.
        self.assertEqual(self.window(23, 0, 30), (True, 0.0))
        inside, wait = self.window(23, 5, 0)
        self.assertFalse(inside)
        self.assertEqual(wait, (8 * 3600 + 55 * 60) - 30)
        self.assertEqual(self.window(7, 59, 40), (True, 0.0))  # 08:00's lead


class FakeSite:
    """Answers the buyer's requests by URL; records every call."""

    def __init__(self, checkout=None, invoice_status="Paid", add_url=None, cycles=None,
                 name="2027 羊年 SeedNet 300Mbps VPS", desc="遠傳 SeedNet 寬頻 動態 IP", promo=None,
                 festive_cards=None):
        self.calls = []
        self.festive_cards = festive_cards
        self.sold_out = False  # product links answer 缺貨中
        self.in_cart = 0
        self.cycles = cycles
        self.name, self.desc = name, desc
        # promo: (code, checkout page once the code is applied)
        self.promo = promo
        self.promo_applied = False
        self.configured = []
        self.checkout = checkout or checkout_page()
        self.invoice_status = invoice_status
        self.add_url = add_url or BASE + "cart.php?a=confproduct&i=0"
        self.posted_checkout = None
        self.invoices = [70]

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if "a=empty" in url:
            assert method == "POST", "BOIL answers a GET of cart.php?a=empty with 403"
            self.in_cart = 0
            return resp(url=BASE + "cart.php?a=view")
        if "a=view" in url and method == "GET":
            return resp(cart_view(self.in_cart), BASE + "cart.php?a=view")
        if "a=empty" not in url and ("a=add&pid=" in url or "store/taiwan-home-broadband/" in url
                                     or re.search(r"/store/[^/]+/product-\d+$", url)):
            if self.sold_out:
                return resp("糟糕，發生問題了... 缺貨中 很抱歉，本產品 / 服務目前處於缺貨中", url)
            self.in_cart += 1
            self.promo_applied = False
            return resp(conf_page(name=self.name, cycles=self.cycles, desc=self.desc), self.add_url)
        if "a=confproduct" in url:
            self.configured.append(kwargs.get("data", {}).get("billingcycle"))
            return resp("")
        if "a=view" in url and method == "POST":
            code = kwargs.get("data", {}).get("promocode")
            self.promo_applied = bool(self.promo) and code == self.promo[0]
            return resp("" if self.promo_applied else '<div class="alert-danger">代碼無效</div>')
        if "a=checkout" in url and method == "GET":
            page = self.promo[1] if self.promo_applied else self.checkout
            return resp(page, BASE + "cart.php?a=checkout")
        if "action=invoices" in url:
            return resp("".join('<a href="viewinvoice.php?id=%d">' % i for i in self.invoices))
        if "viewinvoice.php" in url:
            return resp(invoice_page(self.invoice_status), url)
        if "clientarea.php" in url:
            return resp('<a href="logout.php">登出</a>', BASE + "clientarea.php")
        if "store/festive-offer" in url:
            return resp(
                category_html([("festive-offer", "節日特別提供")], self.festive_cards if self.festive_cards is not None else [
                    {"pid": 540, "name": "2027 羊年 SeedNet 300Mbps VPS", "stock": 2, "bandwidth": DYNAMIC},
                    {"pid": 541, "name": "HKT 777Mbps", "stock": 5, "bandwidth": DYNAMIC},
                ], current="festive-offer"),
                BASE + "store/festive-offer",
            )
        if "store/japan-shared-bandwidth" in url:
            return resp(
                category_html([("japan-shared-bandwidth", "日本")], [
                    {"pid": 311, "name": "2區 - SoftBank 1000Mbps VPS", "stock": 1, "region": "東京・日本",
                     "bandwidth": "動態 IP・支援自主更換"},
                    {"pid": 310, "name": "2區 - 靜態 AsahiNet 1000Mbps VPS", "stock": 1, "region": "東京・日本"},
                ], current="japan-shared-bandwidth"),
                BASE + "store/japan-shared-bandwidth",
            )
        if "store/hong-kong-shared-bandwidth" in url:
            return resp(category_html([("hong-kong-shared-bandwidth", "香港")], [], empty=True))
        raise AssertionError("unexpected request %s %s" % (method, url))

    def post_checkout(self, url, data=None, timeout=None, headers=None):
        self.calls.append(("POST", url))
        self.posted_checkout = data
        self.checkout_headers = headers
        self.invoices.append(71)
        return resp("<p>訂單已完成</p>", BASE + "cart.php?a=complete")


class BuyerTest(unittest.TestCase):
    def buyer(self, tmp, site):
        args = argparse.Namespace(no_notify=True, dry_run_url=None, check_login=False, tos_probe_url=None)
        config = {"boil_autobuy": {
            "marker_file": str(Path(tmp) / "done.json"),
            "cookie_file": str(Path(tmp) / "cookies.json"),
            "account_file": str(Path(tmp) / "none.env"),
            "trigger_dir": str(Path(tmp) / "triggers"),
            "promo_file": str(Path(tmp) / "promo.json"),
            "page_spacing_seconds": 0, "order_spacing_seconds": 0,
        }}
        with patch.object(ba, "STATUS_DIR", str(Path(tmp) / "status")), \
                patch.object(ba, "telegram_credentials", return_value={"token": None, "chat_id": None}):
            buyer = ba.BoilAutoBuyer(config, args)
        buyer.session.request = mock.Mock(side_effect=site.request)
        buyer.public_session.request = mock.Mock(side_effect=site.request)
        buyer.session.post = mock.Mock(side_effect=site.post_checkout)
        buyer.sent = []
        buyer.notify = buyer.sent.append
        buyer.last_login_check = 1e18  # skip the warm-up login in these tests
        return buyer

    def test_public_tg_festive_link_buys_the_seednet_card_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            buyer = self.buyer(tmp, site)
            buyer.arm([BASE + "store/festive-offer"], "tg-channel", True)
            self.assertFalse(buyer.poll_armed())  # Japan/USA still to buy: keeps watching
            festive = BASE + "store/festive-offer"
            added = [url for _, url in site.calls if "/product-" in url or "a=add&pid=" in url]
            # The card's own link, never cart.php?a=add&pid= ("直链下单").
            self.assertEqual(added, [festive + "/product-540"])  # HKT card never touched
            # Read logged out; then opened logged in and clicked, with the group as referer.
            self.assertEqual([c.args[1] for c in buyer.public_session.request.call_args_list], [festive])
            logged_in = buyer.session.request.call_args_list
            urls = [c.args[1] for c in logged_in]
            self.assertLess(urls.index(festive), urls.index(festive + "/product-540"))
            click = logged_in[urls.index(festive + "/product-540")]
            self.assertEqual(click.kwargs["headers"]["Referer"], festive)
            self.assertEqual(site.posted_checkout["accepttos"], "on")
            # Like the page: configure -> cart -> checkout, each with the page before as referer.
            steps = [(c.args[0], c.args[1], (c.kwargs.get("headers") or {}).get("Referer"))
                     for c in buyer.session.request.call_args_list]
            conf = next(i for i, (m, u, _) in enumerate(steps) if m == "POST" and u.endswith("a=confproduct"))
            self.assertEqual([u.rsplit("/", 1)[1] for _, u, _ in steps[conf:conf + 3]],
                             ["cart.php?a=confproduct", "cart.php?a=view", "cart.php?a=checkout"])
            self.assertEqual(steps[conf + 2][2], BASE + "cart.php?a=view")
            self.assertEqual(site.checkout_headers["Referer"], BASE + "cart.php?a=checkout")
            self.assertEqual(site.posted_checkout["applycredit"], "1")
            self.assertEqual(site.configured, ["annually"])
            marker = json.loads((Path(tmp) / "done_seednet.json").read_text("utf-8"))
            self.assertEqual((marker["target"], marker["invoice"], marker["invoice_status"]), ("seednet", "71", "Paid"))
            self.assertFalse((Path(tmp) / "done.json").exists())
            self.assertIn("抢到了", buyer.sent[-2])
            self.assertIn("继续等：日本/美國", buyer.sent[-1])
            self.assertIn(BASE + "store/festive-offer", buyer.armed)
            self.assertFalse(buyer.switch.control.paused())
            # The next round: SeedNet is done, nothing is ordered again.
            buyer.armed[BASE + "store/festive-offer"]["next"] = 0
            site.calls.clear()
            buyer.poll_armed()
            self.assertEqual([url for _, url in site.calls if "a=add" in url or "/product-" in url], [])

    def test_both_targets_bought_pauses_the_buyer(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "done_seednet.json").write_text(json.dumps({"product": "SeedNet", "invoice": "70"}), "utf-8")
            site = FakeSite(name="2區 - SoftBank 1000Mbps VPS", desc="東京練馬・日本 動態 IP・支援自主更換",
                            cycles=[("annually", "$468.00 USD 年繳")],
                            checkout=checkout_page(total="$468.00 USD", balance="$600.00 USD"))
            buyer = self.buyer(tmp, site)
            self.assertEqual(set(buyer.done), {"seednet"})
            buyer.arm([BASE + "store/japan-shared-bandwidth"], "tg-channel", True)
            self.assertTrue(buyer.poll_armed())
            added = [url for _, url in site.calls if "/product-" in url]
            self.assertEqual(added, [BASE + "store/japan-shared-bandwidth/product-311"])  # not the static one
            self.assertTrue((Path(tmp) / "done_jpus.json").exists())
            self.assertTrue((Path(tmp) / "done.json").exists())
            self.assertTrue(buyer.switch.control.paused())
            self.assertEqual(buyer.armed, {})

    def test_japan_is_bought_only_when_the_balance_pays_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(name="2027 羊年 SoftBank 1000Mbps VPS", desc="東京・日本 動態 IP",
                            cycles=[("annually", "$468.00 USD 年繳")],
                            checkout=checkout_page(total="$468.00 USD", balance="$258.00 USD"))
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("600"), ba.OrderResult.WAIT)  # stays armed for a top-up
            self.assertIsNone(site.posted_checkout)
            self.assertIn("余额不够", buyer.sent[0])
            self.assertNotIn("600", buyer.price_dropped)
            buyer.order("600")
            self.assertEqual(len(buyer.sent), 1)  # told once
            site.checkout = checkout_page(total="$468.00 USD", balance="$600.00 USD")
            self.assertEqual(buyer.order("600"), ba.OrderResult.BOUGHT)

    def test_seednet_is_ordered_even_if_the_balance_is_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(checkout=checkout_page(total="$291.00 USD", balance="$100.00 USD"), invoice_status="Unpaid")
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.BOUGHT)
            self.assertIn("还没付清", buyer.sent[0])

    def test_seednet_card_goes_before_japan_in_one_festive_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            site_request = site.request
            page = category_html([("festive-offer", "節日特別提供")], [
                {"pid": 601, "name": "2027 羊年 SoftBank 1000Mbps VPS", "stock": 1, "region": "東京・日本",
                 "bandwidth": "動態 IP", "cycle": ("annually", "年繳")},
                {"pid": 540, "name": "2027 羊年 SeedNet 300Mbps VPS", "stock": 1, "bandwidth": DYNAMIC,
                 "cycle": ("annually", "年繳")},
            ], current="festive-offer")

            def request(method, url, **kwargs):
                if url.endswith("store/festive-offer"):
                    site.calls.append((method, url))
                    return resp(page, BASE + "store/festive-offer")
                return site_request(method, url, **kwargs)

            buyer = self.buyer(tmp, site)
            buyer.session.request = buyer.public_session.request = mock.Mock(side_effect=request)
            buyer.arm([BASE + "store/festive-offer"], "tg-channel", True)
            buyer.poll_armed()
            added = [url for _, url in site.calls if "/product-" in url]
            self.assertEqual(added[0], BASE + "store/festive-offer/product-540")

    def test_japan_annual_only_over_40_a_month_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(name="SoftBank 1000Mbps VPS", desc="日本 動態 IP", cycles=[("annually", "$498.00 USD 年繳")])
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("311"), ba.OrderResult.DROP)
            self.assertIn("价格不符", buyer.sent[0])
            self.assertIn("≤ $40", buyer.sent[0])

    def test_promo_code_brings_a_regular_plan_under_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            regular = [("monthly", "$38.00 USD 月繳"), ("quarterly", "$108.00 USD 季繳"), ("annually", "$428.00 USD 年繳")]
            site = FakeSite(cycles=regular, checkout=checkout_page(total="$428.00 USD", balance="$600.00 USD"),
                            promo=("BF2026", checkout_page(total="$299.60 USD", balance="$600.00 USD")))
            buyer = self.buyer(tmp, site)
            # Without a code: over $28.5 a month, not even configured.
            self.assertEqual(buyer.order("551"), ba.OrderResult.DROP)
            self.assertEqual(site.configured, [])
            self.assertIn("551", buyer.price_dropped)
            # BOIL posts a code: the miss is re-opened and the code tried.
            buyer.learn_codes(["BF2026"], "tg-channel")
            self.assertEqual(buyer.price_dropped, set())
            self.assertIn("BF2026", buyer.sent[-1])
            self.assertEqual(json.loads((Path(tmp) / "promo.json").read_text("utf-8"))[0]["code"], "BF2026")
            self.assertEqual(buyer.order("551"), ba.OrderResult.BOUGHT)
            self.assertEqual(site.configured, ["annually"])
            marker = json.loads((Path(tmp) / "done_seednet.json").read_text("utf-8"))
            self.assertIn("优惠码 BF2026", marker["plan"])

    def test_wrong_promo_code_misses_quietly_for_a_code_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            regular = [("monthly", "$38.00 USD 月繳"), ("annually", "$428.00 USD 年繳")]
            site = FakeSite(cycles=regular, checkout=checkout_page(total="$428.00 USD"), promo=("OTHER", ""))
            buyer = self.buyer(tmp, site)
            buyer.promos = [{"code": "BF2026", "at": 9e12}]
            self.assertEqual(buyer.order("551", quiet=True), ba.OrderResult.DROP)
            self.assertEqual(site.configured, ["annually", "monthly"])  # cheapest per month first
            self.assertEqual(buyer.sent, [])
            self.assertIsNone(site.posted_checkout)

    def test_regular_group_from_the_monitor_disarms_after_one_look(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(name="2區 - SoftBank 1000Mbps VPS", desc="東京練馬・日本 動態 IP",
                            cycles=[("monthly", "$198.00 USD 月繳")])
            buyer = self.buyer(tmp, site)
            japan = BASE + "store/japan-shared-bandwidth"
            buyer.arm([japan], "boilcloud_sale", True, quiet=[japan])
            self.assertFalse(buyer.poll_armed())
            self.assertNotIn(japan, buyer.armed)
            self.assertEqual(buyer.sent, [])

    def test_sale_group_from_the_monitor_disarms_without_a_target_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(festive_cards=[{"pid": 553, "name": "2026年黑五盲盒", "stock": 0,
                                            "bandwidth": "隨機開出 SeedNet 動態 IP"}])
            buyer = self.buyer(tmp, site)
            festive = BASE + "store/festive-offer"
            buyer.arm([festive], "boilcloud_sale", True)
            self.assertFalse(buyer.poll_armed())
            self.assertNotIn(festive, buyer.armed)  # the monitor's 5-min read re-arms it
            # Posted on Telegram too: keeps watching (a card may still come).
            buyer.arm([festive], "boilcloud_sale", True)
            buyer.arm([festive], "tg-channel", True)
            self.assertFalse(buyer.poll_armed())
            self.assertIn(festive, buyer.armed)
            entry = buyer.armed[festive]
            self.assertGreater(entry["next"] - time.monotonic(), CFG["fast_poll_seconds"] - 5)

    def test_block_page_stops_every_request_for_an_hour(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            buyer = self.buyer(tmp, site)
            buyer.last_login_check = float("-inf")  # the warm-up login hits the block
            buyer.session.request = mock.Mock(return_value=resp("Attention Required! | Cloudflare", status=403))
            festive = BASE + "store/festive-offer"
            buyer.arm([festive], "tg-channel", True)
            self.assertFalse(buyer.poll_armed())  # no crash
            self.assertIn("限流", buyer.sent[0])
            self.assertGreater(buyer.blocked_until - time.monotonic(), 3500)
            calls = buyer.session.request.call_count
            # New links while blocked wait too.
            buyer.arm([BASE + "cart.php?a=add&pid=540"], "tg-channel", True)
            self.assertFalse(buyer.poll_armed())
            self.assertEqual(buyer.session.request.call_count, calls)
            self.assertTrue(all(e["next"] >= buyer.blocked_until for e in buyer.armed.values()))

    def test_proxy_is_used_for_every_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(no_notify=True, dry_run_url=None, check_login=False, tos_probe_url=None)
            config = {"boil_autobuy": {"proxy": "socks5://127.0.0.1:1082",
                                       "marker_file": str(Path(tmp) / "done.json"),
                                       "cookie_file": str(Path(tmp) / "cookies.json"),
                                       "promo_file": str(Path(tmp) / "promo.json")}}
            with patch.object(ba, "STATUS_DIR", str(Path(tmp) / "status")), \
                    patch.object(ba, "telegram_credentials", return_value={"token": None, "chat_id": None}):
                buyer = ba.BoilAutoBuyer(config, args)
            for session in (buyer.session, buyer.public_session):
                self.assertEqual(session.proxies["https"], "socks5h://127.0.0.1:1082")

    def test_sold_out_tg_link_is_opened_every_second_until_stock(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            site.sold_out = True
            buyer = self.buyer(tmp, site)
            link = BASE + "store/festive-offer/product-540"
            buyer.arm([link], "tg-channel", True, text="🎄 羊年 SeedNet 300M 年付 開賣")
            self.assertFalse(buyer.poll_armed())
            entry = buyer.armed[link]
            self.assertTrue(buyer.rushing(entry))
            self.assertAlmostEqual(entry["next"] - time.monotonic(), 1.0, delta=0.5)
            # The cart is looked at first (nothing in it: no empty-cart post).
            self.assertEqual(site.calls, [("GET", BASE + "cart.php?a=view"), ("GET", link)])
            # Retries: only the link itself (nothing went into the cart, so no "empty cart" page).
            site.calls.clear()
            entry["next"] = 0
            buyer.poll_armed()
            self.assertEqual(site.calls, [("GET", link)])
            # Stock is out: bought on the next try.
            site.sold_out = False
            entry["next"] = 0
            buyer.poll_armed()
            self.assertIsNotNone(site.posted_checkout)
            self.assertIn("抢到了", "".join(buyer.sent))

    def test_sold_out_tg_link_gives_up_after_the_rush(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            site.sold_out = True
            buyer = self.buyer(tmp, site)
            link = BASE + "store/festive-offer/product-540"
            buyer.arm([link], "tg-channel", True, text="SeedNet 年付")
            buyer.poll_armed()
            buyer.armed[link]["rush_until"] = time.monotonic() - 1
            buyer.armed[link]["next"] = 0
            buyer.poll_armed()
            self.assertNotIn(link, buyer.armed)
            self.assertIn("一直缺货", buyer.sent[-1])

    def test_sold_out_blind_box_link_is_not_rushed(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(festive_cards=[{"pid": 553, "name": "2026年黑五盲盒", "stock": 0,
                                            "bandwidth": "日本 SoftBank 動態 IP"}])
            site.sold_out = True
            buyer = self.buyer(tmp, site)
            link = BASE + "store/festive-offer/product-553"
            # The post names 日本 and HiNet, but also 盲盒 and NAT: its card decides.
            buyer.arm([link], "tg-channel", True, text="神秘盲盒 第二弹 🇯🇵 日本 Docomo 🇨🇦 Telus（NAT）")
            buyer.poll_armed()
            self.assertNotIn(link, buyer.armed)
            self.assertEqual([c.args[1] for c in buyer.public_session.request.call_args_list],
                             [BASE + "store/festive-offer"])
            self.assertEqual(buyer.sent, [])

    def test_sold_out_link_from_the_monitor_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            site.sold_out = True
            buyer = self.buyer(tmp, site)
            link = BASE + "store/festive-offer/product-540"
            buyer.arm([link], "boilcloud_sale", True)
            buyer.poll_armed()
            self.assertNotIn(link, buyer.armed)

    def test_tunnel_down_goes_direct_but_never_resends_a_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            buyer = self.buyer(tmp, site)
            buyer.session.proxies = {"https": "socks5h://127.0.0.1:1082"}
            refused = ba.requests.ConnectionError(SimpleNamespace(reason=ba.NewConnectionError(None, "refused")))

            def flaky(method, url, **kwargs):
                if "proxies" not in kwargs:
                    raise refused
                return site.request(method, url, **kwargs)

            buyer.session.request = mock.Mock(side_effect=flaky)
            page = buyer.request("GET", "clientarea.php")
            self.assertIn("logout", page.text)
            self.assertIn("隧道不通", buyer.sent[0])
            self.assertEqual(buyer.session.request.call_args.kwargs["proxies"], {"http": None, "https": None})
            # Direct for a while, without trying the tunnel first.
            buyer.request("GET", "clientarea.php")
            self.assertEqual(buyer.session.request.call_count, 3)
            # A checkout cut off mid-way is not sent again.
            buyer.direct_until = float("-inf")
            buyer.session.request = mock.Mock(side_effect=ba.requests.ConnectionError("reset by peer"))
            with self.assertRaises(ba.requests.ConnectionError):
                buyer.request("POST", "cart.php?a=checkout", data={})
            self.assertEqual(buyer.session.request.call_count, 1)

    def test_hidden_group_is_never_opened_logged_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            buyer = self.buyer(tmp, site)
            buyer.arm([BASE + "store/festive-offer"], "boilcloud_sale", False)
            self.assertFalse(buyer.poll_armed())
            self.assertEqual(buyer.session.request.call_count, 0)
            self.assertEqual(
                [call.args[1] for call in buyer.public_session.request.call_args_list],
                [CFG["menu_url"]],
            )
            entry = buyer.armed[BASE + "store/festive-offer"]
            self.assertFalse(entry["public"])
            self.assertGreater(entry["next"], entry["since"] + 100)
            # A TG post makes it public at once.
            buyer.arm([BASE + "store/festive-offer"], "tg-channel", True)
            self.assertTrue(buyer.armed[BASE + "store/festive-offer"]["public"])

    def test_over_budget_notifies_once_and_drops(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(checkout=checkout_page(total="$388.12 USD"))
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.DROP)
            self.assertIsNone(site.posted_checkout)
            self.assertIn("价格不符", buyer.sent[0])
            self.assertIn("折合每月 $32.34", buyer.sent[0])
            self.assertIn("540", buyer.price_dropped)
            buyer.order("540")
            self.assertEqual(len(buyer.sent), 1)
            self.assertEqual(site.calls[-1], ("POST", BASE + "cart.php?a=empty"))

    def test_plan_with_several_cycles_buys_the_cheapest_per_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(cycles=[("monthly", "$28.00 USD 月繳"), ("annually", "$288.00 USD 年繳")],
                            checkout=checkout_page(total="$288.00 USD"))
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.BOUGHT)
            self.assertEqual(site.configured, ["annually"])

    def test_plan_with_several_cycles_all_over_the_cap_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(cycles=[("monthly", "$38.00 USD 月繳"), ("annually", "$428.00 USD 年繳")])
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.DROP)
            self.assertIsNone(site.posted_checkout)
            self.assertIn("多周期", buyer.sent[0])
            self.assertIn("折合每月 $35.67", buyer.sent[0])
            self.assertNotIn(("GET", BASE + "cart.php?a=checkout"), site.calls)

    def test_annual_price_over_28_5_a_month_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(cycles=[("annually", "$348.00 USD 年繳")])
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.DROP)
            self.assertIn("只有年付", buyer.sent[0])
            self.assertIn("$348.00 USD 年繳，折合每月 $29.00", buyer.sent[0])
            self.assertIsNone(site.posted_checkout)

    def test_seednet_without_dynamic_is_not_bought(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(desc="遠傳 SeedNet 寬頻")
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.DROP)
            self.assertIsNone(site.posted_checkout)

    def test_annual_only_at_the_cap_is_bought(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(cycles=[("annually", "$342.00 USD 年繳")], checkout=checkout_page(total="$342.00 USD", balance="$600.00 USD"))
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.BOUGHT)

    def test_tg_product_link_without_pid_is_ordered(self):
        # BOIL's store product links redirect straight to confproduct&i=0.
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            buyer = self.buyer(tmp, site)
            link = BASE + "store/taiwan-home-broadband/2027-seednet"
            buyer.arm([link], "tg-channel", True)
            buyer.poll_armed()
            self.assertIn(("GET", link), site.calls)
            self.assertIsNotNone(site.posted_checkout)

    def test_logged_out_checkout_is_never_submitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(checkout=checkout_page(custtype="new"))
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.WAIT)
            self.assertIsNone(site.posted_checkout)

    def test_dry_run_stops_before_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540", mode="dry"), ba.OrderResult.WAIT)
            self.assertIsNone(site.posted_checkout)
            self.assertFalse((Path(tmp) / "done.json").exists())

    def test_unpaid_invoice_sends_pay_link_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(invoice_status="Unpaid")
            buyer = self.buyer(tmp, site)
            self.assertEqual(buyer.order("540"), ba.OrderResult.BOUGHT)
            self.assertIn("还没付清", buyer.sent[0])
            self.assertIn("viewinvoice.php?id=71", buyer.sent[0])
            self.assertIn("请马上手动付款", buyer.sent[-2])

    def test_out_of_stock_keeps_waiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite(add_url=BASE + "cart.php?a=add&pid=540")
            site_request = site.request

            def request(method, url, **kwargs):
                if "a=add&pid=" in url:
                    site.calls.append((method, url))
                    return resp("<p>缺貨</p>", BASE + "cart.php?a=add&pid=540")
                return site_request(method, url, **kwargs)

            buyer = self.buyer(tmp, site)
            buyer.session.request = buyer.public_session.request = mock.Mock(side_effect=request)
            self.assertEqual(buyer.order("540"), ba.OrderResult.WAIT)

    def festive(self, site, cards):
        """Serve festive-offer with ``cards`` (a list the test may change)."""
        site_request = site.request

        def request(method, url, **kwargs):
            if url.endswith("store/festive-offer"):
                site.calls.append((method, url))
                page = category_html([("festive-offer", "節日特別提供")], cards, current="festive-offer")
                return resp(page, BASE + "store/festive-offer")
            return site_request(method, url, **kwargs)

        return request

    def test_target_card_without_stock_starts_hourly_bursts(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            cards = [{"pid": 540, "name": "2027 羊年 SeedNet 300Mbps VPS", "stock": 0, "bandwidth": DYNAMIC,
                      "cycle": ("annually", "年繳")}]
            buyer = self.buyer(tmp, site)
            buyer.session.request = buyer.public_session.request = mock.Mock(side_effect=self.festive(site, cards))
            url = BASE + "store/festive-offer"
            buyer.arm([url], "boilcloud_sale", True)
            with patch.object(buyer, "burst_now", return_value=(False, 40.0)):
                buyer.poll_armed()
                self.assertTrue(buyer.bursting(url))
                self.assertIn("还没放货", buyer.sent[0])
                self.assertIsNone(site.posted_checkout)
                # Outside a window: back no later than the window opens.
                self.assertAlmostEqual(buyer.armed[url]["next"] - time.monotonic(), 20.0, delta=1)
                self.assertEqual(buyer.interval_for(buyer.armed[url], url), 20.0)
                buyer.armed[url]["since"] -= 3600  # past the fast minutes: 60 s, cut to 40
                self.assertEqual(buyer.interval_for(buyer.armed[url], url), 40.0)
            with patch.object(buyer, "burst_now", return_value=(True, 0.0)):
                self.assertEqual(buyer.interval_for(buyer.armed[url], url), 3.0)
                # The hour: stock is out. The burst warm-up already read the invoices.
                cards[0]["stock"] = 2
                buyer.armed[url]["next"] = 0
                site.calls.clear()
                buyer.poll_armed()
            self.assertIsNotNone(site.posted_checkout)
            submit = site.calls.index(("POST", BASE + "cart.php?a=checkout"))
            invoice_reads = [i for i, (_, u) in enumerate(site.calls[:submit]) if "action=invoices" in u]
            # Read once by the warm-up, before the group page; not again right before checkout.
            self.assertEqual(len(invoice_reads), 1)
            self.assertLess(invoice_reads[0], site.calls.index(("GET", url)))
            self.assertEqual(json.loads((Path(tmp) / "done_seednet.json").read_text("utf-8"))["invoice"], "71")
            self.assertLessEqual(len(buyer.sent), 4)  # told about the wait once
            self.assertEqual(sum("还没放货" in text for text in buyer.sent), 1)

    def test_blind_box_without_stock_starts_no_bursts(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            cards = [{"pid": 553, "name": "2026年黑五盲盒", "stock": 0, "region": "香港：",
                      "bandwidth": "日本： SoftBank 1G獨享 月付 美國： ATT 500M 季付"}]
            buyer = self.buyer(tmp, site)
            buyer.session.request = buyer.public_session.request = mock.Mock(side_effect=self.festive(site, cards))
            url = BASE + "store/festive-offer"
            buyer.arm([url], "boilcloud_sale", True)
            buyer.poll_armed()
            self.assertFalse(buyer.bursting(url))
            self.assertEqual(buyer.sent, [])

    def test_bursts_stop_after_the_max_hours(self):
        with tempfile.TemporaryDirectory() as tmp:
            buyer = self.buyer(tmp, FakeSite())
            url = BASE + "store/festive-offer"
            buyer.presale = {url: ["540"]}
            buyer.presale_seen = {"540": time.time() - 37 * 3600}
            self.assertFalse(buyer.bursting(url))
            buyer.presale_seen = {"540": time.time() - 3600}
            self.assertTrue(buyer.bursting(url))


class MonitorHookTest(unittest.TestCase):
    def test_open_festive_group_queues_a_trigger_with_card_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = BoilcloudSaleProvider({
                "name": "boilcloud_sale", "autobuy_trigger_dir": tmp, "feed_every_rounds": 100,
            })
            item._sleep = lambda seconds: None
            item._rounds = 1
            page = category_html(
                [("hong-kong-shared-bandwidth", "香港")],
                [{"pid": 540, "name": "2027 羊年 SeedNet 300Mbps VPS", "stock": 2, "bandwidth": DYNAMIC},
                 {"pid": 541, "name": "2026年黑五盲盒", "stock": 5}],
                current="festive-offer",
            )
            with patch.object(item.session, "get", return_value=resp(page, BASE + "store/festive-offer")):
                item.fetch_products()
            triggers = take_triggers(tmp)
            self.assertEqual(len(triggers), 1)
            self.assertEqual(triggers[0]["source"], "boilcloud_sale")
            self.assertIs(triggers[0]["public"], False)
            self.assertIn("SeedNet", triggers[0]["text"])
            self.assertEqual(len(triggers[0]["cards"]), 2)
            self.assertEqual(
                ba.trigger_targets(triggers[0], CFG), [BASE + "store/festive-offer"]
            )
            self.assertEqual(ba.trigger_targets(triggers[0], CFG, done=["seednet"]), [])

    def test_public_taiwan_group_keeps_only_seednet_and_wakes_buyer_as_public(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = BoilcloudSaleProvider({
                "name": "boilcloud_sale", "autobuy_trigger_dir": tmp, "feed_every_rounds": 100,
                "watch_groups": ["taiwan-shared-bandwidth"], "watch_every_rounds": 2,
            })
            item._sleep = lambda seconds: None
            item._rounds = 2
            festive = category_html([("taiwan-shared-bandwidth", "A區")], [], empty=True)
            taiwan = category_html(
                [("taiwan-shared-bandwidth", "A區台灣家寬「個人用戶」")],
                [{"pid": 74, "name": "1區 - HiNet 500Mbps VPS", "stock": 0},
                 {"pid": 545, "name": "「羊年專輯」20區 - SeedNet 300Mbps VPS", "stock": 3,
                  "cycle": ("annually", "年繳"), "bandwidth": DYNAMIC}],
                current="taiwan-shared-bandwidth",
            )
            pages = [resp(festive, BASE + "store/festive-offer"),
                     resp(taiwan, BASE + "store/taiwan-shared-bandwidth")]
            with patch.object(item.session, "get", side_effect=pages):
                products = item.fetch_products()
            ids = [p.product_id for p in products]
            self.assertEqual(ids, ["category:festive-offer", "545"])
            triggers = take_triggers(tmp)
            self.assertEqual([(t["urls"], t["public"]) for t in triggers],
                             [([BASE + "store/taiwan-shared-bandwidth"], True)])
            self.assertEqual(ba.trigger_targets(triggers[0], CFG), [BASE + "store/taiwan-shared-bandwidth"])
            # Next round skips the group but keeps the card (no fake removal).
            with patch.object(item.session, "get", return_value=resp(festive, BASE + "store/festive-offer")) as get:
                again = item.fetch_products()
            self.assertEqual(get.call_count, 1)
            self.assertEqual([p.product_id for p in again], ids)

    def test_dropped_watch_groups_forget_their_restored_cards(self):
        with tempfile.TemporaryDirectory() as tmp:
            item = BoilcloudSaleProvider({"name": "boilcloud_sale", "autobuy_trigger_dir": tmp, "feed_every_rounds": 100})
            item._sleep = lambda seconds: None
            card = {"provider": "boilcloud_sale", "product_id": "493", "name": "靜態 ATT 2000Mbps", "available": True,
                    "metadata": {"discovery": {"type": "public_group", "hidden": False, "source": "store/usa-shared-bandwidth"}}}
            item.restore_state({"products": {"boilcloud_sale:493": card}})
            self.assertIn("493", item._group_cards)
            festive = category_html([("festive-offer", "節日特別提供")], [], empty=True)
            with patch.object(item.session, "get", return_value=resp(festive, BASE + "store/festive-offer")):
                products = item.fetch_products()
            self.assertEqual([p.product_id for p in products], ["category:festive-offer"])
            self.assertEqual(take_triggers(tmp), [])

    def test_post_trigger_passes_extra_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            post_trigger(tmp, "x", ["https://a/"], "t", public=True)
            self.assertIs(take_triggers(tmp)[0]["public"], True)


if __name__ == "__main__":
    unittest.main()
