#!/usr/bin/env python3
"""
build_site.py — Generate a GitHub-Pages-ready static site from scraped Pajero IV manual data.

Reads:   output/manifest.json, output/raw/**  (raw mirrored HTML + images)
Writes:  site/  (or --out DIR)

URL structure:
  /index.html                             landing page (year × manual matrix)
  /view/{year}/{manual}/index.html        section grid for a specific year+manual
  /view/{year}/{manual}/group/{code}.html page list for a section within year+manual
  /page/{page_id}.html                    content page
  /search.html                            full-text search
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup
from jinja2 import Environment, BaseLoader, TemplateNotFound

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).parent
CONTENT_DIR = ROOT / "content"
OUTPUT_ROOT = ROOT / "output"
JSON_DIR = OUTPUT_ROOT / "json"
MANIFEST_PATH = OUTPUT_ROOT / "manifest.json"
RAW_DIR = OUTPUT_ROOT / "raw"
IMG_SRC = (
    OUTPUT_ROOT
    / "raw/download/pajero_iv/maintenance/Service_Manual_2008_2013/img"
)

# ---------------------------------------------------------------------------
# Manual types and years
# ---------------------------------------------------------------------------

_SVG_WRENCH = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>'
_SVG_WARN   = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" x2="12" y1="9" y2="13"/><line x1="12" x2="12.01" y1="17" y2="17"/></svg>'
_SVG_RULER  = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21.3 15.3a2.4 2.4 0 0 1 0 3.4l-2.6 2.6a2.4 2.4 0 0 1-3.4 0L2.7 8.7a2.4 2.4 0 0 1 0-3.4l2.6-2.6a2.4 2.4 0 0 1 3.4 0Z"/><path d="m14.5 12.5 2-2"/><path d="m11.5 9.5 2-2"/><path d="m8.5 6.5 2-2"/><path d="m17.5 15.5 2-2"/></svg>'
_SVG_CHIP   = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="6" height="6" rx="1"/><path d="M15 2v3M9 2v3M15 19v3M9 19v3M2 9h3M2 15h3M19 9h3M19 15h3"/><rect x="2" y="2" width="20" height="20" rx="3"/></svg>'
_SVG_BOOK   = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>'

CATEGORIES = [
    {"slug": "maintenance",  "title": "Maintenance",     "icon": _SVG_WRENCH, "desc": "Routine service tasks"},
    {"slug": "troubleshoot", "title": "Troubleshoot",    "icon": _SVG_WARN,   "desc": "Diagnose a problem"},
    {"slug": "specs",        "title": "Specifications",  "icon": _SVG_RULER,  "desc": "Torques, fluids, clearances"},
    {"slug": "codes",        "title": "Fault Codes",     "icon": _SVG_CHIP,   "desc": "DTC decoder & repair"},
    {"slug": "reference",    "title": "Reference",       "icon": _SVG_BOOK,   "desc": "Fuses, wiring, schedules"},
]

MANUAL_NAMES: dict[str, str] = {
    "M1": "Service Manual",
    "M2": "Technical Information Manual",
    "M4": "Body Repair Manual",
}

YEAR_LABELS: dict[str, str] = {
    "2008_eur": "2008 EUR",
    "2008_ge":  "2008 GE",
    "2009":     "2009",
    "2010":     "2010",
    "2011":     "2011",
    "2012":     "2012",
    "2013":     "2013",
}

MANUAL_ORDER = ["M1", "M2", "M4"]
YEAR_ORDER   = ["2008_eur", "2008_ge", "2009", "2010", "2011", "2012", "2013"]

# ---------------------------------------------------------------------------
# Site configuration (easily changeable for production deployment)
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8000"  # Change to production URL when available
SITE_NAME = "Gen IV Manual"  # Change when final site name is decided (e.g., "Shogun Manual", "Montero Manual")

# ---------------------------------------------------------------------------
# Group name map
# ---------------------------------------------------------------------------

GROUP_NAMES: dict[str, str] = {
    "00": "General Information",
    "11": "Engine",
    "12": "Engine Lubrication",
    "13": "Engine Cooling & Fuel",
    "14": "Intake & Exhaust",
    "15": "Ignition",
    "16": "Starting & Charging",
    "17": "Engine Electrical",
    "21": "Clutch",
    "22": "Manual Transmission",
    "23": "Automatic Transmission",
    "25": "Transfer Case",
    "26": "Front Axle",
    "27": "Rear Axle",
    "31": "Front Suspension",
    "32": "Rear Suspension",
    "33": "Wheels & Tyres",
    "34": "Driveshaft",
    "35": "Brakes",
    "36": "Parking Brake",
    "37": "Power Steering",
    "42": "Body",
    "51": "Body Electrical",
    "52": "Security & ETACS",
    "54": "Heater & Air Conditioning",
    "55": "Air Conditioning",
    "70": "Maintenance",
    "80": "Diagnostics",
    "90": "Special Tools & Index",
}


def group_name(code: str) -> str:
    return GROUP_NAMES.get(code, f"Section {code}")


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

STYLE_CSS = """\
:root {
  --brand:       #c0392b;
  --brand-light: #e74c3c;
  --hdr:         #1a1a2e;
  --sb-bg:       #16213e;
  --sb-text:     #b8c1cc;
  --sb-hover:    #0f3460;
  --bg:          #f4f6f8;
  --surface:     #ffffff;
  --text:        #2c3e50;
  --muted:       #7f8c8d;
  --border:      #dde1e7;
  --th-bg:       #f0f4f8;
  --torque-bg:   #fff8e1;
  --torque-brd:  #f39c12;
  --r:           6px;
  --shadow:      0 1px 4px rgba(0,0,0,.1);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 15px; line-height: 1.65; color: var(--text); background: var(--bg);
}

/* ── Header ── */
.site-header {
  position: sticky; top: 0; z-index: 100;
  background: var(--hdr); color: #fff;
  display: flex; align-items: center; gap: 1.5rem;
  padding: 0 1.5rem; height: 54px;
  box-shadow: 0 2px 8px rgba(0,0,0,.35);
}
.site-brand {
  font-size: .95rem; font-weight: 700;
  color: #fff; text-decoration: none; white-space: nowrap;
}
.site-brand em { color: var(--brand); font-style: normal; }
.search-form { flex: 1; max-width: 460px; position: relative; }
.search-form input {
  width: 100%; padding: .42rem .9rem .42rem 2.1rem;
  border: none; border-radius: 20px;
  background: rgba(255,255,255,.13); color: #fff; font-size: .88rem; outline: none;
  transition: background .2s;
}
.search-form input::placeholder { color: rgba(255,255,255,.45); }
.search-form input:focus { background: rgba(255,255,255,.22); }
.search-icon {
  position: absolute; left: .65rem; top: 50%; transform: translateY(-50%);
  opacity: .55; pointer-events: none;
}

/* ── Layout ── */
.layout { display: grid; grid-template-columns: 265px 1fr; min-height: calc(100vh - 54px); }

/* ── Sidebar ── */
.sidebar {
  background: var(--sb-bg); color: var(--sb-text);
  position: sticky; top: 54px; height: calc(100vh - 54px);
  overflow-y: auto; padding: .75rem 0;
  scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.12) transparent;
}
.sb-label {
  font-size: .68rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .09em; color: rgba(255,255,255,.3);
  padding: .8rem 1rem .2rem;
}
.sb-divider { border: none; border-top: 1px solid rgba(255,255,255,.07); margin: .5rem 0; }
.sidebar a {
  display: flex; align-items: baseline; gap: .4rem;
  padding: .32rem 1rem;
  color: var(--sb-text); text-decoration: none; font-size: .82rem;
  border-left: 3px solid transparent; transition: background .12s, color .12s;
}
.sidebar a:hover  { background: var(--sb-hover); color: #fff; }
.sidebar a.active { border-left-color: var(--brand); background: rgba(192,57,43,.15); color: #fff; }
.sb-count {
  margin-left: auto; font-size: .7rem;
  background: rgba(255,255,255,.1); padding: .08rem .38rem; border-radius: 10px;
}
.sb-year-grid {
  display: flex; flex-wrap: wrap; gap: .3rem; padding: .4rem 1rem .6rem;
}
.sb-year-btn {
  display: inline-block;
  padding: .18rem .55rem; border-radius: 4px; font-size: .75rem;
  background: rgba(255,255,255,.08); color: var(--sb-text);
  text-decoration: none; transition: background .12s, color .12s;
}
.sb-year-btn:hover { background: var(--sb-hover); color: #fff; }
.sb-year-btn.active { background: var(--brand); color: #fff; }

/* ── Content ── */
.content { padding: 2rem 2.5rem; max-width: 960px; }

/* ── Breadcrumb ── */
.breadcrumb {
  display: flex; gap: .35rem; align-items: center; flex-wrap: wrap;
  font-size: .8rem; color: var(--muted); margin-bottom: 1.25rem;
}
.breadcrumb a { color: var(--brand); text-decoration: none; }
.breadcrumb a:hover { text-decoration: underline; }
.breadcrumb-sep { color: var(--border); }

/* ── Page title ── */
h1.page-title {
  font-size: 1.65rem; font-weight: 700; color: #1a1a2e;
  margin-bottom: 1.5rem; line-height: 1.2;
  padding-bottom: .65rem; border-bottom: 2px solid var(--border);
}

/* ── Manual content ── */
.manual-content h1,.manual-content h2,.manual-content h3,
.manual-content h4,.manual-content h5 {
  margin: 1.4rem 0 .65rem; line-height: 1.25; color: #1a1a2e;
}
.manual-content h1 { font-size: 1.35rem; border-bottom: 1px solid var(--border); padding-bottom: .35rem; }
.manual-content h2 { font-size: 1.15rem; }
.manual-content h3 { font-size: 1rem; }
.manual-content p  { margin-bottom: .85rem; }
.manual-content ul,.manual-content ol { margin: .45rem 0 .85rem 1.4rem; }
.manual-content li { margin-bottom: .15rem; }
.manual-content strong { color: #1a1a2e; }
.manual-content img {
  max-width: 100%; height: auto; display: block; margin: .7rem 0;
  border: 1px solid var(--border); border-radius: var(--r); cursor: zoom-in;
}
.manual-content table {
  border-collapse: collapse; width: 100%; margin: .9rem 0;
  font-size: .87rem; box-shadow: var(--shadow);
  border-radius: var(--r); overflow: hidden;
}
.manual-content table th {
  background: var(--th-bg); font-weight: 600;
  text-align: left; padding: .48rem .7rem;
  border-bottom: 2px solid var(--border);
}
.manual-content table td { padding: .42rem .7rem; border-bottom: 1px solid var(--border); }
.manual-content table tr:last-child td { border-bottom: none; }
.manual-content table tr:nth-child(even) td { background: #fafbfc; }

/* ── Stats ── */
.stats { display: flex; gap: .85rem; flex-wrap: wrap; margin: 1.25rem 0 2rem; }
.stat {
  background: var(--hdr); color: #fff;
  padding: .55rem 1.1rem; border-radius: 22px; font-size: .87rem;
}
.stat strong { color: var(--brand); font-size: 1.05rem; }

/* ── Hero ── */
.hero { margin-bottom: 1.5rem; }
.hero h1 { font-size: 2rem; font-weight: 800; color: #1a1a2e; line-height: 1.15; }
.hero h1 em { color: var(--brand); font-style: normal; }
.hero p { font-size: 1rem; color: var(--muted); margin-top: .5rem; }

/* ── Manual × Year matrix ── */
.matrix { margin-bottom: 2.5rem; }
.matrix-manual {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); margin-bottom: 1rem; overflow: hidden;
  box-shadow: var(--shadow);
}
.matrix-header {
  display: flex; align-items: center; gap: .75rem;
  padding: .75rem 1.1rem;
  background: var(--hdr); color: #fff;
}
.matrix-header .m-code {
  font-size: .72rem; font-weight: 700; font-family: monospace;
  background: var(--brand); padding: .12rem .45rem; border-radius: 4px;
}
.matrix-header .m-name { font-weight: 600; font-size: .95rem; }
.matrix-header .m-total { margin-left: auto; font-size: .8rem; color: rgba(255,255,255,.5); }
.matrix-years {
  display: flex; flex-wrap: wrap; gap: .5rem; padding: .85rem 1.1rem;
}
.year-card {
  display: flex; flex-direction: column; align-items: center;
  padding: .55rem .9rem; border-radius: var(--r);
  border: 1px solid var(--border); background: var(--bg);
  text-decoration: none; color: var(--text);
  transition: border-color .15s, background .15s, box-shadow .15s;
  min-width: 80px;
}
.year-card:hover {
  border-color: var(--brand); background: #fff;
  box-shadow: 0 3px 10px rgba(192,57,43,.15);
}
.year-card .yc-label { font-weight: 700; font-size: .92rem; color: #1a1a2e; }
.year-card .yc-count { font-size: .75rem; color: var(--muted); margin-top: .1rem; }
.year-card-empty {
  padding: .55rem .9rem; border-radius: var(--r);
  border: 1px dashed var(--border); background: transparent;
  color: var(--muted); font-size: .8rem; min-width: 80px;
  display: flex; align-items: center; justify-content: center;
}

/* ── Section grid ── */
.section-heading { font-size: 1rem; font-weight: 700; color: #1a1a2e; margin: 1.5rem 0 .75rem; }
.section-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr));
  gap: .85rem; margin-bottom: 2.5rem;
}
.section-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); padding: 1rem 1.15rem;
  text-decoration: none; color: var(--text); box-shadow: var(--shadow);
  transition: border-color .15s, box-shadow .15s, transform .1s; display: block;
}
.section-card:hover {
  border-color: var(--brand); box-shadow: 0 4px 12px rgba(192,57,43,.15);
  transform: translateY(-2px);
}
.sc-code { font-size: .72rem; font-weight: 700; color: var(--brand); font-family: monospace; margin-bottom: .25rem; }
.sc-name { font-weight: 600; font-size: .92rem; margin-bottom: .2rem; }
.sc-count { font-size: .78rem; color: var(--muted); }

/* ── View header (year+manual context banner) ── */
.view-header {
  display: flex; align-items: center; gap: .75rem; flex-wrap: wrap;
  background: var(--hdr); color: #fff;
  padding: .7rem 1.1rem; border-radius: var(--r);
  margin-bottom: 1.5rem; font-size: .87rem;
}
.view-header .vh-manual {
  font-weight: 700; font-size: 1rem;
}
.view-header .vh-sep { color: rgba(255,255,255,.3); }
.view-header .vh-year {
  background: var(--brand); padding: .1rem .5rem; border-radius: 4px;
  font-size: .82rem; font-weight: 700;
}
.view-header .vh-change {
  margin-left: auto; font-size: .78rem; color: rgba(255,255,255,.6);
  text-decoration: none;
}
.view-header .vh-change:hover { color: #fff; }

/* ── Page list (group page) ── */
.page-list { list-style: none; margin: .75rem 0; }
.page-list li { border-bottom: 1px solid var(--border); }
.page-list a {
  display: flex; justify-content: space-between; align-items: baseline;
  padding: .55rem .2rem; color: var(--text); text-decoration: none;
  font-size: .88rem; transition: color .12s;
}
.page-list a:hover { color: var(--brand); }
.pid { font-size: .7rem; font-family: monospace; color: var(--muted); white-space: nowrap; margin-left: .75rem; }

/* ── Pagination ── */
.pagination {
  display: flex; justify-content: space-between;
  margin-top: 2.5rem; padding-top: 1.25rem; border-top: 1px solid var(--border);
}
.pagination a { color: var(--brand); text-decoration: none; font-size: .87rem; max-width: 46%; }
.pagination a:hover { text-decoration: underline; }
.pag-label { font-size: .7rem; text-transform: uppercase; color: var(--muted); margin-bottom: .15rem; }

/* ── Torque callout ── */
.torque-box {
  background: var(--torque-bg); border-left: 4px solid var(--torque-brd);
  padding: .7rem 1rem; border-radius: 0 var(--r) var(--r) 0; margin: 1.25rem 0;
}
.torque-box h4 {
  font-size: .75rem; text-transform: uppercase; letter-spacing: .07em;
  color: #a0640a; margin-bottom: .45rem;
}
.torque-box ul { margin-left: 1.1rem; font-size: .87rem; }
.torque-box li { margin-bottom: .1rem; }

/* ── Part numbers ── */
.part-box {
  background: #eef2ff; border-left: 4px solid #4f46e5;
  padding: .7rem 1rem; border-radius: 0 var(--r) var(--r) 0; margin: 1.25rem 0;
}
.part-box h4 { font-size: .75rem; text-transform: uppercase; letter-spacing: .07em; color: #3730a3; margin-bottom: .45rem; }
.part-box code { font-size: .85rem; font-family: monospace; }

/* ── Search page ── */
.search-hero { margin-bottom: 1.5rem; }
.search-hero h1 { font-size: 1.5rem; font-weight: 700; color: #1a1a2e; margin-bottom: .75rem; }
.search-big {
  width: 100%; padding: .65rem 1rem; font-size: 1rem;
  border: 2px solid var(--border); border-radius: var(--r); outline: none;
  transition: border-color .2s;
}
.search-big:focus { border-color: var(--brand); }
.result-meta { font-size: .82rem; color: var(--muted); margin: .75rem 0 1rem; }
.search-result {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); padding: .9rem 1.1rem; margin-bottom: .6rem;
  box-shadow: var(--shadow);
}
.search-result a { text-decoration: none; color: var(--text); display: block; }
.sr-title { font-weight: 600; font-size: .97rem; color: var(--brand); }
.sr-group { font-size: .76rem; color: var(--muted); margin: .2rem 0; }
.sr-snippet { font-size: .85rem; color: var(--muted); margin-top: .25rem; }
mark { background: #fff176; padding: .02rem .12rem; border-radius: 2px; }

/* ── Lightbox ── */
.lb-overlay {
  display: none; position: fixed; inset: 0; z-index: 9999;
  background: rgba(0,0,0,.88); cursor: zoom-out;
  align-items: center; justify-content: center;
}
.lb-overlay.open { display: flex; }
.lb-overlay img { max-width: 92vw; max-height: 92vh; object-fit: contain; border-radius: var(--r); }

/* ── Full-width layout (landing, category, topic pages) ── */
.layout-full { min-height: calc(100vh - 54px); }
.content-full { max-width: 1080px; margin: 0 auto; padding: 0; }

/* ── Landing hero ── */
.landing-hero {
  background: var(--hdr); color: #fff;
  padding: 4rem 2.5rem 3.5rem; text-align: center;
}
.landing-hero h1 { font-size: 2.4rem; font-weight: 800; margin-bottom: .65rem; }
.landing-hero h1 em { color: var(--brand); font-style: normal; }
.landing-hero p { font-size: 1rem; color: rgba(255,255,255,.6); max-width: 560px; margin: 0 auto 2rem; }
.landing-search {
  display: flex; max-width: 560px; margin: 0 auto; gap: .5rem;
}
.landing-search input {
  flex: 1; padding: .75rem 1.1rem; font-size: 1rem;
  border: none; border-radius: var(--r); outline: none; color: var(--text);
}
.landing-search button {
  padding: .75rem 1.5rem; background: var(--brand); color: #fff;
  border: none; border-radius: var(--r); font-size: 1rem; font-weight: 700;
  cursor: pointer; transition: background .15s; white-space: nowrap;
}
.landing-search button:hover { background: var(--brand-light); }

/* ── Category grid ── */
.landing-body { padding: 2.5rem 2.5rem 0; }
.landing-section-title { font-size: 1rem; font-weight: 700; color: #1a1a2e; margin-bottom: .85rem; }
.category-grid {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-bottom: 2.5rem;
}
.category-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--r); padding: 1.4rem 1.2rem;
  text-decoration: none; color: var(--text); box-shadow: var(--shadow);
  transition: border-color .15s, box-shadow .15s, transform .1s;
  display: flex; flex-direction: column; gap: .3rem;
}
.category-card:hover {
  border-color: var(--brand); box-shadow: 0 6px 18px rgba(192,57,43,.18); transform: translateY(-3px);
}
.cat-icon { width: 2rem; height: 2rem; margin-bottom: .4rem; color: var(--brand); }
.cat-title { font-size: 1.05rem; font-weight: 700; color: #1a1a2e; }
.cat-desc { font-size: .82rem; color: var(--muted); }
.cat-count { font-size: .75rem; color: var(--brand); font-weight: 600; margin-top: .4rem; }

/* ── Popular topic chips ── */
.topic-chips { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 2.5rem; }
.topic-chip {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 20px; padding: .3rem .85rem; font-size: .83rem;
  text-decoration: none; color: var(--text); transition: border-color .12s, color .12s;
}
.topic-chip:hover { border-color: var(--brand); color: var(--brand); }

/* ── Archive bar ── */
.archive-bar {
  padding: 1.25rem 2.5rem; border-top: 1px solid var(--border);
  font-size: .84rem; color: var(--muted);
}
.archive-bar a { color: var(--brand); text-decoration: none; }
.archive-bar a:hover { text-decoration: underline; }

/* ── Category index page ── */
.cat-page-header {
  display: flex; align-items: center; gap: .85rem;
  padding: 2rem 0 1.5rem; border-bottom: 1px solid var(--border); margin-bottom: 1.5rem;
}
.cat-page-icon { width: 2.5rem; height: 2.5rem; flex-shrink: 0; color: var(--brand); }
.cat-page-header h1 { font-size: 1.75rem; font-weight: 800; color: #1a1a2e; }
.cat-page-header p { font-size: .88rem; color: var(--muted); margin-top: .2rem; }
.topic-card {
  display: flex; align-items: center; gap: .75rem;
  padding: 10px 14px; background: var(--surface);
  border: 1px solid var(--border); border-radius: var(--r);
  text-decoration: none; color: var(--text); margin-bottom: .35rem;
  box-shadow: var(--shadow); transition: border-color .15s, transform .08s;
}
.topic-card:hover { border-color: var(--brand); transform: translateX(3px); }
.engine-filter { display: flex; gap: .5rem; margin-bottom: .85rem; flex-wrap: wrap; }
.ef-btn { padding: 5px 14px; border: 1px solid var(--border); border-radius: 20px; background: var(--surface); color: var(--muted); font-size: .8rem; cursor: pointer; transition: background .12s, color .12s, border-color .12s; }
.ef-btn.active, .ef-btn:hover { background: var(--brand); color: #fff; border-color: var(--brand); }
.tc-icon { font-size: 1.4rem; width: 2rem; text-align: center; flex-shrink: 0; }
.tc-body { flex: 1; }
.tc-title { font-weight: 600; font-size: .95rem; color: #1a1a2e; }
.tc-desc { font-size: .78rem; color: var(--muted); margin-top: .05rem; }
.tc-meta { display: flex; gap: .65rem; align-items: center; flex-shrink: 0; font-size: .78rem; color: var(--muted); }
.tc-diff { font-weight: 600; }
.diff-1 { color: #27ae60; }
.diff-2 { color: #f39c12; }
.diff-3 { color: #e67e22; }
.diff-4 { color: var(--brand); }
.diff-5 { color: #c0392b; }

/* ── Topic detail page ── */
.topic-header { margin-bottom: 1.5rem; }
.topic-header h1 { font-size: 1.7rem; font-weight: 800; color: #1a1a2e; margin-bottom: .75rem; }
.topic-tags { display: flex; flex-wrap: wrap; gap: .45rem; margin-bottom: 1rem; }
.ttag {
  display: inline-flex; align-items: center; gap: .3rem;
  padding: .2rem .65rem; border-radius: 4px; font-size: .78rem;
  background: var(--bg); border: 1px solid var(--border); color: var(--muted);
}
.ttag.diff { color: var(--text); font-weight: 700; border-color: transparent; background: transparent; padding-left: 0; }
.ttag.time { }
.ttag.engine { font-family: monospace; font-size: .75rem; }
.topic-intro { font-size: .95rem; line-height: 1.7; color: var(--text); margin-bottom: 1.75rem; border-bottom: 1px solid var(--border); padding-bottom: 1.5rem; }
.topic-h2 { font-size: 1.05rem; font-weight: 700; color: #1a1a2e; margin: 1.75rem 0 .75rem; }
.symptoms-list { list-style: none; }
.symptoms-list li { padding: .3rem 0 .3rem 1.3rem; position: relative; font-size: .9rem; border-bottom: 1px solid var(--border); }
.symptoms-list li:last-child { border-bottom: none; }
.symptoms-list li::before { content: "→"; position: absolute; left: 0; color: var(--brand); font-weight: 700; }

/* Parts table */
.parts-table { width: 100%; border-collapse: collapse; margin: .5rem 0 .75rem; font-size: .87rem; }
.parts-table th {
  text-align: left; padding: .4rem .65rem; background: var(--th-bg);
  font-size: .74rem; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); border-bottom: 2px solid var(--border);
}
.parts-table td { padding: .5rem .65rem; border-bottom: 1px solid var(--border); vertical-align: middle; }
.parts-table tr:last-child td { border-bottom: none; }
.oem-badge {
  font-family: monospace; font-size: .8rem; background: var(--bg);
  border: 1px solid var(--border); padding: .1rem .4rem; border-radius: 3px;
  color: var(--text); white-space: nowrap;
}
.parts-note { font-size: .77rem; color: var(--muted); margin-top: .15rem; }
.shop-links { display: flex; gap: .3rem; flex-wrap: wrap; }
.shop-btn {
  display: inline-block; padding: .15rem .55rem;
  border: 1px solid var(--border); border-radius: 4px; font-size: .73rem;
  color: var(--brand); text-decoration: none; white-space: nowrap;
  transition: background .12s, color .12s, border-color .12s;
}
.shop-btn:hover { background: var(--brand); color: #fff; border-color: var(--brand); }
.verify-note {
  font-size: .76rem; color: #d68910; background: #fef9e7;
  border: 1px solid #f9e79f; border-radius: 4px; padding: .4rem .75rem;
  margin-top: .5rem; margin-bottom: .75rem;
}

/* Tools list */
.tools-grid { columns: 2; gap: 1rem; list-style: none; margin: .35rem 0; }
.tools-grid li { padding: .22rem 0 .22rem 1.1rem; font-size: .88rem; position: relative; break-inside: avoid; }
.tools-grid li::before { content: "→"; position: absolute; left: 0; color: var(--brand); }

/* Tips box */
.tips-box {
  background: #f0f8ff; border-left: 4px solid #3498db;
  padding: .7rem 1rem; border-radius: 0 var(--r) var(--r) 0; margin: 1.25rem 0;
}
.tips-box h4 { font-size: .75rem; text-transform: uppercase; letter-spacing: .07em; color: #1a6b9a; margin-bottom: .45rem; }
.tips-box ul { margin-left: 1.1rem; font-size: .87rem; }
.tips-box li { margin-bottom: .3rem; }

/* Procedure refs */
.proc-refs { list-style: none; margin: .5rem 0; }
.proc-refs li { margin-bottom: .4rem; }
.proc-refs a { color: var(--brand); text-decoration: none; font-size: .88rem; }
.proc-refs a::before { content: "📖 "; }
.proc-refs a:hover { text-decoration: underline; }

/* Related topics */
.related-chips { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .75rem; }
.related-chip {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 20px; padding: .3rem .85rem; font-size: .83rem;
  text-decoration: none; color: var(--text); transition: border-color .12s, color .12s;
}
.related-chip:hover { border-color: var(--brand); color: var(--brand); }

/* Feedback bar */
.feedback-bar {
  margin-top: 2.5rem; padding: 1.25rem 0;
  border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;
}
.feedback-btn {
  background: none; border: 1px solid var(--border); border-radius: var(--r);
  padding: .45rem 1rem; font-size: .83rem; color: var(--muted);
  cursor: pointer; transition: border-color .12s, color .12s;
}
.feedback-btn:hover { border-color: var(--brand); color: var(--brand); }
.feedback-note { font-size: .78rem; color: var(--muted); }

/* Sidebar categories mode */
.sb-cat-icon { width: 1rem; height: 1rem; margin-right: .4rem; vertical-align: middle; display: inline-block; flex-shrink: 0; }

/* ── Hamburger (mobile) ── */
.hamburger {
  display: none; background: none; border: none; cursor: pointer;
  color: #fff; padding: .35rem; margin-left: .5rem; flex-shrink: 0;
}
.hamburger svg { display: block; }

/* ── Sidebar overlay (mobile) ── */
.sb-overlay {
  display: none; position: fixed; inset: 0; top: 54px; z-index: 199;
  background: rgba(0,0,0,.45);
}
.sb-overlay.open { display: block; }

/* ── Page TOC ── */
.page-toc {
  border: 1px solid var(--border); border-radius: var(--r);
  background: var(--surface); margin-bottom: 1.5rem;
  box-shadow: var(--shadow); overflow: hidden;
}
.page-toc summary {
  padding: .55rem .9rem; font-weight: 600; font-size: .8rem;
  color: var(--muted); text-transform: uppercase; letter-spacing: .06em;
  cursor: pointer; user-select: none; list-style: none;
  display: flex; align-items: center; gap: .45rem;
}
.page-toc summary::-webkit-details-marker { display: none; }
.toc-arrow { transition: transform .15s; display: inline-block; font-size: .7rem; }
.page-toc[open] .toc-arrow { transform: rotate(90deg); }
.toc-list { padding: .25rem .9rem .7rem 1rem; list-style: none; border-top: 1px solid var(--border); }
.toc-list li { margin-bottom: .15rem; }
.toc-list a { color: var(--text); text-decoration: none; font-size: .83rem; line-height: 1.4; }
.toc-list a:hover { color: var(--brand); }
.toc-h3 { padding-left: 1rem; }
.toc-h3 a { font-size: .79rem; color: var(--muted); }
.toc-h3 a:hover { color: var(--brand); }

/* ── Sidebar sub-pages ── */
.sb-subpages { padding: .1rem 0 .5rem 1rem; }
.sb-subpages a {
  display: block; padding: .2rem .5rem; font-size: .78rem;
  color: var(--sb-text); text-decoration: none;
  border-left: 2px solid rgba(255,255,255,.08);
  transition: color .1s, border-color .1s; line-height: 1.35;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.sb-subpages a:hover { color: #fff; border-left-color: rgba(255,255,255,.3); }
.sb-subpages a.current { color: #fff; border-left-color: var(--brand); font-weight: 600; }

/* ── Resume banner (landing) ── */
#resume-banner {
  display: none; align-items: center; gap: .9rem;
  background: var(--surface); border: 1px solid var(--border);
  border-left: 4px solid var(--brand); border-radius: var(--r);
  padding: .7rem 1.1rem; margin-bottom: 1.5rem; box-shadow: var(--shadow);
  font-size: .88rem;
}
#resume-banner .rb-label { color: var(--muted); }
#resume-banner a { font-weight: 600; color: var(--brand); text-decoration: none; }
#resume-banner a:hover { text-decoration: underline; }
#resume-banner .rb-dismiss {
  margin-left: auto; background: none; border: none; cursor: pointer;
  color: var(--muted); font-size: 1.2rem; line-height: 1; padding: 0 .2rem;
}

/* ── Search filters ── */
.search-filters {
  display: flex; flex-wrap: wrap; gap: .4rem; align-items: center;
  margin-bottom: 1.1rem; padding-bottom: .9rem; border-bottom: 1px solid var(--border);
}
.sf-group { display: flex; flex-wrap: wrap; gap: .35rem; align-items: center; }
.sf-label { font-size: .73rem; color: var(--muted); white-space: nowrap; margin-right: .15rem; }
.sf-sep { width: 1px; height: 18px; background: var(--border); margin: 0 .3rem; }
.filter-chip {
  padding: .2rem .65rem; border-radius: 14px; font-size: .77rem; cursor: pointer;
  border: 1px solid var(--border); background: var(--surface); color: var(--muted);
  transition: background .12s, color .12s, border-color .12s; user-select: none;
}
.filter-chip.on { background: var(--brand); color: #fff; border-color: var(--brand); }
.filter-chip:hover:not(.on) { border-color: var(--brand); color: var(--brand); }

/* ── Responsive ── */
@media (max-width: 760px) {
  .hamburger { display: flex; align-items: center; }
  .layout { grid-template-columns: 1fr; }
  .sidebar {
    position: fixed; top: 54px; left: 0; bottom: 0; width: 280px; z-index: 200;
    height: auto; transform: translateX(-100%); transition: transform .25s ease;
  }
  .sidebar.open { transform: translateX(0); }
  .content { padding: 1.25rem 1rem; }
  .section-grid { grid-template-columns: 1fr 1fr; }
  .matrix-years { gap: .35rem; }
}
"""

# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------

APP_JS = r"""
/* Pajero IV Manual */
(function () {
  'use strict';

  const base = () => document.querySelector('meta[name=base-url]')?.content || '/';

  // ── Image lightbox ──
  const lb = document.getElementById('lb');
  if (lb) {
    const lbImg = lb.querySelector('img');
    document.querySelectorAll('.manual-content img').forEach(img => {
      img.addEventListener('click', () => { lbImg.src = img.src; lb.classList.add('open'); });
    });
    lb.addEventListener('click', () => lb.classList.remove('open'));
    document.addEventListener('keydown', e => { if (e.key === 'Escape') lb.classList.remove('open'); });
  }

  // ── Header search ──
  const sf = document.querySelector('.search-form');
  if (sf) {
    sf.addEventListener('submit', e => {
      e.preventDefault();
      const q = sf.querySelector('input').value.trim();
      if (q) window.location.href = base() + 'search.html?q=' + encodeURIComponent(q);
    });
  }

  // ── Mobile sidebar ──
  const toggle = document.getElementById('sb-toggle');
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.getElementById('sb-overlay');
  if (toggle && sidebar) {
    const open  = () => { sidebar.classList.add('open');    overlay?.classList.add('open'); };
    const close = () => { sidebar.classList.remove('open'); overlay?.classList.remove('open'); };
    toggle.addEventListener('click', () => sidebar.classList.contains('open') ? close() : open());
    overlay?.addEventListener('click', close);
    // close on nav (mobile)
    sidebar.querySelectorAll('a').forEach(a => a.addEventListener('click', close));
  }

  // ── Persistent nav state ──
  const metaYear   = document.querySelector('meta[name=view-year]');
  const metaManual = document.querySelector('meta[name=view-manual]');
  if (metaYear && metaManual) {
    try {
      localStorage.setItem('pajero_year', metaYear.content);
      localStorage.setItem('pajero_manual', metaManual.content);
    } catch(e) {}
  }
  // Resume banner on landing
  const banner = document.getElementById('resume-banner');
  if (banner) {
    try {
      const y = localStorage.getItem('pajero_year');
      const m = localStorage.getItem('pajero_manual');
      if (y && m) {
        const yLabels = JSON.parse(document.getElementById('year-labels-json')?.textContent || '{}');
        const mNames  = JSON.parse(document.getElementById('manual-names-json')?.textContent || '{}');
        const label = (mNames[m] || m) + ' · ' + (yLabels[y] || y);
        const link = banner.querySelector('a');
        link.textContent = label;
        link.href = base() + 'view/' + y + '/' + m + '/index.html';
        banner.style.display = 'flex';
        banner.querySelector('.rb-dismiss').addEventListener('click', () => {
          banner.style.display = 'none';
          try { localStorage.removeItem('pajero_year'); localStorage.removeItem('pajero_manual'); } catch(e) {}
        });
      }
    } catch(e) {}
  }

  // ── Page TOC ──
  const content = document.querySelector('.manual-content');
  if (content) {
    const headings = [...content.querySelectorAll('h2, h3')];
    if (headings.length >= 3) {
      headings.forEach((h, i) => { if (!h.id) h.id = 'toc-' + i; });
      const items = headings.map(h => {
        const cls = h.tagName === 'H3' ? 'toc-h3' : '';
        return `<li class="${cls}"><a href="#${h.id}">${h.textContent.trim()}</a></li>`;
      }).join('');
      const toc = document.createElement('details');
      toc.className = 'page-toc';
      if (window.innerWidth >= 900) toc.open = true;
      toc.innerHTML = `<summary><span class="toc-arrow">▶</span>&nbsp;On this page (${headings.length})</summary><ul class="toc-list">${items}</ul>`;
      content.parentNode.insertBefore(toc, content);
    }
  }

  // ── Sidebar sub-pages ──
  const siblingsEl = document.getElementById('pg-siblings');
  if (siblingsEl) {
    try {
      const data = JSON.parse(siblingsEl.textContent);
      const activeLink = document.querySelector('.sidebar a.active');
      if (activeLink && data.pages.length > 1) {
        const sub = document.createElement('div');
        sub.className = 'sb-subpages';
        sub.innerHTML = data.pages.map(p => {
          const cls = p.id === data.current ? 'current' : '';
          const title = p.title.length > 42 ? p.title.slice(0, 42) + '…' : p.title;
          return `<a class="${cls}" href="${base()}page/${p.id}.html" title="${p.title}">${title}</a>`;
        }).join('');
        activeLink.insertAdjacentElement('afterend', sub);
        sub.querySelector('.current')?.scrollIntoView({ block: 'nearest' });
      }
    } catch(e) {}
  }
})();

/* ── Landing page search ── */
(function() {
  const form = document.getElementById('landing-search');
  if (!form) return;
  form.addEventListener('submit', e => {
    e.preventDefault();
    const q = form.querySelector('input').value.trim();
    if (q) {
      const base = document.querySelector('meta[name=base-url]')?.content || '/';
      window.location.href = base + 'search.html?q=' + encodeURIComponent(q);
    }
  });
})();

/* ── Feedback button ── */
window.openFeedback = function() {
  const title  = encodeURIComponent('Feedback: ' + document.title);
  const body   = encodeURIComponent('Page: ' + window.location.href + '\n\nFeedback:\n\n');
  const email  = document.querySelector('meta[name=feedback-email]')?.content || '';
  if (email) {
    window.location.href = 'mailto:' + email + '?subject=' + title + '&body=' + body;
  } else {
    alert('Feedback email not configured.');
  }
};

"""

# ---------------------------------------------------------------------------
# Jinja2 templates
# ---------------------------------------------------------------------------

_TEMPLATES: dict[str, str] = {}

_TEMPLATES["base.html"] = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="base-url" content="{{ base_url }}">
  {% if sidebar_year %}<meta name="view-year"   content="{{ sidebar_year }}">{% endif %}
  {% if sidebar_manual %}<meta name="view-manual" content="{{ sidebar_manual }}">{% endif %}
  {% if feedback_email %}<meta name="feedback-email" content="{{ feedback_email }}">{% endif %}
  <title>{% block title %}{{ SITE_NAME }}{% endblock %}</title>
  <meta name="description" content="{{ description or 'DIY repair guides and factory service manual searchable database' }}">
  <meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large, max-video-preview:-1">
  <link rel="canonical" href="{{ BASE_URL }}{{ canonical_url or base_url }}">
  <!-- Open Graph -->
  <meta property="og:title" content="{% block og_title %}{{ SITE_NAME }}{% endblock %}">
  <meta property="og:description" content="{{ description or 'DIY repair guides and factory service manual searchable database' }}">
  <meta property="og:url" content="{{ BASE_URL }}{{ canonical_url or base_url }}">
  <meta property="og:type" content="{{ og_type or 'website' }}">
  <meta property="og:image" content="{{ BASE_URL }}/assets/og-image.png">
  <meta property="og:site_name" content="{{ SITE_NAME }}">
  <!-- Twitter Card -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{% block twitter_title %}{{ SITE_NAME }}{% endblock %}">
  <meta name="twitter:description" content="{{ description or 'DIY repair guides and factory service manual searchable database' }}">
  <meta name="twitter:image" content="{{ BASE_URL }}/assets/og-image.png">
  {% if json_ld %}<script type="application/ld+json">{{ json_ld | tojson }}</script>{% endif %}
  <link rel="stylesheet" href="{{ base_url }}assets/style.css">
</head>
<body>
<header class="site-header" data-pagefind-ignore>
  <a class="site-brand" href="{{ base_url }}index.html">Pajero <em>IV</em></a>
  <form class="search-form">
    <svg class="search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2.5">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <input type="search" placeholder="Search…" autocomplete="off">
  </form>
  {% if not full_width %}
  <button id="sb-toggle" class="hamburger" aria-label="Menu">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2">
      <line x1="3" y1="6"  x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>
    </svg>
  </button>
  {% endif %}
</header>
<div id="sb-overlay" class="sb-overlay"></div>

<div class="layout">
  <nav class="sidebar" data-pagefind-ignore>
    {% if sidebar_categories %}
    <div class="sb-label">Topics</div>
    {% for cat in sidebar_categories %}
    <a href="{{ base_url }}{{ cat.slug }}/index.html"
       {% if cat.slug == active_category %}class="active"{% endif %}>
      <span><span class="sb-cat-icon">{{ cat.icon | safe }}</span>{{ cat.title }}</span>
      {% if cat.topic_count %}<span class="sb-count">{{ cat.topic_count }}</span>{% endif %}
    </a>
    {% endfor %}
    <a href="{{ base_url }}find-part/index.html"
       {% if active_category == 'find-part' %}class="active"{% endif %}>
      <span><span class="sb-cat-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="21" r="1"/><circle cx="20" cy="21" r="1"/><path d="M1 1h4l2.68 13.39a2 2 0 0 0 2 1.61h9.72a2 2 0 0 0 2-1.61L23 6H6"/></svg></span>Find a Part</span>
    </a>
    <hr class="sb-divider">
    <a href="{{ base_url }}archive/index.html" style="font-size:.78rem;color:var(--muted)">
      <span><span class="sb-cat-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg></span>Factory manual archive</span>
    </a>
    {% elif sidebar_manual and sidebar_year %}
    <div class="sb-label">Manual</div>
    {% for m in manual_order %}
    <a href="{{ base_url }}view/{{ sidebar_year }}/{{ m }}/index.html"
       {% if m == sidebar_manual %}class="active"{% endif %}>
      {{ manual_names[m] }}
    </a>
    {% endfor %}
    <hr class="sb-divider">
    <div class="sb-label">Year</div>
    <div class="sb-year-grid">
    {% for y in year_order %}
      {% if y in available_years.get(sidebar_manual, []) %}
      <a class="sb-year-btn {% if y == sidebar_year %}active{% endif %}"
         href="{{ base_url }}view/{{ y }}/{{ sidebar_manual }}/index.html">
        {{ year_labels[y] }}
      </a>
      {% endif %}
    {% endfor %}
    </div>
    <hr class="sb-divider">
    <div class="sb-label">Sections</div>
    {% for g in sidebar_groups %}
    <a href="{{ base_url }}view/{{ sidebar_year }}/{{ sidebar_manual }}/group/{{ g.code }}.html"
       {% if g.code == active_group %}class="active"{% endif %}>
      <span>{{ g.code }} · {{ g.name }}</span>
      <span class="sb-count">{{ g.count }}</span>
    </a>
    {% endfor %}
    {% else %}
    <div class="sb-label">Browse</div>
    {% for cat in categories %}
    <a href="{{ base_url }}{{ cat.slug }}/index.html">
      <span><span class="sb-cat-icon">{{ cat.icon | safe }}</span>{{ cat.title }}</span>
    </a>
    {% endfor %}
    <hr class="sb-divider">
    <a href="{{ base_url }}archive/index.html" style="font-size:.78rem;color:var(--muted)">
      <span><span class="sb-cat-icon"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg></span>Factory manual archive</span>
    </a>
    {% endif %}
  </nav>
  <main class="content">
    {% block content %}{% endblock %}
  </main>
</div>
<div id="lb" class="lb-overlay"><img src="" alt=""></div>
<script src="{{ base_url }}assets/app.js"></script>
</body>
</html>
"""

_TEMPLATES["base_wide.html"] = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="base-url" content="{{ base_url }}">
  {% if feedback_email %}<meta name="feedback-email" content="{{ feedback_email }}">{% endif %}
  <title>{% block title %}{{ SITE_NAME }}{% endblock %}</title>
  <meta name="description" content="{{ description or 'DIY repair guides and factory service manual searchable database' }}">
  <meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large, max-video-preview:-1">
  <link rel="canonical" href="{{ BASE_URL }}{{ canonical_url or base_url }}">
  <!-- Open Graph -->
  <meta property="og:title" content="{% block og_title %}{{ SITE_NAME }}{% endblock %}">
  <meta property="og:description" content="{{ description or 'DIY repair guides and factory service manual searchable database' }}">
  <meta property="og:url" content="{{ BASE_URL }}{{ canonical_url or base_url }}">
  <meta property="og:type" content="{{ og_type or 'website' }}">
  <meta property="og:image" content="{{ BASE_URL }}/assets/og-image.png">
  <meta property="og:site_name" content="{{ SITE_NAME }}">
  <!-- Twitter Card -->
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{% block twitter_title %}{{ SITE_NAME }}{% endblock %}">
  <meta name="twitter:description" content="{{ description or 'DIY repair guides and factory service manual searchable database' }}">
  <meta name="twitter:image" content="{{ BASE_URL }}/assets/og-image.png">
  {% if json_ld %}<script type="application/ld+json">{{ json_ld | tojson }}</script>{% endif %}
  <link rel="stylesheet" href="{{ base_url }}assets/style.css">
</head>
<body>
<header class="site-header" data-pagefind-ignore>
  <a class="site-brand" href="{{ base_url }}index.html">Pajero <em>IV</em></a>
  <form class="search-form">
    <svg class="search-icon" width="14" height="14" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="2.5">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
    <input type="search" placeholder="Search…" autocomplete="off">
  </form>
</header>
<div class="layout-full">
  <div class="content-full">{% block content %}{% endblock %}</div>
</div>
<div id="lb" class="lb-overlay"><img src="" alt=""></div>
<script src="{{ base_url }}assets/app.js"></script>
</body>
</html>
"""

_TEMPLATES["index.html"] = """{% extends "base_wide.html" %}
{% block title %}Pajero IV Workshop Hub{% endblock %}
{% block content %}
<script id="year-labels-json" type="application/json">{{ year_labels_json | safe }}</script>
<script id="manual-names-json" type="application/json">{{ manual_names_json | safe }}</script>
<div id="resume-banner" class="resume-banner">
  <span class="rb-label">Resume reading:</span>
  <a href="#">—</a>
  <button class="rb-dismiss" title="Dismiss">×</button>
</div>
<div class="landing-hero">
  <h1>Mitsubishi <em>Pajero IV</em> Workshop Hub</h1>
  <p>Find repair procedures, specs, and parts for your Pajero IV (2007–2013)</p>
  <form id="landing-search" class="landing-search">
    <input type="search" placeholder="Search: timing belt, brake bleed, P0400…" autocomplete="off">
    <button type="submit">Search</button>
  </form>
</div>
<div class="landing-body">
  <p class="landing-section-title">What do you need?</p>
  <div class="category-grid">
    {% for cat in categories %}
    <a class="category-card" href="{{ base_url }}{{ cat.slug }}/index.html">
      <span class="cat-icon">{{ cat.icon | safe }}</span>
      <span class="cat-title">{{ cat.title }}</span>
      <span class="cat-desc">{{ cat.desc }}</span>
      {% if cat.topic_count %}<span class="cat-count">{{ cat.topic_count }} topics</span>{% endif %}
    </a>
    {% endfor %}
  </div>
  {% if popular_topics %}
  <p class="landing-section-title">Popular topics</p>
  <div class="topic-chips">
    {% for t in popular_topics %}
    <a class="topic-chip" href="{{ base_url }}topics/{{ t.slug }}.html">{{ t.title }}</a>
    {% endfor %}
  </div>
  {% endif %}
</div>
<div class="archive-bar">
  <a href="{{ base_url }}archive/index.html">Browse factory service manual archive (2008–2013) →</a>
  &nbsp;·&nbsp; {{ total_pages }} pages across 3 manual types
</div>
{% endblock %}
"""

_TEMPLATES["archive_index.html"] = """{% extends "base_wide.html" %}
{% block title %}Factory Manual Archive — Pajero IV{% endblock %}
{% block content %}
<nav class="breadcrumb">
  <a href="{{ base_url }}index.html">Home</a>
  <span class="breadcrumb-sep">›</span>
  <span>Factory Manual Archive</span>
</nav>
<h1 class="page-title">Factory Service Manual Archive</h1>
<p style="color:var(--muted);font-size:.9rem;margin-bottom:1.5rem;">
  Original Mitsubishi factory content — {{ total_pages }} pages across 3 manual types and 7 model years.
</p>
<div class="matrix">
  {% for m in manual_order %}
  {% set m_pages = matrix[m] %}
  <div class="matrix-manual">
    <div class="matrix-header">
      <span class="m-code">{{ m }}</span>
      <span class="m-name">{{ manual_names[m] }}</span>
      <span class="m-total">{{ m_pages.values()|sum }} pages</span>
    </div>
    <div class="matrix-years">
      {% for y in year_order %}
        {% if y in m_pages %}
        <a class="year-card" href="{{ base_url }}view/{{ y }}/{{ m }}/index.html">
          <span class="yc-label">{{ year_labels[y] }}</span>
          <span class="yc-count">{{ m_pages[y] }} pages</span>
        </a>
        {% endif %}
      {% endfor %}
    </div>
  </div>
  {% endfor %}
</div>
{% endblock %}
"""

_TEMPLATES["category.html"] = """{% extends "base_wide.html" %}
{% block title %}{{ category.title }} — Pajero IV Workshop Hub{% endblock %}
{% block content %}
<nav class="breadcrumb">
  <a href="{{ base_url }}index.html">Home</a>
  <span class="breadcrumb-sep">›</span>
  <span>{{ category.title }}</span>
</nav>
<div class="cat-page-header">
  <span class="cat-page-icon">{{ category.icon | safe }}</span>
  <div>
    <h1>{{ category.title }}</h1>
    <p>{{ category.desc }}</p>
  </div>
</div>
{% if topics %}
{% if show_engine_filter %}
<div class="engine-filter">
  <button class="ef-btn active" data-filter="all">All</button>
  <button class="ef-btn" data-filter="4M41">4M41 Diesel</button>
  <button class="ef-btn" data-filter="6G75">6G75 Petrol</button>
</div>
{% endif %}
<div class="topic-list" id="topic-list">
  {% for t in topics %}
  <a class="topic-card" href="{{ base_url }}topics/{{ t.slug }}.html" data-engines="{{ (t.applies_to.engines | join(',')) if t.applies_to and t.applies_to.engines else 'all' }}">
    <div class="tc-body">
      <div class="tc-title">{{ t.title }}</div>
      <div class="tc-desc">{{ t.description[:100] }}…</div>
    </div>
    <div class="tc-meta">
      <span class="tc-diff diff-{{ t.difficulty }}">{{ ["", "● Easy", "● Moderate", "● Intermediate", "● Hard", "● Expert"][t.difficulty] }}</span>
      <span>{{ t.time_minutes }} min</span>
    </div>
  </a>
  {% endfor %}
</div>
<script>
(function(){
  const btns = document.querySelectorAll('.ef-btn');
  if (!btns.length) return;
  btns.forEach(btn => btn.addEventListener('click', function(){
    btns.forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    const f = this.dataset.filter;
    document.querySelectorAll('#topic-list .topic-card').forEach(card => {
      if (f === 'all') { card.style.display = ''; return; }
      const engines = card.dataset.engines || 'all';
      card.style.display = (engines === 'all' || engines.includes(f)) ? '' : 'none';
    });
  }));
})();
</script>
{% else %}
<p style="color:var(--muted);">Topics for this section are being added. Check back soon.</p>
<p style="margin-top:1rem;">
  In the meantime, you can search the <a href="{{ base_url }}archive/index.html" style="color:var(--brand)">factory manual archive</a>
  or use the <a href="{{ base_url }}search.html" style="color:var(--brand)">full-text search</a>.
</p>
{% endif %}
{% endblock %}
"""

_TEMPLATES["topic.html"] = """{% extends "base.html" %}
{% block title %}{{ topic.title }} — Pajero IV Workshop Hub{% endblock %}
{% block content %}
<nav class="breadcrumb">
  <a href="{{ base_url }}index.html">Home</a>
  <span class="breadcrumb-sep">›</span>
  <a href="{{ base_url }}{{ topic.category }}/index.html">{{ category.title }}</a>
  <span class="breadcrumb-sep">›</span>
  <span>{{ topic.title }}</span>
</nav>
<div class="topic-header">
  <h1>{{ topic.title }}</h1>
  <div class="topic-tags">
    <span class="ttag diff diff-{{ topic.difficulty }}">{{ ["", "● Easy", "● Moderate", "● Intermediate", "● Hard", "● Expert"][topic.difficulty] }}</span>
    <span class="ttag time">⏱ {{ topic.time_minutes }} min</span>
    {% if topic.applies_to and topic.applies_to.engines %}
    {% for eng in topic.applies_to.engines %}
    <span class="ttag engine">{{ eng }}</span>
    {% endfor %}
    {% endif %}
    {% if topic.applies_to and topic.applies_to.years %}
    <span class="ttag">{{ topic.applies_to.years[0] }}–{{ topic.applies_to.years[-1] }}</span>
    {% endif %}
  </div>
</div>
<p class="topic-intro">{{ topic.description }}</p>

{% if topic.symptoms %}
<h2 class="topic-h2">When do you need this?</h2>
<ul class="symptoms-list">
  {% for s in topic.symptoms %}<li>{{ s }}</li>{% endfor %}
</ul>
{% endif %}

{% if parts_with_links %}
<h2 class="topic-h2">Parts needed</h2>
{% if has_verify %}
<p class="verify-note">⚠ Some OEM numbers below should be verified for your specific variant before ordering.</p>
{% endif %}
<table class="parts-table">
  <thead><tr><th>Part</th><th>OEM / Spec</th><th>Buy online</th></tr></thead>
  <tbody>
  {% for p in parts_with_links %}
  <tr>
    <td>
      {{ p.name }}
      {% if p.notes %}<div class="parts-note">{{ p.notes }}</div>{% endif %}
    </td>
    <td>
      {% if p.oem %}<span class="oem-badge">{{ p.oem }}</span>{% endif %}
      {% if p.spec %}<span style="font-size:.82rem;color:var(--muted)">{{ p.spec }}</span>{% endif %}
      {% if p.quantity %}<span style="font-size:.78rem;color:var(--muted);display:block">{{ p.quantity }}</span>{% endif %}
    </td>
    <td>
      {% if p.shop_links %}
      <div class="shop-links">
        {% for sl in p.shop_links %}
        <a class="shop-btn" href="{{ sl.url }}" target="_blank" rel="noopener noreferrer">{{ sl.name }} ›</a>
        {% endfor %}
      </div>
      {% endif %}
    </td>
  </tr>
  {% endfor %}
  </tbody>
</table>
{% endif %}

{% if topic.tools %}
<h2 class="topic-h2">Tools needed</h2>
<ul class="tools-grid">
  {% for t in topic.tools %}<li>{{ t }}</li>{% endfor %}
</ul>
{% endif %}

{% if topic.torque %}
<div class="torque-box" style="margin-top:1.5rem">
  <h4>Torque specifications</h4>
  <ul>
    {% for t in topic.torque %}
    <li><strong>{{ t.component }}:</strong> {{ t.value_nm }} N·m{% if t.note %} — <span style="font-weight:normal">{{ t.note }}</span>{% endif %}</li>
    {% endfor %}
  </ul>
</div>
{% endif %}

{% if topic.tips %}
<div class="tips-box">
  <h4>Tips &amp; common mistakes</h4>
  <ul>{% for tip in topic.tips %}<li>{{ tip }}</li>{% endfor %}</ul>
</div>
{% endif %}

{% if topic.procedure_refs %}
<h2 class="topic-h2">Factory manual reference</h2>
<ul class="proc-refs">
  {% for ref in topic.procedure_refs %}
  <li>
    <a href="{{ base_url }}view/{{ default_year }}/{{ ref.manual }}/group/{{ ref.section }}.html">
      {{ ref.label }}
    </a>
  </li>
  {% endfor %}
</ul>
{% endif %}

{% if related_topics %}
<h2 class="topic-h2">Related topics</h2>
<div class="related-chips">
  {% for rt in related_topics %}
  <a class="related-chip" href="{{ base_url }}topics/{{ rt.slug }}.html">{{ rt.title }}</a>
  {% endfor %}
</div>
{% endif %}

<div class="feedback-bar">
  <button class="feedback-btn" onclick="openFeedback()">✏ Improve this page</button>
  <span class="feedback-note">Found an error or have a better tip? Let us know.</span>
</div>
{% endblock %}
"""

_TEMPLATES["view_index.html"] = """{% extends "base.html" %}
{% block title %}{{ manual_names[manual] }} {{ year_labels[year] }} — Pajero IV Manual{% endblock %}
{% block content %}
<div class="view-header">
  <span class="vh-manual">{{ manual_names[manual] }}</span>
  <span class="vh-sep">·</span>
  <span class="vh-year">{{ year_labels[year] }}</span>
  <a class="vh-change" href="{{ base_url }}index.html">← All manuals</a>
</div>
<div class="stats">
  <div class="stat"><strong>{{ stats.pages }}</strong> pages</div>
  <div class="stat"><strong>{{ stats.groups }}</strong> sections</div>
  <div class="stat"><strong>{{ "{:,}".format(stats.images) }}</strong> illustrations</div>
  <div class="stat"><strong>{{ "{:,}".format(stats.tables) }}</strong> tables</div>
</div>
<p class="section-heading">Browse by section</p>
<div class="section-grid">
  {% for g in sidebar_groups %}
  <a class="section-card" href="{{ base_url }}view/{{ year }}/{{ manual }}/group/{{ g.code }}.html">
    <div class="sc-code">{{ g.code }}</div>
    <div class="sc-name">{{ g.name }}</div>
    <div class="sc-count">{{ g.count }} pages</div>
  </a>
  {% endfor %}
</div>
{% endblock %}
"""

_TEMPLATES["view_group.html"] = """{% extends "base.html" %}
{% block title %}{{ group_code }} · {{ group_name_str }} — {{ manual_names[manual] }} {{ year_labels[year] }}{% endblock %}
{% block content %}
<nav class="breadcrumb">
  <a href="{{ base_url }}index.html">Home</a>
  <span class="breadcrumb-sep">›</span>
  <a href="{{ base_url }}view/{{ year }}/{{ manual }}/index.html">{{ manual_names[manual] }} {{ year_labels[year] }}</a>
  <span class="breadcrumb-sep">›</span>
  <span>{{ group_code }} · {{ group_name_str }}</span>
</nav>
<h1 class="page-title">{{ group_code }} · {{ group_name_str }}</h1>
<p style="color:var(--muted);font-size:.88rem;margin-bottom:1rem;">{{ pages|length }} pages · {{ manual_names[manual] }} · {{ year_labels[year] }}</p>
<ul class="page-list">
  {% for p in pages %}
  <li>
    <a href="{{ base_url }}page/{{ p.page_id }}.html">
      <span>{{ p.title }}</span>
      <span class="pid">{{ p.page_id }}</span>
    </a>
  </li>
  {% endfor %}
</ul>
{% endblock %}
"""

_TEMPLATES["page.html"] = """{% extends "base.html" %}
{% block title %}{{ page.title }} — Pajero IV Manual{% endblock %}
{% block content %}
<script id="pg-siblings" type="application/json">{{ siblings_json | safe }}</script>
<span hidden data-pagefind-filter="manual[{{ page.manual_type }}]"></span>
<span hidden data-pagefind-filter="year[{{ page.year }}]"></span>
<nav class="breadcrumb" data-pagefind-ignore>
  <a href="{{ base_url }}index.html">Home</a>
  <span class="breadcrumb-sep">›</span>
  <a href="{{ base_url }}view/{{ page.year }}/{{ page.manual_type }}/index.html">{{ manual_names[page.manual_type] }} {{ year_labels[page.year] }}</a>
  <span class="breadcrumb-sep">›</span>
  <a href="{{ base_url }}view/{{ page.year }}/{{ page.manual_type }}/group/{{ page.group }}.html">{{ page.group }} · {{ page.group_name }}</a>
  <span class="breadcrumb-sep">›</span>
  <span>{{ page.title }}</span>
</nav>
<h1 class="page-title">{{ page.title }}</h1>

{% if torque_specs %}
<div class="torque-box">
  <h4>Torque specifications on this page</h4>
  <ul>
    {% for t in torque_specs %}
    <li>{{ t.value_nm }} N·m{% if t.tolerance_nm %} ± {{ t.tolerance_nm }} N·m{% endif %}
      {% if t.context %}<span style="color:var(--muted);font-size:.82rem"> — {{ t.context[:80] }}</span>{% endif %}
    </li>
    {% endfor %}
  </ul>
</div>
{% endif %}

{% if part_numbers %}
<div class="part-box">
  <h4>Part numbers referenced</h4>
  {% for p in part_numbers %}<code>{{ p }}</code>{% if not loop.last %} &nbsp;{% endif %}{% endfor %}
</div>
{% endif %}

<div class="manual-content" data-pagefind-body>
  {{ body_html | safe }}
</div>

<div class="pagination" data-pagefind-ignore>
  <div>
    {% if prev %}
    <div class="pag-label">← Previous</div>
    <a href="{{ base_url }}page/{{ prev.page_id }}.html">{{ prev.title }}</a>
    {% endif %}
  </div>
  <div style="text-align:right">
    {% if next %}
    <div class="pag-label">Next →</div>
    <a href="{{ base_url }}page/{{ next.page_id }}.html">{{ next.title }}</a>
    {% endif %}
  </div>
</div>
{% endblock %}
"""

_TEMPLATES["search.html"] = """{% extends "base.html" %}
{% block title %}Search — Pajero IV Manual{% endblock %}
{% block content %}
<div class="search-hero">
  <h1>Search manual</h1>
  <input id="q" class="search-big" type="search" placeholder="e.g. timing belt, torque specs, ABS…" autocomplete="off">
</div>
<div class="search-filters">
  <div class="sf-group">
    <span class="sf-label">Manual:</span>
    {% for m in manual_order %}
    <span class="filter-chip" data-type="manual" data-val="{{ m }}">{{ manual_names[m] }}</span>
    {% endfor %}
  </div>
  <div class="sf-sep"></div>
  <div class="sf-group">
    <span class="sf-label">Year:</span>
    {% for y in year_order %}
    <span class="filter-chip" data-type="year" data-val="{{ y }}">{{ year_labels[y] }}</span>
    {% endfor %}
  </div>
</div>
<p class="result-meta"></p>
<div id="results"><p>Enter a search term above.</p></div>
<script type="module">
(async function(){
  const base = document.querySelector('meta[name=base-url]').content;
  const inp  = document.getElementById('q');
  const resultsEl = document.getElementById('results');
  const metaEl    = document.querySelector('.result-meta');
  const MANUAL_NAMES = {{ manual_names | tojson | safe }};
  const YEAR_LABELS  = {{ year_labels  | tojson | safe }};

  let pagefind;
  try {
    pagefind = await import(base + 'pagefind/pagefind.js');
    await pagefind.init();
  } catch(e) {
    resultsEl.innerHTML = '<p style="color:var(--muted)">Search index not available — run <code>npx pagefind --site site/</code> after building.</p>';
    return;
  }

  const params  = new URLSearchParams(location.search);
  const initial = params.get('q') || '';
  if (initial) inp.value = initial;
  inp.focus();

  document.querySelectorAll('.filter-chip').forEach(chip => {
    chip.classList.add('on');
    chip.addEventListener('click', () => { chip.classList.toggle('on'); doSearch(); });
  });

  function getActive(type) {
    const all = [...document.querySelectorAll(`.filter-chip[data-type="${type}"]`)];
    const on  = all.filter(c => c.classList.contains('on'));
    return on.length === all.length ? [] : on.map(c => c.dataset.val);
  }

  async function doSearch() {
    const q = inp.value.trim();
    if (!q) { resultsEl.innerHTML = '<p>Enter a search term above.</p>'; metaEl.textContent = ''; return; }

    const filters = {};
    const manuals = getActive('manual');
    const years   = getActive('year');
    if (manuals.length) filters.manual = manuals;
    if (years.length)   filters.year   = years;

    const search = await pagefind.search(q, { filters });
    const total  = search.results.length;
    metaEl.textContent = total
      ? `${total} result${total !== 1 ? 's' : ''} for "${q}"`
      : `No results for "${q}"`;

    const top = await Promise.all(search.results.slice(0, 50).map(r => r.data()));
    resultsEl.innerHTML = top.map(d => {
      const manual = d.filters?.manual?.[0] || '';
      const year   = d.filters?.year?.[0]   || '';
      const label  = [MANUAL_NAMES[manual], YEAR_LABELS[year]].filter(Boolean).join(' · ');
      return `<div class="search-result">
        <a href="${d.url}">
          <div class="sr-title">${d.meta.title || ''}</div>
          ${label ? `<div class="sr-group">${label}</div>` : ''}
          <div class="sr-snippet">${d.excerpt}</div>
        </a>
      </div>`;
    }).join('') || '<p>No results.</p>';
  }

  let timer;
  inp.addEventListener('input',   () => { clearTimeout(timer); timer = setTimeout(doSearch, 250); });
  inp.addEventListener('keydown', e => { if (e.key === 'Enter') { clearTimeout(timer); doSearch(); } });
  if (initial) doSearch();
})();
</script>
{% endblock %}
"""

_TEMPLATES["find_part.html"] = """{% extends "base.html" %}
{% block title %}Find a Part — Pajero IV Workshop Hub{% endblock %}
{% block content %}
<nav class="breadcrumb">
  <a href="{{ base_url }}index.html">Home</a>
  <span class="breadcrumb-sep">›</span>
  <span>Find a Part</span>
</nav>
<div class="search-hero">
  <h1>Find a Part</h1>
  <p style="color:var(--muted);margin-top:.25rem">Type a part name or OEM number — get instant search links across multiple suppliers.</p>
  <input id="part-q" class="search-big" type="search" placeholder="e.g. NGK4867, EB955, 1770A023, oil filter…" autocomplete="off" style="margin-top:1rem">
</div>

<div id="shop-results" style="display:none;margin:1.5rem 0 2rem">
  <p style="color:var(--muted);font-size:.88rem;margin-bottom:.75rem">Searching for <strong id="q-display"></strong> on:</p>
  <div style="display:flex;flex-wrap:wrap;gap:.6rem">
    {% for s in shops %}
    <a id="shop-{{ s.id }}" class="shop-btn" href="#" target="_blank" rel="noopener noreferrer"
       style="font-size:.95rem;padding:.6rem 1.1rem">
      {{ s.name }} <span style="opacity:.55;font-size:.8rem">{{ s.region }}</span> ›
    </a>
    {% endfor %}
  </div>
</div>

<h2 class="topic-h2" style="margin-top:2rem">Topics with parts lists</h2>
<p style="color:var(--muted);font-size:.88rem;margin-bottom:1rem">
  These guides include specific OEM part numbers and direct shop links.
</p>
<div class="topic-list">
{% for t in topics_with_parts %}
  <a class="topic-card" href="{{ base_url }}topics/{{ t.slug }}.html">
    <div class="tc-body">
      <div class="tc-title">{{ t.title }}</div>
      <div class="tc-desc">{{ t.parts | length }} part{{ 's' if t.parts | length != 1 else '' }} listed</div>
    </div>
    <div class="tc-meta">
      <span class="tc-diff diff-{{ t.difficulty }}">{{ ["","● Easy","● Moderate","● Intermediate","● Hard","● Expert"][t.difficulty] }}</span>
    </div>
  </a>
{% endfor %}
</div>

<script>
(function(){
  const shops = {{ shops_json | safe }};
  const input = document.getElementById('part-q');
  const results = document.getElementById('shop-results');
  const display = document.getElementById('q-display');
  function update() {
    const q = input.value.trim();
    if (!q) { results.style.display = 'none'; return; }
    results.style.display = '';
    display.textContent = q;
    shops.forEach(function(s) {
      const el = document.getElementById('shop-' + s.id);
      if (el) el.href = s.search_url.replace('{oem}', encodeURIComponent(q));
    });
  }
  input.addEventListener('input', update);
  input.focus();
})();
</script>
{% endblock %}
"""


class _DictLoader(BaseLoader):
    def get_source(self, env, name):
        if name in _TEMPLATES:
            src = _TEMPLATES[name]
            return src, name, lambda: True
        raise TemplateNotFound(name)


_jinja = Environment(loader=_DictLoader(), autoescape=True)

# Add tojson filter for JSON-LD serialization
_jinja.filters["tojson"] = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Raw HTML → clean body HTML
# ---------------------------------------------------------------------------

def _raw_path(source_url: str) -> Path:
    rel = source_url.split("faq.out-club.ru/", 1)[-1]
    return RAW_DIR / rel


def raw_html_to_body(source_url: str, base_url: str) -> str:
    path = _raw_path(source_url)
    if not path.exists():
        return "<p><em>Source file not found.</em></p>"

    raw = path.read_bytes()
    text = None
    for enc in ("utf-8", "windows-1251", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    soup = BeautifulSoup(text, "lxml")

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "mail.ru" in src:
            img.decompose()

    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        abs_src = urljoin(source_url, src)
        m = re.search(r"/img/(.+)$", abs_src)
        if m:
            img["src"] = f"{base_url}img/{m.group(1)}"
            img["loading"] = "lazy"

    for a in soup.find_all("a"):
        href = a.get("href", "")
        if not href.startswith("javascript:"):
            continue
        m = re.search(r"['\"]([^'\"]+\.(?:png|jpg|gif|pdf))['\"]", href, re.IGNORECASE)
        if m:
            abs_src = urljoin(source_url, m.group(1))
            pm = re.search(r"/img/(.+)$", abs_src)
            if pm:
                a["href"] = f"{base_url}img/{pm.group(1)}"
                a["target"] = "_blank"
                a["rel"] = "noopener"
                continue
        a.unwrap()

    for tag in soup.find_all(["script", "style", "meta", "link", "title"]):
        tag.decompose()

    body = soup.find("body") or soup
    first_h1 = body.find("h1")
    if first_h1:
        first_h1.decompose()

    return body.decode_contents()


# ---------------------------------------------------------------------------
# Build helpers
# ---------------------------------------------------------------------------

def build_groups_for(pages: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for p in pages:
        counts[p["group"]] = counts.get(p["group"], 0) + 1
    return sorted(
        [{"code": c, "name": group_name(c), "count": n} for c, n in counts.items()],
        key=lambda g: g["code"],
    )


def build_matrix(pages: list[dict]) -> dict:
    """Returns {manual: {year: count}}."""
    matrix: dict[str, dict[str, int]] = {}
    for p in pages:
        m = p.get("manual_type", "M1")
        y = p.get("year", "2010")
        matrix.setdefault(m, {})
        matrix[m][y] = matrix[m].get(y, 0) + 1
    return matrix


def build_available_years(pages: list[dict]) -> dict[str, list[str]]:
    """Returns {manual: [year, ...]} for sidebar year switcher."""
    result: dict[str, set] = {}
    for p in pages:
        m = p.get("manual_type", "M1")
        y = p.get("year", "2010")
        result.setdefault(m, set()).add(y)
    return {m: sorted(ys, key=lambda y: YEAR_ORDER.index(y) if y in YEAR_ORDER else 99)
            for m, ys in result.items()}




def load_json_data(json_path_rel: str) -> dict:
    path = OUTPUT_ROOT / json_path_rel
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def copy_images(out_dir: Path):
    if not IMG_SRC.exists():
        print("  WARNING: img source dir not found, skipping image copy", file=sys.stderr)
        return
    dest = out_dir / "img"
    if dest.exists():
        shutil.rmtree(dest)
    print(f"  copying images {IMG_SRC} → {dest} …")
    shutil.copytree(IMG_SRC, dest)
    img_count = sum(1 for _ in dest.rglob("*") if _.is_file())
    print(f"  copied {img_count} image files")


# ---------------------------------------------------------------------------
# Content loading (YAML)
# ---------------------------------------------------------------------------

def load_topics(content_dir: Path) -> list[dict]:
    topics_dir = content_dir / "topics"
    if not topics_dir.exists():
        return []
    topics = []
    for f in sorted(topics_dir.glob("*.yml")):
        try:
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
            if data:
                topics.append(data)
        except Exception as e:
            print(f"  WARNING: could not load {f.name}: {e}", file=sys.stderr)
    return topics


def load_shops(content_dir: Path) -> tuple[list[dict], str]:
    shops_file = content_dir / "shops.yml"
    if not shops_file.exists():
        return [], ""
    data = yaml.safe_load(shops_file.read_text(encoding="utf-8"))
    return data.get("shops", []), data.get("feedback_email", "")


def make_shop_url(shop: dict, oem: str) -> str:
    return shop["search_url"].format(oem=oem)


def enrich_topics_with_cats(topics: list[dict]) -> list[dict]:
    cat_map = {c["slug"]: c for c in CATEGORIES}
    for t in topics:
        cat = cat_map.get(t.get("category", ""), {})
        t["_category"] = cat
    return topics


def build_parts_with_links(topic: dict, shops: list[dict]) -> tuple[list, bool]:
    parts = topic.get("parts", [])
    has_verify = False
    enriched = []
    for p in parts:
        row = dict(p)
        if p.get("verify"):
            has_verify = True
        if p.get("oem"):
            row["shop_links"] = [
                {"name": s["name"], "url": make_shop_url(s, p["oem"])}
                for s in shops
            ]
        else:
            row["shop_links"] = []
        enriched.append(row)
    return enriched, has_verify


# ---------------------------------------------------------------------------
# Common template context
# ---------------------------------------------------------------------------

def _base_ctx(base_url: str, sidebar_year: str = "", sidebar_manual: str = "",
               sidebar_groups: list | None = None, active_group: str = "",
               available_years: dict | None = None,
               sidebar_categories: list | None = None, active_category: str = "",
               full_width: bool = False, feedback_email: str = "",
               canonical_url: str = "", description: str = "", og_type: str = "website",
               json_ld: dict | None = None) -> dict:
    return {
        "base_url": base_url,
        "BASE_URL": BASE_URL,
        "SITE_NAME": SITE_NAME,
        "manual_names": MANUAL_NAMES,
        "manual_order": MANUAL_ORDER,
        "year_labels": YEAR_LABELS,
        "year_order": YEAR_ORDER,
        "categories": CATEGORIES,
        "sidebar_year": sidebar_year,
        "sidebar_manual": sidebar_manual,
        "sidebar_groups": sidebar_groups or [],
        "active_group": active_group,
        "available_years": available_years or {},
        "sidebar_categories": sidebar_categories,
        "active_category": active_category,
        "full_width": full_width,
        "feedback_email": feedback_email,
        "canonical_url": canonical_url,
        "description": description,
        "og_type": og_type,
        "json_ld": json_ld,
    }


# ---------------------------------------------------------------------------
# Page generators
# ---------------------------------------------------------------------------

def gen_index(pages: list[dict], base_url: str) -> str:
    matrix = build_matrix(pages)
    total_groups = len({p["group"] for p in pages})
    ctx = _base_ctx(base_url)
    ctx.update({
        "total_pages": len(pages),
        "total_groups": total_groups,
        "matrix": matrix,
        "year_labels_json": json.dumps(YEAR_LABELS),
        "manual_names_json": json.dumps(MANUAL_NAMES),
    })
    return _jinja.get_template("index.html").render(**ctx)


def gen_view_index(year: str, manual: str, pages: list[dict],
                   available_years: dict, base_url: str) -> str:
    groups = build_groups_for(pages)
    stats = {
        "pages": len(pages),
        "groups": len(groups),
        "images": sum(p["image_count"] for p in pages),
        "tables": sum(p["table_count"] for p in pages),
    }
    ctx = _base_ctx(base_url, sidebar_year=year, sidebar_manual=manual,
                    sidebar_groups=groups, available_years=available_years,
                    canonical_url=f"/view/{year}/{manual}/",
                    description=f"Factory service manual {MANUAL_NAMES.get(manual, manual)} for {year}",
                    og_type="website")
    ctx.update({
        "year": year, "manual": manual, "stats": stats,
    })
    return _jinja.get_template("view_index.html").render(**ctx)


def gen_view_group(year: str, manual: str, code: str, pages: list[dict],
                   all_groups: list[dict], available_years: dict, base_url: str) -> str:
    ctx = _base_ctx(base_url, sidebar_year=year, sidebar_manual=manual,
                    sidebar_groups=all_groups, active_group=code,
                    available_years=available_years,
                    canonical_url=f"/view/{year}/{manual}/group/{code}.html",
                    description=f"{group_name(code)} from {year} {MANUAL_NAMES.get(manual, manual)}",
                    og_type="website")
    ctx.update({
        "year": year, "manual": manual,
        "group_code": code,
        "group_name_str": group_name(code),
        "pages": sorted(pages, key=lambda p: p["title"]),
    })
    return _jinja.get_template("view_group.html").render(**ctx)


def gen_content_page(page: dict, prev: dict | None, next_: dict | None,
                     available_years: dict, base_url: str) -> str:
    year = page.get("year", "2010")
    manual = page.get("manual_type", "M1")
    body_html = raw_html_to_body(page["source_url"], base_url)
    jdata = load_json_data(page["json_path"])

    sidebar_groups = page.get("_sidebar_groups", [])
    siblings = page.get("_siblings", [])
    siblings_json = json.dumps({"current": page["page_id"], "pages": siblings})

    json_ld = {
        "@context": "https://schema.org",
        "@type": ["WebPage", "ScholarlyArticle"],
        "headline": page["title"],
        "url": f"{BASE_URL}page/{page['page_id']}.html",
    }

    ctx = _base_ctx(base_url, sidebar_year=year, sidebar_manual=manual,
                    sidebar_groups=sidebar_groups, active_group=page["group"],
                    available_years=available_years,
                    canonical_url=f"/page/{page['page_id']}.html",
                    description=page.get("title", ""),
                    og_type="article",
                    json_ld=json_ld)
    ctx.update({
        "page": {**page, "group_name": group_name(page["group"])},
        "body_html": body_html,
        "torque_specs": jdata.get("torque_specs", []),
        "part_numbers": jdata.get("part_numbers", []),
        "prev": prev,
        "next": next_,
        "siblings_json": siblings_json,
    })
    return _jinja.get_template("page.html").render(**ctx)


def gen_parts_search_page(shops: list[dict], topics: list[dict], sidebar_cats: list[dict],
                          feedback_email: str, base_url: str) -> str:
    import json as _json
    shops_with_parts = [t for t in topics if t.get("parts")]
    ctx = _base_ctx(base_url,
                   sidebar_categories=sidebar_cats,
                   feedback_email=feedback_email,
                   canonical_url="/find-part/index.html",
                   description="Search for Pajero IV parts by name or OEM number and get direct links to AutoDoc, eBay, and Amazon.",
                   og_type="website")
    ctx["shops_json"] = _json.dumps(shops)
    ctx["topics_with_parts"] = shops_with_parts
    return _jinja.get_template("find_part.html").render(**ctx)


def gen_search_page(available_years: dict, base_url: str) -> str:
    ctx = _base_ctx(base_url,
                   canonical_url="/search.html",
                   description="Full-text search of repair guides and factory manual",
                   og_type="website")
    return _jinja.get_template("search.html").render(**ctx)


def gen_landing(pages: list[dict], topics: list[dict], shops: list[dict],
                feedback_email: str, base_url: str) -> str:
    topics_by_cat: dict[str, list] = {}
    for t in topics:
        topics_by_cat.setdefault(t.get("category", ""), []).append(t)

    cats_with_counts = []
    for cat in CATEGORIES:
        c = dict(cat)
        c["topic_count"] = len(topics_by_cat.get(cat["slug"], []))
        cats_with_counts.append(c)
    cats_with_counts.append({
        "slug": "find-part", "title": "Find a Part", "icon": "🛒",
        "desc": "Search by part name or OEM number", "topic_count": 0,
    })

    json_ld = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": SITE_NAME,
        "url": BASE_URL,
        "description": "Complete DIY repair guide and factory service manual database",
        "creator": {"@type": "Organization", "name": "Community"}
    }

    ctx = _base_ctx(
        base_url, full_width=True, feedback_email=feedback_email,
        canonical_url="/", og_type="website",
        description="Complete DIY repair guide and factory service manual database",
        json_ld=json_ld
    )
    ctx.update({
        "categories": cats_with_counts,
        "popular_topics": topics[:6],
        "total_pages": len(pages),
        "year_labels_json": json.dumps(YEAR_LABELS),
        "manual_names_json": json.dumps(MANUAL_NAMES),
    })
    return _jinja.get_template("index.html").render(**ctx)


def gen_archive_index(pages: list[dict], base_url: str) -> str:
    matrix = build_matrix(pages)
    ctx = _base_ctx(
        base_url,
        canonical_url="/archive/",
        description="Browse factory service manual archive by year and type",
        og_type="website"
    )
    ctx.update({
        "total_pages": len(pages),
        "matrix": matrix,
    })
    return _jinja.get_template("archive_index.html").render(**ctx)


def gen_category_page(cat: dict, topics: list[dict], all_cats_with_counts: list[dict],
                      feedback_email: str, base_url: str) -> str:
    json_ld = {
        "@context": "https://schema.org",
        "@type": "Collection",
        "name": cat["title"],
        "description": cat.get("desc", ""),
    }
    ctx = _base_ctx(
        base_url, full_width=True, feedback_email=feedback_email,
        canonical_url=f"/{cat['slug']}/",
        description=cat.get("desc", ""),
        og_type="website",
        json_ld=json_ld
    )
    all_engines = set()
    for t in topics:
        for eng in (t.get("applies_to") or {}).get("engines") or []:
            all_engines.add(eng)
    ctx.update({
        "category": cat,
        "topics": topics,
        "categories": all_cats_with_counts,
        "show_engine_filter": len(all_engines) > 1,
    })
    return _jinja.get_template("category.html").render(**ctx)


def gen_topic_page(topic: dict, topics_by_slug: dict, shops: list[dict],
                   sidebar_cats: list[dict], feedback_email: str, base_url: str) -> str:
    parts_with_links, has_verify = build_parts_with_links(topic, shops)
    related = [topics_by_slug[s] for s in topic.get("related", []) if s in topics_by_slug]
    cat = next((c for c in CATEGORIES if c["slug"] == topic.get("category", "")), CATEGORIES[0])

    engines = topic.get("applies_to", {}).get("engines", [])
    engine_keywords = ", ".join(engines) if engines else ""
    keywords = f"{engine_keywords}, {topic.get('category', '')}" if engine_keywords else topic.get('category', '')

    json_ld = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": topic.get("title", ""),
        "description": topic.get("description", ""),
        "keywords": keywords,
        "author": {"@type": "Organization", "name": "Community"},
        "datePublished": "2024-05-13",
        "image": "/assets/og-image.png",
        "isPartOf": {"@type": "WebSite", "url": BASE_URL}
    }

    ctx = _base_ctx(base_url,
                    sidebar_categories=sidebar_cats,
                    active_category=topic.get("category", ""),
                    feedback_email=feedback_email,
                    canonical_url=f"/topics/{topic['slug']}.html",
                    description=topic.get("description", ""),
                    og_type="article",
                    json_ld=json_ld)
    ctx.update({
        "topic": topic,
        "category": cat,
        "parts_with_links": parts_with_links,
        "has_verify": has_verify,
        "related_topics": related,
        "default_year": "2013",
    })
    return _jinja.get_template("topic.html").render(**ctx)


# ---------------------------------------------------------------------------
# SEO: robots.txt and sitemap.xml generation
# ---------------------------------------------------------------------------

def gen_robots_txt(base_url: str) -> str:
    """Generate robots.txt content with AI crawler allowlist."""
    return f"""User-agent: *
Allow: /

User-agent: CCBot
User-agent: ChatGPT-User
User-agent: GPTBot
User-agent: Gemini
User-agent: Claude-Web
Allow: /

Sitemap: {base_url}sitemap.xml

Disallow: /search
"""


def gen_sitemap_xml(
    base_url: str,
    pages: list[dict],
    topics: list[dict],
    categories: list[dict],
    years: list[str],
    manuals: list[str],
) -> str:
    """Generate sitemap.xml with priority-weighted URLs."""
    from datetime import datetime

    lastmod = datetime.now().isoformat()[:10]  # YYYY-MM-DD
    urls = []

    # Helper to add a URL entry
    def add_url(loc: str, priority: float = 0.5):
        urls.append(f"  <url>\n    <loc>{loc}</loc>\n    <lastmod>{lastmod}</lastmod>\n    <priority>{priority:.1f}</priority>\n  </url>")

    # Homepage (0.9 - highest priority)
    add_url(base_url, 0.9)

    # Topics (0.9 - curated, high-value content)
    for topic in topics:
        add_url(f"{base_url}topics/{topic['slug']}.html", 0.9)

    # Categories (0.8 - landing pages)
    for cat in categories:
        add_url(f"{base_url}{cat['slug']}/", 0.8)

    # Archive index (0.7 - utility page)
    add_url(f"{base_url}archive/", 0.7)

    # View indices - year/manual combinations (0.7)
    for year in years:
        for manual in manuals:
            add_url(f"{base_url}view/{year}/{manual}/", 0.7)

    # View group pages - content sections (0.6 - navigation pages)
    by_ym: dict[tuple, list[dict]] = {}
    for p in pages:
        key = (p.get("year", "2010"), p.get("manual_type", "M1"))
        by_ym.setdefault(key, []).append(p)

    for (year, manual), ym_pages in by_ym.items():
        groups: set[str] = set()
        for p in ym_pages:
            groups.add(p["group"])
        for group in sorted(groups):
            add_url(f"{base_url}view/{year}/{manual}/group/{group}.html", 0.6)

    # Content pages (0.5 - large volume, factory manual scrapes)
    for page in pages:
        add_url(f"{base_url}page/{page['page_id']}.html", 0.5)

    # Search page (0.6 - utility)
    add_url(f"{base_url}search.html", 0.6)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{chr(10).join(urls)}
</urlset>
"""
    return xml


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Build Pajero IV manual static site")
    ap.add_argument("--base-url", default="/", metavar="URL")
    ap.add_argument("--out", default="site", metavar="DIR")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--skip-images", action="store_true")
    args = ap.parse_args()

    base_url = args.base_url
    if not base_url.endswith("/"):
        base_url += "/"

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not MANIFEST_PATH.exists():
        sys.exit(f"ERROR: manifest not found at {MANIFEST_PATH}. Run scrape.py first.")
    manifest = json.loads(MANIFEST_PATH.read_text())
    pages = manifest["pages"]
    print(f"Loaded manifest: {len(pages)} pages")

    # Index by year+manual
    by_ym: dict[tuple, list[dict]] = {}
    for p in pages:
        key = (p.get("year", "2010"), p.get("manual_type", "M1"))
        by_ym.setdefault(key, []).append(p)

    available_years = build_available_years(pages)

    # Pre-compute sidebar groups per (year, manual) for content pages
    sidebar_groups_cache: dict[tuple, list[dict]] = {
        ym: build_groups_for(ps) for ym, ps in by_ym.items()
    }

    t0 = time.time()

    # Assets
    assets_dir = out_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    (assets_dir / "style.css").write_text(STYLE_CSS, encoding="utf-8")
    (assets_dir / "app.js").write_text(APP_JS, encoding="utf-8")
    print("  wrote assets/")

    if not args.skip_images:
        copy_images(out_dir)
    else:
        print("  skipping image copy (--skip-images)")

    # Load curated content
    topics = load_topics(CONTENT_DIR)
    shops, feedback_email = load_shops(CONTENT_DIR)
    topics_by_slug = {t["slug"]: t for t in topics}
    topics_by_cat: dict[str, list] = {}
    for t in topics:
        topics_by_cat.setdefault(t.get("category", ""), []).append(t)
    cats_with_counts = [{**c, "topic_count": len(topics_by_cat.get(c["slug"], []))} for c in CATEGORIES]
    sidebar_cats = cats_with_counts
    print(f"  loaded {len(topics)} topics, {len(shops)} shops")

    # New landing index (problem-oriented)
    (out_dir / "index.html").write_text(
        gen_landing(pages, topics, shops, feedback_email, base_url), encoding="utf-8"
    )
    print("  wrote index.html (new landing)")

    # Archive index (old year×manual matrix)
    archive_dir = out_dir / "archive"
    archive_dir.mkdir(exist_ok=True)
    (archive_dir / "index.html").write_text(gen_archive_index(pages, base_url), encoding="utf-8")
    print("  wrote archive/index.html")

    # Category pages
    for cat in cats_with_counts:
        cat_dir = out_dir / cat["slug"]
        cat_dir.mkdir(exist_ok=True)
        html = gen_category_page(cat, topics_by_cat.get(cat["slug"], []),
                                 cats_with_counts, feedback_email, base_url)
        (cat_dir / "index.html").write_text(html, encoding="utf-8")
    print(f"  wrote {len(CATEGORIES)} category pages")

    # Topic pages
    topics_dir = out_dir / "topics"
    topics_dir.mkdir(exist_ok=True)
    for topic in topics:
        html = gen_topic_page(topic, topics_by_slug, shops, sidebar_cats, feedback_email, base_url)
        (topics_dir / f"{topic['slug']}.html").write_text(html, encoding="utf-8")
    print(f"  wrote {len(topics)} topic pages")

    # View pages: one per (year, manual) combo
    view_count = 0
    group_count = 0
    for (year, manual), ym_pages in by_ym.items():
        view_dir = out_dir / "view" / year / manual
        view_dir.mkdir(parents=True, exist_ok=True)
        grp_dir = view_dir / "group"
        grp_dir.mkdir(exist_ok=True)

        groups = sidebar_groups_cache[(year, manual)]

        (view_dir / "index.html").write_text(
            gen_view_index(year, manual, ym_pages, available_years, base_url),
            encoding="utf-8",
        )
        view_count += 1

        pages_by_group: dict[str, list[dict]] = {}
        for p in ym_pages:
            pages_by_group.setdefault(p["group"], []).append(p)

        for code, gp in pages_by_group.items():
            (grp_dir / f"{code}.html").write_text(
                gen_view_group(year, manual, code, gp, groups, available_years, base_url),
                encoding="utf-8",
            )
            group_count += 1

    print(f"  wrote {view_count} view index pages + {group_count} view group pages")

    # Content pages (parallel) — attach sidebar groups to each page dict
    page_dir = out_dir / "page"
    page_dir.mkdir(exist_ok=True)

    # prev/next within each (year, manual, group) context
    prev_next: dict[str, tuple] = {}
    for (year, manual), ym_pages in by_ym.items():
        by_group: dict[str, list] = {}
        for p in ym_pages:
            by_group.setdefault(p["group"], []).append(p)
        for gp in by_group.values():
            sorted_gp = sorted(gp, key=lambda p: p["title"])
            for i, p in enumerate(sorted_gp):
                prev_next[p["page_id"]] = (
                    sorted_gp[i - 1] if i > 0 else None,
                    sorted_gp[i + 1] if i < len(sorted_gp) - 1 else None,
                )

    # Build siblings cache: (year, manual, group) → [{id, title}, ...]
    # Cap at 40 per section to keep sidebar usable
    siblings_cache: dict[tuple, list] = {}
    for (year, manual), ym_pages in by_ym.items():
        by_group: dict[str, list] = {}
        for p in ym_pages:
            by_group.setdefault(p["group"], []).append(p)
        for group, gp in by_group.items():
            sorted_gp = sorted(gp, key=lambda p: p["title"])
            if len(sorted_gp) <= 40:
                siblings_cache[(year, manual, group)] = [
                    {"id": p["page_id"], "title": p["title"]} for p in sorted_gp
                ]
            else:
                siblings_cache[(year, manual, group)] = []  # too many to list

    # Attach sidebar groups and siblings to each page
    for p in pages:
        key = (p.get("year", "2010"), p.get("manual_type", "M1"))
        p["_sidebar_groups"] = sidebar_groups_cache.get(key, [])
        skey = (p.get("year", "2010"), p.get("manual_type", "M1"), p["group"])
        p["_siblings"] = siblings_cache.get(skey, [])

    written = 0
    errors = 0

    def render_one(p: dict) -> tuple[str, str | Exception]:
        prev, next_ = prev_next.get(p["page_id"], (None, None))
        try:
            html = gen_content_page(p, prev, next_, available_years, base_url)
            return p["page_id"], html
        except Exception as e:
            return p["page_id"], e

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(render_one, p): p for p in pages}
        for fut in as_completed(futures):
            pid, result = fut.result()
            if isinstance(result, Exception):
                print(f"  ERROR {pid}: {result}", file=sys.stderr)
                errors += 1
            else:
                (page_dir / f"{pid}.html").write_text(result, encoding="utf-8")
                written += 1
                if written % 500 == 0:
                    print(f"  pages {written}/{len(pages)} …")

    print(f"  wrote {written} content pages ({errors} errors)")

    # Find a Part search page
    find_part_dir = out_dir / "find-part"
    find_part_dir.mkdir(exist_ok=True)
    (find_part_dir / "index.html").write_text(
        gen_parts_search_page(shops, topics, sidebar_cats, feedback_email, base_url),
        encoding="utf-8"
    )
    print("  wrote find-part/index.html")

    # Search (index built post-build by pagefind)
    (out_dir / "search.html").write_text(gen_search_page(available_years, base_url), encoding="utf-8")
    print("  wrote search.html")

    (out_dir / ".nojekyll").write_text("")
    print("  wrote .nojekyll")

    # Generate robots.txt and sitemap.xml for SEO
    (out_dir / "robots.txt").write_text(gen_robots_txt(base_url), encoding="utf-8")
    print("  wrote robots.txt")

    sitemap_content = gen_sitemap_xml(base_url, pages, topics, CATEGORIES, YEAR_ORDER, MANUAL_ORDER)
    (out_dir / "sitemap.xml").write_text(sitemap_content, encoding="utf-8")
    print("  wrote sitemap.xml")

    elapsed = time.time() - t0
    total_files = sum(1 for _ in out_dir.rglob("*") if _.is_file())
    print(f"\nDone in {elapsed:.1f}s — {total_files:,} files in {out_dir}/")
    print(f"  python -m http.server 8080 --directory {out_dir}")


if __name__ == "__main__":
    main()
