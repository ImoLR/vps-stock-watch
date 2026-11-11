from __future__ import annotations

import argparse
import json
import tempfile
import unittest
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


def conf_page(name="2027 羊年 SeedNet 300Mbps VPS", cycles=None, desc="遠傳 SeedNet 寬頻 動態 IP"):
    cycles = cycles or [("monthly", "$38.00 USD 月繳"), ("annually", "$288.00 USD 年繳 + $3.00 USD 設定費")]
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


def resp(text="", url=BASE, status=200, headers=None):
    return SimpleNamespace(status_code=status, text=text, url=url, headers=headers or {})


class HelperTest(unittest.TestCase):
    def test_parse_usd(self):
        self.assertEqual(ba.parse_usd("購物車金額合計 : $1,066.50 USD"), 1066.5)
        self.assertIsNone(ba.parse_usd("免費"))

    def test_target_filter(self):
        inc, exc = CFG["name_include"], CFG["name_exclude"]
        self.assertTrue(ba.text_matches("2027 羊年 SeedNet 300Mbps VPS 動態 IP", inc, exc))
        self.assertTrue(ba.text_matches("台中 遠傳 VPS", inc, exc))
        self.assertFalse(ba.text_matches("1區NAT - SeedNet 300M", inc, exc))
        self.assertFalse(ba.text_matches("靜態 SeedNet 500Mbps", inc, exc))
        self.assertFalse(ba.text_matches("聖誕盲盒（獎池含 SeedNet 年付）", inc, exc))
        self.assertFalse(ba.text_matches("2026年馬上來HiNet 248Mbps", inc, exc))
        # "nat" only as a word.
        self.assertTrue(ba.text_matches("SeedNet International VPS", inc, exc))

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
        # Site monitor: only when a card mentions SeedNet.
        self.assertEqual(targets("boilcloud_sale", "HKT 777Mbps 半年付", [festive], public=True), [])
        self.assertEqual(targets("boilcloud_sale", "羊年 SeedNet 300M", [festive], public=False), [festive])

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


class FakeSite:
    """Answers the buyer's requests by URL; records every call."""

    def __init__(self, checkout=None, invoice_status="Paid", add_url=None):
        self.calls = []
        self.checkout = checkout or checkout_page()
        self.invoice_status = invoice_status
        self.add_url = add_url or BASE + "cart.php?a=confproduct&i=0"
        self.posted_checkout = None
        self.invoices = [70]

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if "a=empty" in url:
            return resp(url=BASE + "cart.php?a=view")
        if "a=add&pid=" in url:
            return resp(conf_page(), self.add_url)
        if "a=confproduct" in url:
            return resp("")
        if "a=checkout" in url and method == "GET":
            return resp(self.checkout, BASE + "cart.php?a=checkout")
        if "action=invoices" in url:
            return resp("".join('<a href="viewinvoice.php?id=%d">' % i for i in self.invoices))
        if "viewinvoice.php" in url:
            return resp(invoice_page(self.invoice_status), url)
        if "clientarea.php" in url:
            return resp('<a href="logout.php">登出</a>', BASE + "clientarea.php")
        if "store/festive-offer" in url:
            return resp(
                category_html([("festive-offer", "節日特別提供")], [
                    {"pid": 540, "name": "2027 羊年 SeedNet 300Mbps VPS", "stock": 2},
                    {"pid": 541, "name": "HKT 777Mbps", "stock": 5},
                ], current="festive-offer"),
                BASE + "store/festive-offer",
            )
        if "store/hong-kong-shared-bandwidth" in url:
            return resp(category_html([("hong-kong-shared-bandwidth", "香港")], [], empty=True))
        raise AssertionError("unexpected request %s %s" % (method, url))

    def post_checkout(self, url, data=None, timeout=None):
        self.calls.append(("POST", url))
        self.posted_checkout = data
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
            self.assertTrue(buyer.poll_armed())
            added = [url for _, url in site.calls if "a=add&pid=" in url]
            self.assertEqual(added, [BASE + "cart.php?a=add&pid=540"])  # HKT card never touched
            self.assertEqual(site.posted_checkout["accepttos"], "on")
            self.assertEqual(site.posted_checkout["applycredit"], "1")
            marker = json.loads((Path(tmp) / "done.json").read_text("utf-8"))
            self.assertEqual((marker["invoice"], marker["invoice_status"]), ("71", "Paid"))
            self.assertIn("抢到了", buyer.sent[-1])

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
            self.assertIn("超出预算", buyer.sent[0])
            self.assertIn("540", buyer.dropped)
            self.assertEqual(site.calls[-1][1], BASE + "cart.php?a=empty")

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
            self.assertIn("请马上手动付款", buyer.sent[-1])

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
            buyer.session.request = mock.Mock(side_effect=request)
            self.assertEqual(buyer.order("540"), ba.OrderResult.WAIT)


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
                [{"pid": 540, "name": "2027 羊年 SeedNet 300Mbps VPS", "stock": 2}],
                current="festive-offer",
            )
            with patch.object(item.session, "get", return_value=resp(page, BASE + "store/festive-offer")):
                item.fetch_products()
            triggers = take_triggers(tmp)
            self.assertEqual(len(triggers), 1)
            self.assertEqual(triggers[0]["source"], "boilcloud_sale")
            self.assertIs(triggers[0]["public"], False)
            self.assertIn("SeedNet", triggers[0]["text"])
            self.assertEqual(
                ba.trigger_targets(triggers[0], CFG), [BASE + "store/festive-offer"]
            )

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
                  "cycle": ("annually", "年繳")}],
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

    def test_post_trigger_passes_extra_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            post_trigger(tmp, "x", ["https://a/"], "t", public=True)
            self.assertIs(take_triggers(tmp)[0]["public"], True)


if __name__ == "__main__":
    unittest.main()
