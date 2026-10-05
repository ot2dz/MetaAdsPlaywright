# -*- coding: utf-8 -*-
"""
Push locally-collected ads to a remote MetaAdsPlaywright dashboard.

Run this on the machine that does the scraping (e.g. Windows) after a scrape,
so the central Coolify dashboard receives the data.

Usage:
    python push_to_cloud.py --url https://ads.botdz.com --token <TOKEN>
    # optional: only push ads from the last N hours
    python push_to_cloud.py --url ... --token ... --since-hours 24
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from collector.db import db


def push(url: str, token: str, since_hours: float | None = None) -> dict:
    ads = db.get_ads(limit=100000, stores_only=False)
    if since_hours:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
        def keep(a):
            ts = a.get("collected_at") or ""
            try:
                return datetime.fromisoformat(ts) >= cutoff
            except Exception:
                return True
        ads = [a for a in ads if keep(a)]

    # group by query so the dashboard keeps the keyword association
    by_query: dict[str, list] = {}
    for a in ads:
        by_query.setdefault(a.get("query") or "", []).append(a)

    total_stored = 0
    for query, group in by_query.items():
        body = json.dumps({"query": query, "ads": group}).encode("utf-8")
        req = urllib.request.Request(
            url.rstrip("/") + "/api/ingest",
            data=body,
            headers={"Content-Type": "application/json", "X-Ingest-Token": token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            total_stored += int(data.get("stored", 0))
            print(f"  query={query!r}: received={data.get('received')} stored={data.get('stored')}")

    return {"pushed": len(ads), "stored": total_stored}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Push local ads to the cloud dashboard.")
    ap.add_argument("--url", required=True, help="dashboard base URL, e.g. https://ads.botdz.com")
    ap.add_argument("--token", required=True, help="INGEST_TOKEN configured on the dashboard")
    ap.add_argument("--since-hours", type=float, default=None,
                    help="only push ads collected in the last N hours")
    args = ap.parse_args(argv)

    result = push(args.url, args.token, args.since_hours)
    print(f"\nDone. pushed={result['pushed']} stored={result['stored']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
