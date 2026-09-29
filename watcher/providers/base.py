from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import requests

from ..models import Change, Product


LOG = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class FetchError(ProviderError):
    def __init__(self, message: str, status: Optional[int] = None, blocked: bool = False):
        super().__init__(message)
        self.status = status
        self.blocked = blocked


class ParseError(ProviderError):
    pass


class BaseProvider(ABC):
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.name = str(config.get("name") or config.get("type") or self.__class__.__name__).lower()
        self.interval = max(10, int(config.get("interval_seconds", 60)))
        self.timeout = max(5, int(config.get("timeout_seconds", 25)))
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.get(
                    "user_agent",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
                ),
                "Accept-Language": config.get("accept_language", "en-US,en;q=0.9"),
            }
        )
        self.session.headers.update(config.get("headers", {}))

    @abstractmethod
    def fetch_products(self) -> List[Product]:
        raise NotImplementedError

    def validate_snapshot(
        self, products: List[Product], previous_state: Dict[str, Any]
    ) -> None:
        """Allow a provider to reject a structurally incomplete snapshot."""

    def additional_changes(
        self, products: List[Product], previous_state: Dict[str, Any]
    ) -> List[Change]:
        """Return provider-specific changes not represented by Product diffing."""
        return []

    def update_state_after_success(
        self, provider_state: Dict[str, Any], products: List[Product]
    ) -> None:
        """Persist provider-specific baseline data in the same atomic state save."""

    def should_notify(self, change: Change) -> bool:
        """Return whether a recorded change should produce a Telegram notification."""
        return not bool(change.product.metadata.get("notification_suppressed", False))

    def get_text(self, url: str, allow_browser: bool = False) -> str:
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FetchError("request failed for %s: %s" % (url, exc)) from exc
        text = response.text
        blocked = response.status_code in (403, 429) or is_challenge_page(text)
        if response.status_code >= 400 or blocked:
            if allow_browser and blocked:
                return self._get_text_browser(url)
            raise FetchError(
                "HTTP %s for %s%s"
                % (response.status_code, url, " (challenge/block page)" if blocked else ""),
                response.status_code,
                blocked,
            )
        if not text.strip():
            raise FetchError("empty response from %s" % url, response.status_code)
        return text

    def get_json(self, url: str) -> Any:
        text = self.get_text(url)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ParseError("invalid JSON from %s: %s" % (url, exc)) from exc

    def _get_text_browser(self, url: str) -> str:
        try:
            from playwright.sync_api import Error as PlaywrightError
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FetchError(
                "site blocked normal HTTP; install Playwright and Chromium for browser fallback",
                blocked=True,
            ) from exc
        LOG.warning("%s: normal HTTP blocked; using Playwright for %s", self.name, url)
        browser = None
        context = None
        page = None
        try:
            with sync_playwright() as playwright:
                headless = bool(self.config.get("browser_headless", True))
                browser = playwright.chromium.launch(
                    headless=headless,
                    args=list(self.config.get("browser_args", [])),
                )
                context_options = {
                    "locale": self.config.get("browser_locale", "en-US"),
                    "service_workers": "block",
                }
                if self.config.get("browser_user_agent"):
                    context_options["user_agent"] = self.config["browser_user_agent"]
                context = browser.new_context(**context_options)
                page = context.new_page()
                blocked_types = set(
                    self.config.get("browser_block_resource_types", ["image", "media", "font"])
                )
                if blocked_types:
                    page.route(
                        "**/*",
                        lambda route: (
                            route.abort()
                            if route.request.resource_type in blocked_types
                            else route.continue_()
                        ),
                    )
                page.goto(url, wait_until="domcontentloaded", timeout=self.timeout * 1000)
                selector = self.config.get("browser_wait_selector")
                if selector:
                    page.wait_for_selector(
                        selector,
                        state=str(self.config.get("browser_wait_state", "attached")),
                        timeout=self.timeout * 1000,
                    )
                page.wait_for_timeout(int(self.config.get("browser_settle_ms", 1500)))
                text = page.content()
        except PlaywrightError as exc:
            raise FetchError("browser fetch failed for %s: %s" % (url, exc), blocked=True) from exc
        finally:
            for resource in (page, context, browser):
                if resource is None:
                    continue
                try:
                    resource.close()
                except PlaywrightError:
                    LOG.debug("%s: browser resource was already closed", self.name)
        if is_challenge_page(text):
            raise FetchError("browser received a challenge page for %s" % url, blocked=True)
        return text


def is_challenge_page(text: str) -> bool:
    lowered = text.lower()
    markers = (
        "attention required! | cloudflare",
        "checking your browser",
        "challenges.cloudflare.com",
        "cf-chl-",
        "why have i been blocked?",
    )
    return any(marker in lowered for marker in markers)
