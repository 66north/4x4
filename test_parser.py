"""Smoke test for the parser using a captured copy of a real content page."""
from pathlib import Path
import sys, json
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).parent))
from scrape import parse_content_page, extract_links_from_html

# A minimal but realistic fragment that mirrors the structure of the live page
# we confirmed via web_fetch earlier (EQUIPMENTS / IMMOBILIZER / AUDIO).
sample_html = ("""<!DOCTYPE html>
<html><head><title>EQUIPMENTS</title></head><body>
<h1>EQUIPMENTS</h1>
<h2>IMMOBILIZER SYSTEM</h2>
<p><strong>&lt;3200&gt;</strong>
<a href="javascript:enlarge('../../../img/00/AC603772AF00ENG.png')">
<img src="../../../img/00/AC603772AF00ENG.png" alt="immobilizer 3200"/></a>
</p>
<p>The engine immobilizer system prevents the engine from starting and
immobilizes the vehicle if a key other than the registered key is used.</p>
<h2>TORQUE SPECIFICATIONS</h2>
<table>
<tr><th>Item</th><th>Torque</th></tr>
<tr><td>Cylinder head bolt</td><td>108 \u00b1 5 N\u00b7m</td></tr>
<tr><td>Oil pan bolt</td><td>12 N\u00b7m</td></tr>
</table>
<p>Replace part MD123456 if damaged. Reference assembly MR998877.</p>
<img src="http://top-fwz1.mail.ru/counter?id=1112105" alt="tracker"/>
</body></html>""").encode("utf-8")

source_url = (
    "http://faq.out-club.ru/download/pajero_iv/maintenance/"
    "Service_Manual_2008_2013/2010/00/html/M200002600086700ENG.HTM"
)

# Test link extraction
html_links, assets = extract_links_from_html(sample_html, source_url)
print("HTML links discovered:", len(html_links))
print("Asset links discovered:", len(assets))
for a in sorted(assets):
    print(" -", a)
print()

# Test full parse
parsed = parse_content_page(sample_html, source_url)
assert parsed is not None, "parse returned None"
result = asdict(parsed)

print("page_id:    ", result["page_id"])
print("group:      ", result["group"])
print("title:      ", result["title"])
print("headings:   ", len(result["headings"]))
for h in result["headings"]:
    print("  ", "#" * h["level"], h["text"])
print("images:     ", len(result["images"]))
for img in result["images"]:
    print("  ", img["id"], "→", img["src"])
print("tables:     ", len(result["tables"]))
for tbl in result["tables"]:
    for row in tbl:
        print("  ", " | ".join(row))
print("torque:     ", len(result["torque_specs"]))
for t in result["torque_specs"]:
    print(f"   {t['value_nm']} N·m" + (f" ±{t['tolerance_nm']}" if t["tolerance_nm"] else ""))
print("part nums:  ", result["part_numbers"])
print()
print("─── markdown preview ───")
print(result["markdown"][:600])
print("...")

# Sanity assertions
assert result["page_id"] == "M200002600086700"
assert result["group"] == "00"
assert result["title"] == "EQUIPMENTS"
assert len(result["images"]) == 1, "mail.ru tracker should be stripped"
assert result["images"][0]["id"] == "AC603772AF00ENG"
assert len(result["tables"]) == 1
assert len(result["torque_specs"]) == 2
assert result["torque_specs"][0]["value_nm"] == 108.0
assert result["torque_specs"][0]["tolerance_nm"] == 5.0
assert "MD123456" in result["part_numbers"]
assert "MR998877" in result["part_numbers"]
# enlarge() js call should add the image to asset links even if <img src> already did
assert any("AC603772AF00ENG.png" in a for a in assets)

print("\nALL ASSERTIONS PASSED")
