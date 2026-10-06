# -*- coding: utf-8 -*-
"""
Unified storage for collected ads.

Uses PostgreSQL when DATABASE_URL is set (production), otherwise falls back to
a local SQLite file. Both share the same query surface so callers don't care.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("meta_ads_db")

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_SQLITE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "meta_ads.sqlite")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_url: Optional[str] = None):
        self.db_url = (db_url or DATABASE_URL).strip()
        self.is_postgres = self.db_url.startswith(("postgres://", "postgresql://"))
        if self.db_url.startswith("postgres://"):
            self.db_url = self.db_url.replace("postgres://", "postgresql://", 1)
        self._init_schema()

    # ── connection helpers ────────────────────────────────────────────────
    def _ph(self) -> str:
        return "%s" if self.is_postgres else "?"

    def _table_exists(self, name: str) -> bool:
        if self.is_postgres:
            row = self._query_one("SELECT to_regclass(%s) AS t", (f"public.{name}",))
        else:
            row = self._query_one("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,))
        return bool(row)

    def _connect(self):
        if self.is_postgres:
            import psycopg2
            conn = psycopg2.connect(self.db_url)
            conn.autocommit = True
            return conn
        conn = sqlite3.connect(_SQLITE_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        try:
            if self.is_postgres:
                self._query_one("SELECT 1")
        except Exception as exc:  # pragma: no cover
            logger.error("PostgreSQL connection failed (%s); falling back to SQLite.", exc)
            self.is_postgres = False

        conn = self._connect()
        cur = conn.cursor()
        sql = """
        CREATE TABLE IF NOT EXISTS meta_ads (
            ad_id TEXT PRIMARY KEY,
            page_name TEXT,
            page_id TEXT,
            start_date TEXT,
            is_active INTEGER,
            link_url TEXT,
            store_domain TEXT,
            platform TEXT,
            body TEXT,
            cta_text TEXT,
            collation_count INTEGER,
            publisher_platforms TEXT,
            query TEXT,
            country TEXT,
            collected_at TEXT
        )
        """
        cur.execute(sql)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_meta_ads_store ON meta_ads (store_domain)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_meta_ads_query ON meta_ads (query)")

        idcol = "SERIAL PRIMARY KEY" if self.is_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS jobs (
            id {idcol},
            query TEXT,
            country TEXT,
            exact_phrase INTEGER DEFAULT 0,
            stores_only INTEGER DEFAULT 0,
            sort_mode TEXT DEFAULT 'total_impressions',
            status TEXT DEFAULT 'pending',
            source TEXT DEFAULT 'manual',
            worker_id TEXT,
            created_at TEXT,
            started_at TEXT,
            finished_at TEXT,
            result_count INTEGER DEFAULT 0,
            error TEXT
        )
        """)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS keywords (
            id {idcol},
            query TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        conn.commit()

        # Idempotent migrations for the jobs table (progress + stop control).
        for col, ddl in [
            ("progress", "INTEGER DEFAULT 0"),
            ("progress_note", "TEXT DEFAULT ''"),
            ("stop_requested", "INTEGER DEFAULT 0"),
        ]:
            try:
                if self.is_postgres:
                    cur.execute(f"ALTER TABLE jobs ADD COLUMN IF NOT EXISTS {col} {ddl}")
                else:
                    cols = [r[1] for r in cur.execute("PRAGMA table_info(jobs)").fetchall()]
                    if col not in cols:
                        cur.execute(f"ALTER TABLE jobs ADD COLUMN {col} {ddl}")
            except Exception:
                pass
        conn.commit()
        conn.close()

    # ── low-level helpers ─────────────────────────────────────────────────
    def _execute(self, sql: str, params: tuple = ()) -> None:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _query(self, sql: str, params: tuple = ()) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            rows = cur.fetchall()
            if self.is_postgres:
                cols = [c[0] for c in cur.description]
                return [dict(zip(cols, r)) for r in rows]
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple = ()) -> Optional[dict]:
        rows = self._query(sql, params)
        return rows[0] if rows else None

    # ── public API ────────────────────────────────────────────────────────
    def save_ad(self, ad: dict[str, Any], query: str = "", country: str = "DZ") -> bool:
        p = self._ph()
        cols = ("ad_id", "page_name", "page_id", "start_date", "is_active", "link_url",
                "store_domain", "platform", "body", "cta_text", "collation_count",
                "publisher_platforms", "query", "country", "collected_at")
        values = (
            str(ad.get("ad_id") or ""),
            ad.get("page_name") or "",
            str(ad.get("page_id") or ""),
            str(ad.get("start_date") or ""),
            1 if ad.get("is_active") else 0,
            ad.get("link_url") or "",
            ad.get("store_domain") or "",
            ad.get("platform") or "",
            ad.get("body") or "",
            ad.get("cta_text") or "",
            int(ad.get("collation_count") or 1),
            json.dumps(ad.get("publisher_platforms") or [], ensure_ascii=False),
            query,
            country,
            _now(),
        )
        if not values[0]:
            return False
        placeholders = ", ".join([p] * len(cols))
        col_list = ", ".join(cols)
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "ad_id")
                sql = (f"INSERT INTO meta_ads ({col_list}) VALUES ({placeholders}) "
                       f"ON CONFLICT (ad_id) DO UPDATE SET {updates}")
            else:
                updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "ad_id")
                sql = (f"INSERT INTO meta_ads ({col_list}) VALUES ({placeholders}) "
                       f"ON CONFLICT(ad_id) DO UPDATE SET {updates}")
            cur.execute(sql, values)
            conn.commit()
            return True
        except Exception as exc:
            logger.error("save_ad failed: %s", exc)
            return False
        finally:
            conn.close()

    def get_ads(self, limit: int = 100, offset: int = 0, search: str = "",
                stores_only: bool = False, query: str = "") -> list[dict]:
        p = self._ph()
        where = []
        params: list = []
        if stores_only:
            where.append("store_domain != '' AND store_domain IS NOT NULL")
        if query:
            where.append(f"query = {p}")
            params.append(query)
        if search:
            like = "%" + search + "%"
            where.append(f"(body LIKE {p} OR page_name LIKE {p} OR store_domain LIKE {p})")
            params.extend([like, like, like])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        sql = f"SELECT * FROM meta_ads {clause} ORDER BY collected_at DESC LIMIT {p} OFFSET {p}"
        params.extend([limit, offset])
        rows = self._query(sql, tuple(params))
        for r in rows:
            try:
                r["publisher_platforms"] = json.loads(r.get("publisher_platforms") or "[]")
            except Exception:
                r["publisher_platforms"] = []
        return rows

    def count_ads(self, search: str = "", stores_only: bool = False,
                  query: str = "") -> int:
        p = self._ph()
        where = []
        params: list = []
        if stores_only:
            where.append("store_domain != '' AND store_domain IS NOT NULL")
        if query:
            where.append(f"query = {p}")
            params.append(query)
        if search:
            like = "%" + search + "%"
            where.append(f"(body LIKE {p} OR page_name LIKE {p} OR store_domain LIKE {p})")
            params.extend([like, like, like])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads {clause}", tuple(params))
        return int(row["c"]) if row else 0

    def get_queries(self) -> list[dict]:
        sql = ("SELECT query, COUNT(*) AS total_ads, "
               "SUM(CASE WHEN store_domain != '' AND store_domain IS NOT NULL THEN 1 ELSE 0 END) AS store_ads, "
               "MAX(collected_at) AS last_run FROM meta_ads "
               "WHERE query != '' GROUP BY query ORDER BY last_run DESC LIMIT 100")
        return self._query(sql)

    def get_stores(self, search: str = "") -> list[dict]:
        p = self._ph()
        clause = ""
        params: tuple = ()
        if search:
            clause = f"WHERE store_domain LIKE {p}"
            params = ("%" + search + "%",)
        sql = (f"SELECT store_domain, platform, COUNT(*) AS total_ads, "
               f"MAX(collected_at) AS last_seen FROM meta_ads "
               f"WHERE store_domain != '' AND store_domain IS NOT NULL "
               f"{('AND store_domain LIKE ' + p) if search else ''} "
               f"GROUP BY store_domain, platform ORDER BY total_ads DESC LIMIT 300")
        return self._query(sql, params)

    def get_stats(self) -> dict:
        total = self.count_ads()
        stores = self._query_one(
            "SELECT COUNT(DISTINCT store_domain) AS c FROM meta_ads "
            "WHERE store_domain != '' AND store_domain IS NOT NULL")
        last = self._query_one("SELECT MAX(collected_at) AS t FROM meta_ads")
        return {
            "backend": "PostgreSQL" if self.is_postgres else "SQLite",
            "total_ads": total,
            "total_stores": int(stores["c"]) if stores and stores.get("c") else 0,
            "last_collection": last["t"] if last else None,
        }

    def clear_ads(self) -> None:
        self._execute("DELETE FROM meta_ads")

    # ── jobs (remote scrape queue) ────────────────────────────────────────
    def create_job(self, query: str, country: str = "DZ", exact_phrase: bool = False,
                   stores_only: bool = False, sort_mode: str = "total_impressions",
                   source: str = "manual") -> int:
        p = self._ph()
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    f"INSERT INTO jobs (query, country, exact_phrase, stores_only, sort_mode, status, source, created_at) "
                    f"VALUES ({p},{p},{p},{p},{p},{p},{p},{p}) RETURNING id",
                    (query, country, int(exact_phrase), int(stores_only), sort_mode, "pending", source, _now()))
                new_id = cur.fetchone()[0]
            else:
                cur.execute(
                    "INSERT INTO jobs (query, country, exact_phrase, stores_only, sort_mode, status, source, created_at) "
                    f"VALUES ({p},{p},{p},{p},{p},{p},{p},{p})",
                    (query, country, int(exact_phrase), int(stores_only), sort_mode, "pending", source, _now()))
                new_id = cur.lastrowid
            conn.commit()
            return int(new_id)
        finally:
            conn.close()

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return self._query(f"SELECT * FROM jobs ORDER BY id DESC LIMIT {limit}")

    def claim_next_job(self, worker_id: str) -> Optional[dict]:
        """Atomically mark the oldest pending job as running and return it."""
        p = self._ph()
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    "UPDATE jobs SET status='running', worker_id=%s, started_at=%s "
                    "WHERE id = (SELECT id FROM jobs WHERE status='pending' ORDER BY id LIMIT 1) "
                    "RETURNING *", (worker_id, _now()))
                row = cur.fetchone()
                if not row:
                    return None
                cols = [c[0] for c in cur.description]
                return dict(zip(cols, row))
            else:
                cur.execute("SELECT id FROM jobs WHERE status='pending' ORDER BY id LIMIT 1")
                r = cur.fetchone()
                if not r:
                    return None
                jid = r[0]
                cur.execute("UPDATE jobs SET status='running', worker_id=?, started_at=? WHERE id=?",
                            (worker_id, _now(), jid))
                conn.commit()
                return self._query_one("SELECT * FROM jobs WHERE id=?", (jid,))
        finally:
            conn.close()

    def finish_job(self, job_id: int, status: str = "done", result_count: int = 0,
                   error: str = "") -> None:
        p = self._ph()
        self._execute(
            f"UPDATE jobs SET status={p}, finished_at={p}, result_count={p}, error={p} WHERE id={p}",
            (status, _now(), result_count, error, job_id))

    def update_job_progress(self, job_id: int, progress: int, note: str = "") -> None:
        p = self._ph()
        self._execute(
            f"UPDATE jobs SET progress={p}, progress_note={p} WHERE id={p}",
            (progress, note, job_id))

    def request_stop(self, job_id: int) -> None:
        self._execute(f"UPDATE jobs SET stop_requested={self._ph()} WHERE id={self._ph()}", (1, job_id))

    def get_job(self, job_id: int) -> Optional[dict]:
        return self._query_one(f"SELECT * FROM jobs WHERE id={self._ph()}", (job_id,))

    def requeue_stale_jobs(self, minutes: int = 20) -> int:
        """Return jobs stuck in 'running' for too long back to 'pending'."""
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        p = self._ph()
        stale = self._query(
            f"SELECT id FROM jobs WHERE status={p} AND started_at IS NOT NULL AND started_at < {p}",
            ("running", cutoff))
        for row in stale:
            self._execute(
                f"UPDATE jobs SET status={p}, worker_id=NULL, started_at=NULL WHERE id={p}",
                ("pending", row["id"]))
        return len(stale)

    # ── keywords (tracked for scheduled scraping) ─────────────────────────
    def add_keyword(self, query: str) -> bool:
        query = (query or "").strip()
        if not query:
            return False
        if self._query_one(f"SELECT id FROM keywords WHERE query={self._ph()}", (query,)):
            return False
        p = self._ph()
        self._execute(f"INSERT INTO keywords (query, enabled, created_at) VALUES ({p},{p},{p})",
                      (query, 1, _now()))
        return True

    def list_keywords(self) -> list[dict]:
        return self._query("SELECT * FROM keywords ORDER BY id DESC")

    def delete_keyword(self, kw_id: int) -> None:
        self._execute(f"DELETE FROM keywords WHERE id={self._ph()}", (kw_id,))

    def toggle_keyword(self, kw_id: int, enabled: bool) -> None:
        self._execute(f"UPDATE keywords SET enabled={self._ph()} WHERE id={self._ph()}",
                      (1 if enabled else 0, kw_id))

    # ── settings (schedule) ───────────────────────────────────────────────
    def get_settings(self) -> dict:
        rows = self._query("SELECT key, value FROM settings")
        return {r["key"]: r["value"] for r in rows}

    def set_setting(self, key: str, value: str) -> None:
        p = self._ph()
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.is_postgres:
                cur.execute(
                    f"INSERT INTO settings (key, value) VALUES ({p},{p}) "
                    f"ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))
            else:
                cur.execute(
                    f"INSERT INTO settings (key, value) VALUES ({p},{p}) "
                    f"ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            conn.commit()
        finally:
            conn.close()


db = Database()
