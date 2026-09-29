from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

import dns.resolver
import requests
import websocket


PING_SX_API = "https://public-us-pingsx.api.clonoth.com/v2"
PING_SX_WS = "wss://public-us-pingsx.api.clonoth.com/v2/probe/ws"
MISAKA_CATALOG_API = "https://app.misaka.io/api/mc2"
PING_SX_ORIGIN = "https://misaka.ping.sx"
LOG = logging.getLogger(__name__)


class RouteWatchError(RuntimeError):
    pass


@dataclass
class RouteChange:
    route_key: str
    node: Dict[str, Any]
    target: Dict[str, Any]
    previous: Dict[str, Any]
    current: Dict[str, Any]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def empty_route_state() -> Dict[str, Any]:
    now = utc_now()
    return {
        "version": 1,
        "created_at": now,
        "updated_at": now,
        "initialized": False,
        "last_check_at": None,
        "last_success_at": None,
        "last_change_at": None,
        "last_error": None,
        "consecutive_failures": 0,
        "nodes": {},
        "targets": {},
        "routes": {},
        "asn_cache": {},
        "asn_org_cache": {},
        "last_cycle_stats": {},
    }


class RouteStateStore:
    """Atomic, independent state file so route work never races stock state."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return empty_route_state()
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except (OSError, ValueError) as exc:
            raise RuntimeError("cannot load route state %s: %s" % (self.path, exc))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise RuntimeError("unsupported route state format in %s" % self.path)
        defaults = empty_route_state()
        for key, default in defaults.items():
            value.setdefault(key, default)
        return value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data["updated_at"] = utc_now()
        fd, temporary = tempfile.mkstemp(
            prefix=".%s." % self.path.name,
            suffix=".tmp",
            dir=str(self.path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self.data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def snapshot(self) -> Dict[str, Any]:
        return deepcopy(self.data)

    def record_failure(
        self, error: str, raw_attempts: Optional[Dict[str, Any]] = None
    ) -> None:
        self.data["last_check_at"] = utc_now()
        self.data["last_error"] = error[:1000]
        self.data["consecutive_failures"] = int(
            self.data.get("consecutive_failures", 0)
        ) + 1
        if raw_attempts:
            # Diagnostic evidence is deliberately separate from confirmed routes.
            # A partial/unenriched attempt must never replace a route baseline.
            self.data["last_failed_raw_attempts"] = deepcopy(raw_attempts)
        self.save()


class CymruASNResolver:
    """Team Cymru DNS enrichment with a persistent caller-owned cache."""

    def __init__(self, timeout: float = 5.0):
        self.resolver = dns.resolver.Resolver()
        self.resolver.lifetime = timeout
        self.resolver.timeout = timeout

    def enrich(
        self,
        ip: str,
        cache: Dict[str, Any],
        org_cache: Dict[str, Any],
    ) -> Optional[Dict[str, str]]:
        if ip in cache:
            value = cache[ip]
            return dict(value) if isinstance(value, dict) else None
        address = ipaddress.ip_address(ip)
        if address.version != 4 or not address.is_global:
            cache[ip] = None
            return None
        query = "%s.origin.asn.cymru.com" % ".".join(reversed(ip.split(".")))
        records = self._txt(query)
        candidates = []
        for record in records:
            fields = [item.strip() for item in record.split("|")]
            if len(fields) < 2:
                continue
            try:
                asn = int(fields[0].split()[0])
                network = ipaddress.ip_network(fields[1], strict=False)
            except (ValueError, IndexError):
                continue
            candidates.append((network.prefixlen, asn, str(network)))
        if not candidates:
            raise RouteWatchError("ASN lookup returned no usable origin for %s" % ip)
        _prefix_length, asn, prefix = max(candidates)
        key = str(asn)
        organization = org_cache.get(key)
        if not isinstance(organization, str):
            organization = self._asn_name(asn)
            org_cache[key] = organization
        result = {
            "asn": "AS%d" % asn,
            "asn_number": str(asn),
            "asn_org": organization,
            "prefix": prefix,
        }
        cache[ip] = result
        return dict(result)

    def _asn_name(self, asn: int) -> str:
        records = self._txt("AS%d.asn.cymru.com" % asn)
        if not records:
            return ""
        fields = [item.strip() for item in records[0].split("|")]
        return fields[4] if len(fields) >= 5 else ""

    def _txt(self, query: str) -> List[str]:
        try:
            answers = self.resolver.resolve(query, "TXT")
        except Exception as exc:
            raise RouteWatchError(
                "ASN DNS lookup failed for %s (%s)" % (query, type(exc).__name__)
            ) from None
        return [
            b"".join(getattr(answer, "strings", ())).decode("utf-8", "replace")
            for answer in answers
        ]


class MisakaRouteWatcher:
    def __init__(
        self,
        config: Dict[str, Any],
        store: RouteStateStore,
        notify: Optional[Callable[[str], None]] = None,
        asn_resolver: Optional[CymruASNResolver] = None,
    ):
        self.config = config
        self.store = store
        self.notify = notify
        self.session = requests.Session()
        self.asn_resolver = asn_resolver or CymruASNResolver(
            float(config.get("asn_timeout_seconds", 5))
        )
        self.request_count = 0
        self._cycle_lock = threading.Lock()

    def run_cycle(self) -> bool:
        if not self._cycle_lock.acquire(blocking=False):
            LOG.warning("Misaka Route Watch skipped overlapping cycle")
            return False
        try:
            return self._run_cycle_locked()
        finally:
            self._cycle_lock.release()

    def _run_cycle_locked(self) -> bool:
        started = time.monotonic()
        self.request_count = 0
        raw_attempts: Dict[str, Any] = {}
        try:
            nodes = self.discover_nodes()
            targets = self._targets()
            cache = deepcopy(self.store.data.get("asn_cache", {}))
            org_cache = deepcopy(self.store.data.get("asn_org_cache", {}))
            snapshots: Dict[str, Dict[str, Any]] = {}
            for node in nodes:
                for target in targets:
                    raw = self.measure(node, target)
                    raw_attempts[self.route_key(node, target)] = raw
                    snapshot = self.enrich_and_normalize(
                        raw, node, target, cache, org_cache
                    )
                    key = self.route_key(node, target)
                    snapshots[key] = snapshot
            changes = self._record_success(
                nodes, targets, snapshots, cache, org_cache, time.monotonic() - started
            )
            for message in self._change_messages(changes, targets):
                if self.notify:
                    try:
                        self.notify(message)
                    except Exception:
                        LOG.exception("Misaka Route Watch Telegram notification failed")
            LOG.info(
                "Misaka Route Watch success nodes=%d targets=%d routes=%d baseline=%s changes=%d requests=%d duration=%.3fs",
                len(nodes),
                len(targets),
                len(snapshots),
                self.store.data.get("last_cycle_stats", {}).get("baseline"),
                len(changes),
                self.request_count,
                time.monotonic() - started,
            )
            return True
        except Exception as exc:
            LOG.exception("Misaka Route Watch failed")
            self.store.record_failure(
                "%s: %s" % (type(exc).__name__, exc), raw_attempts
            )
            return False

    def discover_nodes(self) -> List[Dict[str, Any]]:
        directory_url = str(
            self.config.get(
                "directory_url", PING_SX_API + "/probe/lg/misaka"
            )
        )
        directory = self._get_json(directory_url)
        if (
            not isinstance(directory, dict)
            or directory.get("slug") != "misaka"
            or not isinstance(directory.get("probes"), list)
        ):
            raise RouteWatchError("Misaka probe directory schema changed")
        api_base = str(
            self.config.get("catalog_api_base", MISAKA_CATALOG_API)
        ).rstrip("/")
        regions = self._get_json(api_base + "/regions")
        if not isinstance(regions, list):
            raise RouteWatchError("Misaka region catalog schema changed")
        region_map = {
            str(item.get("country_code", "")).upper(): item
            for item in regions
            if isinstance(item, dict)
        }
        focus = [str(value).upper() for value in self.config.get("focus_regions", [])]
        if not focus:
            focus = ["HK", "TW", "JP"]
        selected = []
        for country in focus:
            choices = [
                item
                for item in directory["probes"]
                if isinstance(item, dict)
                and str(item.get("country", "")).upper() == country
                and item.get("ipv4") is True
                and str(item.get("description") or "").strip()
            ]
            if not choices:
                raise RouteWatchError(
                    "Misaka has no labeled IPv4 line probe for %s" % country
                )
            chosen = max(choices, key=self._level_rank)
            region = region_map.get(country)
            if not isinstance(region, dict):
                raise RouteWatchError("Misaka region API is missing %s" % country)
            hostname = str(chosen.get("test_ipv4") or "").strip()
            if not hostname:
                raise RouteWatchError("Misaka probe %s has no Test IPv4" % chosen.get("id"))
            try:
                ipv4 = socket.gethostbyname(hostname)
            except OSError:
                raise RouteWatchError("cannot resolve Misaka Test IPv4 %s" % hostname) from None
            level = str(chosen.get("description")).strip()
            ipv6 = self._matching_speedtest(region, level, ipv4, 6)
            official_ipv4 = self._matching_speedtest(region, level, ipv4, 4)
            if official_ipv4 != ipv4:
                raise RouteWatchError(
                    "probe %s does not match Misaka region speedtest data" % chosen.get("id")
                )
            selected.append(
                {
                    "id": str(chosen["id"]),
                    "probe_id": int(chosen["id"]),
                    "slug": str(chosen.get("slug") or ""),
                    "country_code": country,
                    "location": str(chosen.get("location") or region.get("name") or country),
                    "level": level,
                    "test_ipv4_hostname": hostname,
                    "test_ipv4": ipv4,
                    "test_ipv6": ipv6,
                    "ipv6_discovered_only": True,
                    "node_type": "line",
                    "classification_evidence": "labeled probe routing profile matched region speedtests",
                    "region_id": str(region.get("id") or ""),
                    "link": str(chosen.get("link") or ""),
                }
            )
        return selected

    @staticmethod
    def _level_rank(probe: Dict[str, Any]) -> Tuple[int, str]:
        level = str(probe.get("description") or "").strip().casefold()
        score = {
            "premium plus": 400,
            "premium": 300,
            "pro": 200,
            "standard": 100,
            "economy": 50,
        }.get(level, 10)
        return score, level

    @staticmethod
    def _matching_speedtest(
        region: Dict[str, Any], level: str, ipv4: str, version: int
    ) -> Optional[str]:
        speedtests = region.get("speedtests")
        if not isinstance(speedtests, list):
            return None
        pending_match = False
        for item in speedtests:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "")
            address = _url_address(str(item.get("url") or ""))
            if label == "%s (IPv4)" % level:
                pending_match = address == ipv4
                if version == 4 and pending_match:
                    return address
            elif label == "%s (IPv6)" % level and pending_match:
                return address if version == 6 else ipv4
            elif label.endswith("(IPv4)"):
                pending_match = False
        return None

    def _targets(self) -> List[Dict[str, Any]]:
        values = self.config.get("targets")
        if not isinstance(values, list) or not values:
            raise RouteWatchError("Route Watch has no mainland targets")
        targets = []
        seen = set()
        for value in values:
            if not isinstance(value, dict):
                raise RouteWatchError("Route Watch target must be an object")
            target_id = str(value.get("id") or "").strip()
            operator = str(value.get("operator") or "").strip()
            address = str(value.get("ip") or "").strip()
            if not target_id or target_id in seen or not operator:
                raise RouteWatchError("Route Watch target has an invalid ID/operator")
            seen.add(target_id)
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                raise RouteWatchError("Route Watch target %s has invalid IP" % target_id) from None
            if parsed.version != 4 or not parsed.is_global:
                raise RouteWatchError("Route Watch target %s is not public IPv4" % target_id)
            target = dict(value)
            target.update({"id": target_id, "operator": operator, "ip": address})
            targets.append(target)
        return targets

    def measure(
        self, node: Dict[str, Any], target: Dict[str, Any]
    ) -> Dict[str, Any]:
        self.request_count += 1
        timeout = float(self.config.get("measurement_timeout_seconds", 120))
        headers = [
            "User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            "Cache-Control: no-cache",
            "Pragma: no-cache",
        ]
        try:
            ws = websocket.create_connection(
                str(self.config.get("websocket_url", PING_SX_WS)),
                origin=str(self.config.get("origin", PING_SX_ORIGIN)),
                header=headers,
                timeout=min(timeout, 20),
            )
        except Exception as exc:
            raise RouteWatchError(
                "Misaka LG WebSocket failed (%s)" % type(exc).__name__
            ) from None
        report: Optional[Dict[str, Any]] = None
        task_id: Optional[str] = None
        hops: Dict[int, Dict[str, Any]] = {}
        complete = False
        deadline = time.monotonic() + timeout
        try:
            handshake = ws.recv()
            if not isinstance(handshake, str) or not handshake.startswith("./"):
                raise RouteWatchError("Misaka LG session handshake missing")
            request = {
                "type": "lg",
                "tool": "mtr",
                "provider": "misaka",
                "options": {
                    "target": target["ip"],
                    "version": "ipv4",
                    "port": 443,
                    "probe": str(node["probe_id"]),
                    "remote_dns": True,
                    "recursive_bit": True,
                    "query_type": "A",
                    "nameserver": "",
                    "nameserver_port": 53,
                    "server_type": "udp",
                },
            }
            ws.send(json.dumps(request, separators=(",", ":")))
            while time.monotonic() < deadline:
                ws.settimeout(max(0.5, min(10.0, deadline - time.monotonic())))
                try:
                    message = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not isinstance(message, str):
                    continue
                if message.startswith("{"):
                    value = json.loads(message)
                    if not value.get("created") or not isinstance(value.get("tasks"), list):
                        raise RouteWatchError(
                            "Misaka LG rejected route request: %s"
                            % str(value.get("message") or "unknown error")
                        )
                    tasks = value["tasks"]
                    if len(tasks) != 1 or str(tasks[0].get("probe_id")) != str(node["probe_id"]):
                        raise RouteWatchError("Misaka LG returned the wrong source probe")
                    report = value
                    task_id = str(tasks[0]["id"])
                    continue
                if task_id and message == "M/%s/C" % task_id:
                    complete = True
                    break
                if task_id and message.startswith("M/%s/" % task_id):
                    self._parse_mtr_frame(message, task_id, hops)
        finally:
            ws.close()
        if not report or not complete:
            raise RouteWatchError("Misaka LG MTR did not complete")
        raw_hops = [hops[index] for index in sorted(hops)]
        destination_reached = any(hop.get("ip") == target["ip"] for hop in raw_hops)
        if not destination_reached:
            raise RouteWatchError(
                "Misaka LG did not reach destination %s" % target["ip"]
            )
        return {
            "report_id": str(report.get("id")),
            "created_at": report.get("created_at"),
            "source_probe_id": str(node["probe_id"]),
            "destination": target["ip"],
            "raw_hops": raw_hops,
            "destination_reached": True,
        }

    @staticmethod
    def _parse_mtr_frame(
        message: str, task_id: str, hops: Dict[int, Dict[str, Any]]
    ) -> None:
        body = message[len("M/%s/" % task_id) :]
        parts = body.split("/", 3)
        if len(parts) < 2 or not parts[0].isdigit():
            return
        position = int(parts[0])
        hop = hops.setdefault(
            position,
            {"hop": position, "ip": None, "ptr": None, "location": None, "samples": []},
        )
        action = parts[1]
        if action == "X":
            hop["samples"].append(None)
        elif action == "P" and len(parts) >= 4:
            try:
                hop["samples"].append(round(float(parts[2]) / 1000.0, 3))
            except ValueError:
                pass
        elif action == "H" and len(parts) >= 3:
            address = parts[2]
            if len(parts) == 3:
                hop["ip"] = address
            elif hop.get("ip") == address:
                key, separator, value = parts[3].partition("=")
                if separator and key == "ptr":
                    hop["ptr"] = value
                elif separator and key == "loc":
                    hop["location"] = value

    def enrich_and_normalize(
        self,
        raw: Dict[str, Any],
        node: Dict[str, Any],
        target: Dict[str, Any],
        cache: Dict[str, Any],
        org_cache: Dict[str, Any],
    ) -> Dict[str, Any]:
        enriched = []
        public = 0
        enriched_count = 0
        for raw_hop in raw["raw_hops"]:
            hop = dict(raw_hop)
            samples = [value for value in hop.get("samples", []) if isinstance(value, (int, float))]
            hop["rtt_avg_ms"] = round(sum(samples) / len(samples), 2) if samples else None
            address = hop.get("ip")
            if address and _public_ipv4(address):
                public += 1
                value = self.asn_resolver.enrich(address, cache, org_cache)
                if value:
                    hop.update(value)
                    enriched_count += 1
            enriched.append(hop)
        minimum_ratio = float(self.config.get("minimum_asn_enrichment_ratio", 0.6))
        if not public or enriched_count / public < minimum_ratio:
            raise RouteWatchError(
                "ASN enrichment incomplete (%d/%d hops)" % (enriched_count, public)
            )
        path = normalized_asn_path(enriched)
        if len(path) < 2:
            raise RouteWatchError("normalized ASN path is too short")
        destination_hop = next(
            (item for item in reversed(enriched) if item.get("ip") == target["ip"]),
            None,
        )
        expected_asn = str(target.get("expected_asn") or "")
        expected_prefix = str(target.get("expected_prefix") or "")
        if not destination_hop:
            raise RouteWatchError("destination hop disappeared during normalization")
        if expected_asn and destination_hop.get("asn") != expected_asn:
            raise RouteWatchError(
                "target %s ASN changed: expected %s, got %s"
                % (target["id"], expected_asn, destination_hop.get("asn"))
            )
        if expected_prefix and destination_hop.get("prefix") != expected_prefix:
            raise RouteWatchError(
                "target %s prefix changed: expected %s, got %s"
                % (target["id"], expected_prefix, destination_hop.get("prefix"))
            )
        return {
            "measured_at": utc_now(),
            "report_id": raw.get("report_id"),
            "source_probe_id": raw.get("source_probe_id"),
            "destination_reached": True,
            "raw_hops": enriched,
            "normalized_asn_path": path,
            "fingerprint": ">".join(path),
            "features": route_features(path),
            "destination_rtt_ms": destination_hop.get("rtt_avg_ms"),
            "asn_enriched_hops": enriched_count,
            "public_hops": public,
        }

    def _record_success(
        self,
        nodes: List[Dict[str, Any]],
        targets: List[Dict[str, Any]],
        snapshots: Dict[str, Dict[str, Any]],
        cache: Dict[str, Any],
        org_cache: Dict[str, Any],
        duration: float,
    ) -> List[RouteChange]:
        now = utc_now()
        previous = self.store.snapshot()
        updated = deepcopy(previous)
        first_run = not bool(previous.get("initialized"))
        changes = []
        routes = updated.setdefault("routes", {})
        node_map = {str(item["id"]): item for item in nodes}
        target_map = {str(item["id"]): item for item in targets}
        for key, snapshot in snapshots.items():
            node_id, target_id = key.split(":", 1)
            entry = deepcopy(routes.get(key, {}))
            baseline = entry.get("baseline")
            entry.update(
                {
                    "node_id": node_id,
                    "target_id": target_id,
                    "last_success_at": now,
                    "current": snapshot,
                }
            )
            if first_run or not isinstance(baseline, dict):
                entry["baseline"] = snapshot
                entry["candidate"] = None
            elif snapshot["fingerprint"] == baseline.get("fingerprint"):
                entry["candidate"] = None
            else:
                candidate = entry.get("candidate")
                if (
                    isinstance(candidate, dict)
                    and candidate.get("fingerprint") == snapshot["fingerprint"]
                ):
                    count = int(candidate.get("count", 0)) + 1
                    candidate.update(
                        {"count": count, "snapshot": snapshot, "last_seen_at": now}
                    )
                else:
                    candidate = {
                        "fingerprint": snapshot["fingerprint"],
                        "count": 1,
                        "snapshot": snapshot,
                        "first_seen_at": now,
                        "last_seen_at": now,
                    }
                if int(candidate["count"]) >= int(
                    self.config.get("confirmation_count", 2)
                ):
                    changes.append(
                        RouteChange(
                            route_key=key,
                            node=node_map[node_id],
                            target=target_map[target_id],
                            previous=baseline,
                            current=snapshot,
                        )
                    )
                    entry["baseline"] = snapshot
                    entry["candidate"] = None
                    entry["last_change_at"] = now
                    updated["last_change_at"] = now
                else:
                    entry["candidate"] = candidate
            routes[key] = entry
        updated.update(
            {
                "initialized": True,
                "last_check_at": now,
                "last_success_at": now,
                "last_error": None,
                "consecutive_failures": 0,
                "nodes": node_map,
                "targets": target_map,
                "routes": routes,
                "asn_cache": cache,
                "asn_org_cache": org_cache,
                "last_cycle_stats": {
                    "nodes": len(nodes),
                    "targets": len(targets),
                    "routes": len(snapshots),
                    "requests": self.request_count,
                    "duration_seconds": round(duration, 3),
                    "baseline": first_run,
                    "changes": len(changes),
                },
            }
        )
        self.store.data = updated
        self.store.save()
        return changes

    def _change_messages(
        self, changes: List[RouteChange], targets: List[Dict[str, Any]]
    ) -> List[str]:
        if not changes:
            return []
        from .telegram import format_route_changes

        groups: Dict[Tuple[str, str], List[RouteChange]] = {}
        for change in changes:
            groups.setdefault(
                (str(change.node["id"]), str(change.target["operator"])), []
            ).append(change)
        messages = []
        for (_node_id, operator), group in groups.items():
            total = sum(1 for target in targets if target["operator"] == operator)
            messages.append(format_route_changes(group, total))
        return messages

    @staticmethod
    def route_key(node: Dict[str, Any], target: Dict[str, Any]) -> str:
        return "%s:%s" % (node["id"], target["id"])

    def _get_json(self, url: str) -> Any:
        self.request_count += 1
        try:
            response = self.session.get(
                url, timeout=float(self.config.get("directory_timeout_seconds", 20))
            )
        except requests.RequestException as exc:
            raise RouteWatchError(
                "Route Watch request failed (%s)" % type(exc).__name__
            ) from None
        if response.status_code >= 400:
            raise RouteWatchError("Route Watch HTTP %d for %s" % (response.status_code, url))
        try:
            return response.json()
        except ValueError:
            raise RouteWatchError("Route Watch returned invalid JSON for %s" % url) from None


class RouteWatchScheduler:
    def __init__(self, watcher: MisakaRouteWatcher, config: Dict[str, Any]):
        self.watcher = watcher
        self.config = config
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self.run_forever,
            name="misaka-route-watch",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def run_forever(self) -> None:
        delay = 0.0
        while not self._stop.wait(delay):
            ok = self.watcher.run_cycle()
            interval = max(60, int(self.config.get("interval_seconds", 1800)))
            failures = int(self.watcher.store.data.get("consecutive_failures", 0))
            if ok:
                delay = float(interval)
            else:
                maximum = max(interval, int(self.config.get("max_backoff_seconds", 7200)))
                delay = float(min(maximum, interval * (2 ** min(max(0, failures - 1), 4))))


def normalized_asn_path(hops: Iterable[Dict[str, Any]]) -> List[str]:
    result = []
    for hop in hops:
        asn = hop.get("asn")
        if not isinstance(asn, str) or not asn.startswith("AS"):
            continue
        if not result or result[-1] != asn:
            result.append(asn)
    return result


def route_features(path: Iterable[str]) -> List[str]:
    values = set(path)
    mapping = [
        ("AS4809", "CTG/CN2 path feature"),
        ("AS58453", "CMI"),
        ("AS10099", "CUG"),
        ("AS4134", "China Telecom"),
        ("AS4837", "China Unicom"),
        ("AS4808", "China Unicom"),
        ("AS9808", "China Mobile"),
        ("AS56040", "China Mobile"),
        ("AS2914", "NTT"),
        ("AS3491", "PCCW"),
        ("AS174", "Cogent"),
        ("AS1299", "Arelion"),
        ("AS3257", "GTT"),
        ("AS917", "Misaka"),
    ]
    result = []
    for asn, feature in mapping:
        if asn in values and feature not in result:
            result.append(feature)
    return result


def _url_address(value: str) -> Optional[str]:
    try:
        return urlparse(value).hostname
    except ValueError:
        return None


def _public_ipv4(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.version == 4 and address.is_global
