# -*- coding: utf-8 -*-
"""
MetaAdsPlaywright web dashboard.

Flask API + SSE dashboard around the Playwright scraper. Ads are streamed live
and persisted to PostgreSQL (or SQLite locally).
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, send_file

from collector.db import db
from collector.scraper import scrape

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("web_app")

app = Flask(__name__)

# Targeting policy for this deployment: Arabic-only ads running in Algeria.
FIXED_COUNTRY = "DZ"
FIXED_LANGS = ["ar"]

# Local scraping needs a Chromium browser. On a lightweight dashboard host
# (e.g. Coolify without Chromium) set SCRAPE_ENABLED=false so the UI becomes
# display/ingest-only and never tries to launch a browser.
SCRAPE_ENABLED = os.environ.get("SCRAPE_ENABLED", "true").strip().lower() in ("1", "true", "yes", "on")


class ScrapeManager:
    def __init__(self):
        self.thread = None
        self.stop_event = threading.Event()
        self.events = queue.Queue()
        self.state = {"running": False, "query": "", "scraped": 0, "stored": 0,
                      "stores": 0, "reported": None, "log": []}

    def _emit(self, kind: str, data: dict) -> None:
        self.events.put({"type": kind, "data": data})

    def start(self, query: str, exact: bool, stores_only: bool,
              sort_mode: str) -> bool:
        if self.state["running"]:
            return False
        self.stop_event.clear()
        while not self.events.empty():
            self.events.get_nowait()
        self.state.update({"running": True, "query": query, "scraped": 0,
                           "stored": 0, "stores": 0, "reported": None, "log": []})
        self.thread = threading.Thread(
            target=self._run, args=(query, exact, stores_only, sort_mode),
            daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self, query, exact, stores_only, sort_mode) -> None:
        country = FIXED_COUNTRY
        languages = FIXED_LANGS
        seen_stores: set = set()

        def progress(msg):
            self.state["log"].append(msg)
            self.state["log"] = self.state["log"][-200:]
            self._emit("log", {"text": msg})

        def on_ad(ad):
            if self.stop_event.is_set():
                return
            stored = db.save_ad(ad.to_dict(), query=query, country=country)
            self.state["scraped"] += 1
            if stored:
                self.state["stored"] += 1
            if ad.store_domain:
                seen_stores.add(ad.store_domain)
            self.state["stores"] = len(seen_stores)
            if not stores_only or ad.store_domain:
                self._emit("ad", ad.to_dict())

        try:
            self._emit("started", {"query": query, "country": country})
            result = scrape(query, country=country, exact_phrase=exact,
                            sort_mode=sort_mode, languages=languages,
                            stores_only=stores_only,
                            progress=progress, on_ad=on_ad)
            self.state["reported"] = result.reported_count
            self.state["stores"] = result.unique_stores
            self._emit("finished", {
                "query": query,
                "scraped": result.total_ads,
                "store_ads": result.store_ads,
                "stored": self.state["stored"],
                "stores": result.unique_stores,
                "reported": result.reported_count,
                "payloads": result.graphql_payloads,
            })
            logger.info("Scrape finished: %s ads, %s stores", result.total_ads, result.unique_stores)
        except Exception as exc:  # pragma: no cover
            logger.exception("scrape failed")
            self._emit("error", {"message": str(exc)})
        finally:
            self.state["running"] = False
            self._emit("done", {})


manager = ScrapeManager()


# ── pages ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


# ── engine control ────────────────────────────────────────────────────────
@app.route("/api/start", methods=["POST"])
def api_start():
    if not SCRAPE_ENABLED:
        return jsonify({"error": "scraping is disabled on this host (display/ingest only)"}), 409
    cfg = request.get_json(force=True, silent=True) or {}
    query = (cfg.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query required"}), 400
    ok = manager.start(
        query=query,
        exact=bool(cfg.get("exact", False)),
        stores_only=bool(cfg.get("stores_only", False)),
        sort_mode=cfg.get("sort", "total_impressions"),
    )
    return jsonify({"ok": ok})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    manager.stop()
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    return jsonify({**manager.state, "scrape_enabled": SCRAPE_ENABLED, "db": db.get_stats()})


@app.route("/api/stream")
def api_stream():
    def gen():
        yield "retry: 3000\n\n"
        while True:
            try:
                item = manager.events.get(timeout=15)
                yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield "data: {\"type\": \"ping\"}\n\n"

    return Response(gen(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


# ── data ──────────────────────────────────────────────────────────────────
@app.route("/api/ads")
def api_ads():
    limit = int(request.args.get("limit", 60))
    offset = int(request.args.get("offset", 0))
    search = request.args.get("search", "")
    query = request.args.get("query", "")
    stores_only = request.args.get("stores_only", "0") == "1"
    ads = db.get_ads(limit=limit, offset=offset, search=search,
                     stores_only=stores_only, query=query)
    return jsonify({"ads": ads, "total": db.count_ads(search=search, stores_only=stores_only, query=query)})


@app.route("/api/queries")
def api_queries():
    return jsonify({"queries": db.get_queries()})


@app.route("/api/stores")
def api_stores():
    return jsonify({"stores": db.get_stores(search=request.args.get("search", ""))})


@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats())


@app.route("/api/reset", methods=["POST"])
def api_reset():
    db.clear_ads()
    return jsonify({"ok": True})


def _ingest_token() -> str:
    return os.environ.get("INGEST_TOKEN", "").strip()


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Receive ads pushed from a remote scraper (e.g. the Windows machine)."""
    expected = _ingest_token()
    provided = (request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    if not expected:
        return jsonify({"error": "ingest disabled (INGEST_TOKEN not set)"}), 503
    if provided != expected:
        return jsonify({"error": "unauthorized"}), 401

    payload = request.get_json(force=True, silent=True) or {}
    ads = payload.get("ads") or []
    query = payload.get("query", "")
    country = payload.get("country", FIXED_COUNTRY)
    stored = 0
    for ad in ads:
        if isinstance(ad, dict) and db.save_ad(ad, query=query, country=country):
            stored += 1
    logger.info("ingest: received=%s stored=%s query=%r", len(ads), stored, query)
    return jsonify({"ok": True, "received": len(ads), "stored": stored})


@app.route("/api/export/<fmt>")
def api_export(fmt: str):
    ads = db.get_ads(limit=100000, stores_only=False)
    if fmt == "csv":
        buf = io.StringIO()
        cols = ["ad_id", "page_name", "page_id", "start_date", "store_domain",
                "platform", "cta_text", "link_url", "body"]
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for ad in ads:
            w.writerow(ad)
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        return send_file(data, mimetype="text/csv", as_attachment=True,
                         download_name=f"meta_ads_{int(datetime.now().timestamp())}.csv")
    if fmt == "json":
        data = io.BytesIO(json.dumps(ads, ensure_ascii=False, indent=2).encode("utf-8"))
        return send_file(data, mimetype="application/json", as_attachment=True,
                         download_name=f"meta_ads_{int(datetime.now().timestamp())}.json")
    return jsonify({"error": "unsupported format"}), 400


if __name__ == "__main__":
    print("=" * 60)
    print("  MetaAdsPlaywright dashboard -> http://127.0.0.1:5002")
    print(f"  DB backend: {'PostgreSQL' if db.is_postgres else 'SQLite'}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5002")), debug=False)
