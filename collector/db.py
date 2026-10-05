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
                stores_only: bool = False) -> list[dict]:
        p = self._ph()
        where = []
        params: list = []
        if stores_only:
            where.append("store_domain != '' AND store_domain IS NOT NULL")
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

    def count_ads(self, search: str = "", stores_only: bool = False) -> int:
        p = self._ph()
        where = []
        params: list = []
        if stores_only:
            where.append("store_domain != '' AND store_domain IS NOT NULL")
        if search:
            like = "%" + search + "%"
            where.append(f"(body LIKE {p} OR page_name LIKE {p} OR store_domain LIKE {p})")
            params.extend([like, like, like])
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        row = self._query_one(f"SELECT COUNT(*) AS c FROM meta_ads {clause}", tuple(params))
        return int(row["c"]) if row else 0

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


db = Database()
