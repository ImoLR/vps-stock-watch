"""BOILCLOUD (cloud.boil.network) auto-buyer, driven by the BOIL monitors.

Targets (user, 2026-11-11; rules of 2026-11-24): one plan of each kind,
each a dynamic IP VPS (the text must say 動態; not static, NAT / CGNAT or a
blind box):

* SeedNet (遠傳): an annual-only plan at <= $28.5 a month, or a plan with
  several cycles whose cheapest cycle comes to <= $28.5 a month;
* Japan / USA: an annual-only plan at <= $40 a month, or a plan with
  several cycles whose cheapest cycle comes to < $30 a month (probably only
  with a promo code posted by BOIL).

"Annual-only" = annually on offer and no monthly / quarterly / half-yearly
cycle. The price per month is today's total (price + setup fee, after a
promo code) over the cycle's months. BOIL sells such holiday plans around
Black Friday, Christmas and the new year, named after the zodiac year (2026
马年 HiNet; next one probably 羊年).

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
no browser. Order: cart.php?a=add&pid=N (a store product link redirects
straight to the configure page, with no pid in it) -> confproduct (the
cheapest cycle per month, default options, no paid upgrades) -> promo code
on the cart page, if BOIL posted one -> checkout page (today's total and
balance) -> submit with "apply credit" (the default). A short balance
leaves an unpaid invoice: Telegram sends the pay link at once, the buyer
tries to apply the balance, then reports. Each kind is bought once (one
marker file per kind); with every kind bought the buyer pauses (resume from
Telegram /autobuy).
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
    # Written once every kind is bought (pauses the buyer); each kind also
    # gets its own marker, <stem>_<key><suffix>.
    "marker_file": "data/boil_autobuy_done.json",
    "promo_file": "data/autobuy/boil_promo.json",
    "trigger_dir": "data/autobuy/boil_triggers",
    # What to buy, first match wins. ``include`` is matched against the
    # product name + description (not the group heading: every Taiwan
    # heading lists "HiNet/SeedNet/Kbro"). Caps are USD per month: one for
    # annual-only plans, one for plans that also sell shorter cycles.
    "targets": [
        {"key": "seednet", "label": "SeedNet", "include": ["seednet", "seed net", "遠傳", "远传"],
         "annual_only_month": 28.5, "any_cycle_month": 28.5},
        # Second choice: only when the balance pays it in full, so it never
        # leaves an invoice that would eat SeedNet's money.
        {"key": "jpus", "label": "日本/美國", "include": ["日本", "japan", "美國", "美国", "usa"],
         "annual_only_month": 40.0, "any_cycle_month": 29.99, "need_balance": True},
    ],
    # The IPv4 must be dynamic (user, 2026-11-24; BOIL: whatever doesn't say
    # 動態 is static): a dynamic word about IPv4 or a plain "IP", and no
    # static word about them. Words about IPv6 don't count either way
    # ("IPv6 /64（SLAAC・動態）", "靜態IPv4・動態IPv6"); see ipv4_dynamic().
    "require_words": ["動態", "动态", "dynamic"],
    "static_words": ["靜態", "静态", "static"],
    # And none of these (Latin ones as whole words): NAT, CGNAT, blind boxes.
    "name_exclude": ["nat", "cgnat", "盲盒", "lucky box", "福袋"],
    # A plan offering any of these is not an annual-only plan.
    "short_cycles": ["monthly", "quarterly", "semiannually"],
    # Hard ceiling on today's total, whatever the rules allow.
    "max_total": 480.0,
    # Per product: at most this many cycle / promo code tries.
    "max_tries": 4,
    "hostname": "boilvps",
    # Sale groups worth watching without a target word in the post.
    "sale_groups": ["festive-offer"],
    # A target sale post with a sale word but no store link arms these.
    "default_urls": ["https://cloud.boil.network/store/festive-offer"],
    # Promo codes from BOIL (channel or staff) are kept this long.
    "promo_days": 14,
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
CYCLE_MONTHS = {"monthly": 1, "quarterly": 3, "semiannually": 6, "annually": 12, "biennially": 24, "triennially": 36}
# "優惠碼：XMAS2026", "promo code BF50" ... (Telegram raw text, no markup).
PROMO = re.compile(
    r"(?:優惠碼|优惠码|折扣碼|折扣码|優惠代碼|优惠代码|促銷代碼|促销代码|促銷碼|促销码|promo\s*code|coupon(?:\s*code)?)"
    r"\s*(?:[:：是为為]|is)?\s*[「『\"'`“]?([A-Za-z0-9][A-Za-z0-9_-]{2,31})(?![A-Za-z0-9_-])",
    re.I,
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


def cart_link(base_url: str, item: str) -> str:
    """What to open to put ``item`` (a PID or a store product link) in the cart."""
    return urljoin(base_url, "cart.php?a=add&pid=%s" % item if item.isdigit() else item)


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


def _word_spans(text: str, words: Iterable[str]) -> List[Tuple[int, int]]:
    lowered = text.lower()  # keeps CJK offsets
    spans = []
    for word in words:
        word = word.lower()
        if not word:
            continue
        pattern = re.escape(word)
        if re.fullmatch(r"[a-z ]+", word):
            pattern = r"(?<![a-z])%s(?![a-z])" % pattern
        spans.extend(match.span() for match in re.finditer(pattern, lowered))
    return spans


def ip_family(text: str, start: int, end: int) -> str:
    """Whether the word at text[start:end] is about "v4", "v6" or a plain IP ("any").

    "動態IPv6" / "IPv6：動態" name the family next to the word; inside
    brackets the family is the one named just before them, as in
    "HiNet IPv6 /64（SLAAC・動態）".
    """
    after = re.sub(r"[\s:：]", "", text[end:end + 12]).lower()
    for family in ("v6", "v4"):
        if after.startswith(("ip" + family, family)):
            return family
    before = re.sub(r"[\s:：]", "", text[max(0, start - 12):start]).lower()
    for family in ("v6", "v4"):
        if before.endswith(family):
            return family
    opener = max(text.rfind("（", 0, start), text.rfind("(", 0, start))
    closer = max(text.rfind("）", 0, start), text.rfind(")", 0, start))
    if opener > closer:
        lead = text[max(0, opener - 24):opener].lower()
        v6, v4 = lead.rfind("v6"), lead.rfind("v4")
        if max(v6, v4) >= 0:
            return "v6" if v6 > v4 else "v4"
    return "any"


def ipv4_dynamic(text: str, dynamic_words: Iterable[str], static_words: Iterable[str]) -> bool:
    """True when the text says the IPv4 (or the plain IP) is dynamic and
    never that it is static. What it says about IPv6 doesn't matter."""
    text = text or ""
    dynamic = any(ip_family(text, a, b) != "v6" for a, b in _word_spans(text, dynamic_words))
    static = any(ip_family(text, a, b) != "v6" for a, b in _word_spans(text, static_words))
    return dynamic and not static


def match_target(text: str, cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The first target whose words ``text`` names, if its IPv4 is dynamic and it is clean."""
    if cfg.get("ipv4_rule", True) and not ipv4_dynamic(text, cfg["require_words"], cfg["static_words"]):
        return None
    for target in cfg["targets"]:
        if text_matches(text, target["include"], cfg["name_exclude"]):
            return target
    return None


def target_words(cfg: Dict[str, Any]) -> List[str]:
    return [str(word) for target in cfg["targets"] for word in target["include"]]


def cycle_prices(cycles: Dict[str, str]) -> Dict[str, float]:
    """Listed first payment (price + setup fee) per known cycle, from labels
    like "$288.00 USD 年繳 + $3.00 USD 設定費"."""
    prices: Dict[str, float] = {}
    for value, label in cycles.items():
        amounts = [float(v.replace(",", "")) for v in re.findall(r"\$\s*(-?\d[\d,]*(?:\.\d+)?)", label or "")]
        if value in CYCLE_MONTHS and amounts and amounts[0] >= 0:
            prices[value] = sum(amounts)
    return prices


def plan_cycles(cycles: Dict[str, str], target: Dict[str, Any], short_cycles: Iterable[str],
                promo: bool) -> Tuple[str, float, List[Tuple[str, float]]]:
    """(rule, cap per month, [(cycle, listed per month)] worth trying, cheapest first).

    Annual-only (annually and no short cycle): only annually, at the
    annual-only cap. Otherwise any cycle at the other cap, cheapest per month
    first (a tie goes to the longer cycle). Without a promo code a cycle
    whose listed price is over the cap is not tried.
    """
    prices = cycle_prices(cycles)
    if "annually" in prices and not any(value in cycles for value in short_cycles):
        rule, cap, pool = "只有年付", float(target["annual_only_month"]), ["annually"]
    else:
        rule, cap, pool = "多周期", float(target["any_cycle_month"]), list(prices)
    ranked = sorted(
        ((value, prices[value] / CYCLE_MONTHS[value]) for value in pool if value in prices),
        key=lambda item: (round(item[1], 4), -CYCLE_MONTHS[item[0]]),
    )
    if not promo:
        ranked = [item for item in ranked if item[1] <= cap + 0.005]
    return rule, cap, ranked


def promo_codes(text: str) -> List[str]:
    """Promo codes named in a post, in order, without repeats."""
    found: List[str] = []
    for match in PROMO.finditer(text or ""):
        code = match.group(1)
        if code.lower() not in ("http", "https", "www") and code not in found:
            found.append(code)
    return found


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
    is armed when the post talks about a target, or it is a sale group posted
    on Telegram (the site monitor's trigger carries the cards' text); a
    public group the site monitor reads arms only for a card that is a
    target. A target sale post without a store link arms the default sale
    group; a post that only carries a promo code arms nothing (the buyer
    learns the code). Maintenance notices and regular HKT restocks arm nothing.
    """
    base = str(cfg["base_url"])
    text = str(trigger.get("text") or "")
    source = str(trigger.get("source") or "")
    about_target = mentions(text, target_words(cfg))
    if trigger.get("promo"):
        return []  # a code-only post: the buyer just learns the code
    targets: List[str] = []
    other = False
    for url in trigger.get("urls") or []:
        kind, value = classify_url(str(url), base)
        target = None
        if kind == "pid":
            target = urljoin(base, "cart.php?a=add&pid=%s" % value)
        elif kind == "product":
            target = value
        elif kind == "group" and source == "boilcloud_sale":
            # The site monitor sends the card texts (a public group: one card).
            if (about_target if value in cfg["sale_groups"] else match_target(text, cfg)):
                target = urljoin(base, "store/%s" % value)
        elif kind == "group" and (about_target or value in cfg["sale_groups"]):
            target = urljoin(base, "store/%s" % value)
        elif kind == "other":
            other = True
        if target and target not in targets:
            targets.append(target)
    if not targets and (other or not trigger.get("urls")) and about_target and mentions(text, cfg["sale_words"]):
        targets.extend(url for url in cfg["default_urls"] if url not in targets)
    return targets


def quiet_target(url: str, trigger: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """A regular group armed by the site monitor (if ``watch_groups`` is used):
    misses are logged, not sent (they would repeat for every regular plan)."""
    kind, value = classify_url(url, str(cfg["base_url"]))
    return kind == "group" and value not in cfg["sale_groups"] and trigger.get("source") == "boilcloud_sale"


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


def conf_form(page_html: str, cycle: Optional[str], hostname: str, password: str) -> Tuple[Optional[Dict[str, str]], Dict[str, Any]]:
    """Form data for ``frmConfigureProduct`` with ``cycle`` (None: the page's
    own choice, set by the caller later) and default options.

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
                data[name] = cycle or (options[0][0] if options else "")
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
    if cycle is not None and cycle not in info["cycles"]:
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
        self.targets = list(cfg["targets"])
        self.short_cycles = [str(v) for v in cfg["short_cycles"]]
        self.max_total = float(cfg["max_total"])
        # One-off tests: this cycle only (public test items may lack annually).
        self.force_cycle: Optional[str] = None
        self.marker_path = Path(str(cfg["marker_file"]))
        self.promo_path = Path(str(cfg["promo_file"]))
        self.promos: List[Dict[str, Any]] = self._load_promos()
        # Targets already bought (key -> marker), from their marker files.
        self.done: Dict[str, Dict[str, Any]] = {}
        for target in self.targets:
            try:
                self.done[target["key"]] = json.loads(self.target_marker(target["key"]).read_text("utf-8"))
            except (OSError, ValueError):
                pass
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
        # PIDs / product links that will never match (name, no cycles) ...
        self.dropped: set = set()
        # ... or not at this price (tried again with a new promo code).
        self.price_dropped: set = set()
        self.spacing = float(cfg["order_spacing_seconds"])
        self.cart_dirty = True
        self.last_page_at = float("-inf")
        self.last_login_check = float("-inf")
        self.stopping = False
        self.heartbeat = Heartbeat(
            STATUS_DIR,
            NAME,
            "BOIL SeedNet / 日本美國",
            "动态 IPv4（写明動態且不是 IPv6 的，IPv4 不能写静态；非 NAT/CGNAT/盲盒）各买一台：%s；按今日总价折合每月，"
            "有官方优惠码就用，余额付款" % "；".join(
                "%s 只有年付 ≤ $%g/月、多周期 ≤ $%g/月" % (t["label"], t["annual_only_month"], t["any_cycle_month"])
                for t in self.targets),
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

    def target_marker(self, key: str) -> Path:
        return self.marker_path.with_name("%s_%s%s" % (self.marker_path.stem, key, self.marker_path.suffix))

    def all_done(self) -> bool:
        return all(target["key"] in self.done for target in self.targets)

    def done_summary(self) -> str:
        return "；".join(
            "%s：%s %s 账单 #%s %s" % (target["label"], d.get("product"), d.get("plan"), d.get("invoice"), d.get("invoice_status"))
            for target in self.targets for d in [self.done.get(target["key"])] if d
        )

    def _load_promos(self) -> List[Dict[str, Any]]:
        try:
            items = json.loads(self.promo_path.read_text("utf-8"))
            return [item for item in items if item.get("code")]
        except (OSError, ValueError, AttributeError, TypeError):
            return []

    def active_codes(self) -> List[str]:
        cutoff = time.time() - float(self.cfg["promo_days"]) * 86400
        return [str(item["code"]) for item in self.promos if float(item.get("at") or 0) >= cutoff][:3]

    def learn_codes(self, codes: Iterable[str], source: str) -> None:
        """Remember BOIL's promo codes (newest first); a new one re-opens price misses."""
        known = {str(item["code"]) for item in self.promos}
        new = [code for code in codes if code not in known]
        if not new:
            return
        self.promos = [{"code": code, "at": time.time(), "source": source} for code in reversed(new)] + self.promos
        self.promos = self.promos[:10]
        try:
            self.promo_path.parent.mkdir(parents=True, exist_ok=True)
            self.promo_path.write_text(json.dumps(self.promos, ensure_ascii=False, indent=2), "utf-8")
        except OSError:
            LOG.exception("cannot save promo codes")
        self.price_dropped.clear()
        LOG.info("promo codes from %s: %s", source, new)
        self.notify("🎟 <b>BOIL 优惠码</b> %s\n来自 %s。之后下单都会先填这个码，再按用码后的价格判断。"
                    % (html.escape("、".join(new)), html.escape(source)))

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
    def miss(self, pid: str, name: str, target: Dict[str, Any], rule: str, cap: float,
             why: str, quiet: bool) -> str:
        """Not at this price: tell the user once (unless quiet), keep for a new code."""
        LOG.info("pid %s (%s) misses %s %s <= $%g/month: %s", pid, name, target["label"], rule, cap, why)
        key = "price:%s" % pid
        if not quiet and key not in self.notified:
            self.notified.add(key)
            self.notify(
                "💸 <b>BOIL 抢购：%s 价格不符，没有下单</b>\n%s · %s：折合每月要 ≤ $%g\n%s\n%s"
                % (html.escape(name), html.escape(target["label"]), rule, cap, html.escape(why),
                   html.escape(cart_link(self.base_url, pid)))
            )
        self.price_dropped.add(pid)
        self.empty_cart()
        return OrderResult.DROP

    def add_to_cart(self, pid: str) -> Optional[requests.Response]:
        """Put ``pid`` in an empty cart; its configure page, or None (not orderable)."""
        if self.cart_dirty:
            self.empty_cart()
        self.cart_dirty = True
        response = self.request("GET", cart_link(self.base_url, pid), spacing=self.spacing)
        if "a=confproduct" in response.url:
            return response
        if OUT_OF_STOCK.search(BeautifulSoup(response.text, "lxml").get_text(" ", strip=True)):
            LOG.info("pid %s out of stock", pid)
        else:
            LOG.info("pid %s: no configure page (%s)", pid, response.url)
        return None

    def apply_promo(self, code: str, token: str) -> None:
        reply = self.request("POST", "cart.php?a=view", spacing=self.spacing,
                             data={"token": token, "promocode": code, "validatepromo": "true"})
        # "您填寫的優惠代碼並不存在。" comes as a warning; hidden alerts are page furniture.
        for node in BeautifulSoup(reply.text, "lxml").select(".alert-warning, .alert-danger, .alert-success"):
            if "w-hidden" not in (node.get("class") or []):
                LOG.info("promo code %s: %s", code, node.get_text(" ", strip=True)[:200])

    def order(self, pid: str, mode: str = "live", quiet: bool = False) -> str:
        """Order ``pid`` (a PID or a store product link) if it is a target at
        its price. ``mode``: live, dry (stop before checkout) or tos (submit
        checkout without the TOS box: WHMCS refuses, no order). ``quiet``:
        misses are only logged, and pages keep the normal spacing."""
        self.spacing = float(self.cfg["page_spacing_seconds" if quiet else "order_spacing_seconds"])
        response = self.add_to_cart(pid)
        if response is None:
            return OrderResult.WAIT
        _, info = conf_form(response.text, None, str(self.cfg["hostname"]), random_password())
        name = info["name"] or "PID %s" % pid
        target = match_target("%s %s" % (info["name"], info["description"]), self.cfg)
        if target is None or target["key"] in self.done:
            LOG.info("pid %s (%s) is not a target%s", pid, name, " still to buy" if target else "")
            self.dropped.add(pid)
            self.empty_cart()
            return OrderResult.DROP
        if not info["cycles"]:
            self.dropped.add(pid)
            self.empty_cart()
            return self.order_failed(pid, name, info["why"] or "页面没有付款周期选项", final=True)
        codes = self.active_codes()
        rule, cap, ranked = plan_cycles(info["cycles"], target, self.short_cycles, bool(codes))
        if codes:
            # A code taking off more than two thirds is not worth the requests.
            ranked = [item for item in ranked if item[1] <= 3 * cap]
        if self.force_cycle:
            ranked = [(self.force_cycle, 0.0)] if self.force_cycle in info["cycles"] else []
        if not ranked:
            cheapest = plan_cycles(info["cycles"], target, self.short_cycles, True)[2]
            why = ("最便宜：%s，折合每月 $%.2f" % (info["cycles"][cheapest[0][0]], cheapest[0][1])
                   if cheapest else "没有可选的周期（%s）" % "、".join(info["cycles"].values()))
            return self.miss(pid, name, target, rule, cap, why, quiet)
        tries = [(cycle, code) for code in (codes or [None]) for cycle, _ in ranked][:int(self.cfg["max_tries"])]
        misses: List[str] = []
        for index, (cycle, code) in enumerate(tries):
            if index:
                response = self.add_to_cart(pid)
                if response is None:
                    return OrderResult.WAIT
            data, _ = conf_form(response.text, cycle, str(self.cfg["hostname"]), random_password())
            if data is None:
                continue
            data["i"] = data.get("i") or parse_qs(urlparse(response.url).query).get("i", ["0"])[0]
            reply = self.request("POST", "cart.php?a=confproduct", spacing=self.spacing, data=dict(data, ajax="1"))
            errors = BeautifulSoup(reply.text, "lxml").get_text(" ", strip=True)
            if errors:
                self.empty_cart()
                return self.order_failed(pid, name, "配置被拒：%s" % errors[:200])
            if code:
                self.apply_promo(code, data.get("token", ""))
            page = self.request("GET", "cart.php?a=checkout", spacing=self.spacing)
            form, totals = checkout_form(page.text, accept_tos=(mode != "tos"))
            total, balance = totals["total"], totals["balance"]
            label = info["cycles"].get(cycle, cycle) + (" · 优惠码 %s" % code if code else "")
            LOG.info("pid %s %s [%s]: total %s, balance %s", pid, name, label, total, balance)
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
            per_month = total / CYCLE_MONTHS.get(cycle, 1)
            if (per_month > cap + 0.005 and not self.force_cycle) or total > self.max_total + 0.005:
                misses.append("%s：今日总价 $%.2f，折合每月 $%.2f" % (label, total, per_month))
                self.empty_cart()
                continue
            if target.get("need_balance") and (balance is None or balance + 0.005 < total):
                # Keep the link armed: it is bought once the user tops up.
                key = "balance:%s" % pid
                if key not in self.notified:
                    self.notified.add(key)
                    self.notify(
                        "💰 <b>BOIL 抢购：%s 符合条件，但余额不够，没有下单</b>\n%s · %s：今日总价 $%.2f"
                        "（每月 $%.2f），余额 %s\n%s 要余额全额付清才买（不占 SeedNet 的钱）；充值后程序会自己再试。\n%s"
                        % (html.escape(name), html.escape(target["label"]), html.escape(label), total, per_month,
                           "$%.2f" % balance if balance is not None else "读不到", html.escape(target["label"]),
                           html.escape(cart_link(self.base_url, pid)))
                    )
                self.empty_cart()
                return OrderResult.WAIT
            if mode == "dry":
                print("DRY RUN ok: %s, %s, total $%.2f ($%.2f/month), balance %s, checkout fields %s"
                      % (name, label, total, per_month, balance, sorted(form)))
                self.empty_cart()
                return OrderResult.WAIT
            if mode == "tos":
                result = self.request("POST", "cart.php?a=checkout", spacing=self.spacing, data=form)
                refused = "a=checkout" in result.url and invoice_id(result.url, result.text) is None
                error = BeautifulSoup(result.text, "lxml").select_one(".checkout-error-feedback, .alert-danger")
                print("TOS probe: refused=%s url=%s error=%r" % (
                    refused, result.url, error.get_text(" ", strip=True)[:200] if error else None))
                self.empty_cart()
                return OrderResult.WAIT
            return self.checkout(pid, name, label, form, total, balance, target)
        return self.miss(pid, name, target, rule, cap, "；".join(misses) or "周期选不上", quiet)

    def checkout(self, pid: str, name: str, cycle_label: str, form: Dict[str, str],
                 total: float, balance: Optional[float], target: Dict[str, Any]) -> str:
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
        return self.record_order(pid, name, cycle_label, invoice, total, balance, target)

    def record_order(self, pid: str, name: str, cycle_label: str, invoice: str,
                     total: float, balance: Optional[float], target: Dict[str, Any]) -> str:
        invoice_url = urljoin(self.base_url, "viewinvoice.php?id=%s" % invoice)
        status = invoice_status(self.request("GET", invoice_url).text)
        result = {
            "at": _now_iso(), "target": target["key"], "product": name, "pid": pid, "plan": cycle_label,
            "invoice": invoice, "invoice_url": invoice_url, "invoice_status": status,
            "total": total, "balance_before": balance,
        }
        # The order exists: write the marker before anything else can fail.
        marker = self.target_marker(target["key"])
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.done[target["key"]] = result
        LOG.info("order created: %s", result)
        title = "%s · %s · %s" % (html.escape(target["label"]), html.escape(name), html.escape(cycle_label))
        if status != "Paid":
            self.notify(
                "🛒 <b>BOIL 已下单，但还没付清</b>\n%s\n账单 #%s：%s，今日总价 $%.2f\n"
                "程序正在尝试用余额付款，结果马上再发。你也可以直接上去付：%s"
                % (title, invoice, html.escape(status), total, html.escape(invoice_url))
            )
            why = self.pay_with_credit(invoice_url)
            status = invoice_status(self.request("GET", invoice_url).text)
            result.update(invoice_status=status, pay_attempt=why)
            marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
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
        left = [t["label"] for t in self.targets if t["key"] not in self.done]
        if left:
            self.notify("👉 BOIL 抢购继续等：%s" % html.escape("、".join(left)))
            self.beat(result=self.done_summary())
        else:
            self.marker_path.write_text(json.dumps(
                {"at": _now_iso(), "bought": self.done}, ensure_ascii=False, indent=2), encoding="utf-8")
            self.switch.bought(self.done_summary())
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
               html.escape(cart_link(self.base_url, key)),
               "已放弃这个商品。" if give_up else "下一轮重试（第 %d 次）。" % count)
        )
        return OrderResult.DROP if give_up else OrderResult.WAIT

    # -- armed links -----------------------------------------------------------
    def attempt(self, url: str, quiet: bool = False) -> str:
        kind, value = classify_url(url, self.base_url)
        if kind == "group" and value:
            page = self.request("GET", url)
            if "/store/%s" % value not in unquote(page.url):
                return OrderResult.WAIT
            cards = self.parser.parse_category(page.text, page.url, value)
            order = {target["key"]: index for index, target in enumerate(self.targets)}
            matching = []
            for card in cards:
                target = match_target("%s %s" % (card.name, card.metadata.get("description") or ""), self.cfg)
                if target is not None and target["key"] not in self.done:
                    # Targets in config order; annual cards (likely annual-only) first.
                    matching.append(((order[target["key"]], card.billing_cycle != "annually"), card))
            matching = [card for _, card in sorted(matching, key=lambda item: item[0])]
            LOG.info("group %s: %d cards, %d matching", value, len(cards), len(matching))
            bought = False
            pending = False
            for card in matching:
                if card.product_id in self.dropped or card.product_id in self.price_dropped:
                    continue
                if self.failures.get(card.product_id, 0) >= int(self.cfg["max_order_failures"]):
                    continue
                if card.available is False:
                    # Sold out: wait for it, unless only a monitor or code armed this.
                    pending = pending or not quiet
                    continue
                result = self.order(card.product_id, quiet=quiet)
                if result == OrderResult.BOUGHT:
                    bought = True
                    if self.all_done():
                        break
                elif result == OrderResult.WAIT:
                    pending = True
            if bought:
                return OrderResult.BOUGHT
            if value in self.cfg["sale_groups"] or pending:
                return OrderResult.WAIT
            return OrderResult.DROP  # a regular group with nothing left to try
        if kind == "pid" and value:
            url = value
        if kind not in ("pid", "product") or url in self.dropped or url in self.price_dropped:
            return OrderResult.DROP
        return self.order(url, quiet=quiet)

    def arm(self, urls: Iterable[str], source: str, public: bool, quiet: Iterable[str] = ()) -> None:
        now = time.monotonic()
        until = now + float(self.cfg["arm_minutes"]) * 60
        quiet = set(quiet)
        new = []
        for url in urls:
            entry = self.armed.get(url)
            if entry is None:
                self.armed[url] = {"since": now, "until": until, "public": public, "next": now,
                                   "hidden_seen": None, "quiet": url in quiet}
                new.append(url)
            else:
                entry["until"] = max(entry["until"], until)
                if url not in quiet:
                    entry["quiet"] = False
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
                result = self.attempt(url, quiet=bool(entry.get("quiet")))
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
            if result == OrderResult.BOUGHT and self.all_done():
                self.armed.clear()
                return True
            if result == OrderResult.DROP:
                self.armed.pop(url, None)
            elif url in self.armed:
                entry["next"] = time.monotonic() + self.interval_for(entry)
        return False

    def idle_detail(self) -> str:
        left = "、".join(t["label"] for t in self.targets if t["key"] not in self.done)
        if self.armed:
            return "已触发，正在盯 %d 个链接：%s" % (len(self.armed), "、".join(
                url if entry["public"] else "%s（未公开，只看菜单）" % url
                for url, entry in self.armed.items()))
        return "待命（还要买：%s）：等 BOIL TG 频道/官方账号发链接，或 festive-offer 上架" % left

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
                    for key in list(self.done):
                        marker = self.target_marker(key)
                        if marker.exists():
                            os.replace(str(marker), str(marker.with_name("%s-%s%s" % (
                                marker.stem, datetime.now().strftime("%Y%m%d-%H%M%S"), marker.suffix))))
                    self.done.clear()
                    LOG.info("resumed after a purchase; old markers archived")
                for trigger in take_triggers(self.trigger_dir):
                    source = str(trigger.get("source"))
                    if source.startswith("tg-"):
                        self.learn_codes(promo_codes(str(trigger.get("text") or "")), source)
                    targets = trigger_targets(trigger, self.cfg)
                    if targets:
                        self.arm(targets, source, trigger_is_public(trigger),
                                 [url for url in targets if quiet_target(url, trigger, self.cfg)])
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
        return self.done_summary()

    def _on_signal(self, signum, frame) -> None:  # noqa: ANN001
        self.stopping = True

    # -- one-off commands --------------------------------------------------
    def run_once(self, url: str, mode: str) -> int:
        if not self.check_login():
            print("NOT logged in")
            return 1
        print("logged in; balance:", self.balance())
        kind, value = classify_url(url, self.base_url)
        if kind not in ("pid", "product") or not value:
            raise SystemExit("need a product link or cart.php?a=add&pid=N")
        pid = value
        # Tests use a broad target so a public regular product can stand in.
        self.cfg = dict(self.cfg, ipv4_rule=False, name_exclude=[], targets=[{
            "key": "test", "label": "测试", "include": [""], "annual_only_month": 1e9, "any_cycle_month": 1e9}])
        self.targets = self.cfg["targets"]
        self.done = {}
        if getattr(self.args, "cycle", None):
            self.force_cycle = str(self.args.cycle)
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
