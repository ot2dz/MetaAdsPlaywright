# -*- coding: utf-8 -*-
"""Helpers for advertiser-page monitoring (`view_all_page_id`)."""
from __future__ import annotations

import re
from typing import Optional

ADS_LIBRARY_BASE = "https://www.facebook.com/ads/library/"


def extract_page_id(text: str) -> Optional[str]:
    """Extract a Meta page id from a URL, an Ad Library page URL, or a raw id."""
    s = (text or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d{6,}", s):
        return s
    m = re.search(r"(?:view_all_page_id|page_id)=(\d{6,})", s, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"-(\d{8,})/?(\?|$)", s)
    if m:
        return m.group(1)
    m = re.search(r"facebook\.com/(?:ads/library/\?[^ ]*)?(\d{9,})", s, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(\d{9,})", s)
    return m.group(1) if m else None


def page_url(page_id: str, country: str = "DZ", active_status: str = "active") -> str:
    return (
        f"{ADS_LIBRARY_BASE}?active_status={active_status}&ad_type=all"
        f"&country={country}&is_targeted_country=false&media_type=all"
        f"&view_all_page_id={page_id}&search_type=page"
    )
