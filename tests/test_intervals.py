from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from watcher.app import WatcherApp
from watcher.telegram import (
    INTERVAL_MENU_CALLBACK,
    INTERVAL_PROVIDER_PREFIX,
)


ADMIN_ID = "12345"


class FakeTelegram:
    def __init__(self):
        self.chat_id = ADMIN_ID
        self.sent = []
        self.answered = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_query_id, text=None, show_alert=False):
        self.answered.append((callback_query_id, text, show_alert))

    def commands(self, _offset):
        return []


def callback(data, chat_id=ADMIN_ID, user_id=ADMIN_ID):
    return {
        "callback_query": {
            "id": "callback-1",
            "from": {"id": int(user_id)},
            "message": {"chat": {"id": int(chat_id)}, "message_id": 7},
            "data": data,
        }
    }


def message(text, chat_id=ADMIN_ID, user_id=ADMIN_ID):
    return {
        "message": {
            "from": {"id": int(user_id)},
            "chat": {"id": int(chat_id)},
            "text": text,
        }
    }


class IntervalMenuTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state_path = str(Path(self.directory.name) / "state.json")
        self.app = self.make_app()

    def tearDown(self):
        self.directory.cleanup()

    def make_app(self, providers=None):
        config = {
            "state_path": self.state_path,
            "poll_interval_seconds": 60,
            "providers": providers
            or [
                {"name": "nexkr", "type": "nexkr", "interval_seconds": 60},
                {"name": "dmit", "type": "dmit", "interval_seconds": 120},
                {"name": "blossom", "type": "blossom", "interval_seconds": 60},
            ],
        }
        app = WatcherApp(config, notifications=False)
        app.telegram = FakeTelegram()
        return app

    def choose(self, name):
        self.app.process_update(callback(INTERVAL_PROVIDER_PREFIX + name))

    def set_interval(self, name, seconds):
        self.choose(name)
        self.app.process_update(message(str(seconds)))

    def test_admin_can_open_interval_settings_from_status_menu(self):
        self.app.process_update(message("/status"))
        status_text, status_markup = self.app.telegram.sent[-1]
        self.assertIn("VPS Stock Watch 状态", status_text)
        self.assertEqual(
            status_markup["inline_keyboard"][0][0]["text"], "⏱ 设置扫描间隔"
        )

        self.app.process_update(callback(INTERVAL_MENU_CALLBACK))
        text, markup = self.app.telegram.sent[-1]
        self.assertIn("NexKr：60 秒", text)
        self.assertIn("DMIT：120 秒", text)
        self.assertIn("Blossom Host：60 秒", text)
        labels = [row[0]["text"] for row in markup["inline_keyboard"]]
        self.assertEqual(labels, ["NexKr", "DMIT", "Blossom Host", "返回"])

    def test_non_admin_cannot_modify_by_callback_or_number(self):
        self.choose("blossom")
        self.app.process_update(message("17", user_id="99999"))
        self.assertEqual(self.app.provider_interval("blossom"), 60)
        self.app.process_update(
            callback(INTERVAL_PROVIDER_PREFIX + "blossom", user_id="99999")
        )
        self.assertEqual(self.app.provider_interval("blossom"), 60)
        self.assertEqual(self.app.telegram.answered[-1], ("callback-1", "无权限", True))

    def test_nexkr_can_be_set_independently(self):
        self.set_interval("nexkr", 21)
        self.assertEqual(self.app.provider_intervals(), {"nexkr": 21, "dmit": 120, "blossom": 60})

    def test_dmit_can_be_set_independently(self):
        self.set_interval("dmit", 47)
        self.assertEqual(self.app.provider_intervals(), {"nexkr": 60, "dmit": 47, "blossom": 60})

    def test_blossom_can_be_set_independently(self):
        self.set_interval("blossom", 19)
        self.assertEqual(self.app.provider_intervals(), {"nexkr": 60, "dmit": 120, "blossom": 19})

    def test_dmit_failure_retry_is_short_and_bounded(self):
        self.app = self.make_app(
            [
                {
                    "name": "dmit",
                    "type": "dmit",
                    "interval_seconds": 60,
                    "failure_retry_seconds": 30,
                    "max_backoff_seconds": 300,
                }
            ]
        )
        provider = self.app.providers[0]
        delays = [
            self.app._failure_delay(provider, count, 60)
            for count in range(1, 7)
        ]
        self.assertEqual(delays, [30, 60, 120, 240, 300, 300])

    def test_arbitrary_legal_integer_is_saved_without_rounding(self):
        self.set_interval("blossom", 17)
        self.assertEqual(self.app.store.provider_intervals()["blossom"], 17)
        self.assertIn("60 秒 → 17 秒", self.app.telegram.sent[-2][0])

    def test_invalid_inputs_are_rejected_and_prompt_repeats(self):
        for value in ("abc", "1.5", "-10", "0", "9", "86401"):
            with self.subTest(value=value):
                self.app._pending_interval_provider = "nexkr"
                self.app.process_update(message(value))
                self.assertEqual(self.app.provider_interval("nexkr"), 60)
                self.assertEqual(self.app._pending_interval_provider, "nexkr")
                self.assertIn("输入无效", self.app.telegram.sent[-1][0])

    def test_cancel_exits_input_and_returns_to_interval_menu(self):
        self.choose("nexkr")
        self.app.process_update(message("取消"))
        self.assertIsNone(self.app._pending_interval_provider)
        self.assertIn("扫描间隔设置", self.app.telegram.sent[-1][0])

    def test_changed_interval_reschedules_target_immediately(self):
        self.app._clock = lambda: 100.0
        self.app._next_due = {"nexkr": 500.0, "dmit": 500.0, "blossom": 500.0}
        self.app.set_provider_interval("blossom", 17)
        self.assertEqual(self.app._next_due["blossom"], 117.0)
        calls = []
        self.app.check = lambda provider: calls.append(provider.name) is None or True
        self.app.run_scheduled_step(now=116.9)
        self.assertEqual(calls, [])
        self.app.run_scheduled_step(now=117.0)
        self.assertTrue(self.app.wait_for_provider_workers())
        self.assertEqual(calls, ["blossom"])

    def test_one_change_does_not_reschedule_other_providers(self):
        self.app._clock = lambda: 50.0
        self.app._next_due = {"nexkr": 101.0, "dmit": 202.0, "blossom": 303.0}
        self.app.set_provider_interval("nexkr", 23)
        self.assertEqual(self.app._next_due, {"nexkr": 73.0, "dmit": 202.0, "blossom": 303.0})
        self.assertEqual(self.app.provider_intervals(), {"nexkr": 23, "dmit": 120, "blossom": 60})

    def test_saved_interval_survives_new_app_instance(self):
        self.app.set_provider_interval("blossom", 17)
        restarted = self.make_app()
        self.assertEqual(restarted.provider_interval("blossom"), 17)

    def test_same_provider_cannot_reenter_scheduler(self):
        self.app = self.make_app(
            [{"name": "nexkr", "type": "nexkr", "interval_seconds": 10}]
        )
        self.app._clock = lambda: 0.0
        calls = []

        def checking(provider):
            calls.append(provider.name)
            self.app.run_scheduled_step(now=0.0)
            return True

        self.app.check = checking
        self.app.run_scheduled_step(now=0.0)
        self.assertTrue(self.app.wait_for_provider_workers())
        self.assertEqual(calls, ["nexkr"])


if __name__ == "__main__":
    unittest.main()
