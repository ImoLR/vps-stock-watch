from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from watcher.app import WatcherApp
from watcher.route_watch import (
    MisakaRouteWatcher,
    RouteChange,
    RouteStateStore,
    RouteWatchError,
    normalized_asn_path,
    route_features,
)
from watcher.models import Product
from watcher.state import StateStore
from watcher.telegram import (
    ROUTE_STATUS_CALLBACK,
    format_route_changes,
    format_route_status,
)


class FakeTelegram:
    chat_id = "12345"

    def __init__(self):
        self.sent = []
        self.answered = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}

    def answer_callback(self, callback_query_id, text=None, show_alert=False):
        self.answered.append((callback_query_id, text, show_alert))


def callback(data):
    return {
        "callback_query": {
            "id": "route-callback",
            "from": {"id": 12345},
            "message": {"chat": {"id": 12345}},
            "data": data,
        }
    }


def node(country="HK", node_id="234"):
    return {
        "id": node_id,
        "probe_id": int(node_id),
        "country_code": country,
        "location": {"HK": "Hong Kong", "TW": "Taipei", "JP": "Tokyo"}[country],
        "level": "Premium Plus",
        "test_ipv4": "45.11.104.140",
        "test_ipv6": "2407:b9c0:f001:7::2",
    }


def target(target_id="telecom", operator="China Telecom"):
    return {
        "id": target_id,
        "operator": operator,
        "label": "广东电信",
        "ip": "202.96.128.86",
    }


def snapshot(path, rtt=30.0):
    return {
        "measured_at": "2026-09-29T00:00:00Z",
        "report_id": "report-1",
        "source_probe_id": "234",
        "destination_reached": True,
        "raw_hops": [],
        "normalized_asn_path": list(path),
        "fingerprint": ">".join(path),
        "features": route_features(path),
        "destination_rtt_ms": rtt,
        "asn_enriched_hops": len(path),
        "public_hops": len(path),
    }


class FakeASNResolver:
    def __init__(self, mapping):
        self.mapping = mapping

    def enrich(self, ip, cache, org_cache):
        value = self.mapping.get(ip)
        cache[ip] = value
        return value


class FakeWebSocket:
    def __init__(self):
        self.messages = iter(
            [
                "./channel-1",
                json.dumps(
                    {
                        "created": True,
                        "id": "report-1",
                        "created_at": "2026-09-29T00:00:00Z",
                        "tasks": [{"id": "task-1", "probe_id": 234}],
                    }
                ),
                "M/task-1/0/H/45.11.104.140",
                "M/task-1/0/P/1000/1",
                "M/task-1/1/H/202.96.128.86",
                "M/task-1/1/P/30000/2",
                "M/task-1/C",
            ]
        )
        self.sent = []

    def recv(self):
        return next(self.messages)

    def send(self, value):
        self.sent.append(json.loads(value))

    def settimeout(self, _value):
        pass

    def close(self):
        pass


class RouteDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = RouteStateStore(str(Path(self.directory.name) / "route.json"))
        self.watcher = MisakaRouteWatcher(
            {"focus_regions": ["HK", "TW", "JP"], "targets": [target()]},
            self.store,
        )

    def tearDown(self):
        self.directory.cleanup()

    def test_discovers_highest_labeled_line_probe_and_excludes_unlabeled(self):
        probes = {
            "slug": "misaka",
            "probes": [
                {"id": 90, "country": "HK", "location": "Hong Kong", "description": "Premium", "ipv4": True, "test_ipv4": "hk-premium.test", "slug": "hk"},
                {"id": 234, "country": "HK", "location": "Hong Kong", "description": "Premium Plus", "ipv4": True, "test_ipv4": "hk-plus.test", "slug": "hk-plus"},
                {"id": 232, "country": "TW", "location": "Taipei", "description": "Premium Plus", "ipv4": True, "test_ipv4": "tw-plus.test", "slug": "tw-plus"},
                {"id": 233, "country": "TW", "location": "Taipei", "description": "", "ipv4": True, "test_ipv4": "tw-unlabeled.test", "slug": "tw"},
                {"id": 116, "country": "JP", "location": "Tokyo", "description": "Premium", "ipv4": True, "test_ipv4": "jp-premium.test", "slug": "jp"},
                {"id": 158, "country": "JP", "location": "Tokyo", "description": "Premium Plus", "ipv4": True, "test_ipv4": "jp-plus.test", "slug": "jp-plus"},
            ],
        }
        addresses = {
            "hk-plus.test": "45.11.104.140",
            "tw-plus.test": "45.150.242.130",
            "jp-plus.test": "103.170.232.190",
        }
        regions = []
        for country, region_id, ipv4, ipv6 in (
            ("HK", "HKG12", "45.11.104.140", "2407:b9c0:f001:7::2"),
            ("TW", "TPE01", "45.150.242.130", "2407:b9c0:b001::2"),
            ("JP", "NRT04", "103.170.232.190", "2407:b9c0:1:40::2"),
        ):
            regions.append(
                {
                    "id": region_id,
                    "country_code": country,
                    "name": country,
                    "speedtests": [
                        {"label": "Premium Plus (IPv4)", "url": "https://%s/" % ipv4},
                        {"label": "Premium Plus (IPv6)", "url": "https://[%s]/" % ipv6},
                    ],
                }
            )
        self.watcher._get_json = lambda url: probes if "probe/lg" in url else regions
        with patch("watcher.route_watch.socket.gethostbyname", side_effect=lambda name: addresses[name]):
            nodes = self.watcher.discover_nodes()
        self.assertEqual([item["id"] for item in nodes], ["234", "232", "158"])
        self.assertEqual([item["level"] for item in nodes], ["Premium Plus"] * 3)
        self.assertTrue(all(item["node_type"] == "line" for item in nodes))
        self.assertEqual(nodes[1]["test_ipv6"], "2407:b9c0:b001::2")
        self.assertNotIn("233", [item["id"] for item in nodes])

    def test_parser_keeps_timeouts_but_normalization_ignores_them(self):
        hops = {}
        task = "abc"
        self.watcher._parse_mtr_frame("M/abc/0/X/1", task, hops)
        self.watcher._parse_mtr_frame("M/abc/1/H/10.0.0.1", task, hops)
        self.watcher._parse_mtr_frame("M/abc/2/H/203.0.113.1", task, hops)
        self.watcher._parse_mtr_frame("M/abc/2/P/2500/2", task, hops)
        self.assertEqual(hops[0]["samples"], [None])
        self.assertEqual(hops[2]["samples"], [2.5])
        self.assertEqual(
            normalized_asn_path(
                [
                    {"asn": "AS917"},
                    {},
                    {"asn": "AS917"},
                    {"asn": "AS4134"},
                ]
            ),
            ["AS917", "AS4134"],
        )

    def test_measurement_uses_selected_source_and_ipv4_only(self):
        fake = FakeWebSocket()
        with patch("watcher.route_watch.websocket.create_connection", return_value=fake):
            result = self.watcher.measure(node(), target())
        payload = fake.sent[0]
        self.assertEqual(payload["type"], "lg")
        self.assertEqual(payload["tool"], "mtr")
        self.assertEqual(payload["options"]["probe"], "234")
        self.assertEqual(payload["options"]["version"], "ipv4")
        self.assertNotIn("test_ipv6", payload["options"])
        self.assertTrue(result["destination_reached"])

    def test_asn_enrichment_failure_rejects_route(self):
        self.watcher.asn_resolver = FakeASNResolver({})
        raw = {
            "report_id": "1",
            "source_probe_id": "234",
            "raw_hops": [
                {"hop": 0, "ip": "8.8.8.8", "samples": [1.0]},
                {"hop": 1, "ip": "202.96.128.86", "samples": [30.0]},
            ],
        }
        configured = dict(target(), expected_asn="AS4134", expected_prefix="202.96.128.0/18")
        with self.assertRaises(RouteWatchError):
            self.watcher.enrich_and_normalize(raw, node(), configured, {}, {})


class RouteLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "route.json"
        self.store = RouteStateStore(str(self.path))
        self.messages = []
        self.config = {
            "targets": [target()],
            "confirmation_count": 2,
            "minimum_asn_enrichment_ratio": 0.5,
        }
        self.watcher = MisakaRouteWatcher(
            self.config, self.store, notify=self.messages.append
        )
        self.watcher.discover_nodes = lambda: [node()]
        self.watcher.measure = lambda _node, _target: {"raw": True}

    def tearDown(self):
        self.directory.cleanup()

    def set_snapshots(self, values):
        iterator = iter(values)
        self.watcher.enrich_and_normalize = lambda *_args: next(iterator)

    def test_baseline_candidate_confirmation_recovery_and_restart(self):
        old = snapshot(["AS917", "AS58453", "AS4134"], 35)
        new = snapshot(["AS917", "AS4809", "AS4134"], 24)
        self.set_snapshots([old, new, new])
        self.assertTrue(self.watcher.run_cycle())
        self.assertEqual(self.messages, [])
        self.assertTrue(self.watcher.run_cycle())
        route = self.store.data["routes"]["234:telecom"]
        self.assertEqual(route["candidate"]["count"], 1)
        self.assertEqual(self.messages, [])

        restarted_store = RouteStateStore(str(self.path))
        restarted = MisakaRouteWatcher(
            self.config, restarted_store, notify=self.messages.append
        )
        restarted.discover_nodes = lambda: [node()]
        restarted.measure = lambda *_args: {"raw": True}
        restarted.enrich_and_normalize = lambda *_args: new
        self.assertTrue(restarted.run_cycle())
        self.assertEqual(
            restarted_store.data["routes"]["234:telecom"]["baseline"]["fingerprint"],
            new["fingerprint"],
        )
        self.assertEqual(len(self.messages), 1)
        self.assertIn("CTG/CN2 path feature", self.messages[0])

        restarted.enrich_and_normalize = lambda *_args: old
        self.assertTrue(restarted.run_cycle())
        self.assertEqual(len(self.messages), 1)
        self.assertTrue(restarted.run_cycle())
        self.assertEqual(len(self.messages), 2)

    def test_same_asn_path_ignores_rtt_and_interface_changes(self):
        first = snapshot(["AS917", "AS58453", "AS4134"], 30)
        second = snapshot(["AS917", "AS58453", "AS4134"], 41)
        first["raw_hops"] = [{"ip": "1.1.1.1"}]
        second["raw_hops"] = [{"ip": "1.0.0.1"}, {"ip": None}]
        self.set_snapshots([first, second])
        self.assertTrue(self.watcher.run_cycle())
        self.assertTrue(self.watcher.run_cycle())
        route = self.store.data["routes"]["234:telecom"]
        self.assertIsNone(route.get("candidate"))
        self.assertEqual(self.messages, [])

    def test_failure_preserves_confirmed_baseline(self):
        base = snapshot(["AS917", "AS4134"])
        self.set_snapshots([base])
        self.assertTrue(self.watcher.run_cycle())
        before = self.store.data["routes"]["234:telecom"]["baseline"]
        self.watcher.measure = lambda *_args: (_ for _ in ()).throw(
            RouteWatchError("destination unreachable")
        )
        self.assertFalse(self.watcher.run_cycle())
        after = self.store.data["routes"]["234:telecom"]["baseline"]
        self.assertEqual(after, before)
        self.assertIn("destination unreachable", self.store.data["last_error"])

    def test_route_failure_cannot_modify_stock_state_file(self):
        stock_path = Path(self.directory.name) / "stock.json"
        stock = StateStore(str(stock_path))
        stock.record_success(
            "misaka", [Product("misaka", "HKG12:1", "Small", available=False)]
        )
        stock.save()
        before = hashlib.sha256(stock_path.read_bytes()).hexdigest()
        self.watcher.measure = lambda *_args: (_ for _ in ()).throw(
            RouteWatchError("rate limited")
        )
        self.assertFalse(self.watcher.run_cycle())
        after = hashlib.sha256(stock_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_failed_enrichment_keeps_raw_attempt_without_replacing_baseline(self):
        base = snapshot(["AS917", "AS4134"])
        self.set_snapshots([base])
        self.assertTrue(self.watcher.run_cycle())
        before = self.store.data["routes"]["234:telecom"]["baseline"]
        raw = {
            "report_id": "failed-enrichment",
            "source_probe_id": "234",
            "destination": "202.96.128.86",
            "raw_hops": [{"hop": 1, "ip": "202.96.128.86", "samples": [30.0]}],
        }
        self.watcher.measure = lambda *_args: raw
        self.watcher.enrich_and_normalize = lambda *_args: (_ for _ in ()).throw(
            RouteWatchError("ASN enrichment incomplete")
        )
        self.assertFalse(self.watcher.run_cycle())
        self.assertEqual(
            self.store.data["routes"]["234:telecom"]["baseline"], before
        )
        self.assertEqual(
            self.store.data["last_failed_raw_attempts"]["234:telecom"], raw
        )

    def test_overlapping_cycle_is_skipped_without_touching_state(self):
        before = self.store.snapshot()
        self.assertTrue(self.watcher._cycle_lock.acquire(blocking=False))
        try:
            self.assertFalse(self.watcher.run_cycle())
        finally:
            self.watcher._cycle_lock.release()
        self.assertEqual(self.store.snapshot(), before)

    def test_feature_detection_covers_cn2_cmi_and_cug(self):
        features = route_features(["AS917", "AS4809", "AS58453", "AS10099"])
        self.assertIn("CTG/CN2 path feature", features)
        self.assertIn("CMI", features)
        self.assertIn("CUG", features)


class RouteTelegramTests(unittest.TestCase):
    def test_multiple_targets_are_rendered_as_consistent_change(self):
        old = snapshot(["AS917", "AS58453", "AS4134"], 35)
        new = snapshot(["AS917", "AS4809", "AS4134"], 24)
        changes = [
            RouteChange("234:a", node(), target("a"), old, new),
            RouteChange("234:b", node(), target("b"), old, new),
        ]
        rendered = format_route_changes(changes, 2)
        self.assertIn("2/2 个中国电信目标确认变化", rendered)
        self.assertIn("方向：回程（Misaka → 中国大陆）", rendered)

    def test_current_route_query_reads_state_without_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            route_path = Path(directory) / "route.json"
            stock_path = Path(directory) / "state.json"
            store = RouteStateStore(str(route_path))
            store.data.update(
                {
                    "initialized": True,
                    "last_success_at": "2026-09-29T00:00:00Z",
                    "nodes": {"234": node()},
                    "targets": {"telecom": target()},
                    "routes": {
                        "234:telecom": {
                            "node_id": "234",
                            "target_id": "telecom",
                            "baseline": snapshot(["AS917", "AS4134"]),
                            "current": snapshot(["AS917", "AS4134"]),
                            "candidate": None,
                        }
                    },
                }
            )
            store.save()
            before = hashlib.sha256(route_path.read_bytes()).hexdigest()
            app = WatcherApp(
                {
                    "state_path": str(stock_path),
                    "poll_interval_seconds": 60,
                    "providers": [{"name": "misaka", "type": "misaka"}],
                    "route_watch": {
                        "enabled": True,
                        "state_path": str(route_path),
                        "targets": [target()],
                    },
                },
                notifications=False,
            )
            app.telegram = FakeTelegram()
            app.process_update(callback(ROUTE_STATUS_CALLBACK))
            after = hashlib.sha256(route_path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            rendered = "\n".join(text for text, _markup in app.telegram.sent)
            self.assertIn("Misaka 当前线路", rendered)
            self.assertIn("AS917 → AS4134", rendered)
            self.assertIn("返回状态", json.dumps(app.telegram.sent[-1][1], ensure_ascii=False))

    def test_route_status_chunks_without_interactive_pagination(self):
        state = {
            "initialized": True,
            "last_success_at": "2026-09-29T00:00:00Z",
            "nodes": {"234": node()},
            "targets": {"telecom": target()},
            "routes": {
                "234:telecom": {
                    "node_id": "234",
                    "target_id": "telecom",
                    "current": snapshot(["AS%d" % value for value in range(100, 150)]),
                }
            },
        }
        messages = format_route_status(state, max_length=500)
        self.assertGreaterEqual(len(messages), 2)
        self.assertTrue(all(len(message) <= 500 for message in messages))


if __name__ == "__main__":
    unittest.main()
