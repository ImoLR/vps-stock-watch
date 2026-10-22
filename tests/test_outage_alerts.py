from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from watcher.app import WatcherApp
from watcher.models import Product
from watcher.providers.base import FetchError


class FakeTelegram:
    chat_id = "123"

    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append(text)
        return {"message_id": len(self.sent)}


def product():
    return Product(provider="vmsilo", product_id="plan-a", name="Plan A", available=True)


class OutageAlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = WatcherApp(
            {
                "state_path": str(Path(self.directory.name) / "state.json"),
                "poll_interval_seconds": 60,
                "outage_alert_after_seconds": 3600,
                "providers": [{"name": "vmsilo", "type": "vmsilo"}],
            },
            notifications=False,
        )
        self.app.telegram = FakeTelegram()
        self.provider = self.app.providers[0]

    def tearDown(self):
        self.directory.cleanup()

    def succeed(self):
        self.provider.fetch_products = lambda: [product()]
        return self.app.check(self.provider)

    def fail(self):
        self.provider.fetch_products = lambda: (_ for _ in ()).throw(
            FetchError("HTTP 403 for https://example.test/")
        )
        return self.app.check(self.provider)

    def age_last_success(self, seconds):
        entry = self.app.store.provider("vmsilo")
        when = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        entry["last_success_at"] = when.isoformat().replace("+00:00", "Z")

    def test_short_outage_stays_silent(self):
        self.succeed()
        for _ in range(5):
            self.fail()
        self.assertEqual(self.app.telegram.sent, [])
        self.succeed()
        self.assertEqual(self.app.telegram.sent, [])

    def test_long_outage_alerts_once_then_reports_recovery(self):
        self.succeed()
        self.fail()
        self.fail()
        self.age_last_success(3700)
        self.fail()
        self.assertEqual(len(self.app.telegram.sent), 1)
        self.assertIn("VMSILO 监控异常", self.app.telegram.sent[0])
        self.assertIn("HTTP 403", self.app.telegram.sent[0])
        self.fail()
        self.fail()
        self.assertEqual(len(self.app.telegram.sent), 1)
        self.succeed()
        self.assertEqual(len(self.app.telegram.sent), 2)
        self.assertIn("VMSILO 监控已恢复", self.app.telegram.sent[1])
        self.assertNotIn("outage_alerted_at", self.app.store.provider("vmsilo"))
        self.succeed()
        self.assertEqual(len(self.app.telegram.sent), 2)

    def test_needs_three_failures_even_after_long_quiet_interval(self):
        self.succeed()
        self.age_last_success(7200)
        self.fail()
        self.fail()
        self.assertEqual(self.app.telegram.sent, [])
        self.fail()
        self.assertEqual(len(self.app.telegram.sent), 1)


if __name__ == "__main__":
    unittest.main()
