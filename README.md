# MetaAdsPlaywright

Scrape the **Meta (Facebook) Ad Library** with a real headless browser using
[Playwright](https://playwright.dev). No LLM, no hardcoded GraphQL doc-ids.

## Why Playwright?

The plain-HTTP engine gets silently truncated by Facebook: from datacenter /
non-browser IPs the GraphQL pagination stops early (often reporting
`has_next_page=false` while thousands of ads remain). A real browser
fingerprint and real session bypass this, and the scroll loop reaches the
full result set.

## Install

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
# Linux also needs: playwright install-deps chromium
```

## Usage

```bash
# basic keyword search
python -m collector.cli "متجر جودة" -o ads.txt

# exact phrase + store filter, export CSV
python -m collector.cli '"التوصيل لكل الولايات"' --exact-phrase --stores-only -f csv -o wilaya.csv

# show the browser (debugging)
python -m collector.cli "foorweb.store" --headed
```

### Python API

```python
from collector.scraper import scrape

res = scrape("foorweb.store", country="DZ", stores_only=True)
print(res.total_ads, res.store_ads, res.unique_stores)
for ad in res.ads[:5]:
    print(ad.page_name, ad.store_domain, ad.start_date)
```

## How it works

1. Launches stealth-hardened Chromium (playwright-stealth) with a real
   fingerprint, randomised viewport and UA.
2. Opens the Ad Library URL for the query.
3. Captures the initial ads from the SSR HTML (`<script data-sjs>`), then
   auto-scrolls and intercepts every `/api/graphql/` response.
4. Parses `search_results_connection` → `edges[].node.collated_results[]`.
5. Deduplicates by `ad_archive_id`, filters store links, stops when the count
   stabilises.
6. Applies a reactive rate-limiter (backoff only after Meta returns 429/403)
   and optional proxy rotation.

## Configuration

Copy `.env.example` to `.env` (optional):

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL URL (falls back to local SQLite if unset) |
| `INGEST_TOKEN` | Server secret that authorises `POST /api/ingest` |
| `META_SCRAPER_PROXIES` | Comma-separated proxies, rotated round-robin |
| `META_RATE_PER_SEC` | Optional proactive throttle (off by default) |
| `META_RATE_BURST` | Burst capacity when the throttle is enabled |

Proxies can also be listed one-per-line in `proxies.txt`.

## Push to a central dashboard

Run the scraper on one machine (e.g. Windows) and push its results to a central
dashboard (e.g. Coolify) so you can analyse/export/track from anywhere.

1. On the **dashboard** set `INGEST_TOKEN`.
2. On the **scraper machine**, after a scrape:

```bash
python push_to_cloud.py --url https://ads.botdz.com --token <INGEST_TOKEN> --since-hours 24
```

The dashboard stores everything in PostgreSQL and serves the UI + API.

## Notes

- The `count` Facebook shows on the page is an **estimate**; the scraper relies
  on the real scroll/pagination, not that number.
- On Linux servers, headless Chromium needs `playwright install-deps chromium`
  (root/apt). Datacenter IPs may still be throttled — a residential proxy can
  help if you hit limits.
- Known quirk: playwright-stealth breaks `response.status()` in the sync API,
  so the scraper filters by URL and detects blocks from response bodies.

