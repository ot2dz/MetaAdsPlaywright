# -*- coding: utf-8 -*-
"""
Meta Ads Collector — Playwright Edition
=======================================
Scrapes the Facebook Ad Library with a real headless browser (no LLM, no
GraphQL doc-ids to maintain). Opens the Ad Library page, auto-scrolls to the
end, captures the internal GraphQL responses, and returns every ad Facebook
loads for the query.

Key advantages over the HTTP engine:
  - Real browser fingerprint -> bypasses the deep-pagination block that
    silently truncates plain HTTP requests from datacenter IPs.
  - No dependency on hardcoded doc_ids or relay tokens.
  - Reaches the full result set (scrolls until the count stabilises).
"""

__version__ = "1.0.0"
