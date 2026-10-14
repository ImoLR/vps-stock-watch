from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.nexkr_autobuy import (
    ApiError,
    AutoBuyer,
    buyable_zones,
    candidate_zones,
    order_body,
    stock_status,
)


GROUP = {
    "id": 15,
    "zones": [
        {"id": 3, "slug": "goyang2"},
        {"id": 4, "slug": "seoul3"},
        {"id": 5, "slug": "seoul4"},
    ],
}
PRODUCT = {
    "id": 34,
    "slug": "kr-2026-nationalday-event",
    "cycle_months": 1,
    "in_stock": True,
    "zones": None,
    "zone_sold_out": None,
}


class CandidateZoneTests(unittest.TestCase):
    def test_product_without_zones_uses_all_group_zones(self):
        zones = candidate_zones(GROUP, PRODUCT)
        self.assertEqual([z["slug"] for z in zones], ["goyang2", "seoul3", "seoul4"])

    def test_product_zone_limits_apply(self):
        product = dict(PRODUCT, zones=[3, 4, 5], zone_sold_out=[3])
        self.assertEqual(
            [z["slug"] for z in candidate_zones(GROUP, product)],
            ["goyang2", "seoul3", "seoul4"],
        )
        product = dict(PRODUCT, zones=[5])
        self.assertEqual([z["slug"] for z in candidate_zones(GROUP, product)], ["seoul4"])

    def test_preferred_zones_go_first(self):
        zones = candidate_zones(GROUP, PRODUCT, ["seoul4", "seoul3"])
        self.assertEqual([z["slug"] for z in zones], ["seoul4", "seoul3", "goyang2"])

    def test_group_without_zones(self):
        self.assertEqual(candidate_zones({"zones": None}, PRODUCT), [])


class BuyableZoneTests(unittest.TestCase):
    def test_only_zone_sold_out_is_not_buyable(self):
        # Live case 2026-10-11..13: in_stock with zones=[5] and zone_sold_out=[5].
        product = dict(PRODUCT, zones=[5], zone_sold_out=[5])
        self.assertIsNone(buyable_zones(GROUP, product))
        self.assertIn("seoul4 (sold out)", stock_status(GROUP, product))

    def test_sold_out_flag_cleared_is_buyable(self):
        product = dict(PRODUCT, zones=[5], zone_sold_out=[])
        self.assertEqual([z["slug"] for z in buyable_zones(GROUP, product)], ["seoul4"])

    def test_product_moved_to_another_zone(self):
        product = dict(PRODUCT, zones=[4], zone_sold_out=[5])
        self.assertEqual([z["slug"] for z in buyable_zones(GROUP, product)], ["seoul3"])
        product = dict(PRODUCT, zones=[4, 5], zone_sold_out=[5])
        self.assertEqual([z["slug"] for z in buyable_zones(GROUP, product)], ["seoul3"])

    def test_sold_out_zones_are_skipped_and_preference_kept(self):
        product = dict(PRODUCT, zone_sold_out=[3])
        zones = buyable_zones(GROUP, product, ["seoul4"])
        self.assertEqual([z["slug"] for z in zones], ["seoul4", "seoul3"])

    def test_out_of_stock_is_not_buyable(self):
        self.assertIsNone(buyable_zones(GROUP, dict(PRODUCT, in_stock=False)))

    def test_unknown_zone_id_is_not_buyable(self):
        # Ordering without a zone would only get the 400 "choose a zone" error.
        self.assertIsNone(buyable_zones(GROUP, dict(PRODUCT, zones=[9])))

    def test_group_without_zones_orders_without_one(self):
        self.assertEqual(buyable_zones({"zones": None}, PRODUCT), [])


class OrderBodyTests(unittest.TestCase):
    def test_body_matches_checkout_page(self):
        body = order_body(PRODUCT, {"slug": "seoul3"}, None)
        self.assertEqual(
            body,
            {
                "product_slug": "kr-2026-nationalday-event",
                "cycle_months": 1,
                "codes": [],
                "zone": "seoul3",
            },
        )

    def test_os_defaults_to_first_image(self):
        product = dict(PRODUCT, images=["debian-12", "ubuntu-24"])
        self.assertEqual(order_body(product, None, None)["os"], "debian-12")
        self.assertEqual(order_body(product, None, "ubuntu-24")["os"], "ubuntu-24")
        self.assertNotIn("zone", order_body(product, None, None))


class FakeClient:
    def __init__(self, order_errors=None, pay_error=None, catalogs=None):
        self.order_errors = list(order_errors or [])
        self.pay_error = pay_error
        self.catalogs = list(catalogs or [])
        self.orders = []
        self.paid = []
        self.on_empty_catalog = None

    def set_cookie(self, cookie):
        pass

    def me(self):
        return {"user": {"email": "a@example.com", "balance_cents": 1000}}

    def catalog(self):
        if len(self.catalogs) == 1 and self.on_empty_catalog:
            self.on_empty_catalog()
        return self.catalogs.pop(0)

    def create_order(self, body):
        self.orders.append(body)
        if self.order_errors:
            raise self.order_errors.pop(0)
        return {"order": {"order_no": "N123", "status": "pending"}}

    def pay_balance(self, order_no):
        self.paid.append(order_no)
        if self.pay_error:
            raise self.pay_error
        return {"order": {"order_no": order_no, "status": "paid"}}


class TryBuyTests(unittest.TestCase):
    def make_buyer(self, tmp: str, client: FakeClient) -> AutoBuyer:
        cookie = Path(tmp) / "cookie.txt"
        cookie.write_text("Cookie: sid=abc\n", encoding="utf-8")
        args = argparse.Namespace(
            slug=PRODUCT["slug"],
            cookie_file=str(cookie),
            marker_file=str(Path(tmp) / "done.json"),
            manual_login_file=str(Path(tmp) / "manual"),
            status_dir=str(Path(tmp) / "autobuy"),
            interval=3.0,
            zone=[],
            os=None,
            dry_run=False,
            browser_headless=True,
            login_only=False,
        )
        with patch("watcher.nexkr_autobuy.telegram_credentials", return_value={"token": "", "chat_id": ""}):
            buyer = AutoBuyer(args)
        self.assertEqual(buyer.client.session.headers["Cookie"], "sid=abc")
        buyer.client = client
        return buyer

    def test_falls_back_to_next_zone_then_pays(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(order_errors=[ApiError(409, "sold_out", "zone full")])
            buyer = self.make_buyer(tmp, client)
            self.assertTrue(buyer.try_buy(GROUP, PRODUCT))
            self.assertEqual([o["zone"] for o in client.orders], ["goyang2", "seoul3"])
            self.assertEqual(client.paid, ["N123"])
            self.assertIn('"paid": true', Path(buyer.marker_path).read_text())

    def test_payment_failure_still_stops_after_one_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(pay_error=ApiError(402, "insufficient", "no money"))
            buyer = self.make_buyer(tmp, client)
            self.assertTrue(buyer.try_buy(GROUP, PRODUCT))
            self.assertEqual(len(client.orders), 1)
            self.assertTrue(buyer.marker_path.exists())
            self.assertTrue(buyer.manual_login)
            self.assertFalse(buyer.can_login)

    def test_insufficient_balance_order_error_hands_login_to_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = [ApiError(400, "insufficient_balance", "余额不足")] * 5
            client = FakeClient(order_errors=list(errors))
            buyer = self.make_buyer(tmp, client)
            self.assertFalse(buyer.try_buy(GROUP, PRODUCT))
            self.assertTrue(buyer.manual_login)
            self.assertFalse(buyer.marker_path.exists())

    def test_sold_out_error_keeps_auto_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            errors = [ApiError(409, "sold_out", "zone full")] * 5
            client = FakeClient(order_errors=list(errors))
            buyer = self.make_buyer(tmp, client)
            self.assertFalse(buyer.try_buy(GROUP, PRODUCT))
            self.assertFalse(buyer.manual_login)

    def test_sold_out_flag_never_calls_order_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient()
            buyer = self.make_buyer(tmp, client)
            product = dict(PRODUCT, zones=[5], zone_sold_out=[5])
            self.assertFalse(buyer.try_buy(GROUP, product))
            self.assertEqual(client.orders, [])

    def test_run_watches_until_a_zone_opens(self):
        def catalog(**product):
            return {"groups": [dict(GROUP, products=[dict(PRODUCT, **product)])]}

        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(
                catalogs=[
                    catalog(zones=[5], zone_sold_out=[5]),
                    catalog(zones=[5], zone_sold_out=[5]),
                    catalog(zones=[4], zone_sold_out=[5]),
                    catalog(zones=[4], zone_sold_out=[5]),
                ]
            )
            buyer = self.make_buyer(tmp, client)
            client.on_empty_catalog = lambda: setattr(buyer, "stopping", True)
            with patch("watcher.nexkr_autobuy.time.sleep"):
                self.assertEqual(buyer.run(), 0)
            self.assertEqual([o["zone"] for o in client.orders], ["seoul3"])
            self.assertEqual(len(client.catalogs), 1)

    def test_unauthorized_stops_zone_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = FakeClient(order_errors=[ApiError(401, "unauthorized", "x")])
            buyer = self.make_buyer(tmp, client)
            buyer.session_ok = True
            self.assertFalse(buyer.try_buy(GROUP, PRODUCT))
            self.assertEqual(len(client.orders), 1)
            self.assertFalse(buyer.session_ok)
            self.assertFalse(buyer.marker_path.exists())


class FakeSessionClient:
    def __init__(self, good_cookie):
        self.good_cookie = good_cookie
        self.cookie = None

    def set_cookie(self, cookie):
        self.cookie = cookie

    def me(self):
        if self.cookie != self.good_cookie:
            raise ApiError(401, "unauthorized", "not signed in")
        return {"user": {"email": "a@example.com"}}

    def pay_methods(self):
        return {"balance": "10.00"}


class SessionTests(unittest.TestCase):
    def make_buyer(self, tmp, env):
        args = argparse.Namespace(
            slug=PRODUCT["slug"],
            cookie_file=str(Path(tmp) / "cookie.txt"),
            marker_file=str(Path(tmp) / "done.json"),
            manual_login_file=str(Path(tmp) / "manual"),
            status_dir=str(Path(tmp) / "autobuy"),
            interval=10.0,
            zone=[],
            os=None,
            dry_run=False,
            browser_headless=True,
            login_only=False,
        )
        with patch.dict("os.environ", env, clear=False), patch(
            "watcher.nexkr_autobuy.telegram_credentials",
            return_value={"token": "", "chat_id": ""},
        ):
            buyer = AutoBuyer(args)
        buyer.client = FakeSessionClient("sid=new")
        return buyer

    def test_missing_cookie_without_account_is_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                self.make_buyer(tmp, {"NEXKR_EMAIL": "", "NEXKR_PASSWORD": ""})

    def test_expired_cookie_triggers_browser_login_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"NEXKR_EMAIL": "a@example.com", "NEXKR_PASSWORD": "pw"}
            buyer = self.make_buyer(tmp, env)
            with patch(
                "watcher.nexkr_autobuy.browser_login", return_value="sid=new"
            ) as login:
                self.assertTrue(buyer.check_session(announce=True))
                self.assertEqual(login.call_count, 1)
            self.assertEqual(buyer.cookie_path.read_text().strip(), "sid=new")
            self.assertEqual(buyer.cookie_path.stat().st_mode & 0o777, 0o600)

    def test_failed_login_is_throttled(self):
        from watcher.nexkr_autobuy import LoginError

        with tempfile.TemporaryDirectory() as tmp:
            env = {"NEXKR_EMAIL": "a@example.com", "NEXKR_PASSWORD": "pw"}
            buyer = self.make_buyer(tmp, env)
            with patch(
                "watcher.nexkr_autobuy.browser_login", side_effect=LoginError("bad")
            ) as login:
                self.assertFalse(buyer.check_session(announce=True))
                self.assertFalse(buyer.check_session(announce=False))
                self.assertEqual(login.call_count, 1)


    def test_manual_login_file_blocks_browser_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"NEXKR_EMAIL": "a@example.com", "NEXKR_PASSWORD": "pw"}
            buyer = self.make_buyer(tmp, env)
            buyer.hand_over_login()
            with patch("watcher.nexkr_autobuy.browser_login") as login:
                self.assertFalse(buyer.check_session(announce=True))
                login.assert_not_called()
            buyer.manual_login_path.unlink()
            self.assertTrue(buyer.can_login)


if __name__ == "__main__":
    unittest.main()
