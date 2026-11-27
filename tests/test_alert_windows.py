from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from watcher.fac_tg_watch import Relay, announced_time, open_prep_alert
from watcher.fac_trigger import alert_every, open_alert
from watcher.providers.boilcloud_sale import BoilcloudSaleProvider


def at(hour, minute, second=0):
    return datetime(2026, 11, 27, hour, minute, second).timestamp()


class AlertWindowTest(unittest.TestCase):
    def test_window_cooldown_and_hourly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "alert.json")
            now = at(16, 30)
            self.assertIsNone(alert_every(path, now))
            self.assertTrue(open_alert(path, "festive-offer-closed", 30, 5, hourly=True, now=now))
            self.assertEqual(alert_every(path, now + 60), 5.0)
            # The same signal again within the hour: no new window.
            self.assertFalse(open_alert(path, "festive-offer-closed", 30, 5, now=now + 600))
            # Around the full hour: 2 s; after the window: nothing.
            self.assertTrue(open_alert(path, "target-card-9", 30, 5, hourly=True, now=at(16, 50)))
            self.assertEqual(alert_every(path, at(16, 59, 30)), 2.0)
            self.assertEqual(alert_every(path, at(17, 1, 0)), 2.0)
            self.assertEqual(alert_every(path, at(17, 5, 0)), 5.0)
            self.assertIsNone(alert_every(path, at(17, 30)))
            # A window that starts later.
            self.assertTrue(open_alert(path, "announce-x", 5, 2, start=at(22, 58), now=at(17, 0)))
            self.assertIsNone(alert_every(path, at(22, 0)))
            self.assertEqual(alert_every(path, at(22, 59)), 2.0)

    def test_announced_time(self):
        now = at(20, 0)
        self.assertEqual(announced_time("今晚 23:00 開賣", now), at(23, 0))
        self.assertEqual(announced_time("0:00 JST 開賣", now), at(23, 0))
        self.assertEqual(announced_time("明早 9：30 上架", now), at(9, 30) + 24 * 3600)
        self.assertIsNone(announced_time("黑五盲盒即將開賣", now))

    def test_announcement_without_link_opens_a_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay("BOIL", {"channel_id": -100, "link_hosts": ["cloud.boil.network"],
                                   "alert_file": "alert.json", "prep_words": ["盲盒", "開賣"]}, Path(tmp))
            path = str(Path(tmp) / "alert.json")
            now = time.time()
            # With a shop link: the buyer is woken instead.
            self.assertFalse(open_prep_alert(relay, "盲盒 https://cloud.boil.network/store/festive-offer/x", [], now))
            self.assertFalse(open_prep_alert(relay, "HKT 維護通知", [], now))
            self.assertTrue(open_prep_alert(relay, "黑五盲盒第二彈 稍後開賣", [], now))
            self.assertEqual(alert_every(path, now + 10), 3.0)

    def test_sale_page_open_or_emptied_opens_a_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "alert.json")
            item = BoilcloudSaleProvider({"name": "boilcloud_sale", "alert_file": path})
            item._note_open("festive-offer", True)  # first look: no window
            self.assertIsNone(alert_every(path))
            item._note_open("festive-offer", True)
            self.assertIsNone(alert_every(path))
            item._note_open("festive-offer", False)  # emptied
            self.assertIn(alert_every(path), (2.0, 5.0))  # 2 s around a full hour


if __name__ == "__main__":
    unittest.main()
