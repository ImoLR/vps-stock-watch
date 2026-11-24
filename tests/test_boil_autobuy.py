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
        # Site monitor: only when a card mentions a target.
        self.assertEqual(targets("boilcloud_sale", "HKT 777Mbps 半年付", [festive], public=True), [])
        self.assertEqual(targets("boilcloud_sale", "羊年 SeedNet 300M", [festive], public=False), [festive])
        # Site monitor on a public group (one card): only a full target.
        japan = BASE + "store/japan-shared-bandwidth"
        self.assertEqual(targets("boilcloud_sale", "靜態 SoftBank 1000Mbps 日本", [japan], public=True), [])
        self.assertEqual(targets("boilcloud_sale", "SoftBank 1000Mbps 日本 動態 IP", [japan], public=True), [japan])
        # A BOIL promo code: the regular groups too (quiet); a promo post with no code: nothing.
        self.assertEqual(targets("tg-channel", "黑五優惠碼：BF2026", [BASE], promo=True), CFG["promo_urls"])
        self.assertEqual(targets("tg-channel", "優惠碼明天公布", [BASE], promo=True), [])
        coded = targets("tg-channel", "聖誕快樂 優惠碼 XMAS", [festive])
        self.assertEqual(coded, [festive] + CFG["promo_urls"])
        trigger = {"source": "tg-channel", "text": "優惠碼 XMAS", "urls": [festive]}
        self.assertFalse(ba.quiet_target(festive, trigger, CFG))
        self.assertTrue(ba.quiet_target(CFG["promo_urls"][0], trigger, CFG))
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


class FakeSite:
    """Answers the buyer's requests by URL; records every call."""

    def __init__(self, checkout=None, invoice_status="Paid", add_url=None, cycles=None,
                 name="2027 羊年 SeedNet 300Mbps VPS", desc="遠傳 SeedNet 寬頻 動態 IP", promo=None):
        self.calls = []
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
            return resp(url=BASE + "cart.php?a=view")
        if "a=empty" not in url and ("a=add&pid=" in url or "store/taiwan-home-broadband/" in url):
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
                category_html([("festive-offer", "節日特別提供")], [
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
            added = [url for _, url in site.calls if "a=add&pid=" in url]
            self.assertEqual(added, [BASE + "cart.php?a=add&pid=540"])  # HKT card never touched
            self.assertEqual(site.posted_checkout["accepttos"], "on")
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
            self.assertEqual([url for _, url in site.calls if "a=add" in url], [])

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
            added = [url for _, url in site.calls if "a=add&pid=" in url]
            self.assertEqual(added, [BASE + "cart.php?a=add&pid=311"])  # not the static one
            self.assertTrue((Path(tmp) / "done_jpus.json").exists())
            self.assertTrue((Path(tmp) / "done.json").exists())
            self.assertTrue(buyer.switch.control.paused())
            self.assertEqual(buyer.armed, {})

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
            self.assertEqual(site.calls[-1][1], BASE + "cart.php?a=empty")

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

    def test_post_trigger_passes_extra_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            post_trigger(tmp, "x", ["https://a/"], "t", public=True)
            self.assertIs(take_triggers(tmp)[0]["public"], True)


if __name__ == "__main__":
    unittest.main()
