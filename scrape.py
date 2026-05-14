#!/usr/bin/env python3
"""
Pajero Gen 4 Service Manual scraper — first pass: 2010 model year only.

Source: http://faq.out-club.ru/download/pajero_iv/maintenance/Service_Manual_2008_2013/2010/

What it does
------------
1. Crawls the 2010 service manual: TOC frame files + every content page they link to.
2. Downloads all referenced images (PNG/PDF illustrations) from the shared /img/ tree.
3. Stores RAW files on disk in their original layout under raw/ for fidelity.
4. Parses each content page and emits:
     - clean Markdown under markdown/ (mirroring the source tree)
     - structured JSON under json/ with title, headings, body text, image refs, tables
5. Builds a master manifest.json listing every page, its TOC location, and assets.

Design choices
--------------
- Async + httpx with a small concurrency cap and a per-request sleep.
- Resumable: re-running skips anything already on disk (size > 0).
- Polite: identifies itself in User-Agent, retries on 5xx with backoff.
- Defensive: the source server is old, slow, and sometimes 502s. Don't trust it.

Usage
-----
    python scrape.py --phase discover     # phase 1: build manifest, no content download
    python scrape.py --phase mirror       # phase 2: download everything (idempotent)
    python scrape.py --phase parse        # phase 3: parse raw HTML → markdown + json
    python scrape.py --phase all          # do everything end-to-end

    Add --limit N to stop after N pages (handy for testing).
    Add --concurrency N to tune parallelism (default 4).
    Add --delay S to set per-request delay seconds (default 0.5).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sqlite3
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urljoin, urlparse, unquote

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import markdownify as md

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_HOST = "http://faq.out-club.ru"
BASE_PATH = "/download/pajero_iv/maintenance/Service_Manual_2008_2013/"

# All years available on the server.
ALL_YEARS = ["2008_eur", "2008_ge", "2009", "2010", "2011", "2012", "2013"]
# M1=Service Manual, M2=Technical Information Manual, M4=Body Repair Manual
ALL_MANUALS = ["M1", "M2", "M4"]

# Scope covers the entire Service_Manual_2008_2013 tree (all years, all manuals).
SCOPE_PREFIX = BASE_PATH

# Images live in a shared sibling directory used by every model year.
IMG_PREFIX = f"{BASE_PATH}img/"

USER_AGENT = (
    "PajeroManualMirror/0.1 (+personal archival project; contact: local)"
)

OUTPUT_ROOT = Path(__file__).parent / "output"
RAW_DIR = OUTPUT_ROOT / "raw"
MD_DIR = OUTPUT_ROOT / "markdown"
JSON_DIR = OUTPUT_ROOT / "json"
DB_PATH = OUTPUT_ROOT / "crawl.sqlite"
MANIFEST_PATH = OUTPUT_ROOT / "manifest.json"

# Filename patterns
CONTENT_PAGE_RE = re.compile(r"/[0-9A-Z]{2}/html/M[0-9A-Z]+ENG\.HTM$", re.IGNORECASE)
NAV_FILE_RE = re.compile(r"_M[0-9]+\.htm$|/(toc|index|left|nav|frame)[_A-Z0-9]*\.htm$", re.IGNORECASE)
IMAGE_RE = re.compile(r"\.(png|jpg|jpeg|gif|pdf)$", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scrape")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_url(url: str, base: str) -> str:
    """Resolve a possibly-relative URL, drop fragments, normalize case of host."""
    absolute = urljoin(base, url)
    absolute = absolute.split("#", 1)[0]
    parsed = urlparse(absolute)
    # Force consistent host (the site uses both http and https inconsistently;
    # http is what the original frames reference, so stick to that).
    netloc = parsed.netloc.lower()
    return f"http://{netloc}{parsed.path}" + (f"?{parsed.query}" if parsed.query else "")


def url_to_local_path(url: str, root: Path) -> Path:
    """Map a remote URL to a path under root, preserving the source layout."""
    parsed = urlparse(url)
    rel = unquote(parsed.path.lstrip("/"))
    # Site sometimes uses .HTM, .htm; preserve as-is for fidelity.
    return root / rel


def is_in_scope_html(url: str) -> bool:
    """True if this URL is HTML we want to crawl (TOC or content for our year)."""
    parsed = urlparse(url)
    if parsed.netloc.lower() != urlparse(BASE_HOST).netloc:
        return False
    p = parsed.path
    if not (p.lower().endswith(".htm") or p.lower().endswith(".html")):
        return False
    return p.startswith(SCOPE_PREFIX)


def is_image_or_pdf(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.lower() != urlparse(BASE_HOST).netloc:
        return False
    return bool(IMAGE_RE.search(parsed.path))


def is_content_page(url: str) -> bool:
    return bool(CONTENT_PAGE_RE.search(urlparse(url).path))


# ---------------------------------------------------------------------------
# Crawl state (SQLite)
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    url            TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,        -- 'frame', 'content', 'asset'
    status         TEXT NOT NULL,        -- 'pending', 'fetched', 'failed', 'skipped'
    http_status    INTEGER,
    bytes          INTEGER,
    content_type   TEXT,
    error          TEXT,
    first_seen     REAL NOT NULL,
    fetched_at     REAL,
    discovered_from TEXT
);
CREATE INDEX IF NOT EXISTS idx_urls_status ON urls(status);
CREATE INDEX IF NOT EXISTS idx_urls_kind   ON urls(kind);
"""


class CrawlDB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def add(self, url: str, kind: str, discovered_from: Optional[str] = None) -> bool:
        """Insert URL if not present. Returns True if newly added."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO urls (url, kind, status, first_seen, discovered_from) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (url, kind, time.time(), discovered_from),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def mark_fetched(self, url: str, http_status: int, n_bytes: int, content_type: str):
        self.conn.execute(
            "UPDATE urls SET status='fetched', http_status=?, bytes=?, "
            "content_type=?, fetched_at=?, error=NULL WHERE url=?",
            (http_status, n_bytes, content_type, time.time(), url),
        )
        self.conn.commit()

    def mark_failed(self, url: str, error: str, http_status: Optional[int] = None):
        self.conn.execute(
            "UPDATE urls SET status='failed', error=?, http_status=?, fetched_at=? WHERE url=?",
            (error, http_status, time.time(), url),
        )
        self.conn.commit()

    def mark_skipped(self, url: str, reason: str):
        self.conn.execute(
            "UPDATE urls SET status='skipped', error=?, fetched_at=? WHERE url=?",
            (reason, time.time(), url),
        )
        self.conn.commit()

    def pending(self, kind: Optional[str] = None) -> list[str]:
        if kind:
            cur = self.conn.execute(
                "SELECT url FROM urls WHERE status='pending' AND kind=?", (kind,)
            )
        else:
            cur = self.conn.execute("SELECT url FROM urls WHERE status='pending'")
        return [r[0] for r in cur.fetchall()]

    def all_fetched(self, kind: Optional[str] = None) -> list[str]:
        if kind:
            cur = self.conn.execute(
                "SELECT url FROM urls WHERE status='fetched' AND kind=?", (kind,)
            )
        else:
            cur = self.conn.execute("SELECT url FROM urls WHERE status='fetched'")
        return [r[0] for r in cur.fetchall()]

    def counts(self) -> dict:
        cur = self.conn.execute(
            "SELECT kind, status, COUNT(*) FROM urls GROUP BY kind, status"
        )
        out: dict = {}
        for kind, status, count in cur.fetchall():
            out.setdefault(kind, {})[status] = count
        return out

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int
    content: bytes
    content_type: str
    error: Optional[str] = None


class PoliteFetcher:
    """Async HTTP client with rate limiting and retries."""

    def __init__(self, concurrency: int, delay: float):
        self.sem = asyncio.Semaphore(concurrency)
        self.delay = delay
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=concurrency * 2),
        )
        self._last_request = 0.0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()

    async def get(self, url: str, retries: int = 3) -> FetchResult:
        async with self.sem:
            # Global rate limit: ensure at least `delay` between request starts.
            now = time.monotonic()
            wait = self.delay - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

            last_exc: Optional[Exception] = None
            for attempt in range(retries):
                try:
                    r = await self.client.get(url)
                    if r.status_code >= 500:
                        last_exc = httpx.HTTPStatusError(
                            f"Server returned {r.status_code}", request=r.request, response=r
                        )
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return FetchResult(
                        url=url,
                        ok=r.status_code == 200,
                        status=r.status_code,
                        content=r.content,
                        content_type=r.headers.get("content-type", ""),
                    )
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as e:
                    last_exc = e
                    log.warning("retry %d for %s: %s", attempt + 1, url, e)
                    await asyncio.sleep(2 ** attempt)
            return FetchResult(
                url=url, ok=False, status=0, content=b"", content_type="",
                error=str(last_exc) if last_exc else "unknown error",
            )


# ---------------------------------------------------------------------------
# HTML link extraction
# ---------------------------------------------------------------------------

def extract_links_from_html(html_bytes: bytes, base_url: str) -> tuple[set[str], set[str]]:
    """
    Return (html_links, asset_links) found in this page.
    Handles:
        - frameset <frame src=...>, <iframe src=...>
        - anchors <a href=...>
        - images <img src=...>
        - inline JS calls like javascript:enlarge('../../img/x.png')
    """
    html_links: set[str] = set()
    asset_links: set[str] = set()

    # Try to decode — site uses windows-1251 sometimes, but mostly utf-8.
    text = None
    for enc in ("utf-8", "windows-1251", "latin-1"):
        try:
            text = html_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = html_bytes.decode("utf-8", errors="replace")

    soup = BeautifulSoup(text, "lxml")

    # Standard navigable references
    for tag, attr in (("a", "href"), ("frame", "src"), ("iframe", "src"),
                      ("link", "href"), ("area", "href")):
        for el in soup.find_all(tag):
            val = el.get(attr)
            if not val:
                continue
            if val.startswith("javascript:"):
                continue
            absolute = normalize_url(val, base_url)
            if is_in_scope_html(absolute):
                html_links.add(absolute)
            elif is_image_or_pdf(absolute):
                asset_links.add(absolute)

    # Images
    for el in soup.find_all("img"):
        val = el.get("src")
        if val:
            absolute = normalize_url(val, base_url)
            if is_image_or_pdf(absolute):
                asset_links.add(absolute)

    # javascript:enlarge('../../../img/00/AC603772AF00ENG.png') style refs
    for match in re.finditer(
        r"""javascript:\w+\(\s*['"]([^'"]+)['"]""", text
    ):
        val = match.group(1)
        absolute = normalize_url(val, base_url)
        if is_image_or_pdf(absolute):
            asset_links.add(absolute)

    return html_links, asset_links


# ---------------------------------------------------------------------------
# Phase 1: discover — crawl HTML, build manifest of every URL
# ---------------------------------------------------------------------------

async def phase_discover(args, db: CrawlDB):
    """Crawl all in-scope HTML pages, recording every URL encountered."""
    for entry in args.entries:
        db.add(entry, kind="frame", discovered_from=None)

    async with PoliteFetcher(args.concurrency, args.delay) as fetcher:
        # Process one wave at a time so the DB always reflects current state
        # for resume. (We could fully pipeline, but waves are simpler to reason about.)
        wave_n = 0
        while True:
            pending_html = db.pending(kind="frame") + db.pending(kind="content")
            if not pending_html:
                break
            if args.limit and len(db.all_fetched("content")) >= args.limit:
                log.info("hit content fetch limit (%d), stopping discovery", args.limit)
                break

            wave_n += 1
            log.info("discover wave %d: %d pages to fetch", wave_n, len(pending_html))

            results = await asyncio.gather(
                *(fetcher.get(u) for u in pending_html), return_exceptions=False
            )

            new_html = 0
            new_assets = 0
            for url, res in zip(pending_html, results):
                if not res.ok:
                    db.mark_failed(url, res.error or f"HTTP {res.status}", res.status)
                    log.warning("FAIL %s [%s]", url, res.error or res.status)
                    continue

                # Save raw HTML for phase 3
                local = url_to_local_path(url, RAW_DIR)
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(res.content)

                db.mark_fetched(url, res.status, len(res.content), res.content_type)

                # Discover further links
                html_links, asset_links = extract_links_from_html(res.content, url)
                for link in html_links:
                    # A frame file ends in _M1.htm (the toolbar/nav); content pages
                    # are under /<group>/html/. Anything else in scope (rare) is
                    # treated as a frame for safety.
                    kind = "content" if is_content_page(link) else "frame"
                    if db.add(link, kind=kind, discovered_from=url):
                        new_html += 1
                for link in asset_links:
                    if db.add(link, kind="asset", discovered_from=url):
                        new_assets += 1

                log.debug("OK %s (%d bytes, %d links, %d assets)",
                          url, len(res.content), len(html_links), len(asset_links))

            log.info("wave %d done: +%d html, +%d assets", wave_n, new_html, new_assets)

    counts = db.counts()
    log.info("discover complete. counts: %s", json.dumps(counts, indent=2))


# ---------------------------------------------------------------------------
# Phase 2: mirror — download all discovered asset files
# ---------------------------------------------------------------------------

async def phase_mirror(args, db: CrawlDB):
    """Download every asset (image/pdf) that was discovered but not yet fetched."""
    pending = db.pending(kind="asset")
    if not pending:
        log.info("no pending assets to download")
        return

    log.info("downloading %d assets", len(pending))

    async with PoliteFetcher(args.concurrency, args.delay) as fetcher:
        # Chunk into waves so SQLite stays in sync if interrupted.
        WAVE = 50
        for i in range(0, len(pending), WAVE):
            chunk = pending[i:i + WAVE]
            results = await asyncio.gather(*(fetcher.get(u) for u in chunk))
            for url, res in zip(chunk, results):
                local = url_to_local_path(url, RAW_DIR)
                if local.exists() and local.stat().st_size > 0:
                    db.mark_skipped(url, "already on disk")
                    continue
                if not res.ok:
                    db.mark_failed(url, res.error or f"HTTP {res.status}", res.status)
                    log.warning("FAIL asset %s", url)
                    continue
                local.parent.mkdir(parents=True, exist_ok=True)
                local.write_bytes(res.content)
                db.mark_fetched(url, res.status, len(res.content), res.content_type)

            log.info("assets %d/%d", min(i + WAVE, len(pending)), len(pending))


# ---------------------------------------------------------------------------
# Phase 3: parse — produce clean markdown + structured JSON
# ---------------------------------------------------------------------------

@dataclass
class ParsedPage:
    page_id: str                          # M200002600086700
    source_url: str
    group: str                            # '00', '11', '13', ...
    title: str                            # from <title> or first <h1>
    breadcrumb: list[str] = field(default_factory=list)
    headings: list[dict] = field(default_factory=list)   # {level, text}
    images: list[dict] = field(default_factory=list)     # {id, src, alt, caption}
    tables: list[list[list[str]]] = field(default_factory=list)
    torque_specs: list[dict] = field(default_factory=list)
    part_numbers: list[str] = field(default_factory=list)
    markdown: str = ""
    raw_text_chars: int = 0


def extract_page_id(url: str) -> Optional[str]:
    m = re.search(r"/([Mm][0-9A-Z]+)ENG\.HTM$", url, re.IGNORECASE)
    return m.group(1).upper() if m else None


def extract_group(url: str) -> Optional[str]:
    m = re.search(r"/([0-9A-Z]{2})/html/", url, re.IGNORECASE)
    return m.group(1).upper() if m else None


def extract_year(url: str) -> str:
    """Return the model-year token from a source URL (e.g. '2010', '2008_eur')."""
    m = re.search(r"/Service_Manual_2008_2013/([^/]+)/", url)
    return m.group(1) if m else "unknown"


def extract_manual_type(page_id: str) -> str:
    """Return M1, M2, or M4 from a page_id prefix."""
    m = re.match(r"^(M[124])", page_id, re.IGNORECASE)
    return m.group(1).upper() if m else "M1"


# Torque spec patterns common in Mitsubishi manuals.
# Examples seen: "44 ± 5 N·m", "98 N·m {10 kgf·m}", "12 Nm".
TORQUE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:±\s*(\d+(?:\.\d+)?)\s*)?N[·\s]*m",
    re.IGNORECASE,
)


def parse_content_page(html_bytes: bytes, source_url: str) -> Optional[ParsedPage]:
    if not is_content_page(source_url):
        return None

    text = None
    for enc in ("utf-8", "windows-1251", "latin-1"):
        try:
            text = html_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return None

    soup = BeautifulSoup(text, "lxml")

    # Strip tracking pixels and the mail.ru counter we noticed earlier.
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "top-fwz1.mail.ru" in src or "mail.ru/counter" in src:
            img.decompose()

    page_id = extract_page_id(source_url) or "UNKNOWN"
    group = extract_group(source_url) or "??"

    title_el = soup.find("title")
    page_title = (title_el.get_text(strip=True) if title_el else "") or (
        soup.find("h1").get_text(strip=True) if soup.find("h1") else ""
    ) or page_id

    parsed = ParsedPage(page_id=page_id, source_url=source_url, group=group, title=page_title)

    # Headings
    for h in soup.find_all(re.compile(r"^h[1-6]$")):
        parsed.headings.append({
            "level": int(h.name[1]),
            "text": h.get_text(" ", strip=True),
        })

    # Images
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        absolute = normalize_url(src, source_url)
        m = re.search(r"/([A-Z]{2}\d+[A-Z0-9]*ENG)\.(?:png|jpg|gif|pdf)$", absolute, re.IGNORECASE)
        image_id = m.group(1) if m else absolute.rsplit("/", 1)[-1]
        parsed.images.append({
            "id": image_id,
            "src": absolute,
            "alt": img.get("alt", "") or "",
        })

    # Tables — keep as 2D arrays of plaintext for now (structured rebuild later)
    for table in soup.find_all("table"):
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            parsed.tables.append(rows)

    # Body text (cleaned) for token-counting and torque extraction
    body_text = soup.get_text(" ", strip=True)
    parsed.raw_text_chars = len(body_text)

    # Torque specs
    for m in TORQUE_RE.finditer(body_text):
        val = float(m.group(1))
        tol = float(m.group(2)) if m.group(2) else None
        parsed.torque_specs.append({
            "value_nm": val,
            "tolerance_nm": tol,
            "context": body_text[max(0, m.start() - 60): m.end() + 20],
        })

    # Part numbers — Mitsubishi part numbers are typically 10 chars, often
    # 4 digits + letters + digits. Conservative pattern to avoid false hits.
    for m in re.finditer(r"\bMD\d{6}\b|\bMR\d{6}\b|\bMN\d{6}\b", body_text):
        parsed.part_numbers.append(m.group(0))
    # dedup, preserve order
    seen = set()
    parsed.part_numbers = [p for p in parsed.part_numbers if not (p in seen or seen.add(p))]

    # Markdown — markdownify on the soup body
    body = soup.find("body") or soup
    parsed.markdown = md(str(body), heading_style="ATX").strip()

    return parsed


def phase_parse(args, db: CrawlDB):
    """Walk every fetched content page on disk and emit markdown + JSON."""
    content_urls = db.all_fetched(kind="content")
    log.info("parsing %d content pages", len(content_urls))

    MD_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)

    parsed_pages: list[dict] = []

    skipped = 0
    for url in content_urls:
        local = url_to_local_path(url, RAW_DIR)
        if not local.exists():
            log.warning("missing raw file for %s", url)
            continue

        json_out = (JSON_DIR / local.relative_to(RAW_DIR)).with_suffix(".json")
        md_out = (MD_DIR / local.relative_to(RAW_DIR)).with_suffix(".md")

        # Fast path: if JSON already exists, read it and skip re-parsing.
        if json_out.exists() and json_out.stat().st_size > 0:
            try:
                cached = json.loads(json_out.read_text(encoding="utf-8"))
                parsed_pages.append({
                    "page_id": cached["page_id"],
                    "group": cached["group"],
                    "year": extract_year(cached["source_url"]),
                    "manual_type": extract_manual_type(cached["page_id"]),
                    "title": cached["title"],
                    "source_url": cached["source_url"],
                    "markdown_path": str(md_out.relative_to(OUTPUT_ROOT)),
                    "json_path": str(json_out.relative_to(OUTPUT_ROOT)),
                    "image_count": len(cached.get("images", [])),
                    "table_count": len(cached.get("tables", [])),
                    "torque_spec_count": len(cached.get("torque_specs", [])),
                    "chars": cached.get("raw_text_chars", 0),
                })
                skipped += 1
                continue
            except Exception:
                pass  # corrupt JSON — fall through to re-parse

        html = local.read_bytes()
        parsed = parse_content_page(html, url)
        if parsed is None:
            continue

        # Mirror path layout under markdown/ and json/, but use .md / .json
        md_out.parent.mkdir(parents=True, exist_ok=True)
        front_matter = (
            "---\n"
            f"page_id: {parsed.page_id}\n"
            f"group: {parsed.group}\n"
            f"title: \"{parsed.title.replace(chr(34), chr(39))}\"\n"
            f"source_url: {parsed.source_url}\n"
            "---\n\n"
        )
        md_out.write_text(front_matter + parsed.markdown, encoding="utf-8")

        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(asdict(parsed), indent=2, ensure_ascii=False),
                            encoding="utf-8")

        parsed_pages.append({
            "page_id": parsed.page_id,
            "group": parsed.group,
            "year": extract_year(parsed.source_url),
            "manual_type": extract_manual_type(parsed.page_id),
            "title": parsed.title,
            "source_url": parsed.source_url,
            "markdown_path": str(md_out.relative_to(OUTPUT_ROOT)),
            "json_path": str(json_out.relative_to(OUTPUT_ROOT)),
            "image_count": len(parsed.images),
            "table_count": len(parsed.tables),
            "torque_spec_count": len(parsed.torque_specs),
            "chars": parsed.raw_text_chars,
        })

    log.info("parse complete: %d new, %d from cache", len(parsed_pages) - skipped, skipped)

    # Build the manifest
    manifest = {
        "source_root": BASE_HOST + BASE_PATH,
        "years": args.years,
        "manuals": args.manuals,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "page_count": len(parsed_pages),
        "asset_count": len(db.all_fetched("asset")),
        "pages": sorted(parsed_pages, key=lambda p: (p["manual_type"], p["year"], p["group"], p["page_id"])),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    log.info("wrote manifest with %d pages → %s", len(parsed_pages), MANIFEST_PATH)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Pajero Gen 4 service manual scraper")
    p.add_argument("--phase", choices=["discover", "mirror", "parse", "all"],
                   default="all", help="which phase to run")
    p.add_argument("--years", default="2010",
                   help="comma-separated model years to scrape "
                        f"(default: 2010; all: {','.join(ALL_YEARS)})")
    p.add_argument("--manuals", default="M1",
                   help="comma-separated manual types: M1, M2, M4 "
                        "(default: M1; all: M1,M2,M4)")
    p.add_argument("--limit", type=int, default=None,
                   help="stop discovery after N content pages (testing)")
    p.add_argument("--concurrency", type=int, default=4,
                   help="max concurrent HTTP requests (default 4)")
    p.add_argument("--delay", type=float, default=0.5,
                   help="min seconds between request starts (default 0.5)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    args.years = [y.strip() for y in args.years.split(",")]
    args.manuals = [m.strip().upper() for m in args.manuals.split(",")]

    # Build entry points: one per (year, manual) combination
    args.entries = [
        f"{BASE_HOST}{BASE_PATH}{yr}/index_{mn}.htm"
        for yr in args.years
        for mn in args.manuals
    ]
    log.info("scraping %d entry points: years=%s manuals=%s",
             len(args.entries), args.years, args.manuals)

    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    db = CrawlDB(DB_PATH)

    try:
        if args.phase in ("discover", "all"):
            asyncio.run(phase_discover(args, db))
        if args.phase in ("mirror", "all"):
            asyncio.run(phase_mirror(args, db))
        if args.phase in ("parse", "all"):
            phase_parse(args, db)
    finally:
        db.close()

    log.info("done. output at %s", OUTPUT_ROOT)


if __name__ == "__main__":
    main()
