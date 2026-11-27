from __future__ import annotations

import argparse
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from watcher import app as app_module
from watcher import boil_autobuy as ba
from watcher.app import WatcherApp
from watcher.fac_trigger import post_trigger, take_triggers
from watcher.quiet_hours import TAIPEI, in_quiet_hours, parse_quiet_hours

from tests import test_boil_autobuy as boil_tests  # module import: its TestCases are not collected twice

BASE = boil_tests.BASE
FakeSite = boil_tests.FakeSite


def taipei(hour, minute):
    return datetime(2026, 11, 28, hour, minute, tzinfo=TAIPEI)


class QuietHoursTest(unittest.TestCase):
    def test_window_past_midnight(self):
        spec = "00:05-09:05"
        self.assertFalse(in_quiet_hours(spec, taipei(0, 4)))
        self.assertTrue(in_quiet_hours(spec, taipei(0, 5)))
        self.assertTrue(in_quiet_hours(spec, taipei(4, 0)))
        self.assertTrue(in_quiet_hours(spec, taipei(9, 4)))
        self.assertFalse(in_quiet_hours(spec, taipei(9, 5)))
        self.assertFalse(in_quiet_hours(spec, taipei(23, 59)))

    def test_window_wrapping_midnight(self):
        spec = "23:30-07:00"
        self.assertTrue(in_quiet_hours(spec, taipei(23, 45)))
        self.assertTrue(in_quiet_hours(spec, taipei(6, 59)))
        self.assertFalse(in_quiet_hours(spec, taipei(12, 0)))

    def test_taiwan_time_whatever_the_host_zone(self):
        utc = datetime.fromisoformat("2026-11-27T20:00:00+00:00")  # 04:00 in Taipei
        self.assertTrue(in_quiet_hours("00:05-09:05", utc))

    def test_empty_is_off_and_typos_fail(self):
        self.assertIsNone(parse_quiet_hours(""))
        self.assertIsNone(parse_quiet_hours(None))
        self.assertFalse(in_quiet_hours("", taipei(4, 0)))
        for bad in ("00:05", "9-10", "25:00-09:00", "09:05-09:05"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_quiet_hours(bad)


class AppQuietHoursTest(unittest.TestCase):
    def make_app(self, tmp, quiet="00:05-09:05"):
        config = {
            "state_path": str(Path(tmp) / "state.json"),
            "poll_interval_seconds": 60,
            "providers": [
                {"name": "nexkr", "type": "nexkr", "interval_seconds": 60, "quiet_hours": quiet},
                {"name": "dmit", "type": "dmit", "interval_seconds": 120},
            ],
        }
        return WatcherApp(config, notifications=False)

    def test_provider_rests_in_its_quiet_hours_and_runs_when_they_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(tmp)
            calls = []
            app.check = lambda provider: calls.append(provider.name) is None or True
            with patch.object(app_module, "in_quiet_hours", side_effect=lambda spec: bool(spec)):
                app.run_scheduled_step(now=0.0)
                self.assertTrue(app.wait_for_provider_workers())
            self.assertEqual(calls, ["dmit"])  # the other provider is not held up
            with patch.object(app_module, "in_quiet_hours", return_value=False):
                app.run_scheduled_step(now=1.0)
                self.assertTrue(app.wait_for_provider_workers())
            self.assertEqual(calls, ["dmit", "nexkr"])  # due at once after the window

    def test_bad_quiet_hours_fail_at_start(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
            self.make_app(tmp, quiet="nightly")


class BuyerQuietHoursTest(unittest.TestCase):
    buyer = boil_tests.BuyerTest.buyer  # the same test buyer

    def test_quiet_hours_send_nothing_and_drop_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            site = FakeSite()
            buyer = self.buyer(tmp, site)
            buyer.cfg["quiet_hours"] = "00:05-09:05"
            buyer.arm([BASE + "store/festive-offer"], "tg-channel", True)
            post_trigger(buyer.trigger_dir, "tg-channel", [BASE + "store/festive-offer"],
                         "黑五優惠碼：BF2026", public=True)
            buyer.refresh_clearance = mock.Mock()
            with patch.object(ba, "in_quiet_hours", return_value=True):
                self.assertTrue(buyer.quiet_step())
                self.assertTrue(buyer.quiet_step())
            self.assertEqual(buyer.armed, {})
            self.assertEqual(take_triggers(buyer.trigger_dir), [])
            self.assertEqual(site.calls, [])
            buyer.refresh_clearance.assert_not_called()
            self.assertIn("BF2026", [promo["code"] for promo in buyer.promos])  # learnt, no request

    def test_end_of_quiet_hours_renews_and_checks_login_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            buyer = self.buyer(tmp, FakeSite())
            buyer.cfg["quiet_hours"] = "00:05-09:05"
            buyer.refresh_clearance = mock.Mock(return_value=True)
            buyer.check_login = mock.Mock(return_value=True)
            with patch.object(ba, "in_quiet_hours", return_value=True):
                buyer.quiet_step()
            with patch.object(ba, "in_quiet_hours", return_value=False):
                self.assertFalse(buyer.quiet_step())
                self.assertFalse(buyer.quiet_step())
            buyer.refresh_clearance.assert_called_once_with()
            buyer.check_login.assert_called_once_with()

    def test_no_quiet_hours_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            buyer = self.buyer(tmp, FakeSite())
            buyer.refresh_clearance = mock.Mock()
            self.assertFalse(buyer.quiet_step())
            buyer.refresh_clearance.assert_not_called()


class FacQuietHoursTest(unittest.TestCase):
    def test_login_check_waits_for_the_end_of_the_quiet_hours(self):
        from watcher import fac_autobuy as fa

        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(no_notify=True, chrome="chromium")
            config = {"fac_autobuy": {
                "quiet_hours": "00:05-09:05",
                "marker_file": str(Path(tmp) / "done.json"),
                "account_file": str(Path(tmp) / "none.env"),
                "trigger_dir": str(Path(tmp) / "triggers"),
                "profile_dir": str(Path(tmp) / "profile"),
            }}
            with patch.object(fa, "STATUS_DIR", str(Path(tmp) / "status")), \
                    patch.object(fa, "telegram_credentials", return_value={"token": None, "chat_id": None}):
                buyer = fa.FacAutoBuyer(config, args)
            every = 6 * 3600
            with patch.object(fa, "in_quiet_hours", return_value=False):
                self.assertTrue(buyer.login_due(every))  # first check at start
                buyer.last_login_check = fa.time.monotonic()
                self.assertFalse(buyer.login_due(every))
            with patch.object(fa, "in_quiet_hours", return_value=True):
                buyer.last_login_check = float("-inf")  # long overdue ...
                self.assertFalse(buyer.login_due(every))  # ... but not at night
            buyer.last_login_check = fa.time.monotonic()  # (checked just before midnight)
            with patch.object(fa, "in_quiet_hours", return_value=False):
                self.assertTrue(buyer.login_due(every))  # once as the window ends


if __name__ == "__main__":
    unittest.main()
