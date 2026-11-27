"""FACHOST (fachost.cloud) auto-buyer, driven by the existing FAC monitors.

It does not watch the site by itself. The Telegram relay (a fachost link
posted on the channel or by a group admin; sale keywords alone don't
count) and the
``fachost_sale`` provider (special-regulations opened) drop trigger files
(see ``fac_trigger``); each trigger "arms" its links for a few hours, and
armed links are checked one page at a time until a matching product can be
ordered. Matching = the product name contains an include word (SeedNet),
no exclude word (NAT), and a plan rank_plans accepts (annual-only products
first, else a cycle at or under $28.5 a month), with today's total at or
under the cap. Of several matching products the best per rank_plans is
bought.

Hidden links are never ordered (user, 2026-11-08): a link counts as public
once it was posted on Telegram, or once its category is linked from the
shop menu. Until then the buyer only re-checks the menu (it never opens the
hidden page while logged in); after that it waits a few seconds, then orders.

The site sits behind Cloudflare Turnstile on login and at checkout.
Turnstile fails at once in a Playwright-launched Chromium, but passes by
itself in a plain Chromium started directly (real user agent, no automation
flags). So the buyer starts that plain Chromium with a persistent profile
(the login lives there) and only attaches over a localhost CDP port. Close
it through CDP ``Browser.close``: killing the process group loses cookies
written since the last flush.

The order is paid from the account balance: the cart's ``use_credits`` is
on by default and the buyer leaves it alone. (Changing it is a Livewire
round trip, and every finished round trip resets Turnstile; turning it off
also made checkout hang without creating an order.) With a short balance the
invoice stays unpaid: Telegram says so at once with the pay link, the buyer
tries to pay it from the balance, then reports the result. If Checkout gets
no answer for a while, Telegram tells the user to step in by hand. Either way the buyer stops after
one order (marker file; resume from Telegram /autobuy). A ``--test-url`` run
reads the balance first and only checks out when the balance is below the
order total, so the test invoice cannot be paid.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import signal
import socket
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

from .autobuy_status import Control, Heartbeat, Switch
from .config import load_config, telegram_credentials
from .fac_trigger import DEFAULT_DIR as TRIGGER_DIR
from .fac_trigger import take_triggers
from .quiet_hours import TAIPEI, in_quiet_hours, parse_quiet_hours
from .telegram import TelegramClient, TelegramError


LOG = logging.getLogger("fac_autobuy")
NAME = "fac"
STATUS_DIR = "data/autobuy"
DEFAULTS: Dict[str, Any] = {
    "base_url": "https://fachost.cloud/",
    "profile_dir": "data/fac_profile",
    "account_file": "data/fac_account.env",
    "marker_file": "data/fac_autobuy_done.json",
    "trigger_dir": TRIGGER_DIR,
    "cdp_port": 9291,
    "name_include": ["seednet"],
    "name_exclude": ["nat"],
    # Today's total (price + setup fee) of any order. Plans: see rank_plans.
    "max_total": 280.0,
    "any_cycle_month": 28.5,
    "hostname": "seednet",
    "coupons": [],
    # A link that does not lead to a product (an announcement, the home
    # page) or a keyword-only post arms these instead.
    "default_urls": ["https://fachost.cloud/products/special-regulations"],
    "arm_minutes": 360,
    "fast_poll_seconds": 20,
    "fast_minutes": 30,
    "slow_poll_seconds": 60,
    # Between two page loads (user, 2026-11-28: 1 s, was 4 s). goto() also
    # waits 2 s after each load for Livewire.
    "page_spacing_seconds": 1,
    "login_check_hours": 6,
    # "Remember me" lasts 30 days from the login and visits don't extend it
    # (user, 2026-11-28): a login check that finds it ending within this many
    # hours signs in afresh, so the renewal happens on a daytime check with
    # room for retries instead of at expiry (on 2026-12-07 09:05 for the first
    # one). 0 = off.
    "relogin_before_hours": 48,
    "max_order_failures": 3,
    # Never order a link that is still hidden (user, 2026-11-08). A link is
    # public once it was posted on Telegram, or once its category shows up
    # in the shop's own menu. Order once it has been public for at least
    # public_delay_seconds (user: 3-5 s; longer risks losing the item). A TG
    # post means it is public already: order at once. A menu check that finds
    # it listed orders at once too, unless the previous check (still hidden)
    # was under public_delay_seconds ago; then wait out the rest. While a sale
    # page is open but not listed yet, re-check the menu every hidden_poll_seconds.
    "public_delay_seconds": 4,
    "hidden_poll_seconds": 120,
    # Checkout has hung without an answer on this site. After
    # checkout_alert_seconds Telegram tells the user to go and buy or pay by
    # hand (user, 2026-11-09); the buyer keeps waiting up to
    # checkout_wait_seconds, then looks for the invoice itself.
    "checkout_alert_seconds": 20,
    "checkout_wait_seconds": 90,
    # Taiwan time, e.g. "00:05-09:05": no login check meanwhile, one as they
    # end (see watcher.quiet_hours). TG triggers still arm the buyer at night.
    "quiet_hours": "",
}
INVOICE_PATH = re.compile(r"/invoices/([^/?#]+)")
# A pay option that takes the account balance. Not "credit card": that is a
# gateway, and paying through it only redirects.
CREDIT_OPTION = re.compile(r"餘額|余额|account credit|\bcredits\b|\bbalance\b", re.I)


# --------------------------------------------------------------------------
# Pure helpers (unit tested)

def parse_money(text: Any) -> Optional[float]:
    """First amount in ``text``; FAC prints ``$1.020,00`` (dot thousands, comma cents)."""
    match = re.search(r"\d[\d.,]*", str(text or ""))
    if not match:
        return None
    value = match.group(0).rstrip(".,")
    last_dot, last_comma = value.rfind("."), value.rfind(",")
    decimal = max(last_dot, last_comma)
    if decimal >= 0 and len(value) - decimal - 1 in (1, 2) and value.count(value[decimal]) == 1:
        whole, cents = value[:decimal], value[decimal + 1:]
    else:
        whole, cents = value, ""
    whole = whole.replace(".", "").replace(",", "")
    try:
        return float("%s.%s" % (whole, cents or "0"))
    except ValueError:
        return None


def money_amounts(text: Any) -> List[float]:
    found = []
    for match in re.finditer(r"\$\s*(\d[\d.,]*)", str(text or "")):
        value = parse_money(match.group(1))
        if value is not None:
            found.append(value)
    return found


def invoice_state(text: str) -> Tuple[str, Optional[str]]:
    """(status, amount still due) from an invoice page's text."""
    status = re.search(r"Invoice Status\s*\n\s*([^\n]+)", text)
    remaining = re.search(r"Remaining\s*\n\s*(\$\s*[\d.,]+)", text)
    return (status.group(1).strip() if status else "?"), (remaining.group(1) if remaining else None)


def name_matches(name: str, include: Iterable[str], exclude: Iterable[str]) -> bool:
    lowered = (name or "").casefold()
    if not any(word.casefold() in lowered for word in include):
        return False
    for word in exclude:
        # Whole word only, so "nat" never hits inside a longer Latin word.
        if re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(word.casefold()), lowered):
            return False
    return True


# Months per billing cycle, read from the plan's name (FAC names its plans:
# "年付 - $990,00 + $30,00 Setup fee"). Longer cycles first, as 兩年付 and
# 半年付 contain 年付; a digit right before a number word is a date ("2026年").
_MONTHS = r"\s*(?:個月|个月|months?)"
CYCLE_PATTERNS = [
    (36, r"三年|(?<!\d)3\s*年|triennial|(?<!\d)3\s*years?|(?<!\d)36" + _MONTHS),
    (24, r"[兩两二]年|(?<!\d)2\s*年|biennial|(?<!\d)2\s*years?|(?<!\d)24" + _MONTHS),
    (6, r"半年|semi-?annual|half[- ]?year|(?<!\d)6" + _MONTHS),
    (12, r"年[付繳缴費费度]|包年|一年|每年|(?<!\d)1\s*年|^年$|annual|yearly|every\s*year|(?<!\d)1\s*year\b|(?<!\d)12" + _MONTHS),
    (3, r"季|quarter|(?<!\d)3" + _MONTHS),
    (1, r"月[付繳缴費费]|包月|^月$|monthly|\bmonth\b|(?<!\d)1" + _MONTHS),
]
ANNUAL_ONLY = "只有年付"
MULTI_CYCLE = "多周期"


def plan_months(label: str) -> Optional[int]:
    """Months of one billing cycle from a plan label, None if unknown."""
    name = re.sub(r"\s+", " ", (label or "").split(" - ")[0]).strip().casefold()
    for months, pattern in CYCLE_PATTERNS:
        if re.search(pattern, name):
            return months
    return None


def rank_plans(
    options: Sequence[Tuple[str, str]], max_month: float
) -> Tuple[str, List[Tuple[str, str, int, float]], str]:
    """(rule, plans worth trying best first, why none) for a product's plans.

    User, 2026-11-28 (same as BOIL's SeedNet rule): a product sold annually
    only comes first; its annual plan is bought, cheapest first, capped
    only by the total. A product that also sells shorter cycles is bought
    only if a cycle comes to at most ``max_month`` a month (today's price +
    setup fee over the cycle's months; a regular restock runs ~$38-55/month),
    cheapest per month first, a tie going to the longer cycle. A plan whose
    cycle can't be read is never bought, and keeps a product from counting
    as annual-only (it may be monthly). Entries: (value, label, months,
    listed per month).
    """
    plans, unknown = [], []
    for value, label in options:
        text = re.sub(r"\s+", " ", label or "").strip()
        if not value:
            continue
        months, total = plan_months(text), sum(money_amounts(text))
        if months is None or total <= 0:
            unknown.append(text)
            continue
        plans.append((value, text, months, round(total / months, 2)))
    if not plans:
        return MULTI_CYCLE, [], "认不出付款周期：%s" % ("、".join(unknown) or "没有选项")
    if not unknown and any(p[2] == 12 for p in plans) and not any(p[2] < 12 for p in plans):
        annual = sorted((p for p in plans if p[2] == 12), key=lambda p: p[3])
        return ANNUAL_ONLY, annual, ""
    ranked = sorted(plans, key=lambda p: (p[3], -p[2]))
    fit = [p for p in ranked if p[3] <= max_month]
    why = "" if fit else "最便宜的是 %s，折合每月 $%.2f（上限 $%g/月）" % (ranked[0][1], ranked[0][3], max_month)
    return MULTI_CYCLE, fit, why


def offer_rank(offer: Dict[str, Any]) -> Tuple[int, float]:
    """Annual-only products first, then the cheapest per month."""
    return (0 if offer["rule"] == ANNUAL_ONLY else 1, offer["plans"][0][3])


def classify_url(url: str, base_url: str) -> Tuple[str, Optional[str]]:
    """("category"|"checkout"|"other", url to load) for a FAC link."""
    parsed = urlparse(url)
    base = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in ((base.hostname or "").lower(), "fachost.com"):
        return "other", None
    parts = [part for part in parsed.path.split("/") if part]
    root = "%s://%s/" % (base.scheme, base.netloc)
    if len(parts) >= 1 and parts[0] == "products":
        if len(parts) == 2:
            return "category", urljoin(root, "products/%s" % parts[1])
        if len(parts) >= 3:
            return "checkout", urljoin(root, "products/%s/%s/checkout" % (parts[1], parts[2]))
    return "other", None


def listed_category(url: str, menu_links: Iterable[str], base_url: str) -> bool:
    """True when ``url``'s category is linked from the shop menu."""
    kind, target = classify_url(url, base_url)
    if not target:
        return False
    category = urlparse(target).path.strip("/").split("/")[:2]
    listed = set()
    for link in menu_links:
        link_kind, link_target = classify_url(link, base_url)
        if link_kind == "category" and link_target:
            listed.add(tuple(urlparse(link_target).path.strip("/").split("/")[:2]))
    return tuple(category) in listed


def public_wait(min_age: float, last_hidden_at: Optional[float], now: float) -> float:
    """Seconds to wait so a link is public for ``min_age`` s before ordering.

    It turned public at some point after ``last_hidden_at`` (the last check
    that still saw it hidden); with no such check it may have been public for
    any time, so no wait.
    """
    if last_hidden_at is None:
        return 0.0
    return max(0.0, min_age - (now - last_hidden_at))


def trigger_is_public(trigger: Dict[str, Any]) -> bool:
    """Telegram posts are public; the site monitor only sees a page open."""
    return str(trigger.get("source") or "").startswith("tg-")


def trigger_targets(trigger: Dict[str, Any], base_url: str, default_urls: Sequence[str]) -> List[str]:
    """The links to arm for one trigger."""
    targets: List[str] = []
    fallback = False
    for url in trigger.get("urls") or []:
        kind, target = classify_url(str(url), base_url)
        if kind == "other":
            fallback = True
        elif target and target not in targets:
            targets.append(target)
    if fallback or not targets:
        for url in default_urls:
            if url not in targets:
                targets.append(url)
    return targets


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
# Plain Chromium + CDP

def chrome_executable() -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return playwright.chromium.executable_path


class ChromeSession:
    """A plain Chromium on the current DISPLAY, attached over localhost CDP."""

    def __init__(self, executable: str, profile_dir: str, port: int, extra_args: Sequence[str] = ()):
        self.executable = executable
        self.profile_dir = Path(profile_dir)
        self.port = port
        self.extra_args = list(extra_args)
        self.process: Optional[subprocess.Popen] = None
        self._playwright = None
        self.browser = None
        self.page = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None and self.page is not None

    def start(self) -> None:
        if self.running:
            return
        self.stop()
        self.profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if _port_open(self.port):
            raise RuntimeError("CDP port %s is already in use" % self.port)
        args = [
            self.executable,
            *self.extra_args,
            "--no-sandbox",
            "--user-data-dir=%s" % self.profile_dir.resolve(),
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=zh-CN",
            "--window-position=0,0",
            "--window-size=1280,900",
            "--password-store=basic",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=%d" % self.port,
            "about:blank",
        ]
        LOG.info("starting Chromium (profile %s)", self.profile_dir)
        self.process = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("Chromium exited at start (code %s)" % self.process.returncode)
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json/version" % self.port, timeout=2).read()
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("Chromium CDP port did not come up")
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.connect_over_cdp("http://127.0.0.1:%d" % self.port)
        context = self.browser.contexts[0]
        pages = context.pages
        self.page = pages[0] if pages else context.new_page()
        for extra in pages[1:]:
            try:
                extra.close()
            except Exception:
                pass
        self.page.set_default_timeout(30000)
        self.page.on("dialog", lambda dialog: dialog.accept())

    def stop(self) -> None:
        if self.browser is not None:
            try:
                # Graceful close: Chromium flushes the cookie store first.
                self.browser.new_browser_cdp_session().send("Browser.close")
            except Exception:
                pass
        if self.process is not None:
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                LOG.warning("Chromium did not exit; killing it")
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except OSError:
                    pass
                self.process.wait(timeout=10)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self.process = None
        self._playwright = None
        self.browser = None
        self.page = None


def _port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


# --------------------------------------------------------------------------
# The buyer

class OrderResult:
    BOUGHT = "bought"   # an order (invoice) was created
    WAIT = "wait"       # not orderable yet / sold out: keep the link armed
    DROP = "drop"       # never going to match: disarm the link


class FacAutoBuyer:
    def __init__(self, config: Dict[str, Any], args: argparse.Namespace):
        cfg = dict(DEFAULTS)
        cfg.update(config.get("fac_autobuy") or {})
        self.cfg = cfg
        self.args = args
        self.base_url = str(cfg["base_url"])
        self.include = [str(v) for v in cfg["name_include"]]
        self.exclude = [str(v) for v in cfg["name_exclude"]]
        self.max_total = float(cfg["max_total"])
        self.any_cycle_month = float(cfg["any_cycle_month"])
        self.marker_path = Path(str(cfg["marker_file"]))
        self.trigger_dir = str(cfg["trigger_dir"])
        account_file = Path(str(cfg["account_file"]))
        account = _read_env(account_file) if account_file.exists() else {}
        self.email = account.get("FAC_EMAIL", "")
        self.password = account.get("FAC_PASSWORD", "")
        self.chrome = ChromeSession(
            getattr(args, "chrome", None) or chrome_executable(),
            str(cfg["profile_dir"]),
            int(cfg["cdp_port"]),
        )
        self.telegram: Optional[TelegramClient] = None
        credentials = telegram_credentials()
        if credentials["token"] and credentials["chat_id"] and not getattr(args, "no_notify", False):
            self.telegram = TelegramClient(credentials["token"], credentials["chat_id"])
        self.armed: Dict[str, Dict[str, float]] = {}
        self.failures: Dict[str, int] = {}
        self.price_notified: set = set()
        self.test_balance: Optional[float] = None
        # Newest invoice seen before ordering; a newer one means an order went through.
        self.known_invoice: Optional[str] = None
        self.unconfirmed_checkout = False
        self.last_page_at = float("-inf")
        self.last_login_check = float("-inf")
        parse_quiet_hours(cfg["quiet_hours"])  # a typo fails at start
        self.quiet = False
        self.stopping = False
        self.heartbeat = Heartbeat(
            STATUS_DIR,
            NAME,
            "FAC SeedNet 年付",
            "名称含 %s、不含 %s；只有年付的优先，多周期折合每月 ≤ $%g；总价 ≤ $%g，余额付款"
            % ("/".join(self.include), "/".join(self.exclude), self.any_cycle_month, self.max_total),
            float(cfg["fast_poll_seconds"]),
        )
        self.switch = Switch(Control(STATUS_DIR, NAME), self.marker_path, self.beat)

    # -- plumbing ----------------------------------------------------------
    def beat(self, **values: Any) -> None:
        # One-off test/login runs must not overwrite the service's status.
        if not (getattr(self.args, "test_url", None) or getattr(self.args, "check_login", False)):
            self.heartbeat.update(**values)

    def notify(self, text: str) -> None:
        LOG.info("notify: %s", text.replace("\n", " | "))
        if self.telegram is None:
            return
        try:
            self.telegram.send(text)
        except TelegramError:
            LOG.exception("Telegram notification failed")

    @property
    def page(self):
        return self.chrome.page

    def goto(self, url: str):
        """Load one page, keeping the site's request spacing."""
        wait = float(self.cfg["page_spacing_seconds"]) - (time.monotonic() - self.last_page_at)
        if wait > 0:
            time.sleep(wait)
        self.last_page_at = time.monotonic()
        response = self.page.goto(url, wait_until="domcontentloaded")
        time.sleep(2)  # let Livewire hydrate
        status = response.status if response is not None else 0
        # Not is_challenge_page(): the Turnstile widget on login/cart pages
        # carries the same markers. A real block replaces the whole page.
        title = (self.page.title() or "").lower()
        if status in (403, 429) or "just a moment" in title or "attention required" in title:
            raise BlockedError("HTTP %s (%s) for %s" % (status, title, url))
        return status

    def wire_data(self, name: str) -> Dict[str, Any]:
        raw = self.page.eval_on_selector(
            '[wire\\:name="%s"]' % name, 'e => e.getAttribute("wire:snapshot")'
        )
        return (json.loads(raw) or {}).get("data") or {}

    def body_text(self) -> str:
        main = self.page.locator("main")
        return main.first.inner_text() if main.count() else self.page.inner_text("body")

    # -- login -------------------------------------------------------------
    def ensure_login(self) -> bool:
        if "/login" not in self.page.url:
            return True
        if not (self.email and self.password):
            return False
        LOG.info("signing in to FAC as %s", self.email)
        self.page.wait_for_selector('input[name="email"]')
        self.page.fill('input[name="email"]', self.email)
        self.page.fill('input[name="password"]', self.password)
        remember = self.page.locator('input[name="remember"]')
        if remember.count() and not remember.first.is_checked():
            remember.first.check()
        self.wait_turnstile()
        self.page.locator('form button[type="submit"]').first.click()
        try:
            self.page.wait_for_url(lambda url: "/login" not in url, timeout=30000)
        except Exception:
            pass
        ok = "/login" not in self.page.url
        LOG.info("FAC login %s", "ok" if ok else "failed")
        return ok

    def wait_turnstile(self, seconds: int = 45) -> bool:
        for _ in range(seconds):
            token = self.page.evaluate(
                '() => (document.querySelector("[name=cf-turnstile-response]") || {}).value || ""'
            )
            if token:
                return True
            time.sleep(1)
        return False

    def remember_expiry(self) -> Optional[float]:
        """Epoch expiry of the "remember me" cookie, None if there is none."""
        expiries = [
            float(cookie.get("expires") or 0)
            for cookie in self.page.context.cookies(self.base_url)
            if "remember" in cookie.get("name", "").casefold()
        ]
        return max(expiries) if expiries and max(expiries) > 0 else None

    def relogin(self, left: float) -> bool:
        """Sign in afresh for a new 30-day "remember me". Only our session
        and remember cookies are dropped (no logout: logging out would cycle
        the account's remember token and sign out the user's own devices);
        if the new login fails they are put back, so the old one still
        holds until it ends."""
        LOG.info("remember-me ends in %.1f h: signing in afresh", left / 3600)
        context = self.page.context
        names = re.compile(r"remember|session", re.I)
        saved = [cookie for cookie in context.cookies() if names.search(cookie.get("name", ""))]
        context.clear_cookies(name=names)
        self.goto(urljoin(self.base_url, "account"))
        ok = self.ensure_login()
        until = self.remember_expiry() if ok else None
        if not (ok and until and until - time.time() > left + 3600):
            LOG.warning("fresh login failed: restoring the old cookies")
            context.clear_cookies(name=names)
            context.add_cookies(saved)
            ok = False
        if ok:
            self.notify(
                "🔑 <b>FAC 抢购：「记住我」快到期，已主动重新登录</b>\n新的到期时间 %s（台湾时间）"
                % datetime.fromtimestamp(until, TAIPEI).strftime("%Y-%m-%d %H:%M")
            )
            return True
        self.notify(
            "⚠️ <b>FAC 抢购：「记住我」%.0f 小时后到期，主动重新登录没成功</b>\n"
            "下次查登录（6 小时后）会再试；到期前一直不行就请找 Claude 用远程浏览器登录。" % (left / 3600)
        )
        return False

    def check_login(self) -> bool:
        self.goto(urljoin(self.base_url, "account"))
        if "/login" not in self.page.url:
            before = float(self.cfg["relogin_before_hours"]) * 3600
            until = self.remember_expiry()
            if before > 0 and until is not None and until - time.time() < before:
                return self.relogin(until - time.time())
            return True
        ok = self.ensure_login()
        if ok:
            self.notify("🔑 <b>FAC 抢购：登录已过期，已自动重新登录</b>")
        else:
            self.notify(
                "⚠️ <b>FAC 抢购：登录失效，自动登录失败</b>\n"
                "有货时会下不了单。请找 Claude 重新登录（远程浏览器）。"
            )
        return ok

    # -- buying ------------------------------------------------------------
    def scan_category(self, url: str) -> Tuple[Optional[List[Tuple[str, str]]], List[str]]:
        """(matching orderable (name, checkout url) or None if closed, all card names)."""
        status = self.goto(url)
        if status == 404 or urlparse(self.page.url).path.rstrip("/") != urlparse(url).path.rstrip("/"):
            return None, []
        cards = self.page.eval_on_selector_all(
            "article.fh-product-card",
            """els => els.map(e => ({
                name: ((e.querySelector('.fh-product-top h2') || {}).innerText || '').trim(),
                stock: ((e.querySelector('.fh-stock') || {}).innerText || '').trim(),
                buy: (e.querySelector('a.fh-product-buy[href]') || {}).href || '',
                links: [...e.querySelectorAll('a[href]')].map(a => a.href)
            }))""",
        )
        found, names = [], []
        for card in cards:
            name = re.sub(r"\s+", " ", card["name"]).strip()
            names.append(name)
            if not name_matches(name, self.include, self.exclude):
                continue
            if re.search(r"sold out|售罄|已售完|缺貨|缺货", card["stock"], re.I):
                continue
            for link in [card["buy"]] + card["links"]:
                kind, target = classify_url(link, self.base_url)
                if kind == "checkout" and target:
                    found.append((name, target))
                    break
        return found, names

    def order(self, checkout_url: str, test: bool = False) -> str:
        result, offer = self.read_offer(checkout_url, test)
        return self.buy_offer(offer, test) if offer else result

    def recover_unconfirmed(self, checkout_url: str, test: bool) -> Optional[str]:
        """A checkout that never answered may still have created an order:
        look for it before ordering anything again (never order twice)."""
        if not self.unconfirmed_checkout:
            return None
        found = self.open_new_invoice()
        self.unconfirmed_checkout = False
        if found:
            return self.record_order(checkout_url, "（上次结账超时后补查到）", "?", None, True, test)
        return None

    def open_checkout(self, checkout_url: str) -> bool:
        """Load a checkout page, signing in if needed; False if it isn't orderable now."""
        status = self.goto(checkout_url)
        if "/login" in self.page.url:
            if not self.ensure_login():
                self.notify("⚠️ <b>FAC 抢购：有链接但登录失效</b>\n%s" % html.escape(checkout_url))
                return False
            status = self.goto(checkout_url)
        return status != 404 and "/checkout" in urlparse(self.page.url).path  # else sold out / not open

    def read_offer(self, checkout_url: str, test: bool = False) -> Tuple[str, Optional[Dict[str, Any]]]:
        """(result, None) if ``checkout_url`` can't be bought, else ("", offer)."""
        if not self.open_checkout(checkout_url):
            return OrderResult.WAIT, None
        root = self.page.locator('[wire\\:name="products.checkout"]')
        name = re.sub(r"\s+", " ", root.locator("h1").first.inner_text()).strip()
        if not test and not name_matches(name, self.include, self.exclude):
            LOG.info("%s is %r, not a target", checkout_url, name)
            return OrderResult.DROP, None
        options = self.page.eval_on_selector_all(
            'select[wire\\:model\\.live="plan_id"] option',
            "os => os.map(o => [o.value, o.textContent])",
        )
        # A test order may take any readable cycle (public items are monthly).
        rule, plans, why = rank_plans(options, float("inf") if test else self.any_cycle_month)
        LOG.info("%s (%s): %s, plans %s", checkout_url, name, rule, plans or why)
        if not plans:
            self.notify_skip(checkout_url, "找到 %s，但没有符合条件的付款周期，不下单" % name, why)
            return OrderResult.DROP, None
        return "", {"url": checkout_url, "name": name, "rule": rule, "plans": plans}

    def notify_skip(self, checkout_url: str, title: str, why: str) -> None:
        if checkout_url in self.price_notified:
            return
        self.price_notified.add(checkout_url)
        self.notify("💸 <b>FAC：%s</b>\n%s\n%s" % (html.escape(title), html.escape(why), html.escape(checkout_url)))

    def buy_offer(self, offer: Dict[str, Any], test: bool = False) -> str:
        """Order the best plan of an offer from read_offer."""
        checkout_url, name, rule = offer["url"], offer["name"], offer["rule"]
        recovered = self.recover_unconfirmed(checkout_url, test)
        if recovered:
            return recovered
        if urlparse(self.page.url).path.rstrip("/") != urlparse(checkout_url).path.rstrip("/"):
            if not self.open_checkout(checkout_url):
                return OrderResult.WAIT
        value, label, months, _ = offer["plans"][0]
        root = self.page.locator('[wire\\:name="products.checkout"]')
        self.page.select_option('select[wire\\:model\\.live="plan_id"]', value)
        time.sleep(2)
        hostname = self.page.locator('input[name="checkoutConfig.hostname"]')
        if hostname.count() and not hostname.first.input_value():
            hostname.first.fill(str(self.cfg["hostname"]))
            time.sleep(1.5)
        button = root.locator('button[wire\\:click="checkout"]')
        if not button.count() or button.first.is_disabled():
            return OrderResult.WAIT
        total_match = re.search(r"Total today:?\s*(\$\s*[\d.,]+)", self.body_text())
        total = parse_money(total_match.group(1)) if total_match else None
        if total is None:
            total = sum(money_amounts(label)) or None
        LOG.info("checkout %s: %s, %s plan %s, total %s", checkout_url, name, rule, label, total)
        if not test:
            why = ""
            if total is None:
                why = "读不到今日总价"
            elif total > self.max_total:
                why = "今日总价 $%.2f，超过上限 $%g" % (total, self.max_total)
            elif rule == MULTI_CYCLE and total / months > self.any_cycle_month:
                why = "今日总价 $%.2f，折合每月 $%.2f，超过 $%g/月" % (total, total / months, self.any_cycle_month)
            if why:
                self.notify_skip(checkout_url, "找到 %s（%s），但价格不符合，不下单" % (name, label), why)
                return OrderResult.DROP
        self.beat(state="ordering", detail="正在下单 %s %s" % (name, label))
        button.first.click()
        try:
            self.page.wait_for_url(lambda url: "/checkout" not in url, timeout=20000)
        except Exception:
            errors = self.page_errors()
            LOG.warning("checkout did not reach the cart: %s", errors)
            return self.order_failed(checkout_url, name, "加入购物车失败：%s" % errors)
        time.sleep(2)
        if "/cart" not in self.page.url:
            return self.order_failed(checkout_url, name, "没进购物车，停在 %s" % self.page.url)
        return self.checkout_cart(checkout_url, name, label, test)

    def checkout_cart(self, checkout_url: str, name: str, plan_label: str, test: bool) -> str:
        # Leftovers from an earlier failed run would be billed too: keep only
        # the item just added (the last one).
        for _ in range(5):
            removes = self.page.locator('[wire\\:name="cart"] [wire\\:click^="removeProduct"]')
            if removes.count() <= 1:
                break
            removes.first.click()
            time.sleep(2.5)
        data = self.wire_data("cart")
        try:
            cart_total = float(data["total"][0]["price"])
        except (KeyError, IndexError, TypeError, ValueError):
            cart_total = None
        if not test and (cart_total is None or cart_total > self.max_total):
            return self.order_failed(
                checkout_url, name, "购物车总价 %s 超过上限 $%g，已放弃" % (cart_total, self.max_total), final=True
            )
        for code in self.cfg.get("coupons") or []:
            self.page.fill('[wire\\:name="cart"] input[wire\\:model="coupon"]', str(code))
            self.page.click('[wire\\:name="cart"] button[wire\\:click="applyCoupon"]')
            time.sleep(2.5)
        use_credits = bool(self.wire_data("cart").get("use_credits"))
        if test and (self.test_balance is None or cart_total is None or self.test_balance >= cart_total):
            return self.order_failed(
                checkout_url, name,
                "测试单：余额 %s 不低于总价 %s，可能会被扣款，已停下" % (self.test_balance, cart_total),
                final=True,
            )
        tos = self.page.locator('[wire\\:name="cart"] input[type="checkbox"][wire\\:model="tos"]')
        if tos.count() and not tos.first.is_checked():
            tos.first.check()  # deferred wire:model, no round trip
            time.sleep(1)
        # Each finished round trip above (remove item, coupon) reset Turnstile;
        # its callback stores the new token in the cart's ``captcha``.
        time.sleep(2)
        if not self.wait_turnstile():
            return self.order_failed(checkout_url, name, "结账页人机验证没有通过")
        self.page.click('[wire\\:name="cart"] button[wire\\:click="checkout"]')
        # Checkout has hung on this site without an answer; give a busy sale
        # server time, then look for the invoice ourselves. Tell the user early
        # so they can step in by hand meanwhile.
        alert_after = float(self.cfg["checkout_alert_seconds"])
        wait_total = max(float(self.cfg["checkout_wait_seconds"]), alert_after)
        try:
            self.page.wait_for_url(lambda url: "/invoices/" in url, timeout=alert_after * 1000)
        except Exception:
            if not test:
                self.notify(
                    "⏳ <b>FAC 结账卡住了，请你马上上去手动买</b>\n%s · %s\n"
                    "点了 Checkout %d 秒还没回应。先看账单页有没有新账单：有就直接付那张，"
                    "<b>别再下一单</b>；没有就在购物车里自己点结账，或者重新下单。\n"
                    "账单：%s\n购物车：%s\n商品：%s\n程序再等 %d 秒，然后去账单页查，有结果会再发消息。"
                    % (html.escape(name), html.escape(plan_label), alert_after,
                       html.escape(urljoin(self.base_url, "invoices")),
                       html.escape(urljoin(self.base_url, "cart")),
                       html.escape(checkout_url), wait_total - alert_after)
                )
            try:
                self.page.wait_for_url(
                    lambda url: "/invoices/" in url, timeout=(wait_total - alert_after) * 1000
                )
            except Exception:
                errors = self.page_errors()
                self.unconfirmed_checkout = True
                if not self.open_new_invoice():
                    return self.order_failed(checkout_url, name, "结账没有回应：%s" % errors)
        self.unconfirmed_checkout = False
        time.sleep(3)
        return self.record_order(checkout_url, name, plan_label, cart_total, use_credits, test)

    def newest_invoice(self) -> Optional[str]:
        self.goto(urljoin(self.base_url, "invoices"))
        match = re.search(r"Invoice #\s*(\S+)", self.body_text())
        return match.group(1) if match else None

    def open_new_invoice(self) -> bool:
        """After a checkout that never answered: open the invoice it created, if any."""
        newest = self.newest_invoice()
        if not newest or newest == self.known_invoice:
            return False
        LOG.info("checkout created invoice %s after all", newest)
        self.goto(urljoin(self.base_url, "invoices/%s" % newest))
        return True

    def record_order(
        self, checkout_url: str, name: str, plan_label: str, cart_total: Optional[float],
        use_credits: bool, test: bool,
    ) -> str:
        invoice_url = self.page.url.split("?")[0]
        match = INVOICE_PATH.search(invoice_url)
        invoice = match.group(1) if match else invoice_url
        invoice_status, remaining = invoice_state(self.body_text())
        paid = invoice_status.lower() == "paid"
        result = {
            "at": _now_iso(),
            "product": name,
            "plan": plan_label,
            "checkout_url": checkout_url,
            "invoice": invoice,
            "invoice_url": invoice_url,
            "invoice_status": invoice_status,
            "remaining": remaining,
            "cart_total": cart_total,
            "use_credits": use_credits,
            "test": test,
        }
        LOG.info("order created: %s", result)
        if test:
            self.notify(
                "🧪 <b>FAC 抢购测试：已下单（余额不足，不会扣款）</b>\n%s · %s\n账单 %s：%s\n%s"
                % (html.escape(name), html.escape(plan_label), html.escape(invoice),
                   html.escape(invoice_status), html.escape(invoice_url))
            )
            return OrderResult.BOUGHT
        # The order exists: write the marker before anything else can fail.
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        self.marker_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        title = "%s · %s" % (html.escape(name), html.escape(plan_label))
        if not paid:
            # Tell the user first, so they can pay by hand if this attempt stalls too.
            self.notify(
                "🛒 <b>FAC 已下单，但还没付清</b>\n%s\n账单 %s：%s，待付 %s\n"
                "程序正在尝试用余额付款，结果马上再发。你也可以直接上去付：%s"
                % (title, html.escape(invoice), html.escape(invoice_status),
                   html.escape(remaining or "?"), html.escape(invoice_url + "?pay=1"))
            )
            why = self.pay_with_credits(invoice_url)
            try:
                self.goto(invoice_url)
                invoice_status, remaining = invoice_state(self.body_text())
            except Exception:
                LOG.exception("could not re-read invoice %s", invoice)
            paid = invoice_status.lower() == "paid"
            result.update(invoice_status=invoice_status, remaining=remaining, pay_attempt=why)
            self.marker_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        summary = "%s %s 账单 %s %s" % (name, plan_label, invoice, invoice_status)
        if paid:
            self.notify(
                "🎉 <b>FAC 抢到了，已用余额付款</b>\n%s\n账单 %s：已付款\n%s"
                % (title, html.escape(invoice), html.escape(invoice_url))
            )
        else:
            self.notify(
                "🚨 <b>FAC 余额付款没成功，请马上手动付款</b>\n%s\n账单 %s：%s，待付 %s\n原因：%s\n%s"
                % (title, html.escape(invoice), html.escape(invoice_status),
                   html.escape(remaining or "?"), html.escape(why),
                   html.escape(invoice_url + "?pay=1"))
            )
        self.switch.bought(summary)
        return OrderResult.BOUGHT

    def pay_with_credits(self, invoice_url: str) -> str:
        """Best effort: pay an unpaid invoice from the balance. Returns what happened.

        Untested on a real unpaid invoice with enough balance (the page was
        only seen with $0 balance: gateways + "Pay Now", no balance option).
        It only clicks an option that names the balance, never a gateway.
        """
        try:
            self.goto(invoice_url + "?pay=1")
            option = self.page.locator('main label, main button, main [role="radio"]').filter(
                has_text=CREDIT_OPTION
            )
            if not option.count():
                return "付款页没有余额付款选项（余额可能不够）"
            option.first.click()
            time.sleep(2)
            pay = self.page.locator("main button").filter(
                has_text=re.compile(r"pay now|立即付款|確認付款|确认付款", re.I)
            )
            if not pay.count():
                return "付款页找不到 Pay Now 按钮"
            pay.last.click()
            for _ in range(30):
                time.sleep(1)
                if invoice_state(self.body_text())[0].lower() == "paid":
                    return "已用余额付款"
            return "点了余额付款，30 秒内账单还没变成已付款"
        except Exception as exc:  # incl. BlockedError: the user must still hear about it
            LOG.exception("paying %s from the balance failed", invoice_url)
            return "余额付款出错：%s" % exc

    def page_errors(self) -> str:
        try:
            errors = self.page.eval_on_selector_all(
                "main .text-error, main .text-red-500, main .text-red-600, main [role=alert]",
                "els => els.map(e => e.innerText.trim()).filter(Boolean)",
            )
        except Exception:
            errors = []
        return "; ".join(errors[:5]) or "（页面没有错误提示）"

    def order_failed(self, url: str, name: str, why: str, final: bool = False) -> str:
        try:
            self.page.screenshot(path=str(Path(STATUS_DIR) / "fac_last_failure.png"), full_page=True)
        except Exception:
            pass
        count = self.failures.get(url, 0) + 1
        self.failures[url] = count
        give_up = final or count >= int(self.cfg["max_order_failures"])
        self.notify(
            "❌ <b>FAC 下单失败</b>：%s\n%s\n%s\n%s"
            % (html.escape(name), html.escape(why), html.escape(url),
               "已放弃这个链接。" if give_up else "下一轮重试（第 %d 次）。" % count)
        )
        return OrderResult.DROP if give_up else OrderResult.WAIT

    # -- arming and the main loop ------------------------------------------
    def arm(self, urls: Iterable[str], source: str, public: bool) -> None:
        now = time.monotonic()
        until = now + float(self.cfg["arm_minutes"]) * 60
        new = []
        for url in urls:
            entry = self.armed.get(url)
            if entry is None:
                # A TG post is public already: order right away.
                self.armed[url] = {
                    "since": now, "until": until, "public": public, "next": now,
                    "hidden_seen": None,
                }
                self.failures.pop(url, None)
                new.append(url)
            else:
                entry["until"] = max(entry["until"], until)
                if public and not entry["public"]:
                    self.make_public(url, "TG 已发布")
        if new:
            LOG.info("armed by %s (%s): %s", source, "public" if public else "not public yet", new)

    def make_public(self, url: str, why: str) -> float:
        """Mark ``url`` public; returns the wait before ordering it."""
        entry = self.armed[url]
        entry["public"] = True
        now = time.monotonic()
        wait = 0.0
        if why != "TG 已发布":
            wait = public_wait(float(self.cfg["public_delay_seconds"]), entry.get("hidden_seen"), now)
        entry["next"] = now + wait
        LOG.info("%s is public now (%s); ordering in %.1f s", url, why, wait)
        return wait

    def check_public(self, url: str) -> bool:
        """For a link only the site monitor saw: is it in the shop menu yet?"""
        self.goto(self.base_url)
        links = self.page.eval_on_selector_all('a[href*="/products/"]', "els => els.map(e => e.href)")
        if listed_category(url, links, self.base_url):
            wait = self.make_public(url, "已出现在商城菜单")
            self.notify("👀 <b>FAC 活动页已出现在商城菜单</b>，%s\n%s"
                        % ("马上下单" if wait < 0.5 else "%.0f 秒后下单" % wait, html.escape(url)))
            return True
        self.armed[url]["hidden_seen"] = time.monotonic()
        return False

    def interval_for(self, entry: Dict[str, float]) -> float:
        fast = self.switch.interval(float(self.cfg["fast_poll_seconds"]))
        if time.monotonic() - entry["since"] < float(self.cfg["fast_minutes"]) * 60:
            return fast
        return max(fast, float(self.cfg["slow_poll_seconds"]))

    def attempt(self, url: str) -> str:
        kind, target = classify_url(url, self.base_url)
        if kind == "category":
            found, names = self.scan_category(target or url)
            if found is None:
                return OrderResult.WAIT
            LOG.info("category %s open: %d cards, %d matching", url, len(names), len(found))
            # Read every matching product's plans first, then buy the best
            # (user, 2026-11-28: annual-only first, else the cheapest).
            offers = []
            for name, checkout_url in found:
                offer = self.read_offer(checkout_url)[1]
                if offer:
                    offers.append(offer)
            for offer in sorted(offers, key=offer_rank):
                result = self.buy_offer(offer)
                if result == OrderResult.BOUGHT:
                    return result
                if self.unconfirmed_checkout:
                    break  # find out about that checkout before another order
            return OrderResult.WAIT
        if kind == "checkout" and target:
            return self.order(target)
        return OrderResult.DROP

    def poll_armed(self) -> bool:
        """Check every due link once. True once an order was placed."""
        now = time.monotonic()
        for url in list(self.armed):
            if self.armed[url]["until"] < now:
                LOG.info("disarming %s (window over)", url)
                self.armed.pop(url, None)
        if not self.armed:
            return False
        if not self.chrome.running:
            # Warm up as soon as something is armed, i.e. during the
            # public_delay wait, so the order follows the delay directly.
            self.chrome.start()
            self.known_invoice = self.newest_invoice()
        now = time.monotonic()
        due = [url for url, entry in self.armed.items() if entry["next"] <= now]
        for url in due:
            if self.stopping or self.switch.control.paused():
                return False
            entry = self.armed[url]
            try:
                if not entry["public"]:
                    # Hidden so far: never open it while logged in; only
                    # watch the public menu (the monitors already notified).
                    self.beat(state="watching", detail="等 %s 公开（商城菜单或 TG）" % url)
                    if not self.check_public(url):
                        entry["next"] = time.monotonic() + float(self.cfg["hidden_poll_seconds"])
                    continue
                self.beat(state="watching", detail="检查 %s" % url)
                result = self.attempt(url)
            except BlockedError as exc:
                self.notify(
                    "⛔ <b>FAC 抢购：疑似被网站限流</b>\n%s\n暂停 30 分钟。" % html.escape(str(exc))
                )
                for entry in self.armed.values():
                    entry["next"] = time.monotonic() + 1800
                return False
            except Exception as exc:  # a page changed under us; retry next round
                LOG.exception("attempt on %s failed", url)
                result = OrderResult.WAIT
                if self.failures.get(url, 0) == 0:
                    self.notify("⚠️ <b>FAC 抢购出错</b>（会重试）\n%s\n%s" % (
                        html.escape(url), html.escape(str(exc).splitlines()[0][:300])))
                self.failures[url] = self.failures.get(url, 0) + 1
            if result == OrderResult.BOUGHT:
                self.armed.clear()
                return True
            if result == OrderResult.DROP:
                self.armed.pop(url, None)
            elif url in self.armed:
                entry = self.armed[url]
                entry["next"] = time.monotonic() + self.interval_for(entry)
        return False

    def idle_detail(self) -> str:
        if self.armed:
            return "已触发，正在盯 %d 个链接：%s" % (len(self.armed), "、".join(
                url if entry["public"] else "%s（未公开，只看菜单）" % url
                for url, entry in self.armed.items()))
        return "待命：等 FAC TG 公告/群消息或活动分类页开放"

    def login_due(self, every: float) -> bool:
        """Every ``every`` seconds, but not in the quiet hours; once as they end
        (user, 2026-11-28: the 04:13 check moves to 09:05)."""
        quiet = in_quiet_hours(self.cfg["quiet_hours"])
        if quiet != self.quiet:
            self.quiet = quiet
            if quiet:
                LOG.info("quiet hours %s (Taiwan): no login checks until they end", self.cfg["quiet_hours"])
            else:
                LOG.info("quiet hours over: checking the login")
                self.last_login_check = float("-inf")
        return not quiet and time.monotonic() - self.last_login_check >= every

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
                    self.chrome.stop()
                    continue
                if self.switch.rearm():
                    LOG.info("resumed after a purchase; old marker archived")
                for trigger in take_triggers(self.trigger_dir):
                    targets = trigger_targets(trigger, self.base_url, self.cfg["default_urls"])
                    self.arm(targets, str(trigger.get("source")), trigger_is_public(trigger))
                if self.armed:
                    if self.poll_armed():
                        self.chrome.stop()
                        continue
                elif self.login_due(login_every):
                    self.last_login_check = time.monotonic()
                    try:
                        self.chrome.start()
                        self.check_login()
                    except Exception:
                        LOG.exception("login check failed")
                    finally:
                        self.chrome.stop()
                if not self.armed and self.chrome.running:
                    self.chrome.stop()
                self.beat(state="watching", detail=self.idle_detail())
                time.sleep(1)
        finally:
            self.chrome.stop()
            self.beat(state="stopped", detail="进程已退出")
        return 0

    def marker_result(self) -> str:
        try:
            data = json.loads(self.marker_path.read_text("utf-8"))
        except (OSError, ValueError):
            return ""
        return "%s %s 账单 %s %s" % (
            data.get("product"), data.get("plan"), data.get("invoice"), data.get("invoice_status"))

    def _on_signal(self, signum, frame) -> None:  # noqa: ANN001
        self.stopping = True

    # -- one-off commands --------------------------------------------------
    def service_ids(self) -> List[int]:
        self.goto(urljoin(self.base_url, "services"))
        links = self.page.eval_on_selector_all('a[href*="/services/"]', "els => els.map(e => e.href)")
        return sorted({int(m.group(1)) for m in (re.search(r"/services/(\d+)$", l) for l in links) if m})

    def cancel_service(self, service_id: int) -> str:
        self.goto(urljoin(self.base_url, "services/%d" % service_id))
        if "Pending" not in self.body_text():
            return "服务 #%d 不是待开通状态，没有取消" % service_id
        self.page.click("button[wire\\:click=\"$set('showCancel', true)\"]")
        time.sleep(2)
        self.page.select_option('select[wire\\:model="type"]', "immediate")
        self.page.fill('textarea[name="reason"]', "Test order, not needed. 測試訂單，請取消。")
        self.page.click('button[wire\\:click="cancelService"]')
        time.sleep(4)
        self.goto(urljoin(self.base_url, "services/%d" % service_id))
        return "服务 #%d：%s" % (service_id, "已取消" if "Cancelled" in self.body_text() else "取消可能没成功，请手动检查")

    def account_balance(self) -> Optional[float]:
        """The account credit in USD, or None when the page can't be read."""
        self.goto(urljoin(self.base_url, "account/credits"))
        text = self.body_text()
        if "/account/credits" not in self.page.url:
            return None
        if re.search(r"you have no credits", text, re.I):
            return 0.0
        start = text.find("Credits")
        end = text.find("Add credit", start)
        amounts = money_amounts(text[start:end if end > start else None])
        return max(amounts) if amounts else None

    def run_test(self, url: str, cancel_after: bool) -> int:
        """Order ``url`` once without paying: only when the balance is below its total."""
        kind, target = classify_url(url, self.base_url)
        if kind != "checkout" or not target:
            raise SystemExit("--test-url must be a FAC product or checkout link")
        try:
            self.chrome.start()
            if not self.check_login():
                return 1
            self.test_balance = self.account_balance()
            print("account balance:", self.test_balance)
            self.known_invoice = self.newest_invoice()
            before = self.service_ids() if cancel_after else []
            result = self.order(target, test=True)
            print("result:", result)
            if result == OrderResult.BOUGHT and cancel_after:
                new = [sid for sid in self.service_ids() if sid not in before]
                if len(new) == 1:
                    message = self.cancel_service(new[0])
                else:
                    message = "找不到唯一的新服务（%s），请手动取消" % new
                print(message)
                self.notify("🧪 FAC 抢购测试单清理：%s" % html.escape(message))
            return 0 if result == OrderResult.BOUGHT else 1
        finally:
            self.chrome.stop()

    def run_check_login(self) -> int:
        try:
            self.chrome.start()
            ok = self.check_login()
            print("logged in" if ok else "NOT logged in")
            return 0 if ok else 1
        finally:
            self.chrome.stop()


class BlockedError(RuntimeError):
    pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--chrome", help="Chromium executable (default: Playwright's)")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--check-login", action="store_true", help="check/renew the login once and exit")
    parser.add_argument("--test-url", help="order this product once WITHOUT paying (needs balance < total), then exit")
    parser.add_argument("--cancel-after", action="store_true", help="with --test-url: cancel the new service")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = load_config(args.config)
    if not args.test_url and not args.check_login and not (config.get("fac_autobuy") or {}).get("enabled"):
        LOG.info("fac_autobuy is disabled in the config")
        return 0
    if not os.environ.get("DISPLAY"):
        LOG.error("no DISPLAY: run under xvfb-run (Turnstile fails in headless Chromium)")
        return 2
    buyer = FacAutoBuyer(config, args)
    if args.check_login:
        return buyer.run_check_login()
    if args.test_url:
        return buyer.run_test(args.test_url, args.cancel_after)
    return buyer.run()


if __name__ == "__main__":
    raise SystemExit(main())
