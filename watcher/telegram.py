from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from math import ceil
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from .models import Change, ChangeType, Product


LOG = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, chat_id: str, timeout: int = 20):
        self.chat_id = str(chat_id)
        self.timeout = timeout
        self.base_url = "https://api.telegram.org/bot%s" % token
        self.session = requests.Session()

    def send(
        self, text: str, reply_markup: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        request: Dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            request["reply_markup"] = reply_markup
        result = self._post("sendMessage", request)
        return result if isinstance(result, dict) else {}

    def answer_callback(
        self, callback_query_id: str, text: Optional[str] = None, show_alert: bool = False
    ) -> None:
        request: Dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert,
        }
        if text:
            request["text"] = text
        self._post("answerCallbackQuery", request)

    def _post(self, method: str, request: Dict[str, Any]) -> Any:
        try:
            response = self.session.post(
                self.base_url + "/" + method,
                json=request,
                timeout=self.timeout,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            # requests exceptions can contain the full bot-token URL.
            raise TelegramError("Telegram request failed (%s)" % type(exc).__name__) from None
        if response.status_code >= 400 or not payload.get("ok"):
            raise TelegramError("Telegram request failed: HTTP %s" % response.status_code)
        return payload.get("result")

    def commands(self, offset: int) -> List[Dict[str, Any]]:
        try:
            response = self.session.get(
                self.base_url + "/getUpdates",
                params={
                    "offset": offset,
                    "timeout": 0,
                    "allowed_updates": '["message","callback_query"]',
                },
                timeout=self.timeout,
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise TelegramError("Telegram getUpdates failed (%s)" % type(exc).__name__) from None
        if response.status_code >= 400 or not payload.get("ok"):
            raise TelegramError("Telegram getUpdates failed: HTTP %s" % response.status_code)
        return list(payload.get("result", []))

    def set_commands(self, commands: List[Dict[str, str]]) -> None:
        self._post("setMyCommands", {"commands": commands})

    def bot_commands(self) -> List[Dict[str, str]]:
        result = self._post("getMyCommands", {})
        return list(result) if isinstance(result, list) else []


INTERVAL_MENU_CALLBACK = "scan_interval:menu"
INTERVAL_BACK_CALLBACK = "scan_interval:back"
INTERVAL_PROVIDER_PREFIX = "scan_interval:provider:"
STOCK_MENU_CALLBACK = "stock:menu"
STOCK_PROVIDER_PREFIX = "stock:provider:"
STOCK_PAGE_SIZE = 5

BOT_COMMANDS = [
    {"command": "status", "description": "查看监控状态"},
    {"command": "stock", "description": "查询当前可购买库存"},
]


def status_menu_markup() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "⏱ 设置扫描间隔", "callback_data": INTERVAL_MENU_CALLBACK}]
        ]
    }


def is_currently_available(product: Dict[str, Any]) -> bool:
    if product.get("available") is False:
        return False
    stock = product.get("stock")
    if isinstance(stock, int) and not isinstance(stock, bool):
        return stock > 0
    return product.get("available") is True


def is_hidden_inventory(product: Dict[str, Any]) -> bool:
    metadata = product.get("metadata")
    if not isinstance(metadata, dict):
        return False
    discovery = metadata.get("discovery")
    if isinstance(discovery, dict) and isinstance(discovery.get("hidden"), bool):
        return discovery["hidden"]
    # Compatibility for snapshots written before the unified discovery object.
    return bool(
        metadata.get("hidden_public")
        or metadata.get("configured_extra_pid")
    )


def available_products(provider_state: Dict[str, Any]) -> List[Dict[str, Any]]:
    products = provider_state.get("products", {})
    if not isinstance(products, dict):
        return []
    result = [item for item in products.values() if isinstance(item, dict) and is_currently_available(item)]
    return sorted(
        result,
        key=lambda item: (
            0 if is_hidden_inventory(item) else 1,
            str(item.get("region") or "").casefold(),
            str(item.get("category") or "").casefold(),
            str(item.get("name") or "").casefold(),
            str(item.get("product_id") or ""),
        ),
    )


def format_stock_menu(state: Dict[str, Any], provider_names: List[str]) -> str:
    lines = ["<b>📦 当前可购买库存</b>", "", "请选择网站："]
    providers = state.get("providers", {})
    for name in provider_names:
        count = len(available_products(providers.get(name, {})))
        lines.append("%s：%d" % (html.escape(_provider_name(name)), count))
    return "\n".join(lines)


def stock_menu_markup(
    state: Dict[str, Any], provider_names: List[str]
) -> Dict[str, Any]:
    providers = state.get("providers", {})
    rows = []
    for name in provider_names:
        count = len(available_products(providers.get(name, {})))
        rows.append(
            [
                {
                    "text": "%s · %d" % (_provider_name(name), count),
                    "callback_data": STOCK_PROVIDER_PREFIX + name + ":0",
                }
            ]
        )
    rows.append([{"text": "🔄 刷新", "callback_data": STOCK_MENU_CALLBACK}])
    return {"inline_keyboard": rows}


def format_stock_provider(
    state: Dict[str, Any],
    name: str,
    interval: int,
    page: int,
    page_size: int = STOCK_PAGE_SIZE,
    now: Optional[datetime] = None,
) -> Tuple[str, int, int]:
    provider = state.get("providers", {}).get(name, {})
    products = available_products(provider)
    hidden = sum(1 for item in products if is_hidden_inventory(item))
    normal = len(products) - hidden
    page_count = max(1, int(ceil(len(products) / float(page_size))))
    current_page = min(max(0, page), page_count - 1)
    start = current_page * page_size
    shown = products[start : start + page_size]

    lines = [
        "<b>🟢 %s 当前可购买库存</b>" % html.escape(_provider_name(name)),
        "",
        "最近成功扫描：%s" % _time(provider.get("last_success_at")),
    ]
    if _is_stale(provider.get("last_success_at"), interval, now):
        lines.append("⚠️ 数据可能已过期")
    lines.extend(
        [
            "当前可购买：%d" % len(products),
            "🟢 正常库存：%d" % normal,
            "🕵️ 隐藏库存：%d" % hidden,
            "",
            "第 %d / %d 页" % (current_page + 1, page_count),
        ]
    )
    if not shown:
        lines.extend(["", "当前没有可购买商品。"])
    for item in shown:
        lines.extend(["", "<b>%s</b>" % ("🕵️ 隐藏库存" if is_hidden_inventory(item) else "🟢 正常库存")])
        lines.append("<b>%s</b>" % html.escape(str(item.get("name") or item.get("product_id") or "未命名商品")))
        _append(lines, "地区", item.get("region"))
        _append(lines, "分类", item.get("category"))
        stock = item.get("stock")
        if isinstance(stock, int) and not isinstance(stock, bool) and stock > 0:
            lines.append("库存：%d" % stock)
        else:
            lines.append("库存：有货")
        _append(lines, "价格", item.get("price"))
        _append(lines, "周期", item.get("billing_cycle"))
        lines.append("ID / PID：%s" % html.escape(str(item.get("product_id") or "未提供")))
        url = item.get("url")
        if url:
            lines.append(
                '<a href="%s">🛒 购买 / 查看商品</a>'
                % html.escape(str(url), quote=True)
            )
        else:
            lines.append("购买链接：未提供")
    return "\n".join(lines), current_page, page_count


def stock_provider_markup(name: str, page: int, page_count: int) -> Dict[str, Any]:
    rows = []
    navigation = []
    if page > 0:
        navigation.append(
            {
                "text": "← 上一页",
                "callback_data": STOCK_PROVIDER_PREFIX + name + ":%d" % (page - 1),
            }
        )
    if page + 1 < page_count:
        navigation.append(
            {
                "text": "下一页 →",
                "callback_data": STOCK_PROVIDER_PREFIX + name + ":%d" % (page + 1),
            }
        )
    if navigation:
        rows.append(navigation)
    rows.append([{"text": "← 返回网站列表", "callback_data": STOCK_MENU_CALLBACK}])
    return {"inline_keyboard": rows}


def format_interval_menu(intervals: Dict[str, int], provider_names: List[str]) -> str:
    lines = ["<b>⏱ 扫描间隔设置</b>", ""]
    for name in provider_names:
        lines.append("%s：%s 秒" % (html.escape(_provider_name(name)), intervals[name]))
    lines.extend(["", "请选择要修改的商家："])
    return "\n".join(lines)


def interval_menu_markup(provider_names: List[str]) -> Dict[str, Any]:
    rows = [
        [
            {
                "text": _provider_name(name),
                "callback_data": INTERVAL_PROVIDER_PREFIX + name,
            }
        ]
        for name in provider_names
    ]
    rows.append([{"text": "返回", "callback_data": INTERVAL_BACK_CALLBACK}])
    return {"inline_keyboard": rows}


def format_interval_prompt(name: str, current: int, minimum: int, maximum: int) -> str:
    return "\n".join(
        [
            "<b>⏱ 设置 %s 扫描间隔</b>" % html.escape(_provider_name(name)),
            "",
            "当前：%s 秒" % current,
            "",
            "请直接输入新的扫描间隔（秒）。",
            "允许范围：%s–%s 秒。" % (minimum, maximum),
            "",
            "发送“取消”可退出设置。",
        ]
    )


def format_interval_success(name: str, old: int, new: int) -> str:
    return "\n".join(
        [
            "<b>✅ 设置成功</b>",
            "",
            html.escape(_provider_name(name)),
            "%s 秒 → %s 秒" % (old, new),
            "",
            "新的扫描间隔已经生效。",
        ]
    )


def format_interval_invalid(minimum: int, maximum: int) -> str:
    return "\n".join(
        [
            "输入无效，请输入 %s–%s 之间的整数秒数。" % (minimum, maximum),
            "发送“取消”可退出设置。",
        ]
    )


def format_change(change: Change, suspicious_keywords: Iterable[str]) -> str:
    product, old = change.product, change.old
    provider = _provider_name(product.provider)
    headings = {
        ChangeType.NEW: "🆕 %s 发现新品" % provider,
        ChangeType.RESTOCK: "🔥 %s 补货" % provider,
        ChangeType.SOLD_OUT: "🔴 %s 售罄" % provider,
        ChangeType.STOCK: "📦 %s 库存变化" % provider,
        ChangeType.PRICE: "💰 %s 价格变化" % provider,
        ChangeType.NAME: "✏️ %s 商品名称变化" % provider,
        ChangeType.REMOVED: "🗑 %s 商品下架" % provider,
    }
    lines = ["<b>%s</b>" % html.escape(headings[change.type]), ""]
    lines.append("名称：%s" % html.escape(product.name))
    lines.append("ID / PID：%s" % html.escape(product.product_id))
    if change.type == ChangeType.NAME and old:
        lines.append("原名称：%s" % html.escape(old.name))
    _append(lines, "类型", product.category)
    _append(lines, "地区", product.region)
    _append(lines, "价格", product.price)
    _append(lines, "付款周期", product.billing_cycle)
    for key, label in (
        ("cpu", "CPU"), ("ram", "RAM"), ("disk", "硬盘"),
        ("traffic", "流量"), ("bandwidth", "带宽"),
        ("ipv4", "IPv4"), ("ipv6", "IPv6"),
    ):
        _append(lines, label, product.specs.get(key))
    if change.type == ChangeType.RESTOCK:
        lines.append("状态：无货 → 有货")
    elif change.type == ChangeType.SOLD_OUT:
        lines.append("状态：有货 → 无货")
    elif product.available is not None:
        lines.append("状态：%s" % ("有货" if product.available else "无货"))
    if old and "stock" in change.fields:
        lines.append("库存：%s → %s" % (old.stock, product.stock))
    elif product.stock is not None:
        lines.append("库存：%s" % product.stock)
    if change.type == ChangeType.PRICE and old:
        if "price" in change.fields:
            lines.append("价格变化：%s → %s" % (_shown(old.price), _shown(product.price)))
        if "billing_cycle" in change.fields:
            lines.append(
                "付款周期变化：%s → %s"
                % (_shown(old.billing_cycle), _shown(product.billing_cycle))
            )
    if change.type == ChangeType.NEW and _suspicious(product, suspicious_keywords):
        lines.extend(["", "⚠️ 疑似测试商品"])
    if product.url:
        lines.extend(["", '<a href="%s">购买 / 查看商品</a>' % html.escape(product.url, quote=True)])
    return "\n".join(lines)


def format_status(
    state: Dict[str, Any], provider_intervals: Optional[Dict[str, int]] = None
) -> str:
    providers = state.get("providers", {})
    all_products = [item for provider in providers.values() for item in provider.get("products", {}).values()]
    available = sum(1 for item in all_products if item.get("available") is True)
    healthy = bool(providers) and all(
        provider.get("last_success_at") and not provider.get("last_error")
        for provider in providers.values()
    )
    successes = [
        provider.get("last_success_at")
        for provider in providers.values()
        if provider.get("last_success_at")
    ]
    lines = [
        "<b>%s VPS Stock Watch 状态</b>" % ("✅" if healthy else "⚠️"),
        "",
        "程序状态：%s" % ("正常" if healthy else "异常或尚未完成首次检查"),
        "最近成功：%s" % _time(max(successes) if successes else None),
    ]
    lines.append("Provider 数量：%s" % len(providers))
    lines.append("商品数量：%s" % len(all_products))
    lines.append("当前有货：%s" % available)
    lines.append("累计发现新品：%s" % state.get("counters", {}).get("new_products", 0))
    lines.append("累计通知：%s" % state.get("counters", {}).get("notifications", 0))
    for name, provider in sorted(providers.items()):
        provider_products = list(provider.get("products", {}).values())
        provider_available = sum(
            1 for item in provider_products if item.get("available") is True
        )
        provider_healthy = bool(provider.get("last_success_at")) and not provider.get("last_error")
        lines.extend(
            [
                "",
                "<b>%s</b>" % html.escape(_provider_name(name)),
                "状态：%s" % ("正常" if provider_healthy else "异常或尚未完成首次检查"),
                "最近检查：%s" % _time(provider.get("last_check_at")),
                "最近成功：%s" % _time(provider.get("last_success_at")),
                "商品：%s" % len(provider_products),
                "有货：%s" % provider_available,
            ]
        )
        if provider_intervals and name in provider_intervals:
            lines.append("扫描间隔：%s 秒" % provider_intervals[name])
        lines.append("最近错误：%s" % html.escape(str(provider.get("last_error") or "无")))
    return "\n".join(lines)


def _append(lines: List[str], label: str, value: Optional[str]) -> None:
    if value:
        lines.append("%s：%s" % (label, html.escape(str(value))))


def _shown(value: Optional[str]) -> str:
    return html.escape(str(value)) if value is not None else "未提供"


def _suspicious(product: Product, keywords: Iterable[str]) -> bool:
    haystack = "%s %s" % (product.name, product.metadata.get("description", ""))
    lowered = haystack.casefold()
    return any(str(keyword).casefold() in lowered for keyword in keywords)


def _time(value: Optional[str]) -> str:
    if not value:
        return "从未"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    except ValueError:
        return html.escape(value)


def _is_stale(
    value: Optional[str], interval: int, now: Optional[datetime] = None
) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current.astimezone(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds() > 2 * interval


def _provider_name(value: str) -> str:
    return {
        "nexkr": "NexKr",
        "dmit": "DMIT",
        "blossom": "Blossom Host",
        "boilcloud": "BOILCLOUD",
        "fachost": "FACHOST",
        "leikwanhost": "LeiKwanHost",
    }.get(value, value.title())
