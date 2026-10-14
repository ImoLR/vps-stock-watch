"""One-shot NexKr auto-buyer.

Polls the public NexKr catalog and, as soon as the target product is in
stock in at least one zone not flagged sold out, places an order with the
signed-in session cookie and pays it from the account balance. While every
zone is flagged sold out it only watches and never calls the order API. It buys at most once: after any order is created the process
writes a marker file and exits, whether or not the payment went through.

Login on nexkr.sh is behind reCAPTCHA, so the session comes from a browser
Cookie header saved to a file. Order creation and balance payment need no
captcha.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from .autobuy_status import DEFAULT_DIR as STATUS_DIR, Heartbeat
from .config import telegram_credentials
from .telegram import TelegramClient, TelegramError


LOG = logging.getLogger("nexkr_autobuy")

SITE = "https://nexkr.sh"
API = SITE + "/api/v1"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
SESSION_CHECK_SECONDS = 600
RELOGIN_MIN_SECONDS = 900
BALANCE_ERROR_HINTS = ("balance", "insufficient", "fund", "余额", "不足")
MANUAL_LOGIN_NOTICE = (
    "\n🔒 程序已停止自动登录，你可以放心自己登录网站（充值/手动付款），不会被程序顶号。"
)

# Runs inside the real login page: the same reCAPTCHA v3 call and login POST
# the SPA makes, so the session cookie lands in the browser context.
LOGIN_SCRIPT = """
async ([email, password]) => {
  const config = await (await fetch('/api/v1/config')).json();
  const captcha = config.captcha || {};
  let token = '';
  if (captcha.enabled && captcha.site_key) {
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://www.google.com/recaptcha/api.js?render=' + encodeURIComponent(captcha.site_key);
      s.onload = () => window.grecaptcha.ready(resolve);
      s.onerror = () => reject(new Error('captcha script failed to load'));
      document.head.appendChild(s);
    });
    token = await window.grecaptcha.execute(captcha.site_key, {action: 'login'});
  }
  const response = await fetch('/api/v1/auth/login', {
    method: 'POST',
    credentials: 'include',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email, password, captcha_token: token}),
  });
  return {status: response.status, body: await response.text()};
}
"""


class LoginError(RuntimeError):
    pass


def browser_login(email: str, password: str, headless: bool, timeout: int = 60) -> str:
    """Sign in through Chromium and return the resulting Cookie header value."""
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = browser.new_context(user_agent=USER_AGENT, locale="zh-CN")
                page = context.new_page()
                page.set_default_timeout(timeout * 1000)
                page.goto(SITE + "/app/auth", wait_until="networkidle")
                result = page.evaluate(LOGIN_SCRIPT, [email, password])
                cookies = context.cookies(SITE)
            finally:
                browser.close()
    except PlaywrightError as exc:
        raise LoginError("browser failed: %s" % str(exc).splitlines()[0]) from None
    try:
        body = json.loads(result.get("body") or "{}")
    except ValueError:
        body = {}
    if result.get("status") != 200:
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        raise LoginError(
            "login rejected (%s %s): %s"
            % (result.get("status"), error.get("code", ""), error.get("message", ""))
        )
    if body.get("totp_required"):
        raise LoginError("account has 2FA (TOTP) enabled; automatic login is not supported")
    if not cookies:
        raise LoginError("login succeeded but no cookies were set")
    return "; ".join("%s=%s" % (c["name"], c["value"]) for c in cookies)


class ApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__("%s %s: %s" % (status, code, message))
        self.status = status
        self.code = code
        self.message = message


class NexKrClient:
    def __init__(self, cookie: str, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Origin": SITE,
                "Referer": SITE + "/app/shop",
            }
        )
        self.set_cookie(cookie)

    def set_cookie(self, cookie: str) -> None:
        self.session.headers["Cookie"] = cookie

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        try:
            response = self.session.request(
                method, API + path, json=body, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ApiError(0, "unreachable", type(exc).__name__) from None
        payload: Any = None
        if response.text:
            try:
                payload = response.json()
            except ValueError:
                payload = None
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else None
            error = error if isinstance(error, dict) else {}
            raise ApiError(
                response.status_code,
                str(error.get("code") or "http_%s" % response.status_code),
                str(error.get("message") or response.text[:200]),
            )
        return payload

    def catalog(self) -> Dict[str, Any]:
        # The catalog is public; skip the cookie so polling never touches the session.
        try:
            response = requests.get(
                API + "/shop/groups",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ApiError(0, "catalog", type(exc).__name__) from None

    def me(self) -> Dict[str, Any]:
        return self.request("GET", "/auth/me") or {}

    def pay_methods(self) -> Dict[str, Any]:
        return self.request("GET", "/pay/methods") or {}

    def create_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.request("POST", "/orders", body) or {}

    def pay_balance(self, order_no: str) -> Dict[str, Any]:
        return self.request("POST", "/orders/%s/pay-balance" % order_no) or {}


def find_product(
    catalog: Dict[str, Any], slug: str
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    for group in catalog.get("groups") or []:
        if not isinstance(group, dict):
            continue
        for product in group.get("products") or []:
            if isinstance(product, dict) and product.get("slug") == slug:
                return group, product
    return None, None


def candidate_zones(
    group: Dict[str, Any], product: Dict[str, Any], preferred: Sequence[str] = ()
) -> List[Dict[str, Any]]:
    """Zones the shop offers for this product, sold out or not, best first.

    Mirrors the shop page: a product without its own zone list is sold in
    every zone of its group. An empty result means none of the product's
    zones exist in the group (or the group has no zones at all).
    """
    group_zones = [z for z in group.get("zones") or [] if isinstance(z, dict)]
    product_zone_ids = product.get("zones") or [z.get("id") for z in group_zones]
    zones = [z for z in group_zones if z.get("id") in product_zone_ids]
    rank = {slug: index for index, slug in enumerate(preferred)}
    zones.sort(key=lambda z: rank.get(z.get("slug"), len(rank)))
    return zones


def buyable_zones(
    group: Dict[str, Any], product: Dict[str, Any], preferred: Sequence[str] = ()
) -> Optional[List[Dict[str, Any]]]:
    """Zones worth ordering in right now, or None while the product can't be bought.

    ``in_stock`` alone is not enough: the shop reported in_stock with its only
    zone in ``zone_sold_out`` for days (2026-10-11..13), and every order then
    failed with 409. The zone list is read fresh from each catalog poll, so a
    product moved to another zone is picked up automatically. An empty list
    means the group has no zones at all and the order goes out without one.
    """
    if not product.get("in_stock"):
        return None
    zones = candidate_zones(group, product, preferred)
    if not zones:
        # Zone ids the group doesn't describe can't be named in an order, and
        # an order without a zone is rejected whenever the product has zones.
        return None if product.get("zones") or group.get("zones") else []
    sold_out = set(product.get("zone_sold_out") or [])
    return [z for z in zones if z.get("id") not in sold_out] or None


def stock_status(group: Dict[str, Any], product: Dict[str, Any]) -> str:
    sold_out = set(product.get("zone_sold_out") or [])
    zones = ", ".join(
        "%s%s" % (z.get("slug"), " (sold out)" if z.get("id") in sold_out else "")
        for z in candidate_zones(group, product)
    )
    return "in_stock=%s zones=%s zone_sold_out=%s -> %s" % (
        bool(product.get("in_stock")),
        product.get("zones"),
        product.get("zone_sold_out"),
        zones or "no known zone",
    )


def order_body(
    product: Dict[str, Any], zone: Optional[Dict[str, Any]], os_image: Optional[str]
) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "product_slug": product["slug"],
        "cycle_months": product.get("cycle_months") or 1,
        "codes": [],
    }
    if zone is not None:
        body["zone"] = zone["slug"]
    images = product.get("images") or []
    if images:
        body["os"] = os_image if os_image in images else images[0]
    return body


class AutoBuyer:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cookie_path = Path(args.cookie_file)
        self.marker_path = Path(args.marker_file)
        self.manual_login_path = Path(
            getattr(args, "manual_login_file", None) or "data/nexkr_manual_login"
        )
        self.email = os.environ.get("NEXKR_EMAIL", "").strip()
        self.password = os.environ.get("NEXKR_PASSWORD", "")
        self.client = NexKrClient(self.read_cookie())
        self.telegram: Optional[TelegramClient] = None
        credentials = telegram_credentials()
        if credentials["token"] and credentials["chat_id"]:
            self.telegram = TelegramClient(credentials["token"], credentials["chat_id"])
        self.session_ok = False
        self.stopping = False
        self.last_order_failure_notice = float("-inf")
        self.last_login_attempt = float("-inf")
        self.heartbeat: Optional[Heartbeat] = None
        if not args.dry_run:
            self.heartbeat = Heartbeat(
                getattr(args, "status_dir", None) or STATUS_DIR,
                "nexkr",
                "NexKr 国庆活动",
                "商品 %s，余额付款" % args.slug,
                args.interval,
            )

    def beat(self, **values: Any) -> None:
        if self.heartbeat is not None:
            self.heartbeat.update(**values)

    @property
    def can_login(self) -> bool:
        return bool(self.email and self.password) and not self.manual_login

    @property
    def manual_login(self) -> bool:
        # While this file exists the user is on the site themselves, so never
        # sign in (a new login could kick their session). Delete it to resume.
        return self.manual_login_path.exists()

    def hand_over_login(self) -> None:
        self.manual_login_path.parent.mkdir(parents=True, exist_ok=True)
        self.manual_login_path.write_text(
            datetime.now(timezone.utc).isoformat() + "\n", encoding="utf-8"
        )
        LOG.info("automatic login disabled; %s created", self.manual_login_path)

    def read_cookie(self) -> str:
        try:
            cookie = self.cookie_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            cookie = ""
        if cookie.lower().startswith("cookie:"):
            cookie = cookie[len("cookie:"):].strip()
        if not cookie and not (self.email and self.password):
            raise SystemExit(
                "no cookie in %s and NEXKR_EMAIL/NEXKR_PASSWORD are not set" % self.cookie_path
            )
        return cookie

    def write_cookie(self, cookie: str) -> None:
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.cookie_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(cookie + "\n")
        os.chmod(str(self.cookie_path), 0o600)

    def relogin(self) -> bool:
        """Sign in with the stored account; throttled so a bad password can't hammer the site."""
        if not self.can_login:
            return False
        if time.monotonic() - self.last_login_attempt < RELOGIN_MIN_SECONDS:
            return False
        self.last_login_attempt = time.monotonic()
        LOG.info("signing in as %s through the browser", self.email)
        try:
            cookie = browser_login(self.email, self.password, headless=self.args.browser_headless)
        except LoginError as exc:
            LOG.warning("automatic login failed: %s", exc)
            self.notify(
                "⚠️ <b>NexKr 自动登录失败</b>\n%s\n%s 分钟后重试。"
                % (html.escape(str(exc)), RELOGIN_MIN_SECONDS // 60)
            )
            return False
        self.write_cookie(cookie)
        return True

    def notify(self, text: str) -> None:
        LOG.info("notify: %s", text.replace("\n", " | "))
        if self.telegram is None:
            return
        try:
            self.telegram.send(text)
        except TelegramError as exc:
            LOG.warning("telegram send failed: %s", exc)

    def check_session(self, announce: bool, allow_login: bool = True) -> bool:
        """Verify the cookie; reload it from disk first so it can be swapped live."""
        try:
            cookie = self.read_cookie()
            if not cookie:
                raise ApiError(401, "no_cookie", "no saved session")
            self.client.set_cookie(cookie)
            me = self.client.me()
        except ApiError as exc:
            if exc.status in (401, 403):
                if allow_login and self.relogin():
                    return self.check_session(announce, allow_login=False)
                if self.can_login or self.manual_login:
                    # relogin() already reported why it failed or was throttled,
                    # or the user is signing in themselves.
                    self.session_ok = False
                    return False
                if self.session_ok or announce:
                    self.notify(
                        "⚠️ <b>NexKr 自动购买：登录已失效</b>\n"
                        "请重新从浏览器复制 Cookie 写入 <code>%s</code>，程序会自动重新加载。"
                        % html.escape(str(self.cookie_path))
                    )
                self.session_ok = False
                return False
            LOG.warning("session check failed: %s", exc)
            return self.session_ok
        user = me.get("user") if isinstance(me.get("user"), dict) else me
        label = user.get("email") or user.get("phone") or user.get("id") or "?"
        if not self.session_ok or announce:
            balance: Any = "?"
            if isinstance(user.get("balance_cents"), int):
                balance = "$%.2f" % (user["balance_cents"] / 100)
            else:
                try:
                    balance = self.client.pay_methods().get("balance", "?")
                except ApiError as exc:
                    LOG.warning("balance lookup failed: %s", exc)
            self.notify(
                "🟢 <b>NexKr 自动购买已就绪</b>\n"
                "账号：%s\n余额：%s\n目标：<code>%s</code>\n轮询：每 %s 秒"
                % (
                    html.escape(str(label)),
                    html.escape(str(balance)),
                    html.escape(self.args.slug),
                    self.args.interval,
                )
            )
        self.session_ok = True
        return True

    def write_marker(self, data: Dict[str, Any]) -> None:
        self.marker_path.parent.mkdir(parents=True, exist_ok=True)
        data["at"] = datetime.now(timezone.utc).isoformat()
        self.marker_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def try_buy(self, group: Dict[str, Any], product: Dict[str, Any]) -> bool:
        """Attempt the purchase. Returns True once an order exists (stop polling)."""
        zones = buyable_zones(group, product, self.args.zone)
        if zones is None:
            return False
        attempts: List[Optional[Dict[str, Any]]] = list(zones) or [None]
        errors = []
        for zone in attempts:
            body = order_body(product, zone, self.args.os)
            LOG.info("creating order %s", body)
            if self.args.dry_run:
                self.notify("🧪 dry-run：会提交订单 <code>%s</code>" % html.escape(json.dumps(body)))
                return True
            try:
                result = self.client.create_order(body)
            except ApiError as exc:
                LOG.warning("order failed in zone %s: %s", body.get("zone"), exc)
                errors.append("%s: %s" % (body.get("zone") or "-", exc))
                if exc.status in (401, 403):
                    self.session_ok = False
                    break
                continue
            order = result.get("order") or {}
            order_no = str(order.get("order_no") or "")
            link = "%s/app/order/%s" % (SITE, order_no)
            self.write_marker({"order": order, "request": body})
            self.beat(state="done", result="已下单 %s，余额付款中" % order_no)
            try:
                paid = self.client.pay_balance(order_no).get("order") or {}
            except ApiError as exc:
                self.hand_over_login()
                self.beat(state="done", result="已下单 %s，余额支付失败，需手动付款" % order_no)
                self.notify(
                    "⚠️ <b>NexKr 已下单但余额支付失败</b>\n订单：%s\n区域：%s\n错误：%s\n"
                    "请尽快手动支付：%s%s"
                    % (
                        html.escape(order_no),
                        html.escape(str(body.get("zone") or "-")),
                        html.escape(str(exc)),
                        html.escape(link),
                        MANUAL_LOGIN_NOTICE,
                    )
                )
                return True
            self.write_marker({"order": paid or order, "request": body, "paid": True})
            self.beat(state="done", result="已抢到并付款，订单 %s" % order_no)
            self.notify(
                "✅ <b>NexKr 抢购成功并已付款</b>\n订单：%s\n区域：%s\n状态：%s\n%s"
                % (
                    html.escape(order_no),
                    html.escape(str(body.get("zone") or "-")),
                    html.escape(str(paid.get("status") or "paid")),
                    html.escape(link),
                )
            )
            return True
        text = "\n".join(errors) or "unknown"
        if not self.manual_login and any(h in text.lower() for h in BALANCE_ERROR_HINTS):
            # The user has to top up on the site; keep ordering with the
            # current cookie but stop signing in so they aren't kicked out.
            self.hand_over_login()
            self.last_order_failure_notice = time.monotonic()
            self.notify(
                "💰 <b>NexKr 有货但余额不足，下单失败</b>，程序继续用当前会话重试\n%s%s"
                % (html.escape(text), MANUAL_LOGIN_NOTICE)
            )
            return False
        # Keep retrying every poll, but only report failures once a minute.
        if time.monotonic() - self.last_order_failure_notice >= 60:
            self.last_order_failure_notice = time.monotonic()
            self.notify("❌ <b>NexKr 有货但下单失败</b>，继续重试\n%s" % html.escape(text))
        return False

    def run(self) -> int:
        if self.marker_path.exists():
            # Exit cleanly so a Restart=on-failure unit does not loop.
            LOG.error("marker %s exists; already bought once, refusing to run", self.marker_path)
            self.beat(state="done", result=marker_result(self.marker_path))
            return 0
        self.beat(state="starting")
        if not self.check_session(announce=True) and not self.args.dry_run:
            if not self.can_login and not self.manual_login:
                LOG.error("session cookie is not valid")
                return 1
            # Keep polling; relogin() retries on its own throttle.
            LOG.warning("not signed in yet; will keep retrying the login")
        last_session_check = time.monotonic()
        last_failure_notice = float("-inf")
        last_status = None
        while not self.stopping:
            if time.monotonic() - last_session_check >= SESSION_CHECK_SECONDS:
                self.check_session(announce=False)
                last_session_check = time.monotonic()
            try:
                group, product = find_product(self.client.catalog(), self.args.slug)
            except ApiError as exc:
                LOG.warning("catalog fetch failed: %s", exc)
                self.beat(state="watching", detail="商品目录获取失败：%s" % exc)
                time.sleep(self.args.interval)
                continue
            if product is None:
                LOG.warning("product %s not in catalog", self.args.slug)
                self.beat(state="watching", detail="商品不在目录中")
                time.sleep(self.args.interval)
                continue
            group = group or {}
            status = stock_status(group, product)
            if status != last_status:
                LOG.info("stock status: %s", status)
                last_status = status
            buyable = buyable_zones(group, product, self.args.zone) is not None
            self.beat(
                state="ordering" if buyable else "watching",
                detail="%s%s"
                % (
                    "有货" if buyable else "无货（全部区域售罄或下架）",
                    "" if self.session_ok else "，登录失效",
                ),
            )
            if buyable and not self.session_ok and self.can_login:
                # Stock is up: retry the login now instead of waiting out the
                # usual throttle, but still no more than once a minute.
                if time.monotonic() - self.last_login_attempt >= 60:
                    self.last_login_attempt = float("-inf")
                self.check_session(announce=False)
            if buyable and (self.session_ok or self.args.dry_run):
                LOG.info("IN STOCK: %s", self.args.slug)
                if self.try_buy(group, product):
                    return 0
            elif buyable and time.monotonic() - last_failure_notice > 300:
                last_failure_notice = time.monotonic()
                handed_over = not self.manual_login
                if handed_over:
                    self.hand_over_login()
                self.notify(
                    "🚨 NexKr 目标已有货，但登录失效无法下单！请立即手动购买。%s"
                    % (MANUAL_LOGIN_NOTICE if handed_over else "")
                )
            time.sleep(self.args.interval)
        self.beat(state="stopped")
        return 0


def marker_result(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "已下单（详情见 %s）" % path
    order = data.get("order") or {}
    return "订单 %s%s" % (
        order.get("order_no") or "?",
        "，已付款" if data.get("paid") else "，未付款",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--slug", default="kr-2026-nationalday-event")
    parser.add_argument("--cookie-file", default="data/nexkr_cookie.txt")
    parser.add_argument("--marker-file", default="data/nexkr_autobuy_done.json")
    parser.add_argument(
        "--manual-login-file",
        default="data/nexkr_manual_login",
        help="while this file exists the bot never signs in (the user is on the site)",
    )
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument(
        "--zone",
        action="append",
        default=[],
        help="preferred zone slug, repeatable (goyang2, seoul3, seoul4); others are fallbacks",
    )
    parser.add_argument("--os", default=None, help="preferred OS image if the product offers images")
    parser.add_argument("--status-dir", default=STATUS_DIR, help="heartbeat dir read by the bot")
    parser.add_argument("--dry-run", action="store_true", help="never create an order")
    parser.add_argument(
        "--browser-headless",
        action="store_true",
        help="run the login browser headless (default: headed, needs xvfb-run)",
    )
    parser.add_argument(
        "--login-only",
        action="store_true",
        help="sign in with NEXKR_EMAIL/NEXKR_PASSWORD, save the cookie and exit",
    )
    args = parser.parse_args(argv)
    args.interval = max(args.interval, 2.0)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    buyer = AutoBuyer(args)
    if args.login_only:
        if not buyer.can_login:
            LOG.error("NEXKR_EMAIL and NEXKR_PASSWORD are required for --login-only")
            return 1
        if not buyer.relogin():
            return 1
        buyer.client.set_cookie(buyer.read_cookie())
        try:
            profile = buyer.client.me()
            LOG.info("signed in; /auth/me -> %s", json.dumps(profile, ensure_ascii=False)[:500])
        except ApiError as exc:
            LOG.error("login cookie saved but /auth/me failed: %s", exc)
            return 1
        return 0

    def stop(*_: Any) -> None:
        buyer.stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    return buyer.run()


if __name__ == "__main__":
    sys.exit(main())
