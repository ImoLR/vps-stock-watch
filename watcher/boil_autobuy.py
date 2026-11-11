"""BOILCLOUD (cloud.boil.network) auto-buyer, driven by the BOIL monitors.

Target (user, 2026-11-11): one SeedNet (遠傳) annual plan, an independent
dynamic IP VPS (not NAT, not static, not a blind box), at most $300 due
today. BOIL sells such holiday plans around Christmas, named after the
zodiac year (2026 马年 HiNet; next one probably 羊年 SeedNet).

Like the FAC buyer it does not watch the site by itself. The Telegram relay
(a cloud.boil.network link posted on BOIL's channel or by BOIL's own
accounts in the group) and the ``boilcloud_sale`` provider (festive-offer
lists products) drop trigger files (``fac_trigger``); each trigger arms its
links for a few hours, and armed links are checked one page at a time.

Hidden links are never ordered (user, 2026-11-08 / 2026-11-11). A link is
public once it was posted on Telegram, or once its group shows up in the
shop's category menu. Until then the buyer only re-checks that menu on a
public page, logged out; it never opens the hidden page while logged in.

The shop is plain WHMCS with no captcha, so this is plain HTTP (requests),
no browser. Order: cart.php?a=add&pid=N -> confproduct (billingcycle =
annually, default options, no paid upgrades) -> checkout page (today's total
and balance) -> submit with "apply credit" (the default). A short balance
leaves an unpaid invoice: Telegram sends the pay link at once, the buyer
tries to apply the balance, then reports. It stops after one order (marker
file; resume from Telegram /autobuy).
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import logging
import os
import re
import secrets
import signal
import string
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from .autobuy_status import Control, Heartbeat, Switch
from .config import load_config, telegram_credentials
from .fac_trigger import take_triggers
from .providers.base import is_challenge_page
from .providers.boilcloud import BoilcloudProvider
from .telegram import TelegramClient, TelegramError


LOG = logging.getLogger("boil_autobuy")
NAME = "boil"
STATUS_DIR = "data/autobuy"
DEFAULTS: Dict[str, Any] = {
    "base_url": "https://cloud.boil.network/",
    "account_file": "data/boil_account.env",
    "cookie_file": "data/boil_cookies.json",
    "marker_file": "data/boil_autobuy_done.json",
    "trigger_dir": "data/autobuy/boil_triggers",
    # Matched against the product name + description (not the group
    # heading: every Taiwan heading lists "HiNet/SeedNet/Kbro").
    "name_include": ["seednet", "seed net", "遠傳", "远传"],
    # Whole words: NAT, static IP, random blind boxes.
    "name_exclude": ["nat", "靜態", "静态", "static", "盲盒", "lucky box", "福袋"],
    "billing_cycle": "annually",
    "max_total": 300.0,
    "hostname": "seednet",
    # Sale groups worth watching without a SeedNet word in the post.
    "sale_groups": ["festive-offer"],
    # A post about SeedNet with a sale word but no store link arms these.
    "default_urls": ["https://cloud.boil.network/store/festive-offer"],
    "sale_words": [
        "年付", "年繳", "年缴", "聖誕", "圣诞", "節日", "节日", "活動", "活动", "上架", "上新",
        "補貨", "补货", "限量", "優惠", "优惠", "黑五", "週年", "周年", "開賣", "开卖", "羊年",
        "festive", "christmas", "annual",
    ],
    # A public page whose category sidebar lists every public group.
    "menu_url": "https://cloud.boil.network/store/hong-kong-shared-bandwidth",
    "arm_minutes": 360,
    "fast_poll_seconds": 20,
    "fast_minutes": 30,
    "slow_poll_seconds": 60,
    # Polls and menu checks keep >=4 s between pages; the few requests of one
    # order (add, configure, checkout page, submit) run closer together: the
    # agreed snipe target only.
    "page_spacing_seconds": 4,
    "order_spacing_seconds": 1.5,
    "login_check_hours": 12,
    "max_order_failures": 3,
    "public_delay_seconds": 4,
    "hidden_poll_seconds": 120,
    "checkout_alert_seconds": 20,
    "checkout_wait_seconds": 90,
    "timeout_seconds": 30,
}
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
OUT_OF_STOCK = re.compile(r"缺貨|缺货|售罄|out of stock", re.I)
PAID = re.compile(r"\b(paid)\b|已付款", re.I)
UNPAID = re.compile(r"\b(unpaid)\b|未付款", re.I)


class OrderResult:
    BOUGHT = "bought"   # an order (invoice) was created
    WAIT = "wait"       # not orderable yet / sold out: keep the link armed
    DROP = "drop"       # never going to match: disarm the link


class BlockedError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Pure helpers (unit tested)

def parse_usd(text: Any) -> Optional[float]:
    """First ``$1,234.56`` amount in ``text``."""
    match = re.search(r"\$\s*(-?\d[\d,]*(?:\.\d+)?)", str(text or ""))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def text_matches(text: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    lowered = " ".join((text or "").split()).casefold()
    if not any(word.casefold() in lowered for word in include):
        return False
    for word in exclude:
        # Whole Latin words only, so "nat" never hits inside a longer word.
        if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(word.casefold()), lowered):
            return False
    return True


def mentions(text: str, words: Iterable[str]) -> bool:
    lowered = (text or "").casefold()
    return any(word.casefold() in lowered for word in words)


def classify_url(url: str, base_url: str) -> Tuple[str, Optional[str]]:
    """("group", slug) | ("product", url) | ("pid", pid) | ("other", None)."""
    parsed = urlparse(url)
    base = urlparse(base_url)
    if (parsed.hostname or "").lower() != (base.hostname or "").lower():
        return "other", None
    query = parse_qs(parsed.query)
    if parsed.path.rstrip("/").endswith("cart.php") and query.get("pid"):
        pid = query["pid"][0]
        return ("pid", pid) if pid.isdigit() else ("other", None)
    route = unquote(query.get("rp", [""])[0]) or unquote(parsed.path)
    parts = [part for part in route.split("/") if part and part != "index.php"]
    if len(parts) >= 2 and parts[0] == "store":
        if len(parts) == 2:
            return "group", parts[1]
        return "product", urljoin(base_url, "store/%s/%s" % (parts[1], parts[2]))
    return "other", None


def trigger_is_public(trigger: Dict[str, Any]) -> bool:
    """Telegram posts are public; the site monitor says whether the menu lists it."""
    return str(trigger.get("source") or "").startswith("tg-") or trigger.get("public") is True


def trigger_targets(trigger: Dict[str, Any], cfg: Dict[str, Any]) -> List[str]:
    """The links worth arming for one trigger (as canonical URLs).

    Product links are always tried (one check shows the name). A group link
    is armed when the post talks about SeedNet, or it is a sale group posted
    on Telegram (the site monitor's trigger carries the cards' text); a
    SeedNet sale post without a store link arms the default sale group.
    Maintenance notices and regular HKT restocks arm nothing.
    """
    base = str(cfg["base_url"])
    text = str(trigger.get("text") or "")
    about_target = mentions(text, cfg["name_include"])
    targets: List[str] = []
    other = False
    for url in trigger.get("urls") or []:
        kind, value = classify_url(str(url), base)
        target = None
        if kind == "pid":
            target = urljoin(base, "cart.php?a=add&pid=%s" % value)
        elif kind == "product":
            target = value
        elif kind == "group" and (
            about_target
            # The site monitor sends the group's card texts: no SeedNet, no arming.
            or (value in cfg["sale_groups"] and trigger.get("source") != "boilcloud_sale")
        ):
            target = urljoin(base, "store/%s" % value)
        elif kind == "other":
            other = True
        if target and target not in targets:
            targets.append(target)
    if not targets and (other or not trigger.get("urls")) and about_target and mentions(text, cfg["sale_words"]):
        targets.extend(url for url in cfg["default_urls"] if url not in targets)
    return targets


def group_listed(page_html: str, slug: str, base_url: str) -> bool:
    """True when the page's category sidebar links ``store/<slug>``."""
    soup = BeautifulSoup(page_html, "lxml")
    for node in soup.select('.cart-sidebar [menuitemname="Categories"] a[href]'):
        kind, value = classify_url(urljoin(base_url, str(node.get("href") or "")), base_url)
        if kind == "group" and value == slug:
            return True
    return False


def public_wait(min_age: float, last_hidden_at: Optional[float], now: float) -> float:
    """Seconds left so a link has been public for ``min_age`` s (see FAC buyer)."""
    if last_hidden_at is None:
        return 0.0
    return max(0.0, min_age - (now - last_hidden_at))


def random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    core = "".join(secrets.choice(alphabet) for _ in range(14))
    return core + secrets.choice("!#%+") + secrets.choice(string.digits)


def conf_form(page_html: str, cycle: str, hostname: str, password: str) -> Tuple[Optional[Dict[str, str]], Dict[str, Any]]:
    """Form data for ``frmConfigureProduct`` with ``cycle`` and default options.

    Returns (data or None, info). ``info`` has the product name, its
    description, the cycles on offer and why it can't be configured.
    Selects keep their selected (else first) option, but never pick an option
    that costs extra when a free one exists; checkboxes (add-ons) stay off.
    """
    soup = BeautifulSoup(page_html, "lxml")
    form = soup.select_one("form#frmConfigureProduct")
    title = soup.select_one(".product-info .product-title")
    info_node = soup.select_one(".product-info")
    info: Dict[str, Any] = {
        "name": " ".join(title.get_text(" ", strip=True).split()) if title else "",
        "description": " ".join(info_node.get_text(" ", strip=True).split()) if info_node else "",
        "cycles": {},
        "why": "",
    }
    if form is None:
        info["why"] = "没有商品配置表单"
        return None, info
    data: Dict[str, str] = {}
    for node in form.select("input[name], select[name], textarea[name]"):
        name = str(node.get("name"))
        if node.name == "select":
            options = [
                (str(opt.get("value") or ""), " ".join(opt.get_text(" ", strip=True).split()))
                for opt in node.select("option")
            ]
            if name == "billingcycle":
                info["cycles"] = dict(options)
                data[name] = cycle
                continue
            chosen = node.select_one("option[selected]")
            value = str(chosen.get("value") or "") if chosen else (options[0][0] if options else "")
            free = [opt for opt in options if "+" not in opt[1]]
            if free and value not in [opt[0] for opt in free]:
                value = free[0][0]
            data[name] = value
            continue
        kind = str(node.get("type") or "text").lower()
        if kind in ("checkbox", "radio"):
            if node.has_attr("checked") and kind == "radio":
                data[name] = str(node.get("value") or "on")
            continue
        data[name] = str(node.get("value") or "")
    if "billingcycle" not in data:
        info["why"] = "页面没有付款周期选项"
        return None, info
    if cycle not in info["cycles"]:
        info["why"] = "没有%s周期（只有 %s）" % (cycle, "、".join(info["cycles"].values()) or "?")
        return None, info
    if "hostname" in data and not data["hostname"]:
        data["hostname"] = hostname
    if "rootpw" in data and not data["rootpw"]:
        data["rootpw"] = password
    for key in ("ns1prefix", "ns2prefix"):
        if key in data and not data[key]:
            data[key] = key[:3]
    token = soup.select_one("input[name=token]")
    if "token" not in data and token is not None:
        data["token"] = str(token.get("value") or "")
    return data, info


def checkout_form(page_html: str, accept_tos: bool = True) -> Tuple[Optional[Dict[str, str]], Dict[str, Any]]:
    """Form data for ``frmCheckout`` as the page sets it (apply credit on),
    plus the TOS box. Info: today's total and the account balance."""
    soup = BeautifulSoup(page_html, "lxml")
    total_node = soup.select_one("#totalDueToday") or soup.select_one("#totalCartPrice")
    text = soup.get_text(" ", strip=True)
    balance = re.search(r"(?:信用餘額為|信用余额为|credit balance of)\s*(\$\s*[\d,]+(?:\.\d+)?)", text, re.I)
    info: Dict[str, Any] = {
        "total": parse_usd(total_node.get_text(" ", strip=True)) if total_node else None,
        "balance": parse_usd(balance.group(1)) if balance else (0.0 if "applycredit" not in page_html else None),
        "why": "",
    }
    form = soup.select_one("form#frmCheckout")
    if form is None:
        info["why"] = "没有结账表单（购物车可能是空的）"
        return None, info
    data: Dict[str, str] = {}
    for node in form.select("input[name], select[name], textarea[name]"):
        name = str(node.get("name"))
        kind = str(node.get("type") or "text").lower()
        if node.name == "select":
            chosen = node.select_one("option[selected]") or node.select_one("option")
            data[name] = str(chosen.get("value") or "") if chosen else ""
        elif kind == "radio":
            if node.has_attr("checked"):
                data[name] = str(node.get("value") or "")
        elif kind == "checkbox":
            if name == "accepttos":
                if accept_tos:
                    data[name] = "on"
            elif node.has_attr("checked"):
                data[name] = str(node.get("value") or "on")
        elif name.startswith("cc") or name in ("loginemail", "loginpassword"):
            continue  # card fields and the "log in as another user" box
        else:
            data.setdefault(name, str(node.get("value") or node.get_text() or ""))
    return data, info


def invoice_id(url: str, page_html: str = "") -> Optional[str]:
    for source in (url, page_html):
        match = re.search(r"viewinvoice\.php\?id=(\d+)", source or "")
        if match:
            return match.group(1)
    return None


def invoice_status(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "lxml")
    node = soup.select_one(".invoice-status span, .invoice-status")
    text = " ".join((node.get_text(" ", strip=True) if node else "").split())
    if UNPAID.search(text):
        return "Unpaid"
    if PAID.search(text):
        return "Paid"
    return text or "?"


def _read_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in path.read_text("utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.strip().split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------

class BoilAutoBuyer:
    def __init__(self, config: Dict[str, Any], args: argparse.Namespace):
        cfg = dict(DEFAULTS)
        cfg.update(config.get("boil_autobuy") or {})
        self.cfg = cfg
        self.args = args
        self.base_url = str(cfg["base_url"])
        self.include = [str(v) for v in cfg["name_include"]]
        self.exclude = [str(v) for v in cfg["name_exclude"]]
        self.cycle = str(cfg["billing_cycle"])
        self.max_total = float(cfg["max_total"])
        self.marker_path = Path(str(cfg["marker_file"]))
        self.trigger_dir = str(cfg["trigger_dir"])
        self.cookie_path = Path(str(cfg["cookie_file"]))
        account_file = Path(str(cfg["account_file"]))
        account = _read_env(account_file) if account_file.exists() else {}
        self.email = account.get("BOIL_EMAIL", "")
        self.password = account.get("BOIL_PASSWORD", "")
        self.timeout = float(cfg["timeout_seconds"])
        self.session = self._new_session()
        self._load_cookies()
        # Menu checks run logged out, so a hidden page is never seen logged in.
        self.public_session = self._new_session()
        self.parser = BoilcloudProvider({"name": "boilcloud", "base_url": self.base_url})
        self.telegram: Optional[TelegramClient] = None
        credentials = telegram_credentials()
        if credentials["token"] and credentials["chat_id"] and not getattr(args, "no_notify", False):
            self.telegram = TelegramClient(credentials["token"], credentials["chat_id"])
        self.armed: Dict[str, Dict[str, Any]] = {}
        self.failures: Dict[str, int] = {}
        self.notified: set = set()
        # PIDs that will never match (name, no annual cycle, over budget).
        self.dropped: set = set()
        self.cart_dirty = True
        self.last_page_at = float("-inf")
        self.last_login_check = float("-inf")
        self.stopping = False
        self.heartbeat = Heartbeat(
            STATUS_DIR,
            NAME,
            "BOIL SeedNet 年付",
            "名称/说明含 SeedNet/遠傳，非 NAT/静态/盲盒，年付，今日总价 ≤ $%g，余额付款" % self.max_total,
            float(cfg["fast_poll_seconds"]),
        )
        self.switch = Switch(Control(STATUS_DIR, NAME), self.marker_path, self.beat)

    # -- plumbing ----------------------------------------------------------
    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
        return session

    def _load_cookies(self) -> None:
        try:
            for item in json.loads(self.cookie_path.read_text("utf-8")):
                self.session.cookies.set(item["name"], item["value"], domain=item.get("domain"), path=item.get("path", "/"))
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def _save_cookies(self) -> None:
        items = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self.session.cookies
        ]
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cookie_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.cookie_path)

    def beat(self, **values: Any) -> None:
        # One-off test/login runs must not overwrite the service's status.
        if not (getattr(self.args, "dry_run_url", None) or getattr(self.args, "check_login", False)
                or getattr(self.args, "tos_probe_url", None)):
            self.heartbeat.update(**values)

    def notify(self, text: str) -> None:
        LOG.info("notify: %s", text.replace("\n", " | "))
        if self.telegram is None:
            return
        try:
            self.telegram.send(text)
        except TelegramError:
            LOG.exception("Telegram notification failed")

    def request(self, method: str, url: str, spacing: Optional[float] = None,
                public: bool = False, **kwargs: Any) -> requests.Response:
        """One request, spaced from the last; a block page raises BlockedError."""
        gap = float(self.cfg["page_spacing_seconds"] if spacing is None else spacing)
        wait = gap - (time.monotonic() - self.last_page_at)
        if wait > 0:
            time.sleep(wait)
        self.last_page_at = time.monotonic()
        session = self.public_session if public else self.session
        kwargs.setdefault("timeout", self.timeout)
        response = session.request(method, urljoin(self.base_url, url), **kwargs)
        if response.status_code in (403, 429) or is_challenge_page(response.text):
            raise BlockedError("HTTP %s for %s" % (response.status_code, url))
        if not public:
            self._save_cookies()
        return response

    # -- login -------------------------------------------------------------
    @staticmethod
    def logged_in(page_html: str) -> bool:
        return "logout" in page_html.lower()

    def ensure_login(self) -> bool:
        response = self.request("GET", "clientarea.php")
        if "login" not in urlparse(response.url).path and self.logged_in(response.text):
            return True
        if not self.email or not self.password:
            self.notify("⚠️ <b>BOIL 抢购：没有账号密码</b>（data/boil_account.env）")
            return False
        page = self.request("GET", "login")
        token = BeautifulSoup(page.text, "lxml").select_one("form.login-form input[name=token]")
        response = self.request("POST", "login", data={
            "token": str(token.get("value")) if token else "",
            "username": self.email, "password": self.password, "rememberme": "on",
        })
        if self.logged_in(response.text):
            LOG.info("logged in to BOIL")
            return True
        error = BeautifulSoup(response.text, "lxml").select_one(".login-form .alert-danger, .alert-danger")
        why = error.get_text(" ", strip=True)[:200] if error else "未知原因"
        self.notify("⚠️ <b>BOIL 抢购：登录失败</b>\n%s" % html.escape(why))
        return False

    def check_login(self) -> bool:
        self.last_login_check = time.monotonic()
        ok = self.ensure_login()
        self.beat(login_ok=ok, login_checked_at=_now_iso())
        return ok

    def empty_cart(self) -> None:
        self.request("GET", "cart.php?a=empty", spacing=float(self.cfg["order_spacing_seconds"]))
        self.cart_dirty = False

    def newest_invoice(self) -> Optional[int]:
        response = self.request("GET", "clientarea.php?action=invoices")
        ids = [int(value) for value in re.findall(r"viewinvoice\.php\?id=(\d+)", response.text)]
        return max(ids) if ids else None

    def balance(self) -> Optional[float]:
        response = self.request("GET", "clientarea.php?action=addfunds")
        text = BeautifulSoup(response.text, "lxml").get_text(" ", strip=True)
        # Not "餘額上限" (the balance cap) on the same page.
        match = re.search(r"(?:目前餘額|目前余额|current balance)\s*:?\s*(\$\s*[\d,]+(?:\.\d+)?)", text, re.I)
        return parse_usd(match.group(1)) if match else None

    # -- one product ---------------------------------------------------------
    def resolve_pid(self, url: str) -> Optional[str]:
        """The PID a store product link redirects to, without adding it to the cart."""
        kind, value = classify_url(url, self.base_url)
        if kind == "pid":
            return value
        if kind != "product":
            return None
        response = self.request("GET", url, allow_redirects=False)
        location = response.headers.get("Location", "")
        match = re.search(r"pid=(\d+)", location)
        return match.group(1) if match else None

    def order(self, pid: str, mode: str = "live") -> str:
        """Order PID ``pid``. ``mode``: live, dry (stop before checkout) or
        tos (submit checkout without the TOS box: WHMCS refuses, no order)."""
        fast = float(self.cfg["order_spacing_seconds"])
        if self.cart_dirty:
            self.empty_cart()
        self.cart_dirty = True
        response = self.request("GET", "cart.php?a=add&pid=%s" % pid, spacing=fast)
        if "a=confproduct" not in response.url:
            if OUT_OF_STOCK.search(BeautifulSoup(response.text, "lxml").get_text(" ", strip=True)):
                LOG.info("pid %s out of stock", pid)
            else:
                LOG.info("pid %s: no configure page (%s)", pid, response.url)
            return OrderResult.WAIT
        data, info = conf_form(response.text, self.cycle, str(self.cfg["hostname"]), random_password())
        name = info["name"] or "PID %s" % pid
        if not text_matches("%s %s" % (info["name"], info["description"]), self.include, self.exclude):
            LOG.info("pid %s (%s) is not the target", pid, name)
            self.dropped.add(pid)
            self.empty_cart()
            return OrderResult.DROP
        if data is None:
            self.dropped.add(pid)
            self.empty_cart()
            return self.order_failed(pid, name, info["why"], final=True)
        index = parse_qs(urlparse(response.url).query).get("i", [data.get("i", "0")])[0]
        data["i"] = data.get("i") or index
        reply = self.request("POST", "cart.php?a=confproduct", spacing=fast, data=dict(data, ajax="1"))
        errors = BeautifulSoup(reply.text, "lxml").get_text(" ", strip=True)
        if errors:
            self.empty_cart()
            return self.order_failed(pid, name, "配置被拒：%s" % errors[:200])
        page = self.request("GET", "cart.php?a=checkout", spacing=fast)
        form, totals = checkout_form(page.text, accept_tos=(mode != "tos"))
        total, balance = totals["total"], totals["balance"]
        LOG.info("pid %s %s: total %s, balance %s", pid, name, total, balance)
        if form is None or total is None:
            self.empty_cart()
            return self.order_failed(pid, name, totals["why"] or "读不到今日总价")
        if form.get("custtype") != "existing" or not form.get("account_id"):
            # Logged out: checkout would try to sign up a new customer.
            LOG.warning("checkout is not for the logged-in account; logging in again")
            self.empty_cart()
            self.last_login_check = float("-inf")
            self.ensure_login()
            return OrderResult.WAIT
        if total > self.max_total + 0.005:
            key = "price:%s" % pid
            if key not in self.notified:
                self.notified.add(key)
                self.notify(
                    "💸 <b>BOIL 抢购：%s 超出预算，没有下单</b>\n今日总价 $%.2f &gt; 上限 $%g\n%s"
                    % (html.escape(name), total, self.max_total,
                       html.escape(urljoin(self.base_url, "cart.php?a=add&pid=%s" % pid)))
                )
            self.dropped.add(pid)
            self.empty_cart()
            return OrderResult.DROP
        if mode == "dry":
            print("DRY RUN ok: %s, %s, total $%.2f, balance %s, checkout fields %s"
                  % (name, info["cycles"].get(self.cycle), total, balance, sorted(form)))
            self.empty_cart()
            return OrderResult.WAIT
        if mode == "tos":
            result = self.request("POST", "cart.php?a=checkout", spacing=fast, data=form)
            refused = "a=checkout" in result.url and invoice_id(result.url, result.text) is None
            error = BeautifulSoup(result.text, "lxml").select_one(".checkout-error-feedback, .alert-danger")
            print("TOS probe: refused=%s url=%s error=%r" % (
                refused, result.url, error.get_text(" ", strip=True)[:200] if error else None))
            self.empty_cart()
            return OrderResult.WAIT
        return self.checkout(pid, name, info["cycles"].get(self.cycle, self.cycle), form, total, balance)

    def checkout(self, pid: str, name: str, cycle_label: str, form: Dict[str, str],
                 total: float, balance: Optional[float]) -> str:
        known = self.newest_invoice()
        self.beat(state="ordering", detail="正在下单 %s" % name)
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(
            self.session.post, urljoin(self.base_url, "cart.php?a=checkout"),
            data=form, timeout=float(self.cfg["checkout_wait_seconds"]),
        )
        alerted = False
        response = None
        started = time.monotonic()
        while True:
            try:
                response = future.result(timeout=1)
                break
            except concurrent.futures.TimeoutError:
                if not alerted and time.monotonic() - started >= float(self.cfg["checkout_alert_seconds"]):
                    alerted = True
                    self.notify(
                        "⏳ <b>BOIL 结账 %d 秒没有回应，请马上手动接手</b>\n%s\n"
                        "先看账单页，有新账单就付那张，别再下一单：%s"
                        % (int(self.cfg["checkout_alert_seconds"]), html.escape(name),
                           html.escape(urljoin(self.base_url, "clientarea.php?action=invoices")))
                    )
            except Exception:
                LOG.exception("checkout request failed")
                break
        pool.shutdown(wait=False)
        self.last_page_at = time.monotonic()
        self._save_cookies()
        invoice = invoice_id(response.url, response.text) if response is not None else None
        if invoice is None:
            newest = self.newest_invoice()
            if newest is not None and (known is None or newest > known):
                invoice = str(newest)
        if invoice is None:
            why = "结账后没有找到新账单（%s）" % (response.url if response is not None else "请求没有回应")
            return self.order_failed(pid, name, why)
        self.cart_dirty = False
        return self.record_order(pid, name, cycle_label, invoice, total, balance)

    def record_order(self, pid: str, name: str, cycle_label: str, invoice: str,
                     total: float, balance: Optional[float]) -> str:
        invoice_url = urljoin(self.base_url, "viewinvoice.php?id=%s" % invoice)
        status = invoice_status(self.request("GET", invoice_url).text)
        result = {
            "at": _now_iso(), "product": name, "pid": pid, "plan": cycle_label,
            "invoice": invoice, "invoice_url": invoice_url, "invoice_status": status,
            "total": total, "balance_before": balance,
        }
        # The order exists: write the marker before anything else can fail.
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        LOG.info("order created: %s", result)
        title = "%s · %s" % (html.escape(name), html.escape(cycle_label))
        if status != "Paid":
            self.notify(
                "🛒 <b>BOIL 已下单，但还没付清</b>\n%s\n账单 #%s：%s，今日总价 $%.2f\n"
                "程序正在尝试用余额付款，结果马上再发。你也可以直接上去付：%s"
                % (title, invoice, html.escape(status), total, html.escape(invoice_url))
            )
            why = self.pay_with_credit(invoice_url)
            status = invoice_status(self.request("GET", invoice_url).text)
            result.update(invoice_status=status, pay_attempt=why)
            self.marker_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            why = ""
        if status == "Paid":
            self.notify("🎉 <b>BOIL 抢到了，已用余额付款</b>\n%s\n账单 #%s：已付款\n%s"
                        % (title, invoice, html.escape(invoice_url)))
        else:
            self.notify(
                "🚨 <b>BOIL 余额付款没成功，请马上手动付款</b>\n%s\n账单 #%s：%s\n原因：%s\n%s"
                % (title, invoice, html.escape(status), html.escape(why), html.escape(invoice_url))
            )
        self.switch.bought("%s %s 账单 #%s %s" % (name, cycle_label, invoice, status))
        return OrderResult.BOUGHT

    def pay_with_credit(self, invoice_url: str) -> str:
        """Best effort: WHMCS invoices offer "apply credit" when there is a balance."""
        try:
            page = self.request("GET", invoice_url, spacing=float(self.cfg["order_spacing_seconds"]))
            soup = BeautifulSoup(page.text, "lxml")
            field = soup.select_one('form input[name="applycredit"]')
            if field is None:
                return "账单页没有“使用余额”选项（余额可能不够）"
            form = field.find_parent("form")
            data = {
                str(node.get("name")): str(node.get("value") or "")
                for node in form.select("input[name]")
                if str(node.get("type") or "").lower() not in ("checkbox", "radio") or node.has_attr("checked")
            }
            action = urljoin(invoice_url, str(form.get("action") or invoice_url))
            self.request("POST", action, spacing=float(self.cfg["order_spacing_seconds"]), data=data)
            return "已提交“使用余额付款”"
        except Exception as exc:  # incl. BlockedError: the user must still hear about it
            LOG.exception("applying credit to %s failed", invoice_url)
            return "余额付款出错：%s" % exc

    def order_failed(self, key: str, name: str, why: str, final: bool = False) -> str:
        count = self.failures.get(key, 0) + 1
        self.failures[key] = count
        give_up = final or count >= int(self.cfg["max_order_failures"])
        self.notify(
            "❌ <b>BOIL 下单失败</b>：%s\n%s\n%s\n%s"
            % (html.escape(name), html.escape(why),
               html.escape(urljoin(self.base_url, "cart.php?a=add&pid=%s" % key)),
               "已放弃这个商品。" if give_up else "下一轮重试（第 %d 次）。" % count)
        )
        return OrderResult.DROP if give_up else OrderResult.WAIT

    # -- armed links -----------------------------------------------------------
    def attempt(self, url: str) -> str:
        kind, value = classify_url(url, self.base_url)
        if kind == "group" and value:
            page = self.request("GET", url)
            if "/store/%s" % value not in unquote(page.url):
                return OrderResult.WAIT
            cards = self.parser.parse_category(page.text, page.url, value)
            matching = [
                card for card in cards
                if text_matches("%s %s" % (card.name, card.metadata.get("description") or ""),
                                self.include, self.exclude)
            ]
            LOG.info("group %s: %d cards, %d matching", value, len(cards), len(matching))
            for card in matching:
                if (
                    card.available is False
                    or card.product_id in self.dropped
                    or self.failures.get(card.product_id, 0) >= int(self.cfg["max_order_failures"])
                ):
                    continue
                if self.order(card.product_id) == OrderResult.BOUGHT:
                    return OrderResult.BOUGHT
            return OrderResult.WAIT
        pid = self.resolve_pid(url)
        if pid is None or pid in self.dropped:
            return OrderResult.DROP
        return self.order(pid)

    def arm(self, urls: Iterable[str], source: str, public: bool) -> None:
        now = time.monotonic()
        until = now + float(self.cfg["arm_minutes"]) * 60
        new = []
        for url in urls:
            entry = self.armed.get(url)
            if entry is None:
                self.armed[url] = {"since": now, "until": until, "public": public, "next": now, "hidden_seen": None}
                new.append(url)
            else:
                entry["until"] = max(entry["until"], until)
                if public and not entry["public"]:
                    self.make_public(url, "TG 已发布" if source.startswith("tg-") else "已出现在商城菜单")
        if new:
            LOG.info("armed by %s (%s): %s", source, "public" if public else "not public yet", new)

    def make_public(self, url: str, why: str) -> float:
        entry = self.armed[url]
        entry["public"] = True
        now = time.monotonic()
        wait = 0.0 if why == "TG 已发布" else public_wait(
            float(self.cfg["public_delay_seconds"]), entry.get("hidden_seen"), now)
        entry["next"] = now + wait
        LOG.info("%s is public now (%s); ordering in %.1f s", url, why, wait)
        return wait

    def check_public(self, url: str) -> bool:
        """For a group only the site monitor saw: is it in the shop menu yet?"""
        kind, slug = classify_url(url, self.base_url)
        page = self.request("GET", str(self.cfg["menu_url"]), public=True)
        if kind == "group" and slug and group_listed(page.text, slug, self.base_url):
            wait = self.make_public(url, "已出现在商城菜单")
            self.notify("👀 <b>BOIL 活动分组已出现在商城菜单</b>，%s\n%s"
                        % ("马上下单" if wait < 0.5 else "%.0f 秒后下单" % wait, html.escape(url)))
            return True
        self.armed[url]["hidden_seen"] = time.monotonic()
        return False

    def interval_for(self, entry: Dict[str, Any]) -> float:
        fast = self.switch.interval(float(self.cfg["fast_poll_seconds"]))
        if time.monotonic() - entry["since"] < float(self.cfg["fast_minutes"]) * 60:
            return fast
        return max(fast, float(self.cfg["slow_poll_seconds"]))

    def poll_armed(self) -> bool:
        now = time.monotonic()
        for url in list(self.armed):
            if self.armed[url]["until"] < now:
                LOG.info("disarming %s (window over)", url)
                self.armed.pop(url, None)
        due = [url for url, entry in self.armed.items() if entry["next"] <= time.monotonic()]
        if due and any(self.armed[url]["public"] for url in due) and time.monotonic() - self.last_login_check > 600:
            # Warm up: be logged in with an empty cart before the first order.
            if not self.check_login():
                for entry in self.armed.values():
                    entry["next"] = time.monotonic() + 300
                return False
            if self.cart_dirty:
                self.empty_cart()
        for url in due:
            if self.stopping or self.switch.control.paused():
                return False
            entry = self.armed[url]
            try:
                if not entry["public"]:
                    # Hidden so far: never open it while logged in.
                    self.beat(state="watching", detail="等 %s 公开（商城菜单或 TG）" % url)
                    if not self.check_public(url):
                        entry["next"] = time.monotonic() + float(self.cfg["hidden_poll_seconds"])
                    continue
                self.beat(state="watching", detail="检查 %s" % url)
                result = self.attempt(url)
            except BlockedError as exc:
                self.notify("⛔ <b>BOIL 抢购：疑似被网站限流</b>\n%s\n暂停 30 分钟。" % html.escape(str(exc)))
                for item in self.armed.values():
                    item["next"] = time.monotonic() + 1800
                return False
            except Exception as exc:  # a page changed under us; retry next round
                LOG.exception("attempt on %s failed", url)
                result = OrderResult.WAIT
                if self.failures.get(url, 0) == 0:
                    self.notify("⚠️ <b>BOIL 抢购出错</b>（会重试）\n%s\n%s" % (
                        html.escape(url), html.escape(str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__)))
                self.failures[url] = self.failures.get(url, 0) + 1
            if result == OrderResult.BOUGHT:
                self.armed.clear()
                return True
            if result == OrderResult.DROP:
                self.armed.pop(url, None)
            elif url in self.armed:
                entry["next"] = time.monotonic() + self.interval_for(entry)
        return False

    def idle_detail(self) -> str:
        if self.armed:
            return "已触发，正在盯 %d 个链接：%s" % (len(self.armed), "、".join(
                url if entry["public"] else "%s（未公开，只看菜单）" % url
                for url, entry in self.armed.items()))
        return "待命：等 BOIL TG 频道/官方账号发链接，或 festive-offer 上架"

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        self.switch.startup(self.marker_result())
        self.beat(state="watching", detail=self.idle_detail())
        login_every = float(self.cfg["login_check_hours"]) * 3600
        try:
            while not self.stopping:
                if self.switch.hold():
                    take_triggers(self.trigger_dir)  # a paused buyer ignores sightings
                    self.armed.clear()
                    continue
                if self.switch.rearm():
                    LOG.info("resumed after a purchase; old marker archived")
                for trigger in take_triggers(self.trigger_dir):
                    targets = trigger_targets(trigger, self.cfg)
                    if targets:
                        self.arm(targets, str(trigger.get("source")), trigger_is_public(trigger))
                    else:
                        LOG.info("trigger from %s arms nothing: %s", trigger.get("source"), trigger.get("urls"))
                if self.armed:
                    if self.poll_armed():
                        continue
                elif time.monotonic() - self.last_login_check >= login_every:
                    try:
                        self.check_login()
                    except Exception:
                        LOG.exception("login check failed")
                self.beat(state="watching", detail=self.idle_detail())
                time.sleep(1)
        finally:
            self.beat(state="stopped", detail="进程已退出")
        return 0

    def marker_result(self) -> str:
        try:
            data = json.loads(self.marker_path.read_text("utf-8"))
        except (OSError, ValueError):
            return ""
        return "%s %s 账单 #%s %s" % (data.get("product"), data.get("plan"), data.get("invoice"), data.get("invoice_status"))

    def _on_signal(self, signum, frame) -> None:  # noqa: ANN001
        self.stopping = True

    # -- one-off commands --------------------------------------------------
    def run_once(self, url: str, mode: str) -> int:
        if not self.check_login():
            print("NOT logged in")
            return 1
        print("logged in; balance:", self.balance())
        kind, value = classify_url(url, self.base_url)
        pid = value if kind == "pid" else self.resolve_pid(url)
        if pid is None:
            raise SystemExit("need a product link or cart.php?a=add&pid=N")
        # Tests use a broad include so a public regular product can stand in.
        self.include = [""]
        if getattr(self.args, "cycle", None):
            self.cycle = str(self.args.cycle)
        print("result:", self.order(pid, mode=mode))
        return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--check-login", action="store_true", help="log in once (if needed) and exit")
    parser.add_argument("--dry-run-url", help="run the order flow on this PUBLIC product up to the checkout page, then empty the cart")
    parser.add_argument("--tos-probe-url", help="like --dry-run-url, then submit checkout WITHOUT the TOS box (WHMCS refuses; no order)")
    parser.add_argument("--cycle", help="with --dry-run-url / --tos-probe-url: billing cycle to test (public test items may lack annually)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    one_off = args.check_login or args.dry_run_url or args.tos_probe_url
    if not one_off and not (config.get("boil_autobuy") or {}).get("enabled"):
        LOG.info("boil_autobuy is disabled in the config")
        return 0
    buyer = BoilAutoBuyer(config, args)
    if args.check_login:
        ok = buyer.check_login()
        print("logged in" if ok else "NOT logged in", "| balance:", buyer.balance() if ok else None)
        return 0 if ok else 1
    if args.dry_run_url:
        return buyer.run_once(args.dry_run_url, "dry")
    if args.tos_probe_url:
        return buyer.run_once(args.tos_probe_url, "tos")
    return buyer.run()


if __name__ == "__main__":
    raise SystemExit(main())
