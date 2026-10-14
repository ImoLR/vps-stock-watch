from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.test_intervals import FakeTelegram, callback, message
from watcher.app import WatcherApp
from watcher.autobuy_status import (
    Control,
    Heartbeat,
    effective_state,
    format_autobuy_status,
    load_statuses,
)
from watcher.telegram import AUTOBUY_CALLBACK, STATUS_BACK_CALLBACK, status_menu_markup


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)


def status(name, state, age_seconds=5, **values):
    data = {
        "name": name,
        "title": name.upper(),
        "target": "target-" + name,
        "interval": 5,
        "state": state,
        "started_at": (NOW - timedelta(hours=1)).isoformat(),
        "updated_at": (NOW - timedelta(seconds=age_seconds)).isoformat(),
    }
    data.update(values)
    return data


class HeartbeatTests(unittest.TestCase):
    def test_writes_and_reloads_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            beat = Heartbeat(tmp, "nnb", "NewNB 沪港", "关键词 港", 5)
            beat.update(state="watching", detail="目标节点还没上架")
            beat.update(state="done", result="已扣余额开通")
            (Path(tmp) / "broken.json").write_text("{", encoding="utf-8")
            loaded = load_statuses(tmp)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["state"], "done")
        self.assertEqual(loaded[0]["result"], "已扣余额开通")
        self.assertEqual(loaded[0]["detail"], "目标节点还没上架")

    def test_missing_directory_is_empty(self):
        self.assertEqual(load_statuses("/nonexistent/autobuy"), [])


class FormatTests(unittest.TestCase):
    def test_stale_heartbeat_counts_as_stopped(self):
        self.assertEqual(effective_state(status("a", "watching", 30), NOW), "watching")
        self.assertEqual(effective_state(status("a", "watching", 600), NOW), "stopped")
        self.assertEqual(effective_state(status("a", "done", 99999), NOW), "done")

    def test_lists_running_buyers_first_with_targets(self):
        text = format_autobuy_status(
            [
                status("old", "watching", 600),
                status("nnb", "watching", detail="目标节点还没上架"),
                status("nexkr", "done", result="订单 X，已付款"),
            ],
            NOW,
        )
        self.assertIn("运行中 1 / 共 3", text)
        self.assertLess(text.index("NNB"), text.index("NEXKR"))
        self.assertLess(text.index("NEXKR"), text.index("OLD"))
        self.assertIn("🟢 正在抢购", text)
        self.assertIn("目标：target-nnb", text)
        self.assertIn("状态：目标节点还没上架", text)
        self.assertIn("✅ 已抢到", text)
        self.assertIn("结果：订单 X，已付款", text)
        self.assertIn("⚠️ 进程未运行", text)

    def test_escapes_html_and_handles_empty(self):
        self.assertIn("没有抢购程序", format_autobuy_status([], NOW))
        text = format_autobuy_status([status("x", "watching", detail="<b>")], NOW)
        self.assertIn("&lt;b&gt;", text)


class TelegramAutobuyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.status_dir = Path(self.directory.name) / "autobuy"
        self.status_dir.mkdir()
        (self.status_dir / "nnb.json").write_text(
            json.dumps(
                status(
                    "nnb",
                    "watching",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.app = WatcherApp(
            {
                "state_path": str(Path(self.directory.name) / "state.json"),
                "autobuy_status_dir": str(self.status_dir),
                "poll_interval_seconds": 60,
                "providers": [{"name": "nexkr", "type": "nexkr", "interval_seconds": 60}],
            },
            notifications=False,
        )
        self.app.telegram = FakeTelegram()

    def test_status_menu_has_autobuy_button(self):
        buttons = [row[0]["callback_data"] for row in status_menu_markup()["inline_keyboard"]]
        self.assertIn(AUTOBUY_CALLBACK, buttons)

    def test_button_and_command_show_buyers(self):
        self.app.process_update(callback(AUTOBUY_CALLBACK))
        self.app.process_update(message("/autobuy"))
        self.assertEqual(len(self.app.telegram.sent), 2)
        for text, markup in self.app.telegram.sent:
            self.assertIn("🟢 正在抢购", text)
            self.assertIn("目标：target-nnb", text)
            rows = [[b["callback_data"] for b in row] for row in markup["inline_keyboard"]]
            self.assertEqual(
                rows,
                [
                    ["autobuy:stop:nnb", "autobuy:interval:nnb"],
                    [AUTOBUY_CALLBACK, STATUS_BACK_CALLBACK],
                ],
            )

    def test_back_returns_to_status(self):
        self.app.process_update(callback(STATUS_BACK_CALLBACK))
        _text, markup = self.app.telegram.sent[-1]
        self.assertEqual(markup, status_menu_markup())

    def control(self):
        return Control(str(self.status_dir), "nnb")

    def write_status(self, **values):
        data = status("nnb", "watching", updated_at=datetime.now(timezone.utc).isoformat())
        data.update(values)
        (self.status_dir / "nnb.json").write_text(json.dumps(data), encoding="utf-8")

    def test_stop_then_start(self):
        self.app.process_update(callback("autobuy:stop:nnb"))
        self.assertTrue(self.control().paused())
        text, markup = self.app.telegram.sent[-1]
        self.assertIn("停止指令已下达", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "autobuy:start:nnb")
        self.write_status(state="paused")
        self.app.process_update(callback("autobuy:start:nnb"))
        self.assertFalse(self.control().paused())

    def test_start_after_purchase_asks_to_confirm(self):
        self.write_status(state="paused", result="已扣余额开通，服务 #380")
        self.control().set(True, "已抢到，自动停止")
        self.app.process_update(callback("autobuy:start:nnb"))
        self.assertTrue(self.control().paused())
        text, markup = self.app.telegram.sent[-1]
        self.assertIn("再下单一台并扣款", text)
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "autobuy:start!:nnb")
        self.app.process_update(callback("autobuy:start!:nnb"))
        self.assertFalse(self.control().paused())

    def test_interval_is_typed_and_validated(self):
        self.app.process_update(callback("autobuy:interval:nnb"))
        self.assertIn("当前：每 5 秒", self.app.telegram.sent[-1][0])
        self.app.process_update(message("1"))
        self.assertIn("2–3600", self.app.telegram.sent[-1][0])
        self.app.process_update(message("8"))
        self.assertEqual(self.control().interval(5), 8)
        self.assertIn("轮询：每 8 秒", self.app.telegram.sent[-1][0])
        # Once set, plain numbers no longer change anything.
        self.app.process_update(message("9"))
        self.assertEqual(self.control().interval(5), 8)

    def test_dead_buyer_cannot_be_controlled(self):
        self.write_status(updated_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
        self.app.process_update(callback("autobuy:stop:nnb"))
        self.assertFalse(self.control().exists())
        self.assertIn("没有在运行", self.app.telegram.sent[0][0])

    def test_unauthorized_user_sees_nothing(self):
        self.app.process_update(message("/autobuy", user_id="999"))
        self.assertEqual(self.app.telegram.sent, [])


if __name__ == "__main__":
    unittest.main()
