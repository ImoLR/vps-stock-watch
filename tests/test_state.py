from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watcher.models import ChangeType, Product
from watcher.state import StateStore


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.directory.name) / "state.json"))
        self.one = Product("x", "1", "Plan", available=False)

    def tearDown(self):
        self.directory.cleanup()

    def test_first_snapshot_is_baseline_without_notifications(self):
        first, changes = self.store.record_success("x", [self.one])
        self.assertTrue(first)
        self.assertEqual(changes, [])
        first, changes = self.store.record_success("x", [self.one, Product("x", "2", "New")])
        self.assertFalse(first)
        self.assertEqual([change.type for change in changes], [ChangeType.NEW])
        self.assertEqual(self.store.data["counters"]["new_products"], 1)

    def test_failure_does_not_destroy_old_snapshot(self):
        self.store.record_success("x", [self.one])
        before = self.store.snapshot()["providers"]["x"]["products"]
        success_at = self.store.provider("x")["last_success_at"]
        self.store.record_failure("x", "HTTP 403")
        self.assertEqual(self.store.provider("x")["products"], before)
        self.assertEqual(self.store.provider("x")["last_success_at"], success_at)
        self.assertEqual(self.store.provider("x")["consecutive_failures"], 1)

    def test_atomic_save_and_reload(self):
        self.store.record_success("x", [self.one])
        self.store.save()
        loaded = StateStore(str(self.store.path))
        self.assertTrue(loaded.provider("x")["initialized"])

    def test_provider_interval_settings_persist(self):
        self.store.set_provider_interval("x", 17)
        self.store.save()
        loaded = StateStore(str(self.store.path))
        self.assertEqual(loaded.provider_intervals(), {"x": 17})


if __name__ == "__main__":
    unittest.main()
