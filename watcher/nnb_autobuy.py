"""One-shot NewNB (nnb.ltd) auto-buyer.

Polls the public node list and, as soon as a node matching the keywords is
listed as orderable with the wanted network (CM = China Mobile entry by
default), orders the cheapest configuration of it:

* if the wallet balance covers the price, ``POST /user/services`` pays from
  the balance and the VM is created on the spot;
* otherwise ``POST /user/services/checkout`` creates a payment order and the
  link is sent to Telegram. NewNB only creates the VM after that payment, so
  a checkout does not hold stock.

It buys at most once: after either request succeeds it writes a marker file
and exits. Login is a plain JSON POST (no captcha) and the bearer token lasts
15 days, so the bot signs in with the stored account whenever it has to.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .autobuy_status import DEFAULT_DIR as STATUS_DIR, Control, Heartbeat, Switch
from .config import telegram_credentials
from .providers.nnb import (
    API_BASE,
    SITE,
    default_spec,
    node_available,
    node_items,
    starting_price_raw,
    yuan,
)
from .telegram import TelegramClient, TelegramError


LOG = logging.getLogger("nnb_autobuy")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
RELOGIN_MIN_SECONDS = 60
MAX_RETRY_SECONDS = 60
BALANCE_ERROR_HINTS = ("balance", "insufficient", "余额", "不足")
KYC_ERROR_HINTS = ("kyc", "实名", "认证")
CM_LABEL = re.compile(r"\bCM\b|CMCC|移动", re.I)


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, errors: Optional[List[Any]] = None):
        details = "; ".join(
            "%s: %s" % (e.get("field"), e.get("msg"))
            for e in errors or []
            if isinstance(e, dict)
        )
        super().__init__("%s %s%s" % (status, message, " (%s)" % details if details else ""))
        self.status = status
        self.message = message
        self.errors = errors or []


class NnbClient:
    def __init__(self, token: str = "", timeout: int = 15):
        self.token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Accept-Language": "zh-CN",
                "Origin": SITE,
                "Referer": SITE + "/dashboard/create",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        auth: bool = True,
        timeout: Optional[int] = None,
    ) -> Any:
        headers = {"Authorization": "Bearer " + self.token} if auth and self.token else {}
        try:
            response = self.session.request(
                method,
                API_BASE + path,
                json=body,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(0, "unreachable: %s" % type(exc).__name__) from None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise ApiError(response.status_code, "non-JSON reply: %s" % response.text[:200])
        code = payload.get("code", response.status_code)
        if response.status_code >= 400 or (isinstance(code, int) and code >= 400):
            raise ApiError(
                response.status_code if response.status_code >= 400 else code,
                str(payload.get("msg") or "HTTP %s" % response.status_code),
                payload.get("errors") if isinstance(payload.get("errors"), list) else None,
            )
        return payload

    def login(self, username: str, password: str) -> Dict[str, Any]:
        data = self.request(
            "POST",
            "/user/login",
            {"username": username, "password": password, "keep_login": True},
            auth=False,
        ).get("data") or {}
        if not data.get("token"):
            raise ApiError(200, "login reply has no token")
        self.token = str(data["token"])
        return data

    def nodes(self) -> List[Dict[str, Any]]:
        # Public endpoint; no token so polling never touches the session.
        return node_items(self.request("GET", "/nodes", auth=False))

    def templates(self, node_id: Any) -> Dict[str, Any]:
        return self.request("GET", "/nodes/%s/templates" % node_id, auth=False, timeout=30).get(
            "data"
        ) or {}

    def wallet(self) -> Dict[str, Any]:
        return self.request("GET", "/user/wallet").get("data") or {}

    def kyc(self) -> Dict[str, Any]:
        return self.request("GET", "/user/kyc").get("data") or {}

    def ssh_keys(self) -> List[str]:
        items = (self.request("GET", "/user/ssh-keys").get("data") or {}).get("items") or []
        return [
            str(item["public_key"]).strip()
            for item in items
            if isinstance(item, dict) and item.get("public_key")
        ]

    def create_service(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/user/services", body, timeout=60).get("data") or {}

    def checkout(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/user/services/checkout", body, timeout=60).get("data") or {}


def node_text(node: Dict[str, Any]) -> str:
    group = node.get("group") if isinstance(node.get("group"), dict) else {}
    nics = list(node.get("primary_nics") or []) + list(node.get("extra_nics") or [])
    parts = [
        node.get("name"),
        node.get("description"),
        node.get("region"),
        node.get("pve_node"),
        group.get("name"),
    ] + [nic.get("label") for nic in nics if isinstance(nic, dict)]
    return " ".join(str(part) for part in parts if part)


def matching_nodes(
    nodes: Sequence[Dict[str, Any]], keywords: Sequence[str], exclude: Sequence[int] = ()
) -> List[Dict[str, Any]]:
    wanted = [k.casefold() for k in keywords if k]
    result = []
    for node in nodes:
        if node.get("id") in exclude:
            continue
        text = node_text(node).casefold()
        if any(k in text for k in wanted):
            result.append(node)
    return result


def is_cm(nic: Dict[str, Any]) -> bool:
    return bool(nic.get("cmcc_nat")) or bool(CM_LABEL.search(str(nic.get("label") or "")))


def choose_nics(
    node: Dict[str, Any], network: str
) -> Optional[Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]]:
    """(primary NIC, extra NIC) giving the wanted network, or None if the node lacks it.

    CM may be sold either as a primary NIC (like SH #1's 沪日专线-CM) or as
    an add-on extra NIC on top of a non-CM primary (like Tokyo's IX + 移动入口).
    """
    primaries = [n for n in node.get("primary_nics") or [] if isinstance(n, dict)]
    extras = [
        n for n in node.get("extra_nics") or [] if isinstance(n, dict) and n.get("traffic")
    ]
    if network == "cm":
        for nic in primaries:
            if is_cm(nic):
                return nic, None
        for extra in extras:
            if is_cm(extra):
                return (primaries[0] if primaries else None), extra
        return None
    for nic in primaries:
        if not is_cm(nic):
            return nic, None
    if not primaries:
        return None, None
    return None


def build_order(
    node: Dict[str, Any],
    primary: Optional[Dict[str, Any]],
    extra: Optional[Dict[str, Any]],
    regions: Sequence[str],
    months: int,
) -> Tuple[Dict[str, Any], int]:
    """Order body for the cheapest monthly configuration, and its price (raw units)."""
    spec = default_spec(node, primary)
    body: Dict[str, Any] = {
        "billing_mode": "monthly",
        "node_id": node["id"],
        "cpu": spec["cpu"],
        "memory_gb": spec["memory_gb"],
        "disk_gb": spec["disk_gb"],
        "bandwidth_mbps": spec["bandwidth_mbps"],
        "months": months,
        "auto_renew": False,
    }
    if spec.get("traffic_gb") is not None:
        body["traffic_gb"] = spec["traffic_gb"]
    if primary is not None:
        body["primary_nic"] = primary["id"]
    whitelist = None
    if extra is not None:
        body["extra_nic"] = extra["id"]
        if extra.get("max_bandwidth_mbps"):
            body["extra_nic_bandwidth_mbps"] = int(extra.get("min_bandwidth_mbps") or 0)
        body["extra_nic_traffic_gb"] = int(extra["traffic"]["min_gb"])
        if extra.get("whitelist"):
            whitelist = extra
    if whitelist is None and primary is not None and primary.get("whitelist"):
        whitelist = primary
    if whitelist is not None:
        body["regional_codes"] = list(regions)[: int(whitelist.get("max_regions") or 3)]
    else:
        body["regional_codes"] = []
    price = starting_price_raw(node, primary, spec, extra) * months
    return body, price


def pick_template(templates: Sequence[Dict[str, Any]], preferred: str) -> Optional[int]:
    wanted = preferred.casefold()
    for template in templates:
        text = "%s %s" % (template.get("name"), template.get("group"))
        if wanted and wanted in text.casefold():
            return template.get("id")
    return templates[0].get("id") if templates else None


def describe(node: Dict[str, Any]) -> str:
    nics = ", ".join(
        "%s%s" % (n.get("label"), " [CM]" if is_cm(n) else "")
        for n in list(node.get("primary_nics") or []) + list(node.get("extra_nics") or [])
        if isinstance(n, dict)
    )
    return "#%s %s orderable=%s status=%s monthly=%s nics=%s" % (
        node.get("id"),
        node.get("name"),
        node.get("orderable"),
        node.get("status"),
        node.get("monthly_available"),
        nics or "-",
    )


class AutoBuyer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.token_path = Path(args.token_file)
        self.marker_path = Path(args.marker_file)
        self.username = os.environ.get("NNB_USERNAME", "").strip()
        self.password = os.environ.get("NNB_PASSWORD", "")
        self.client = NnbClient(self.read_token())
        self.telegram: Optional[TelegramClient] = None
        credentials = telegram_credentials()
        if credentials["token"] and credentials["chat_id"]:
            self.telegram = TelegramClient(credentials["token"], credentials["chat_id"])
        self.stopping = False
        self.last_login_attempt = float("-inf")
        self.failures = 0
        self.next_attempt = 0.0
        self.last_failure_notice = float("-inf")
        self.noticed: set = set()
        self.account_keys: List[str] = []
        self.heartbeat: Optional[Heartbeat] = None
        if not args.dry_run:
            self.heartbeat = Heartbeat(
                args.status_dir,
                "nnb",
                "NewNB 沪港",
                "关键词 %s，%s，最低配置，≤¥%g/月"
                % (
                    " / ".join(args.keyword),
                    "移动入口(CM)" if args.network == "cm" else "非移动入口(IX)",
                    args.max_price,
                ),
                args.interval,
            )

        # Pause/resume from Telegram; not used by --dry-run.
        self.default_interval = args.interval
        self.switch: Optional[Switch] = None
        if self.heartbeat is not None:
            self.switch = Switch(Control(args.status_dir, "nnb"), self.marker_path, self.beat)

    def beat(self, **values: Any) -> None:
        if self.heartbeat is not None:
            self.heartbeat.update(**values)

    def mark_bought(self) -> None:
        if self.switch is not None:
            self.switch.bought(marker_result(self.marker_path))

    def reset_attempts(self) -> None:
        self.failures = 0
        self.next_attempt = 0.0
        self.last_failure_notice = float("-inf")
        self.noticed = set()

    def notify(self, text: str) -> None:
        LOG.info("notify: %s", text.replace("\n", " | "))
        if self.telegram is None:
            return
        try:
            self.telegram.send(text)
        except TelegramError as exc:
            LOG.warning("telegram send failed: %s", exc)

    def notify_once(self, key: str, text: str) -> None:
        if key not in self.noticed:
            self.noticed.add(key)
            self.notify(text)

    def read_token(self) -> str:
        try:
            return self.token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def write_token(self, token: str) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")

    def login(self) -> bool:
        if not (self.username and self.password):
            LOG.error("NNB_USERNAME/NNB_PASSWORD are not set")
            return False
        if time.monotonic() - self.last_login_attempt < RELOGIN_MIN_SECONDS:
            return False
        self.last_login_attempt = time.monotonic()
        try:
            self.client.login(self.username, self.password)
        except ApiError as exc:
            LOG.warning("login failed: %s", exc)
            self.notify("⚠️ <b>NewNB 自动登录失败</b>\n%s" % html.escape(str(exc)))
            return False
        self.write_token(self.client.token)
        LOG.info("signed in as %s", self.username)
        return True

    def authed(self, call: Any, *args: Any) -> Any:
        """Run an authenticated call, signing in again once if the token is rejected."""
        if not self.client.token:
            self.login()
        try:
            return call(*args)
        except ApiError as exc:
            if exc.status != 401 or not self.login():
                raise
        return call(*args)

    def announce(self) -> bool:
        try:
            wallet = self.authed(self.client.wallet)
            kyc = self.authed(self.client.kyc)
            self.account_keys = self.authed(self.client.ssh_keys)
        except ApiError as exc:
            LOG.error("account check failed: %s", exc)
            self.notify("⚠️ <b>NewNB 自动购买：账号检查失败</b>\n%s" % html.escape(str(exc)))
            return False
        balance = (wallet.get("balance") or {}).get("yuan", "?")
        kyc_status = kyc.get("status") or "?"
        warnings = []
        if not self.ssh_keys():
            warnings.append("❗ 没有 SSH 公钥，下单会被拒（站点要求必填）")
        if self.args.network == "cm" and kyc.get("enabled") and kyc_status != "verified":
            warnings.append("❗ 未完成 KYC（%s），移动入口下单会被拒，请先实名认证" % kyc_status)
        try:
            balance_raw = int((wallet.get("balance") or {}).get("raw") or 0)
        except (TypeError, ValueError):
            balance_raw = 0
        if balance_raw < self.args.max_price * 10000:
            warnings.append(
                "💡 余额不足 ¥%g 时只能创建支付订单（付款后才开通，不占库存），"
                "建议提前充值以便直接扣款开通" % self.args.max_price
            )
        self.notify(
            "🟢 <b>NewNB 自动购买已就绪</b>\n账号：%s\n余额：¥%s\nKYC：%s\n"
            "SSH 公钥：%s 个\n目标：关键词 %s，%s，最高 ¥%g/月\n轮询：每 %g 秒%s"
            % (
                html.escape(self.username),
                html.escape(str(balance)),
                html.escape(kyc_status),
                len(self.ssh_keys()),
                html.escape(" / ".join(self.args.keyword)),
                "移动入口(CM)" if self.args.network == "cm" else "非移动入口(IX)",
                self.args.max_price,
                self.args.interval,
                "".join("\n" + html.escape(w) for w in warnings),
            )
        )
        return True

    def write_marker(self, data: Dict[str, Any]) -> None:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        data["at"] = datetime.now(timezone.utc).isoformat()
        self.marker_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def offer(self, node: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], int]]:
        """Order body and price for a matching node, or None if it can't/shouldn't be bought."""
        nics = choose_nics(node, self.args.network)
        if nics is None:
            self.notify_once(
                "nonic-%s" % node.get("id"),
                "👀 <b>NewNB 发现匹配节点，但没有%s网络，不下单</b>\n%s"
                % (
                    "移动入口" if self.args.network == "cm" else "非移动",
                    html.escape(describe(node)),
                ),
            )
            return None
        body, price = build_order(node, nics[0], nics[1], self.args.region, self.args.months)
        if price > self.args.max_price * 10000 * self.args.months:
            self.notify_once(
                "price-%s-%s" % (node.get("id"), price),
                "👀 <b>NewNB 发现匹配节点，但价格 ¥%s 超过上限 ¥%g，不下单</b>\n%s"
                % (yuan(price), self.args.max_price, html.escape(describe(node))),
            )
            return None
        return body, price

    def ssh_keys(self) -> List[str]:
        """Public keys for the VM: the key file plus keys saved on the account."""
        keys: List[str] = []
        try:
            lines = Path(self.args.ssh_key_file).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        for key in [line.strip() for line in lines] + self.account_keys:
            if key and not key.startswith("#") and key not in keys:
                keys.append(key)
        return keys

    def attach_template(self, node: Dict[str, Any], body: Dict[str, Any]) -> None:
        provisionable = bool(node.get("provisionable"))
        if provisionable:
            data = self.client.templates(node["id"])
            provisionable = bool(data.get("provisionable", True))
            if provisionable:
                template_id = pick_template(data.get("items") or [], self.args.template)
                if template_id is None:
                    raise ApiError(0, "node has no OS template")
                body["template_id"] = template_id
                # The site rejects an order without at least one public key.
                keys = self.ssh_keys()
                if not keys:
                    raise ApiError(0, "no SSH public key in %s" % self.args.ssh_key_file)
                body["ssh_keys"] = "\n".join(keys)

    def try_buy(self, node: Dict[str, Any], body: Dict[str, Any], price: int) -> bool:
        """Attempt the purchase. Returns True once an order exists (stop polling)."""
        label = "%s (#%s)" % (node.get("name"), node.get("id"))
        try:
            self.attach_template(node, body)
            balance = int((self.authed(self.client.wallet).get("balance") or {}).get("raw") or 0)
        except ApiError as exc:
            self.record_failure(label, exc)
            return False
        use_balance = balance >= price
        LOG.info(
            "ordering %s for ¥%s (balance ¥%s, %s): %s",
            label,
            yuan(price),
            yuan(balance),
            "balance" if use_balance else "checkout",
            body,
        )
        if self.args.dry_run:
            self.notify(
                "🧪 dry-run：会%s <code>%s</code>\n价格 ¥%s，余额 ¥%s"
                % (
                    "直接扣余额开通" if use_balance else "创建支付订单",
                    html.escape(json.dumps(body, ensure_ascii=False)),
                    yuan(price),
                    yuan(balance),
                )
            )
            return True
        if use_balance:
            try:
                service = self.authed(self.client.create_service, body)
            except ApiError as exc:
                if not any(h in exc.message.lower() for h in BALANCE_ERROR_HINTS):
                    self.record_failure(label, exc)
                    return False
                LOG.warning("balance order rejected (%s); falling back to checkout", exc)
            else:
                self.write_marker({"service": service, "request": body, "paid": True})
                self.mark_bought()
                self.notify(
                    "✅ <b>NewNB 抢购成功，已从余额扣款开通</b>\n节点：%s\n价格：¥%s\n%s/dashboard/services/%s"
                    % (html.escape(label), yuan(price), SITE, html.escape(str(service.get("id"))))
                )
                return True
        checkout_body = dict(body, method=self.args.method, use_balance=balance > 0)
        try:
            result = self.authed(self.client.checkout, checkout_body)
        except ApiError as exc:
            self.record_failure(label, exc)
            return False
        url = str(result.get("checkout_url") or "")
        self.write_marker({"checkout": result, "request": checkout_body, "paid": False})
        self.mark_bought()
        self.notify(
            "🧾 <b>NewNB 有货！已创建支付订单，请立刻付款</b>\n节点：%s\n价格：¥%s（余额 ¥%s）\n"
            "%s\n⚠️ 付款后才开通，不占库存，售完会失败，越快越好。"
            % (html.escape(label), yuan(price), yuan(balance), html.escape(url or json.dumps(result)))
        )
        return True

    def record_failure(self, label: str, exc: ApiError) -> None:
        self.failures += 1
        delay = min(self.args.interval * 2 ** (self.failures - 1), MAX_RETRY_SECONDS)
        self.next_attempt = time.monotonic() + delay
        LOG.warning("order for %s failed (%s); retry in %ss", label, exc, delay)
        text = str(exc)
        if any(h in text.lower() for h in KYC_ERROR_HINTS):
            self.notify_once(
                "kyc",
                "🪪 <b>NewNB 有货但因 KYC 被拒</b>\n%s\n请立刻去 %s/dashboard/kyc 完成实名，程序继续重试"
                % (html.escape(text), SITE),
            )
            return
        if time.monotonic() - self.last_failure_notice >= 60:
            self.last_failure_notice = time.monotonic()
            self.notify(
                "❌ <b>NewNB 有货但下单失败</b>，%ss 后重试\n%s\n%s"
                % (int(delay), html.escape(label), html.escape(text))
            )

    def run(self) -> int:
        if self.switch is None:
            if self.marker_path.exists():
                LOG.error("marker %s exists; already bought once", self.marker_path)
                return 0
        else:
            # A past purchase keeps the buyer paused until resumed from Telegram.
            self.switch.startup(marker_result(self.marker_path))
        last_status: Dict[Any, str] = {}
        announced = False
        while not self.stopping:
            if self.switch is not None:
                if self.switch.hold():
                    continue
                self.args.interval = self.switch.interval(self.default_interval)
                if self.switch.rearm():
                    LOG.info("resumed after a purchase; the next match will be bought again")
                    self.reset_attempts()
                    announced = False
            if not announced:
                # Only once not paused, so a paused buyer makes no requests at all.
                self.beat(state="starting")
                if not self.announce() and not self.args.dry_run:
                    LOG.warning("account check failed; polling anyway, login retried on demand")
                announced = True
            try:
                nodes = self.client.nodes()
            except Exception as exc:  # ApiError or ParseError from a bad reply
                LOG.warning("node list fetch failed: %s", exc)
                self.beat(state="watching", detail="节点列表获取失败：%s" % exc)
                time.sleep(self.args.interval)
                continue
            targets = matching_nodes(nodes, self.args.keyword, self.args.exclude_node)
            self.beat(
                state="watching",
                detail="；".join(describe(node) for node in targets)
                or "目标节点还没上架（站点共 %d 个节点）" % len(nodes),
            )
            for node in targets:
                status = describe(node)
                if last_status.get(node["id"]) != status:
                    LOG.info("node status: %s", status)
                    last_status[node["id"]] = status
                if not node_available(node):
                    continue
                offer = self.offer(node)
                if offer is None or time.monotonic() < self.next_attempt:
                    continue
                LOG.info("TARGET ORDERABLE: %s", status)
                self.beat(state="ordering", detail=status)
                if self.try_buy(node, *offer):
                    if self.switch is None:
                        return 0
                    break  # now paused; the next loop holds
            time.sleep(self.args.interval)
        self.beat(state="stopped")
        return 0


def marker_result(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "已下单（详情见 %s）" % path
    if data.get("paid"):
        service = data.get("service") or {}
        return "已扣余额开通，服务 #%s（%s/dashboard/services/%s）" % (
            service.get("id"),
            SITE,
            service.get("id"),
        )
    checkout = data.get("checkout") or {}
    return "已创建支付订单 %s，等待付款" % (checkout.get("order_no") or "")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--keyword",
        action="append",
        default=None,
        help="node name/group/description/NIC keyword, repeatable (default: 港, HK, Hong Kong)",
    )
    parser.add_argument(
        "--exclude-node", action="append", type=int, default=[], help="node id to ignore"
    )
    parser.add_argument("--network", choices=("cm", "ix"), default="cm")
    parser.add_argument(
        "--max-price", type=float, default=30.0, help="highest monthly price in CNY to buy at"
    )
    parser.add_argument("--months", type=int, default=1, choices=(1, 3, 6, 12))
    parser.add_argument(
        "--region",
        action="append",
        default=None,
        help="whitelist region code for CM NICs, repeatable (default: 100000 不确定地区)",
    )
    parser.add_argument("--template", default="Debian", help="preferred OS template name")
    parser.add_argument(
        "--ssh-key-file",
        default="data/nnb_ssh_keys.pub",
        help="public keys for the VM, one per line (account-saved keys are added too)",
    )
    parser.add_argument("--method", default="aggpay", help="payment method for checkout orders")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--token-file", default="data/nnb_token.txt")
    parser.add_argument("--marker-file", default="data/nnb_autobuy_done.json")
    parser.add_argument("--status-dir", default=STATUS_DIR, help="heartbeat dir read by the bot")
    parser.add_argument("--dry-run", action="store_true", help="never create an order")
    args = parser.parse_args(argv)
    args.keyword = args.keyword or ["港", "HK", "Hong Kong", "HongKong"]
    args.region = args.region or ["100000"]
    args.interval = max(args.interval, 2.0)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    buyer = AutoBuyer(args)

    def stop(*_: Any) -> None:
        buyer.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return buyer.run()


if __name__ == "__main__":
    sys.exit(main())
