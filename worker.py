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
from collector.slicing import build_pass_urls


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
    job_url = (job.get("url") or "").strip()
    country = job.get("country") or "DZ"
    exact = bool(job.get("exact_phrase"))
    stores_only = bool(job.get("stores_only"))
    sort_mode = job.get("sort_mode") or "total_impressions"
    sweep = bool(job.get("sweep")) and bool(job_url)
    job_id = job.get("id")
    label = query or job_url or "(url)"
    print(f"  ▶ سحب: {label!r} (url={bool(job_url)}, sweep={sweep}, stores_only={stores_only})")

    # Union across passes, keyed by ad_id.
    merged: dict[str, dict] = {}
    state = {"stop": False, "pass": 0, "passes": 1}

    def on_ad(ad):
        d = ad.to_dict()
        if d.get("ad_id"):
            merged[d["ad_id"]] = d

    def should_stop() -> bool:
        note = f"تمريرة {state['pass']}/{state['passes']} · {len(merged)} إعلان"
        try:
            _, r = _req(f"{base_url}/api/worker/progress", token, method="POST",
                        payload={"job_id": job_id, "progress": len(merged), "note": note})
            if r and r.get("stop"):
                state["stop"] = True
                print("  ⏹ طُلب الإيقاف من اللوحة — إيقاف السحب.")
                return True
        except Exception:
            pass
        return False

    if sweep:
        passes = build_pass_urls(job_url)
        state["passes"] = len(passes)
        for i, p in enumerate(passes, 1):
            if state["stop"]:
                break
            state["pass"] = i
            win = p["window"]
            win_label = f"{win[0]}..{win[1]}" if win else "all"
            print(f"  ↻ تمريرة {i}/{len(passes)} [{p['sort']} | {win_label}] — تراكمي {len(merged)}")
            try:
                scrape(query, country=country, url=p["url"], stores_only=False,
                       progress=lambda m: None, on_ad=on_ad, should_stop=should_stop)
            except Exception as exc:
                print(f"    ⚠️ تمريرة {i} فشلت: {exc}")
    else:
        scrape(query, country=country, url=job_url or None, exact_phrase=exact,
               stores_only=False, sort_mode=sort_mode,
               progress=lambda m: print(f"    {m}"),
               on_ad=on_ad, should_stop=should_stop)

    ads = list(merged.values())
    if stores_only:
        ads = [a for a in ads if a.get("store_domain")]
    return {
        "job_id": job_id,
        "status": "stopped" if state["stop"] else "done",
        "query": query,
        "country": country,
        "ads": ads,
        "reported": len(ads),
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
