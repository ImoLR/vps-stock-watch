from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import requests

from watcher.models import Change, ChangeType, Product
from watcher.telegram import TelegramClient, TelegramError, format_change, format_status


class TelegramFormattingTests(unittest.TestCase):
    def test_no_numeric_stock_is_fabricated(self):
        product = Product("dmit", "183", "TEST Plan", available=False, stock=None)
        message = format_change(Change(ChangeType.NEW, product), ["test"])
        self.assertIn("疑似测试商品", message)
        self.assertIn("状态：无货", message)
        self.assertNotIn("库存：", message)

    def test_status_contains_provider_health(self):
        state = {
            "counters": {"new_products": 2, "notifications": 4},
            "providers": {
                "nexkr": {
                    "last_check_at": None,
                    "last_success_at": None,
                    "last_error": "HTTP 403",
                    "products": {"nexkr:1": {"available": True}},
                }
            },
        }
        message = format_status(state)
        self.assertIn("程序状态：异常", message)
        self.assertIn("Provider 数量：1", message)
        self.assertIn("当前有货：1", message)
        self.assertIn("商品：1", message)
        self.assertIn("有货：1", message)
        self.assertIn("累计通知：4", message)
        self.assertIn("HTTP 403", message)

    def test_network_error_does_not_expose_bot_token(self):
        client = TelegramClient("secret-token", "123")
        with patch.object(
            client.session,
            "post",
            side_effect=requests.RequestException("https://api.telegram.org/botsecret-token/sendMessage"),
        ):
            with self.assertRaises(TelegramError) as caught:
                client.send("hello")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_get_updates_requests_messages_and_callback_queries(self):
        client = TelegramClient("secret-token", "123")
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"ok": True, "result": []}
        with patch.object(client.session, "get", return_value=response) as get:
            client.commands(7)
        self.assertEqual(
            get.call_args.kwargs["params"]["allowed_updates"],
            '["message","callback_query"]',
        )

    def test_get_updates_409_includes_safe_api_diagnostics(self):
        client = TelegramClient("secret-token", "123")
        response = Mock()
        response.status_code = 409
        response.json.return_value = {
            "ok": False,
            "error_code": 409,
            "description": (
                "Conflict: terminated by other getUpdates request; "
                "token=secret-token"
            ),
        }
        with patch.object(client.session, "get", return_value=response):
            with self.assertRaises(TelegramError) as caught:
                client.commands(7)
        error = caught.exception
        self.assertEqual(error.status, 409)
        self.assertEqual(error.error_code, 409)
        self.assertIn("terminated by other getUpdates request", str(error))
        self.assertNotIn("secret-token", str(error))
        self.assertNotIn("api.telegram.org", str(error))

    def test_send_api_error_includes_code_and_description(self):
        client = TelegramClient("secret-token", "123")
        response = Mock()
        response.status_code = 400
        response.json.return_value = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message text is empty",
        }
        with patch.object(client.session, "post", return_value=response):
            with self.assertRaises(TelegramError) as caught:
                client.send("hello")
        self.assertIn("error_code=400", str(caught.exception))
        self.assertIn("message text is empty", str(caught.exception))

    def test_send_supports_inline_keyboard(self):
        client = TelegramClient("secret-token", "123")
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        markup = {"inline_keyboard": [[{"text": "Menu", "callback_data": "menu"}]]}
        with patch.object(client.session, "post", return_value=response) as post:
            result = client.send("hello", reply_markup=markup)
        self.assertEqual(result["message_id"], 1)
        self.assertEqual(post.call_args.kwargs["json"]["reply_markup"], markup)

    def test_bot_command_menu_can_be_registered_and_read(self):
        client = TelegramClient("secret-token", "123")
        set_response = Mock()
        set_response.status_code = 200
        set_response.json.return_value = {"ok": True, "result": True}
        get_response = Mock()
        get_response.status_code = 200
        get_response.json.return_value = {
            "ok": True,
            "result": [
                {"command": "status", "description": "查看监控状态"},
                {"command": "stock", "description": "查询当前可购买库存"},
            ],
        }
        commands = get_response.json.return_value["result"]
        with patch.object(
            client.session, "post", side_effect=[set_response, get_response]
        ) as post:
            client.set_commands(commands)
            self.assertEqual(client.bot_commands(), commands)
        self.assertTrue(post.call_args_list[0].args[0].endswith("/setMyCommands"))
        self.assertEqual(post.call_args_list[0].kwargs["json"], {"commands": commands})
        self.assertTrue(post.call_args_list[1].args[0].endswith("/getMyCommands"))


if __name__ == "__main__":
    unittest.main()
