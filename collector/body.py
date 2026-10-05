# -*- coding: utf-8 -*-
"""Reliable response-body capture for Playwright sync API.

The sync API's ``response.text()`` can throw when called from the response
event before the body is buffered (a race that happens especially right after
navigation). This helper retries with a short backoff and falls back to
``response.body()`` decoded as UTF-8.
"""
from __future__ import annotations

import time
from typing import Optional


def safe_body(response, retries: int = 3, backoff: float = 0.25) -> Optional[str]:
    """Return the response body as text, or None if it can't be read."""
    for attempt in range(retries):
        try:
            return response.text()
        except Exception:
            pass
        try:
            raw = response.body()
            if isinstance(raw, (bytes, bytearray)):
                return raw.decode("utf-8", errors="replace")
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(backoff)
    return None
