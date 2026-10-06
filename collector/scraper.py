# -*- coding: utf-8 -*-
"""
Core Playwright-based Meta Ad Library scraper.

Opens the Ads Library in a real Chromium browser, intercepts the internal
GraphQL responses (`/api/graphql/`), and auto-scrolls until no new ads are
loaded. Everything is captured from real browser traffic, so Facebook's
anti-bot pagination block for plain HTTP clients does not apply.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from .browser import new_context
from .body import safe_body
from .rate_limiter import acquire, report_blocked, report_ok, is_block_status, total_block_count
from .store_filter import extract_store_domain

ADS_LIBRARY_URL = "https://www.facebook.com/ads/library/"


@dataclass
class Ad:
    ad_id: str = ""
    page_name: str = ""
    page_id: str = ""
    start_date: str = ""
    is_active: bool = False
    link_url: str = ""
    store_domain: str = ""
    platform: str = ""
    body: str = ""
    cta_text: str = ""
    collation_count: int = 0
    publisher_platforms: list = field(default_factory=list)
    snapshot: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScrapeResult:
    query: str
    country: str
    total_ads: int = 0
    store_ads: int = 0
    unique_stores: int = 0
    reported_count: Optional[int] = None
    graphql_payloads: int = 0
    ads: list = field(default_factory=list)


def build_url(query: str, country: str = "DZ", exact_phrase: bool = False,
              sort_mode: str = "total_impressions",
              languages: Optional[list[str]] = None) -> str:
    q = f'"{query}"' if exact_phrase else query
    search_type = "keyword_exact_phrase" if exact_phrase else "keyword_unordered"
    langs = f"&langs={quote(json.dumps(list(languages)))}" if languages else ""
    return (
        f"{ADS_LIBRARY_URL}?active_status=active&ad_type=all&country={country}"
        f"&is_targeted_country=false&media_type=all&q={quote(q)}"
        f"&search_type={search_type}"
        f"&sort_data[direction]=desc&sort_data[mode]={sort_mode}"
        f"{langs}"
    )


def _iter_json_blocks(body: str):
    """Yield every JSON object in a (possibly streamed) response body."""
    text = body.strip()
    if text.startswith("for (;;);"):
        text = text[9:].strip()
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \n\r\t":
            i += 1
        if i >= n:
            break
        try:
            obj, end = dec.raw_decode(text, i)
        except Exception:
            break
        yield obj
        i = end


def _find_connection(body: str):
    """Return the search_results_connection dict from a GraphQL body."""
    for obj in _iter_json_blocks(body):
        data = obj.get("data") if isinstance(obj, dict) else None
        if not isinstance(data, dict):
            continue
        alm = data.get("ad_library_main") or data.get("adLibraryMain")
        if isinstance(alm, dict):
            conn = alm.get("search_results_connection") or alm.get("searchResultsConnection")
            if isinstance(conn, dict):
                return conn
    return None


def _build_ad(col: dict) -> Ad:
    snap = col.get("snapshot") or {}
    link = col.get("link_url") or snap.get("link_url") or ""
    domain, platform = extract_store_domain(link)
    body_obj = snap.get("body") or {}
    body_text = body_obj.get("text", "") if isinstance(body_obj, dict) else str(body_obj)
    start = col.get("start_date")
    return Ad(
        ad_id=str(col.get("ad_archive_id") or ""),
        page_name=col.get("page_name") or snap.get("page_name") or "",
        page_id=str(col.get("page_id") or ""),
        start_date=str(start) if start else "",
        is_active=bool(col.get("is_active")),
        link_url=link,
        store_domain=domain or "",
        platform=platform or "",
        body=body_text,
        cta_text=snap.get("cta_text") or "",
        collation_count=int(col.get("collation_count") or 1),
        publisher_platforms=col.get("publisher_platforms") or [],
        snapshot=snap,
    )


def _collect_ad_nodes(obj, depth: int = 0, out: Optional[list] = None) -> list:
    """Walk the whole parsed tree and collect every dict carrying ad_archive_id.

    Meta wraps search results in different envelopes (data.ad_library_main in
    GraphQL responses, __bbox module dumps in the SSR HTML). Keying off the
    ad_archive_id marker instead of a fixed path survives both — and any future
    rename of the surrounding structure.
    """
    if out is None:
        out = []
    if depth > 20 or obj is None:
        return out
    if isinstance(obj, dict):
        if "ad_archive_id" in obj:
            out.append(obj)
            return out
        for v in obj.values():
            _collect_ad_nodes(v, depth + 1, out)
    elif isinstance(obj, list):
        for n in obj:
            _collect_ad_nodes(n, depth + 1, out)
    return out


def _iter_ads_from_json(json_obj) -> list[Ad]:
    return [_build_ad(n) for n in _collect_ad_nodes(json_obj)]


def _parse_ads_from_connection(conn: dict) -> list[Ad]:
    out: list[Ad] = []
    for edge in conn.get("edges") or []:
        node = edge.get("node", {}) if isinstance(edge, dict) else {}
        for col in (node.get("collated_results") or []):
            out.append(_build_ad(col))
    return out


def _iter_ads_from_html(html: str) -> list[Ad]:
    """Extract the SSR-embedded ads from <script data-sjs> tags in the page HTML."""
    if "ad_archive_id" not in html:
        return []
    out: list[Ad] = []
    for match in re.finditer(r"<script[^>]+data-sjs[^>]*>([\s\S]*?)</script>", html, re.IGNORECASE):
        content = match.group(1)
        if "ad_archive_id" not in content:
            continue
        try:
            out.extend(_iter_ads_from_json(json.loads(content)))
        except Exception:
            for obj in _iter_json_blocks(content):
                out.extend(_iter_ads_from_json(obj))
    return out


def scrape(
    query: str = "",
    country: str = "DZ",
    *,
    url: Optional[str] = None,
    headless: bool = True,
    exact_phrase: bool = False,
    sort_mode: str = "total_impressions",
    languages: Optional[list[str]] = None,
    max_scrolls: int = 800,
    stable_rounds: int = 12,
    scroll_pause_ms: int = 1500,
    stores_only: bool = False,
    progress: Optional[Callable[[str], None]] = None,
    on_ad: Optional[Callable[[Ad], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
) -> ScrapeResult:
    """Scrape every ad Facebook loads for `query` using a real browser.

    If `url` is provided, it is used verbatim (exact Ad Library URL) and the
    other query/sort params are ignored.
    """
    log = progress or (lambda msg: None)

    target_url = url or build_url(query, country=country, exact_phrase=exact_phrase,
                                  sort_mode=sort_mode, languages=languages)

    ads: dict[str, Ad] = {}
    payloads = 0
    reported: Optional[int] = None
    blocked = {"flag": False}
    raw_html = {"text": ""}

    def _ingest_ads(new_ads: list[Ad]) -> None:
        for ad in new_ads:
            if ad.ad_id and ad.ad_id not in ads:
                ads[ad.ad_id] = ad
                if on_ad:
                    try:
                        on_ad(ad)
                    except Exception:
                        pass

    def _ingest_body(body: str) -> None:
        """Ingest any GraphQL response body: count via the known path, ads via
        a structural deep-walk so renamed wrappers don't hide results."""
        nonlocal reported
        conn = _find_connection(body)
        if conn:
            c = conn.get("count")
            if isinstance(c, int):
                reported = c if reported is None else max(reported, c)
        for obj in _iter_json_blocks(body):
            _ingest_ads(_iter_ads_from_json(obj))

    def on_response(resp):
        nonlocal payloads
        try:
            url = resp.url
            is_graphql = "/api/graphql/" in url
            is_page = "ads/library" in url and not is_graphql
            if not is_graphql and not is_page:
                return
            # NOTE: response.status() is broken by playwright-stealth in the
            # sync API ('int' object is not callable), so we filter by URL and
            # detect rate-limits from the body instead.
            if is_graphql:
                body = safe_body(resp)
                if not body:
                    return
                if "search_results_connection" in body or "ad_archive_id" in body:
                    payloads += 1
                    report_ok()
                    _ingest_body(body)
                return
            # Page HTML: keep the biggest ads/library document that carries data,
            # in case an early redirect/chrome response also mentions ad_archive_id.
            if is_page:
                body = safe_body(resp)
                if body and "ad_archive_id" in body and len(body) > len(raw_html["text"]):
                    raw_html["text"] = body
                    report_ok()
        except Exception:
            return

    with sync_playwright() as p:
        browser, ctx = new_context(p, headless=headless)
        page = ctx.new_page()
        page.on("response", on_response)

        log(f"Opening Ad Library for {query!r} ...")
        acquire()
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # 1) Pull the initial batch straight out of the SSR HTML.
        if raw_html["text"]:
            _ingest_ads(_iter_ads_from_html(raw_html["text"]))
            log(f"SSR HTML batch: {len(ads)} ads")

        # Meta's on-page "N results" label is the best reference count when the
        # connection payload doesn't carry one.
        if reported is None:
            try:
                m = re.search(r"([\d.,]+)\s*(?:results|نتيجة|نتائج)",
                              page.evaluate("document.body.innerText || ''"))
                if m:
                    reported = int(m.group(1).replace(",", "").replace(".", ""))
            except Exception:
                pass

        # 2) Nudge the page so Facebook fires the first search GraphQL call.
        for _ in range(10):
            if ads:
                break
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1500)
        if not ads:
            log("Waiting a bit longer for the first results to load ...")
            page.wait_for_timeout(5000)

        last, stable = 0, 0
        for i in range(max_scrolls):
            if should_stop and should_stop():
                log("Stopped by request.")
                break
            acquire()
            page.mouse.wheel(0, 30000)
            page.wait_for_timeout(scroll_pause_ms)
            cur = len(ads)
            if i % 10 == 0:
                log(f"scroll {i}: ads={cur} | payloads={payloads}")
            if cur == last:
                stable += 1
                if stable >= stable_rounds:
                    log("No new ads — reached the end.")
                    break
            else:
                stable = 0
            last = cur

        browser.close()

    if blocked["flag"]:
        log(f"Meta rate-limited some requests (total blocks: {total_block_count()}).")

    all_ads = list(ads.values())
    store_ads = [a for a in all_ads if a.store_domain]
    selected = store_ads if stores_only else all_ads

    return ScrapeResult(
        query=query,
        country=country,
        total_ads=len(all_ads),
        store_ads=len(store_ads),
        unique_stores=len({a.store_domain for a in store_ads}),
        reported_count=reported,
        graphql_payloads=payloads,
        ads=selected,
    )
