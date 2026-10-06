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
import time
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
    cfg = request.get_json(force=True, silent=True) or {}
    url = (cfg.get("url") or "").strip()
    query = (cfg.get("query") or "").strip()
    if not url and not query:
        return jsonify({"error": "query or url required"}), 400

    # No local browser on this host -> queue the job for a remote worker.
    if not SCRAPE_ENABLED:
        job_id = db.create_job(
            query=query,
            url=url,
            country=FIXED_COUNTRY,
            exact_phrase=bool(cfg.get("exact", False)),
            stores_only=bool(cfg.get("stores_only", False)),
            sort_mode=cfg.get("sort", "total_impressions"),
            source="manual",
        )
        manager.events.put({"type": "queued", "data": {"job_id": job_id, "query": query or url}})
        return jsonify({"ok": True, "queued": True, "job_id": job_id})

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


# ── remote job queue ──────────────────────────────────────────────────────
def _worker_token() -> str:
    return (os.environ.get("WORKER_TOKEN") or os.environ.get("INGEST_TOKEN") or "").strip()


def _worker_ok() -> bool:
    expected = _worker_token()
    provided = (request.headers.get("X-Worker-Token", "")
                or request.headers.get("X-Ingest-Token", "")
                or request.headers.get("Authorization", "").replace("Bearer ", "")).strip()
    return bool(expected) and provided == expected


@app.route("/api/jobs", methods=["GET"])
def api_jobs():
    return jsonify({"jobs": db.list_jobs(limit=60)})


@app.route("/api/jobs", methods=["POST"])
def api_create_job():
    cfg = request.get_json(force=True, silent=True) or {}
    url = (cfg.get("url") or "").strip()
    query = (cfg.get("query") or "").strip()
    if not url and not query:
        return jsonify({"error": "query or url required"}), 400
    job_id = db.create_job(
        query=query,
        url=url,
        country=cfg.get("country", FIXED_COUNTRY),
        exact_phrase=bool(cfg.get("exact", False)),
        stores_only=bool(cfg.get("stores_only", False)),
        sort_mode=cfg.get("sort", "total_impressions"),
        source=cfg.get("source", "manual"),
    )
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/worker/next")
def api_worker_next():
    if not _worker_ok():
        return jsonify({"error": "unauthorized"}), 401
    worker_id = request.args.get("worker_id", "worker")
    job = db.claim_next_job(worker_id)
    if not job:
        return ("", 204)
    return jsonify({"job": job})


@app.route("/api/worker/result", methods=["POST"])
def api_worker_result():
    if not _worker_ok():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(force=True, silent=True) or {}
    job_id = payload.get("job_id")
    status = payload.get("status", "done")
    error = payload.get("error", "")
    ads = payload.get("ads") or []
    query = payload.get("query", "")
    country = payload.get("country", FIXED_COUNTRY)

    stored = 0
    for ad in ads:
        if isinstance(ad, dict) and db.save_ad(ad, query=query, country=country):
            stored += 1
            manager.events.put({"type": "ad", "data": ad})
    if job_id is not None:
        db.finish_job(int(job_id), status=status, result_count=stored, error=error)
    manager.events.put({"type": "job", "data": {"job_id": job_id, "status": status, "stored": stored}})
    logger.info("worker result: job=%s status=%s stored=%s", job_id, status, stored)
    return jsonify({"ok": True, "stored": stored})


@app.route("/api/worker/progress", methods=["POST"])
def api_worker_progress():
    """Worker reports live progress; response tells it whether to stop."""
    if not _worker_ok():
        return jsonify({"error": "unauthorized"}), 401
    payload = request.get_json(force=True, silent=True) or {}
    job_id = payload.get("job_id")
    progress = int(payload.get("progress", 0))
    note = payload.get("note", "")
    if job_id is not None:
        db.update_job_progress(int(job_id), progress, note)
    stop = False
    if job_id is not None:
        job = db.get_job(int(job_id))
        stop = bool(job and job.get("stop_requested"))
    manager.events.put({"type": "progress",
                        "data": {"job_id": job_id, "progress": progress, "note": note, "stop": stop}})
    return jsonify({"ok": True, "stop": stop})


@app.route("/api/jobs/<int:job_id>/stop", methods=["POST"])
def api_job_stop(job_id):
    db.request_stop(job_id)
    manager.events.put({"type": "job", "data": {"job_id": job_id, "status": "stopping"}})
    return jsonify({"ok": True})


# ── keywords ──────────────────────────────────────────────────────────────
@app.route("/api/keywords", methods=["GET"])
def api_keywords():
    return jsonify({"keywords": db.list_keywords()})


@app.route("/api/keywords", methods=["POST"])
def api_add_keyword():
    cfg = request.get_json(force=True, silent=True) or {}
    ok = db.add_keyword(cfg.get("query", ""))
    return jsonify({"ok": ok})


@app.route("/api/keywords/<int:kw_id>", methods=["DELETE"])
def api_delete_keyword(kw_id):
    db.delete_keyword(kw_id)
    return jsonify({"ok": True})


@app.route("/api/keywords/<int:kw_id>", methods=["PATCH"])
def api_toggle_keyword(kw_id):
    cfg = request.get_json(force=True, silent=True) or {}
    db.toggle_keyword(kw_id, bool(cfg.get("enabled", True)))
    return jsonify({"ok": True})


# ── settings / schedule ───────────────────────────────────────────────────
@app.route("/api/settings", methods=["GET"])
def api_get_settings():
    s = db.get_settings()
    return jsonify({
        "schedule_enabled": s.get("schedule_enabled", "false"),
        "interval_hours": s.get("interval_hours", "24"),
        "last_scheduled_at": s.get("last_scheduled_at", ""),
    })


@app.route("/api/settings", methods=["POST"])
def api_set_settings():
    cfg = request.get_json(force=True, silent=True) or {}
    if "schedule_enabled" in cfg:
        db.set_setting("schedule_enabled", "true" if cfg["schedule_enabled"] else "false")
    if "interval_hours" in cfg:
        db.set_setting("interval_hours", str(cfg["interval_hours"]))
    return jsonify({"ok": True, **db.get_settings()})


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


def scheduler_loop() -> None:
    """Enqueue jobs for enabled keywords on the configured interval."""
    from datetime import timedelta
    while True:
        try:
            # Recover jobs abandoned by a crashed/stopped worker.
            n = db.requeue_stale_jobs(minutes=20)
            if n:
                manager.events.put({"type": "log", "data": {"text": f"♻️ أعيد {n} أمر عالق إلى قائمة الانتظار."}})
            s = db.get_settings()
            if s.get("schedule_enabled", "false").lower() in ("1", "true", "yes", "on"):
                try:
                    interval = float(s.get("interval_hours", "24") or 24)
                except ValueError:
                    interval = 24.0
                last = s.get("last_scheduled_at", "")
                due = True
                if last:
                    try:
                        due = datetime.now(timezone.utc) - datetime.fromisoformat(last) >= timedelta(hours=interval)
                    except Exception:
                        due = True
                if due:
                    keywords = [k for k in db.list_keywords() if k.get("enabled")]
                    for k in keywords:
                        db.create_job(query=k["query"], country=FIXED_COUNTRY,
                                      stores_only=True, sort_mode="total_impressions",
                                      source="schedule")
                    if keywords:
                        db.set_setting("last_scheduled_at", datetime.now(timezone.utc).isoformat())
                        manager.events.put({"type": "log",
                                            "data": {"text": f"⏰ جدولة: أُضيفت {len(keywords)} مهمة سحب."}})
        except Exception:
            logger.exception("scheduler error")
        time.sleep(60)


if __name__ == "__main__":
    print("=" * 60)
    print(f"  MetaAdsPlaywright dashboard -> http://127.0.0.1:{os.environ.get('PORT', '5002')}")
    print(f"  DB backend: {'PostgreSQL' if db.is_postgres else 'SQLite'}")
    print(f"  Local scraping: {'ENABLED' if SCRAPE_ENABLED else 'DISABLED (worker mode)'}")
    print("=" * 60)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5002")), debug=False)
