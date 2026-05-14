# Pajero Gen 4 Service Manual scraper

First-pass scraper for the 2010 service manual at
`faq.out-club.ru/download/pajero_iv/maintenance/Service_Manual_2008_2013/2010/`.

## What it produces

```
output/
├── raw/                     # Verbatim mirror of every fetched URL (HTML, PNG, PDF)
│   └── download/pajero_iv/maintenance/Service_Manual_2008_2013/
│       ├── 2010/
│       │   ├── index_M1.htm
│       │   ├── 00/html/M2…ENG.HTM
│       │   ├── 11/html/M2…ENG.HTM
│       │   └── …
│       └── img/00/…PNG, img/11/…PNG, …
├── markdown/                # Same layout, .md files with YAML front matter
├── json/                    # Same layout, structured JSON per page
├── manifest.json            # Master index of every page + metadata
└── crawl.sqlite             # Resumable crawl state (don't delete mid-run)
```

Each Markdown file has front matter like:

```yaml
---
page_id: M200002600086700
group: "00"
title: "EQUIPMENTS"
source_url: http://faq.out-club.ru/.../2010/00/html/M200002600086700ENG.HTM
---
```

Each JSON file is the same content as a structured object: title, headings tree,
image references, tables as 2-D arrays, extracted torque specs (parsed from
"NN N·m" patterns), and detected Mitsubishi part numbers.

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
# Quick smoke test — 5 content pages only
python scrape.py --phase discover --limit 5
python scrape.py --phase mirror
python scrape.py --phase parse

# Full first-pass scrape (probably ~30–60 min depending on server mood)
python scrape.py --phase all

# Tune politeness
python scrape.py --concurrency 4 --delay 0.5    # defaults
python scrape.py --concurrency 8 --delay 0.25   # faster, riskier
```

## Phases

1. **discover** — crawls the frameset and every TOC/content page in scope.
   Stores raw HTML to `raw/`. Records every image/PDF reference for phase 2.
2. **mirror** — downloads every referenced image and PDF.
3. **parse** — turns every raw content page into clean Markdown + structured JSON,
   and writes `manifest.json`.

All phases are **resumable**. `crawl.sqlite` tracks which URLs are pending,
fetched, failed, or skipped. Re-running picks up where it stopped.

## Tuning knobs

| Flag | Default | Notes |
|---|---|---|
| `--concurrency` | 4 | Max parallel HTTP requests. The source server is old; don't go nuts. |
| `--delay` | 0.5 | Minimum seconds between request starts. |
| `--limit` | none | Stop discovery after N content pages (testing). |
| `--verbose` | off | Print every URL fetched. |

## Scope and what's next

This first pass intentionally covers only:
- Service Manual
- 2010 model year only

Once we've verified output quality on this slice, we extend with:
1. Other model years (2008_eur, 2008_ge, 2009, 2011, 2012, 2013) — content-addressable
   storage so cross-year duplicates are stored once.
2. Technical Information Manual + Body Repair Manual (sibling URL trees).
3. Bonus PDFs under `/spec/pdf/`.
4. Static-site generator that consumes `manifest.json` + `markdown/` + `json/`.

## Failure modes you might hit

- **502/504 from the source server.** The script retries with backoff. If a URL
  keeps failing, the DB marks it `failed` and the rest of the crawl continues.
  Re-run later; `--phase mirror` will retry any pending assets.
- **Encoding glitches.** A few pages use windows-1251. The script tries utf-8 →
  windows-1251 → latin-1 in that order.
- **Mail.ru tracking pixel** appears at the bottom of every content page. The
  parser strips it automatically.
- **`javascript:enlarge()` image links.** Treated as image references during
  link extraction so they end up in the asset queue.
