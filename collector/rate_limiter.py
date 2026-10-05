# -*- coding: utf-8 -*-
"""
Meta request backoff (reactive by default).

Normally acquire() returns immediately — no global pacing. We only slow down
*after* Meta actually pushes back (HTTP 429/403), at which point a shared
cooldown makes every request wait it out, then lifts once Meta serves cleanly.

Env:
  META_RATE_PER_SEC   opt-in sustained requests/sec across everything (unset = off)
  META_RATE_BURST     bucket capacity for short bursts (default 8, only if rate set)
"""
from __future__ import annotations

import random
import threading
import time

from .config import env_float, env_int

_RATE_PER_SEC = env_float("META_RATE_PER_SEC", 0.0) or None
if _RATE_PER_SEC is not None and _RATE_PER_SEC <= 0:
    _RATE_PER_SEC = None
_BURST = env_int("META_RATE_BURST", 8)

_BASE_BACKOFF_MS = 2_000
_MAX_BACKOFF_MS = 60_000
_MAX_BLOCK_EXP = 5
_BLOCK_RESET_S = 45

_lock = threading.Lock()
_consecutive = 0
_cooldown_until = 0.0
_last_block_at = 0.0
_total_blocks = 0

# optional token bucket
_bucket_tokens = float(_BURST)
_bucket_last = time.monotonic()


def _cooldown_remaining() -> float:
    return max(0.0, _cooldown_until - time.monotonic())


def report_blocked(source: str = "") -> None:
    global _consecutive, _cooldown_until, _last_block_at, _total_blocks
    with _lock:
        now = time.monotonic()
        if _last_block_at and now - _last_block_at > _BLOCK_RESET_S:
            _consecutive = 0
        _consecutive = min(_MAX_BLOCK_EXP, _consecutive + 1)
        _total_blocks += 1
        raw = min(_MAX_BACKOFF_MS, _BASE_BACKOFF_MS * (2 ** (_consecutive - 1)))
        jittered = raw * (0.7 + random.random() * 0.6) / 1000.0
        _cooldown_until = max(_cooldown_until, now + jittered)
        _last_block_at = now


def report_ok() -> None:
    global _consecutive
    with _lock:
        _consecutive = 0


def total_block_count() -> int:
    return _total_blocks


def _take_bucket() -> None:
    global _bucket_tokens, _bucket_last
    if _RATE_PER_SEC is None:
        return
    while True:
        now = time.monotonic()
        _bucket_tokens = min(_BURST, _bucket_tokens + (now - _bucket_last) * _RATE_PER_SEC)
        _bucket_last = now
        if _bucket_tokens >= 1:
            _bucket_tokens -= 1
            return
        time.sleep(max(0.05, (1 - _bucket_tokens) / _RATE_PER_SEC))


def acquire() -> None:
    """Wait until it's safe to make one request to Meta."""
    while True:
        cd = _cooldown_remaining()
        if cd <= 0:
            break
        time.sleep(min(cd, 1.5))
    _take_bucket()


def is_block_status(status: int) -> bool:
    """Only HTTP 429/403 counts as a real rate-limit."""
    return status in (429, 403)
