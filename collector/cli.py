# -*- coding: utf-8 -*-
"""Command-line interface for the Playwright-based collector."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from .scraper import scrape


def _write_output(result, out_path: Path, fmt: str):
    if fmt == "json":
        data = {
            "query": result.query,
            "country": result.country,
            "total_ads": result.total_ads,
            "store_ads": result.store_ads,
            "unique_stores": result.unique_stores,
            "reported_count": result.reported_count,
            "graphql_payloads": result.graphql_payloads,
            "ads": [a.to_dict() for a in result.ads],
        }
        out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    elif fmt == "csv":
        cols = ["ad_id", "page_name", "page_id", "start_date", "is_active",
                "store_domain", "platform", "cta_text", "link_url", "body"]
        with out_path.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for ad in result.ads:
                w.writerow(ad.to_dict())
    else:  # txt / jsonl
        with out_path.open("w", encoding="utf-8") as fh:
            for ad in result.ads:
                fh.write(json.dumps(ad.to_dict(), ensure_ascii=False) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="meta-ads-pw",
        description="Scrape the Meta Ad Library with a real browser (Playwright).",
    )
    ap.add_argument("query", help="keyword or phrase to search")
    ap.add_argument("-c", "--country", default="DZ")
    ap.add_argument("-n", "--max-scrolls", type=int, default=800)
    ap.add_argument("--exact-phrase", action="store_true", help="treat query as an exact phrase")
    ap.add_argument("--stores-only", action="store_true", help="keep only ads with a store link")
    ap.add_argument("--sort", default="total_impressions",
                    choices=["total_impressions", "relevancy_monthly_grouped"])
    ap.add_argument("-o", "--output", default=None, help="output file path")
    ap.add_argument("-f", "--format", default="txt", choices=["txt", "json", "csv"])
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    args = ap.parse_args(argv)

    def progress(msg):
        print(f"  {msg}", flush=True)

    print(f"Scraping: {args.query!r} [{args.country}]")
    result = scrape(
        args.query,
        country=args.country,
        headless=not args.headed,
        exact_phrase=args.exact_phrase,
        sort_mode=args.sort,
        max_scrolls=args.max_scrolls,
        stores_only=args.stores_only,
        progress=progress,
    )

    print("\n" + "=" * 60)
    print(f"Total ads scraped : {result.total_ads}")
    print(f"Ads with a store  : {result.store_ads}")
    print(f"Unique stores     : {result.unique_stores}")
    print(f"Reported count    : {result.reported_count}")
    print(f"GraphQL payloads  : {result.graphql_payloads}")
    print("=" * 60)

    if args.output:
        out = Path(args.output)
        _write_output(result, out, args.format)
        print(f"Saved -> {out} ({len(result.ads)} ads)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
