# -*- coding: utf-8 -*-
"""
Search sweeping: expand coverage of a Meta Ad Library search by running it
several times with different parameters and unioning the results.

Facebook's keyword search pagination recycles after a few thousand ads and
then stops (`has_next_page=false`) even though the advertised count is much
larger. Different sort modes return largely *different* ads, so unioning
several passes recovers far more of the library.
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit

SORT_MODES = ["total_impressions", "relevancy_monthly_grouped"]


def _strip_param(pairs: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
    return [(k, v) for (k, v) in pairs if k != name]


def with_params(base_url: str, **params) -> str:
    """Return base_url with the given query params set (overwriting existing)."""
    parts = urlsplit(base_url)
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    for key, value in params.items():
        if value is None:
            pairs = _strip_param(pairs, key)
        else:
            pairs = _strip_param(pairs, key)
            pairs.append((key, value))
    return urlunsplit(parts._replace(query=urlencode(pairs)))


def month_windows(months_back: int = 12) -> list[tuple[str, str] | None]:
    """Return (min,max) date windows, one per month, plus an open 'all' window."""
    windows: list[tuple[str, str] | None] = [None]  # None = no date restriction
    today = date.today()
    y, m = today.year, today.month
    for _ in range(months_back):
        first = date(y, m, 1)
        if m == 12:
            nxt = date(y + 1, 1, 1)
        else:
            nxt = date(y, m + 1, 1)
        last = nxt - timedelta(days=1)
        windows.append((first.isoformat(), last.isoformat()))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return windows


def build_pass_urls(base_url: str, sorts: list[str] | None = None,
                    windows: list[tuple[str, str] | None] | None = None) -> list[dict]:
    """Build the list of passes: each is {url, sort, window}."""
    sorts = sorts or SORT_MODES
    windows = windows if windows is not None else month_windows(12)
    passes: list[dict] = []
    for sort in sorts:
        for win in windows:
            params = {"sort_data[mode]": sort, "sort_data[direction]": "desc"}
            if win is None:
                params["start_date[min]"] = None
                params["start_date[max]"] = None
            else:
                params["start_date[min]"] = win[0]
                params["start_date[max]"] = win[1]
            passes.append({"url": with_params(base_url, **params), "sort": sort, "window": win})
    return passes
