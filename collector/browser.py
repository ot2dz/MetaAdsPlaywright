# -*- coding: utf-8 -*-
"""
Browser factory with stealth hardening.

Uses playwright-stealth when available (patched fingerprint, hides webdriver,
fakes plugins/languages/chrome runtime). Falls back to an init-script patch if
the package is not installed, so the scraper still runs.
"""
from __future__ import annotations

import random
from typing import Optional

from .proxies import next_proxy

try:  # pragma: no cover - optional dependency
    from playwright_stealth import Stealth  # type: ignore
    _HAS_STEALTH = True
except Exception:  # pragma: no cover
    Stealth = None  # type: ignore
    _HAS_STEALTH = False


_LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-dev-shm-usage",
]

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
]


def apply_stealth(context) -> None:
    """Best-effort stealth on a BrowserContext."""
    if _HAS_STEALTH:
        try:
            Stealth().apply_stealth_sync(context)
            return
        except Exception:
            pass
    # Fallback: neutralise the most common automation leaks.
    context.add_init_script(
        """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', { get: () => ['ar','fr','en-US','en'] });
        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        window.chrome = window.chrome || { runtime: {} };
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {
          window.navigator.permissions.query = (p) =>
            p && p.name === 'notifications'
              ? Promise.resolve({ state: Notification.permission })
              : origQuery(p);
        }
        """
    )


def new_context(playwright, *, headless: bool = True, proxy: Optional[dict] = None):
    """Create a stealth-patched browser + context. Returns (browser, context)."""
    browser = playwright.chromium.launch(headless=headless, args=_LAUNCH_ARGS)
    px = proxy if proxy is not None else next_proxy()
    ctx = browser.new_context(
        viewport={"width": 1280 + random.randint(0, 640),
                  "height": 768 + random.randint(0, 312)},
        user_agent=random.choice(_USER_AGENTS),
        locale="en-US",
        timezone_id="Africa/Algiers",
        extra_http_headers={"Accept-Language": "ar,fr-FR;q=0.9,en-US;q=0.8,en;q=0.7"},
        **({"proxy": px} if px else {}),
    )
    apply_stealth(ctx)
    return browser, ctx
