from __future__ import annotations

import argparse
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from watcher.nnb_autobuy import (
    ApiError,
    AutoBuyer,
    build_order,
    choose_nics,
    matching_nodes,
)
from watcher.providers.base import ParseError
from watcher.providers.nnb import NnbProvider, default_spec, node_items


FIXTURE = Path(__file__).parent / "fixtures" / "nnb_nodes.json"
KEYWORDS = ["港", "HK", "Hong Kong", "HongKong"]


def payload():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def node(node_id):
    return next(n for n in node_items(payload()) if n["id"] == node_id)


class NnbProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = NnbProvider({"name": "nnb", "type": "nnb"})

    def test_one_product_per_primary_nic_with_starting_price(self):
        products = {p.product_id: p for p in self.provider.parse_nodes(payload())}
        self.assertEqual(
            sorted(products),
            ["node-2-nic-1", "node-3-nic-1", "node-4-nic-1", "node-7-nic-1", "node-7-nic-2"],
        )
        sh = products["node-3-nic-1"]
        self.assertEqual(sh.name, "SH #1 · 沪日专线-CM")
        self.assertEqual(sh.category, "上海")
        self.assertEqual(sh.price, "¥19.90 CNY 起")
        self.assertTrue(sh.available)
        self.assertIsNone(sh.stock)
        self.assertEqual(sh.url, "https://nnb.ltd/dashboard/create?node=3")
        self.assertEqual(sh.specs["network"], "CMCC NAT")
        self.assertEqual(products["node-4-nic-1"].price, "¥15.00 CNY 起")
        self.assertIn("IX + 移动入口 +¥13.00", products["node-4-nic-1"].specs["extra_nics"])
        self.assertEqual(products["node-7-nic-1"].price, "¥15.00 CNY 起")
        self.assertEqual(products["node-7-nic-2"].price, "¥22.00 CNY 起")
        self.assertFalse(products["node-7-nic-2"].available)

    def test_rejects_malformed_and_empty_payloads(self):
        with self.assertRaises(ParseError):
            self.provider.parse_nodes({"code": 200, "data": {}})
        with self.assertRaises(ParseError):
            self.provider.parse_nodes({"code": 200, "data": {"items": []}})

    def test_default_spec_is_node_minimum_with_free_tier_bandwidth(self):
        hk = node(7)
        spec = default_spec(hk, hk["primary_nics"][1])
        self.assertEqual(
            spec,
            {"cpu": 1, "memory_gb": 0.75, "disk_gb": 15, "bandwidth_mbps": 200, "traffic_gb": 300},
        )


class TargetSelectionTests(unittest.TestCase):
    def test_keywords_only_match_the_hong_kong_node(self):
        matched = matching_nodes(node_items(payload()), KEYWORDS)
        self.assertEqual([n["id"] for n in matched], [7])
        self.assertEqual(matching_nodes(node_items(payload()), KEYWORDS, exclude=[7]), [])

    def test_cm_prefers_cm_primary_nic(self):
        primary, extra = choose_nics(node(7), "cm")
        self.assertEqual(primary["label"], "沪港专线-CM")
        self.assertIsNone(extra)
        primary, extra = choose_nics(node(7), "ix")
        self.assertEqual(primary["label"], "沪港专线-IX")

    def test_cm_falls_back_to_cm_extra_nic(self):
        primary, extra = choose_nics(node(4), "cm")
        self.assertEqual(primary["label"], "Skyline 网络")
        self.assertEqual(extra["label"], "IX + 移动入口")

    def test_node_without_cm_is_skipped(self):
        hk = node(7)
        hk["primary_nics"] = hk["primary_nics"][:1]
        self.assertIsNone(choose_nics(hk, "cm"))

    def test_order_body_matches_site_payload(self):
        hk = node(7)
        primary, extra = choose_nics(hk, "cm")
        body, price = build_order(hk, primary, extra, ["100000"], 1)
        self.assertEqual(
            body,
            {
                "billing_mode": "monthly",
                "node_id": 7,
                "cpu": 1,
                "memory_gb": 0.75,
                "disk_gb": 15,
                "bandwidth_mbps": 200,
                "months": 1,
                "auto_renew": False,
                "traffic_gb": 300,
                "primary_nic": 2,
                "regional_codes": ["100000"],
            },
        )
        self.assertEqual(price, 220000)

    def test_extra_nic_order_includes_its_traffic_and_regions(self):
        tokyo = node(4)
        primary, extra = choose_nics(tokyo, "cm")
        body, price = build_order(tokyo, primary, extra, ["100000", "110000"], 1)
        self.assertEqual(body["primary_nic"], 1)
        self.assertEqual(body["extra_nic"], 1)
        self.assertEqual(body["extra_nic_traffic_gb"], 50)
        self.assertEqual(body["extra_nic_bandwidth_mbps"], 1)
        self.assertEqual(body["regional_codes"], ["100000", "110000"])
        self.assertEqual(price, 150000 + 130000)


def buyer_args(tmp, **values):
    defaults = dict(
        keyword=KEYWORDS,
        exclude_node=[],
        network="cm",
        max_price=30.0,
        months=1,
        region=["100000"],
        template="Debian",
        method="aggpay",
        interval=5.0,
        token_file=str(Path(tmp) / "token.txt"),
        marker_file=str(Path(tmp) / "done.json"),
        ssh_key_file=str(Path(tmp) / "keys.pub"),
        status_dir=str(Path(tmp) / "autobuy"),
        dry_run=False,
    )
    defaults.update(values)
    return argparse.Namespace(**defaults)


class AutoBuyerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = patch(
            "watcher.nnb_autobuy.telegram_credentials",
            return_value={"token": "", "chat_id": ""},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.hk = node(7)
        self.hk["orderable"] = True

    def make(self, balance_raw, **values):
        Path(self.tmp.name, "keys.pub").write_text(
            "# comment\nssh-ed25519 AAAAfile bot\n\n", encoding="utf-8"
        )
        buyer = AutoBuyer(buyer_args(self.tmp.name, **values))
        buyer.account_keys = ["ssh-ed25519 AAAAsaved me", "ssh-ed25519 AAAAfile bot"]
        buyer.client = MagicMock()
        buyer.client.token = "t"
        buyer.client.templates.return_value = {
            "items": [{"id": 9, "name": "Alpine"}, {"id": 4, "name": "Debian13"}],
            "provisionable": True,
        }
        buyer.client.wallet.return_value = {"balance": {"raw": balance_raw}}
        buyer.notify = MagicMock()
        return buyer

    def offer(self, buyer):
        return buyer.offer(self.hk)

    def test_pays_from_balance_when_it_covers_the_price(self):
        buyer = self.make(300000)
        buyer.client.create_service.return_value = {"id": 55}
        body, price = self.offer(buyer)
        self.assertTrue(buyer.try_buy(self.hk, body, price))
        sent = buyer.client.create_service.call_args[0][0]
        self.assertEqual(sent["template_id"], 4)
        self.assertEqual(sent["ssh_keys"], "ssh-ed25519 AAAAfile bot\nssh-ed25519 AAAAsaved me")
        buyer.client.checkout.assert_not_called()
        marker = json.loads(Path(buyer.marker_path).read_text(encoding="utf-8"))
        self.assertTrue(marker["paid"])
        self.assertIn("/dashboard/services/55", buyer.notify.call_args[0][0])

    def test_short_balance_creates_checkout_link(self):
        buyer = self.make(0)
        buyer.client.checkout.return_value = {"checkout_url": "https://pay.example/x"}
        self.assertTrue(buyer.try_buy(self.hk, *self.offer(buyer)))
        buyer.client.create_service.assert_not_called()
        sent = buyer.client.checkout.call_args[0][0]
        self.assertEqual(sent["method"], "aggpay")
        self.assertFalse(sent["use_balance"])
        self.assertIn("https://pay.example/x", buyer.notify.call_args[0][0])

    def test_balance_rejection_falls_back_to_checkout(self):
        buyer = self.make(300000)
        buyer.client.create_service.side_effect = ApiError(400, "余额不足")
        buyer.client.checkout.return_value = {"checkout_url": "https://pay.example/y"}
        self.assertTrue(buyer.try_buy(self.hk, *self.offer(buyer)))
        self.assertTrue(buyer.client.checkout.call_args[0][0]["use_balance"])

    def test_other_failures_back_off_and_keep_polling(self):
        buyer = self.make(300000)
        buyer.client.create_service.side_effect = ApiError(409, "Sold out")
        self.assertFalse(buyer.try_buy(self.hk, *self.offer(buyer)))
        self.assertFalse(buyer.marker_path.exists())
        self.assertEqual(buyer.failures, 1)
        self.assertFalse(buyer.try_buy(self.hk, *self.offer(buyer)))
        self.assertEqual(buyer.failures, 2)

    def test_kyc_rejection_is_reported_once(self):
        buyer = self.make(300000)
        buyer.client.create_service.side_effect = ApiError(403, "请先完成 KYC 实名认证")
        body, price = self.offer(buyer)
        buyer.try_buy(self.hk, copy.deepcopy(body), price)
        buyer.try_buy(self.hk, copy.deepcopy(body), price)
        self.assertEqual(buyer.notify.call_count, 1)
        self.assertIn("KYC", buyer.notify.call_args[0][0])

    def test_missing_ssh_key_fails_before_ordering(self):
        buyer = self.make(300000)
        buyer.account_keys = []
        Path(self.tmp.name, "keys.pub").unlink()
        self.assertFalse(buyer.try_buy(self.hk, *self.offer(buyer)))
        buyer.client.create_service.assert_not_called()
        self.assertIn("SSH", buyer.notify.call_args[0][0])

    def test_price_above_cap_is_not_bought(self):
        buyer = self.make(300000, max_price=20.0)
        self.assertIsNone(self.offer(buyer))
        self.assertIsNone(self.offer(buyer))
        self.assertEqual(buyer.notify.call_count, 1)

    def test_expired_token_signs_in_again(self):
        buyer = self.make(0)
        buyer.username, buyer.password = "u", "p"
        buyer.client.wallet.side_effect = [ApiError(401, "Unauthorized"), {"balance": {"raw": 5}}]
        buyer.client.token = "fresh"
        self.assertEqual(buyer.authed(buyer.client.wallet), {"balance": {"raw": 5}})
        buyer.client.login.assert_called_once_with("u", "p")
        self.assertEqual(Path(buyer.token_path).read_text(encoding="utf-8").strip(), "fresh")

    def test_existing_marker_refuses_to_run(self):
        buyer = self.make(0)
        buyer.marker_path.write_text("{}", encoding="utf-8")
        self.assertEqual(buyer.run(), 0)
        buyer.client.nodes.assert_not_called()


if __name__ == "__main__":
    unittest.main()
