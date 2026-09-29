from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from watcher.app import WatcherApp
from watcher.isolated_scan import (
    SCAN_MARKER_ENV,
    _marked_processes,
    fetch_products_isolated,
    terminate_marked_processes,
)
from watcher.models import Product
from watcher.providers.base import FetchError


class FakeTelegram:
    chat_id = "12345"

    def __init__(self):
        self.sent = []

    def commands(self, _offset):
        return []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return {"message_id": len(self.sent)}


class SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(2)
        body = b"<html><body>slow</body></html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def log_message(self, _format, *_args):
        pass


class DaemonThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class CatalogHandler(BaseHTTPRequestHandler):
    body = (Path(__file__).parent / "fixtures" / "dmit_catalog.html").read_bytes()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, _format, *_args):
        pass


class ProviderIsolationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def make_app(self):
        route_path = self.root / "route.json"
        route_path.write_text(
            '{"version":1,"initialized":true,"routes":{"stable":{}}}\n',
            encoding="utf-8",
        )
        config = {
            "state_path": str(self.root / "state.json"),
            "poll_interval_seconds": 60,
            "providers": [
                {"name": "dmit", "type": "dmit", "interval_seconds": 120},
                {"name": "nexkr", "type": "nexkr", "interval_seconds": 60},
            ],
            "route_watch": {
                "enabled": True,
                "state_path": str(route_path),
                "targets": [],
            },
        }
        app = WatcherApp(config, notifications=False)
        app.telegram = FakeTelegram()
        return app, route_path

    def test_slow_dmit_does_not_block_other_provider_or_commands(self):
        app, route_path = self.make_app()
        release = threading.Event()
        started = threading.Event()
        fast_finished = threading.Event()
        calls = []

        def check(provider):
            calls.append(provider.name)
            if provider.name == "dmit":
                started.set()
                release.wait(3)
            else:
                fast_finished.set()
            return True

        app.check = check
        route_hash = hashlib.sha256(route_path.read_bytes()).hexdigest()
        before = time.monotonic()
        app.run_scheduled_step(now=0.0)
        elapsed = time.monotonic() - before
        self.assertLess(elapsed, 0.5)
        self.assertTrue(started.wait(1))
        self.assertTrue(fast_finished.wait(1))
        self.assertIn("dmit", app._running)
        app.run_scheduled_step(now=0.0)
        self.assertEqual(calls.count("dmit"), 1)

        app.process_update(
            {
                "message": {
                    "from": {"id": 12345},
                    "chat": {"id": 12345},
                    "text": "/status",
                }
            }
        )
        app.process_update(
            {
                "message": {
                    "from": {"id": 12345},
                    "chat": {"id": 12345},
                    "text": "/stock",
                }
            }
        )
        self.assertEqual(len(app.telegram.sent), 2)
        self.assertIn("VPS Stock Watch 状态", app.telegram.sent[0][0])
        self.assertIn("当前可购买库存", app.telegram.sent[1][0])
        self.assertEqual(
            hashlib.sha256(route_path.read_bytes()).hexdigest(), route_hash
        )

        release.set()
        self.assertTrue(app.wait_for_provider_workers())
        self.assertEqual(sorted(calls), ["dmit", "nexkr"])

    def test_normal_dmit_scan_returns_complete_snapshot(self):
        server = DaemonThreadingHTTPServer(("127.0.0.1", 0), CatalogHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {
            "name": "dmit",
            "type": "dmit",
            "catalog_url": "http://127.0.0.1:%d/" % server.server_port,
            "fetch_mode": "http",
            "timeout_seconds": 5,
            "scan_timeout_seconds": 3,
        }
        try:
            products = fetch_products_isolated(config)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual([item.product_id for item in products], ["265", "266"])

    def test_timeout_preserves_old_snapshot_without_removed_notifications(self):
        app, _route_path = self.make_app()
        provider = next(item for item in app.providers if item.name == "dmit")
        provider.config["isolate_process"] = True
        previous = Product(
            provider="dmit",
            product_id="265",
            name="DMIT Existing",
            available=True,
        )
        app.store.record_success("dmit", [previous])
        app.store.save()
        notifications_before = app.store.data["counters"]["notifications"]

        with patch(
            "watcher.app.fetch_products_isolated",
            side_effect=FetchError(
                "isolated provider scan exceeded 75.0s wall-clock timeout"
            ),
        ):
            self.assertFalse(app.check(provider))

        state = app.store.snapshot()
        dmit = state["providers"]["dmit"]
        self.assertEqual(list(dmit["products"]), ["dmit:265"])
        self.assertTrue(dmit["products"]["dmit:265"]["available"])
        self.assertIn("wall-clock timeout", dmit["last_error"])
        self.assertEqual(dmit["consecutive_failures"], 1)
        self.assertEqual(
            state["counters"]["notifications"], notifications_before
        )
        self.assertEqual(app.telegram.sent, [])

    def test_isolated_scan_enforces_wall_clock_timeout(self):
        server = DaemonThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        marker = "test-" + uuid.uuid4().hex
        config = {
            "name": "dmit",
            "type": "dmit",
            "catalog_url": "http://127.0.0.1:%d/" % server.server_port,
            "fetch_mode": "http",
            "timeout_seconds": 5,
            "scan_timeout_seconds": 0.3,
        }
        fake_uuid = Mock(hex=marker)
        before = time.monotonic()
        try:
            with patch("watcher.isolated_scan.uuid.uuid4", return_value=fake_uuid):
                with self.assertRaises(FetchError) as caught:
                    fetch_products_isolated(config)
        finally:
            server.shutdown()
            server.server_close()
        elapsed = time.monotonic() - before
        self.assertLess(elapsed, 1.5)
        self.assertIn("wall-clock timeout", str(caught.exception))
        self.assertEqual(_marked_processes(marker), set())

    def test_marker_cleanup_removes_reparentable_descendants(self):
        marker = "test-" + uuid.uuid4().hex
        environment = os.environ.copy()
        environment[SCAN_MARKER_ENV] = marker
        code = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            "print(p.pid,flush=True); time.sleep(60)"
        )
        process = subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=environment,
        )
        self.assertIsNotNone(process.stdout)
        child_pid = int(process.stdout.readline().strip())
        terminate_marked_processes(marker, process)
        process.wait(timeout=2)
        process.stdout.close()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and Path("/proc/%d" % child_pid).exists():
            time.sleep(0.05)
        self.assertEqual(_marked_processes(marker), set())
        self.assertFalse(Path("/proc/%d" % child_pid).exists())


if __name__ == "__main__":
    unittest.main()
