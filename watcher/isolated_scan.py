from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .models import Product
from .providers.base import FetchError


LOG = logging.getLogger(__name__)
SCAN_MARKER_ENV = "VPS_STOCK_SCAN_ID"
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_SCANS: Dict[str, tuple[subprocess.Popen[str], Optional[Path]]] = {}


def fetch_products_isolated(config: Dict[str, Any]) -> List[Product]:
    """Fetch a provider snapshot in a killable process with a wall-clock limit."""
    timeout = max(0.1, float(config.get("scan_timeout_seconds", 75)))
    marker = uuid.uuid4().hex
    environment = os.environ.copy()
    environment[SCAN_MARKER_ENV] = marker
    process = subprocess.Popen(
        [sys.executable, "-m", "watcher.scan_worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=environment,
    )
    cgroup = _create_scan_cgroup(marker, process.pid, config)
    with _ACTIVE_LOCK:
        _ACTIVE_SCANS[marker] = (process, cgroup)
    stdout = ""
    stderr = ""
    timed_out = False
    try:
        stdout, stderr = process.communicate(
            json.dumps(config, ensure_ascii=False), timeout=timeout
        )
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_marked_processes(marker, process, cgroup=cgroup)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            terminate_marked_processes(marker, process, force=True, cgroup=cgroup)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                LOG.warning(
                    "isolated provider worker pid=%d did not exit after SIGKILL",
                    process.pid,
                )
    finally:
        # Chromium crash handlers can re-parent themselves before the worker exits.
        # The inherited marker lets us remove only processes from this scan.
        terminate_marked_processes(marker, None, cgroup=cgroup)
        _remove_scan_cgroup(cgroup)
        with _ACTIVE_LOCK:
            _ACTIVE_SCANS.pop(marker, None)

    if timed_out:
        raise FetchError(
            "isolated provider scan exceeded %.1fs wall-clock timeout" % timeout
        )
    try:
        payload = json.loads(stdout)
    except (TypeError, ValueError):
        detail = _last_line(stderr)
        raise FetchError(
            "isolated provider scan returned invalid output%s"
            % ((": " + detail) if detail else "")
        ) from None
    if not isinstance(payload, dict) or not payload.get("ok"):
        error_type = (
            str(payload.get("error_type") or "ProviderError")
            if isinstance(payload, dict)
            else "ProviderError"
        )
        error = (
            str(payload.get("error") or "unknown worker failure")
            if isinstance(payload, dict)
            else "unknown worker failure"
        )
        raise FetchError("isolated provider scan failed (%s): %s" % (error_type, error))
    products = payload.get("products")
    if not isinstance(products, list) or not all(
        isinstance(item, dict) for item in products
    ):
        raise FetchError("isolated provider scan returned no product list")
    try:
        return [Product.from_dict(item) for item in products]
    except (TypeError, ValueError) as exc:
        raise FetchError(
            "isolated provider scan returned invalid products (%s)" % type(exc).__name__
        ) from None


def terminate_marked_processes(
    marker: str,
    process: Optional[subprocess.Popen[str]] = None,
    force: bool = False,
    cgroup: Optional[Path] = None,
) -> None:
    signal_number = signal.SIGKILL if force else signal.SIGTERM
    pids = _marked_processes(marker)
    if process is not None and process.poll() is None:
        pids.add(process.pid)
    if force and cgroup is not None:
        try:
            (cgroup / "cgroup.kill").write_text("1", encoding="ascii")
        except OSError:
            pass
    for pid in sorted(pids, reverse=True):
        try:
            os.kill(pid, signal_number)
        except (ProcessLookupError, PermissionError):
            continue
    if not pids or force:
        return
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if not _marked_processes(marker):
            return
        time.sleep(0.05)
    terminate_marked_processes(marker, process, force=True, cgroup=cgroup)


def terminate_active_scans() -> None:
    with _ACTIVE_LOCK:
        scans = list(_ACTIVE_SCANS.items())
    for marker, (process, cgroup) in scans:
        terminate_marked_processes(marker, process, cgroup=cgroup)


def _create_scan_cgroup(
    marker: str, pid: int, config: Dict[str, Any]
) -> Optional[Path]:
    high_mb = config.get("scan_memory_high_mb")
    max_mb = config.get("scan_memory_max_mb")
    if high_mb is None and max_mb is None:
        return None
    try:
        relative = next(
            line.split("::", 1)[1]
            for line in Path("/proc/self/cgroup").read_text(encoding="ascii").splitlines()
            if line.startswith("0::")
        )
        current = Path("/sys/fs/cgroup") / relative.lstrip("/")
        # New systemd releases can place the service in DelegateSubgroup=watcher.
        # Older supported releases ignore that directive, so move all service
        # processes into the same leaf before enabling child memory controllers.
        parent = _prepare_delegated_parent(current)
        subtree_control = parent / "cgroup.subtree_control"
        enabled = subtree_control.read_text(encoding="ascii").split()
        if "memory" not in enabled:
            subtree_control.write_text("+memory", encoding="ascii")
        cgroup = parent / ("provider-scan-" + marker[:12])
        cgroup.mkdir(mode=0o700)
        if high_mb is not None:
            (cgroup / "memory.high").write_text(
                str(int(float(high_mb) * 1024 * 1024)), encoding="ascii"
            )
        if max_mb is not None:
            (cgroup / "memory.max").write_text(
                str(int(float(max_mb) * 1024 * 1024)), encoding="ascii"
            )
        (cgroup / "memory.oom.group").write_text("1", encoding="ascii")
        # The worker blocks reading its config from stdin, so it cannot start
        # Chromium before it has been moved into this delegated cgroup.
        (cgroup / "cgroup.procs").write_text(str(pid), encoding="ascii")
        LOG.info(
            "isolated scan cgroup ready high_mb=%s max_mb=%s",
            high_mb,
            max_mb,
        )
        return cgroup
    except (OSError, StopIteration, TypeError, ValueError) as exc:
        LOG.warning(
            "cannot apply isolated scan cgroup limits (%s)", type(exc).__name__
        )
        try:
            cgroup.rmdir()
        except (OSError, UnboundLocalError):
            pass
        return None


def _prepare_delegated_parent(current: Path) -> Path:
    if current.name == "watcher":
        return current.parent
    leaf = current / "watcher"
    leaf.mkdir(mode=0o700, exist_ok=True)
    root_procs = current / "cgroup.procs"
    leaf_procs = leaf / "cgroup.procs"
    for _attempt in range(4):
        pids = [
            value
            for value in root_procs.read_text(encoding="ascii").split()
            if value
        ]
        if not pids:
            return current
        for pid in pids:
            leaf_procs.write_text(pid, encoding="ascii")
    if root_procs.read_text(encoding="ascii").split():
        raise OSError("delegated service cgroup still contains root processes")
    return current


def _remove_scan_cgroup(cgroup: Optional[Path]) -> None:
    if cgroup is None:
        return
    try:
        cgroup.rmdir()
    except OSError:
        LOG.warning("isolated scan cgroup could not be removed: %s", cgroup.name)


def _marked_processes(marker: str) -> Set[int]:
    expected = (SCAN_MARKER_ENV + "=" + marker).encode("utf-8")
    found: Set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            values = (entry / "environ").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if expected in values:
            found.add(int(entry.name))
    return found


def _last_line(value: str) -> str:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    return lines[-1][:500] if lines else ""
