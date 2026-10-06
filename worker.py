# -*- coding: utf-8 -*-
"""
MetaAdsPlaywright worker agent.

Runs on the machine that HAS a browser (e.g. Windows). It polls the central
dashboard for queued scrape jobs, executes them locally with Playwright, and
posts the results back. This is what lets you control scraping from the
dashboard even though the dashboard host has no Chromium.

Usage:
    python worker.py --url https://ads.botdz.com --token <WORKER_TOKEN>
    python worker.py --url https://ads.botdz.com --token <TOKEN> --poll 10
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from collector.scraper import scrape


def _req(url: str, token: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "X-Worker-Token": token},
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        body = resp.read().decode("utf-8")
        return resp.status, (json.loads(body) if body.strip() else None)


def run_job(base_url: str, token: str, job: dict) -> dict:
    query = job.get("query", "")
    country = job.get("country") or "DZ"
    exact = bool(job.get("exact_phrase"))
    stores_only = bool(job.get("stores_only"))
    sort_mode = job.get("sort_mode") or "total_impressions"
    print(f"  ▶ سحب: {query!r} (exact={exact}, stores_only={stores_only})")

    result = scrape(query, country=country, exact_phrase=exact,
                    stores_only=stores_only, sort_mode=sort_mode,
                    progress=lambda m: print(f"    {m}"))
    return {
        "job_id": job.get("id"),
        "status": "done",
        "query": query,
        "country": country,
        "ads": [a.to_dict() for a in result.ads],
        "reported": result.reported_count,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MetaAdsPlaywright worker agent")
    ap.add_argument("--url", required=True, help="dashboard base URL")
    ap.add_argument("--token", required=True, help="WORKER_TOKEN / INGEST_TOKEN")
    ap.add_argument("--poll", type=int, default=10, help="seconds between polls")
    ap.add_argument("--worker-id", default="windows-worker")
    args = ap.parse_args(argv)

    base = args.url.rstrip("/")
    print(f"🤖 Worker started. dashboard={base} poll={args.poll}s")
    while True:
        try:
            status, data = _req(f"{base}/api/worker/next?worker_id={args.worker_id}", args.token)
            if status == 204 or not data:
                time.sleep(args.poll)
                continue
            job = data["job"]
            print(f"📥 وصل أمر جديد #{job['id']}: {job.get('query')!r}")
            try:
                payload = run_job(base, args.token, job)
            except Exception as exc:
                payload = {"job_id": job.get("id"), "status": "failed",
                           "query": job.get("query", ""), "ads": [], "error": str(exc)}
                print(f"  ❌ فشل: {exc}")
            st, res = _req(f"{base}/api/worker/result", args.token, method="POST", payload=payload)
            print(f"  ✅ أُرسلت النتيجة: stored={res.get('stored') if res else '?'}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                print("❌ توكن غير صحيح (401). تحقق من --token.")
                return 1
            print(f"⚠️ HTTP {e.code}; retry in {args.poll}s")
            time.sleep(args.poll)
        except Exception as exc:
            print(f"⚠️ {exc}; retry in {args.poll}s")
            time.sleep(args.poll)


if __name__ == "__main__":
    sys.exit(main())
