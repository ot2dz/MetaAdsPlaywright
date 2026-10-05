# -*- coding: utf-8 -*-
"""
Optional proxy rotation (fully opt-in).

With no proxies configured the scraper connects directly, exactly as before.
Supply proxies via either:
  - env  META_SCRAPER_PROXIES = comma-separated list
  - file <project>/proxies.txt  = one proxy per line (# comments allowed)

Each entry: [scheme://][user:pass@]host:port  (scheme defaults to http).
Proxies are handed out round-robin per browser context.
"""
from __future__ import annotations

import itertools
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, unquote

from .config import env

_POOL: Optional[list] = None
_CYCLE = None


def _load() -> list:
    global _POOL, _CYCLE
    if _POOL is not None:
        return _POOL

    out: list = []
    raw_env = env("META_SCRAPER_PROXIES")
    if raw_env:
        out.extend(p.strip() for p in raw_env.split(",") if p.strip())

    f = Path(__file__).resolve().parent.parent / "proxies.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)

    _POOL = out
    _CYCLE = itertools.cycle(out) if out else None
    return _POOL


def has_proxies() -> bool:
    return len(_load()) > 0


def parse_proxy(raw: str) -> Optional[dict]:
    """Return Playwright proxy dict {server, username?, password?} or None."""
    raw = (raw or "").strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"http://{raw}"
    try:
        u = urlparse(candidate)
    except Exception:
        return {"server": candidate}
    if not u.hostname:
        return {"server": candidate}
    server = f"{u.scheme or 'http'}://{u.hostname}:{u.port}" if u.port else f"{u.scheme or 'http'}://{u.hostname}"
    res: dict = {"server": server}
    if u.username:
        res["username"] = unquote(u.username)
    if u.password:
        res["password"] = unquote(u.password)
    return res


def next_proxy() -> Optional[dict]:
    """Next proxy in round-robin, or None when none are configured."""
    pool = _load()
    if not pool:
        return None
    return parse_proxy(next(_CYCLE))
