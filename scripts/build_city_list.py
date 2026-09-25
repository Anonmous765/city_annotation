#!/usr/bin/env python3
"""
build_city_list.py — Parse a city-list PDF into the CSV that batch_generate.py
consumes, adding terrain elevation from OpenTopoData (Mapzen, global 30 m).

    python3 scripts/build_city_list.py "data/pdfs/700 cities.pdf" --range 351 700 --out data/cities_351_700/cities.csv
    python3 scripts/build_city_list.py data/pdfs/500cities.pdf --range 251 500 --out data/cities_251_500/cities.csv

The two lists are laid out differently — column positions move from page to
page and the heading block wraps in different places — so the parser reads the
table from the position of each word rather than from fixed column offsets.

Columns written: n, city, country, continent, anchor, lat, lon, terrain_m,
poi_alt_m (= terrain_m + POI_ABOVE_DEM_M, matching where Earth Studio's Orbit
quickstart puts its target). Requires `pdftotext` (poppler-utils) on PATH.
"""
import argparse, csv, json, re, statistics, subprocess, sys, time, urllib.request
from collections import namedtuple
import xml.etree.ElementTree as ET
from ges_esp import POI_ABOVE_DEM_M

# The table's seven columns, named by the word that heads each one.
HDR = ["#", "City", "Country", "Continent", "Downtown", "Latitude", "Longitude"]
HDR_WORDS = set(HDR) | {"Region", "anchor", "/"}
KEYS = ["n", "city", "country", "continent", "anchor"]
COORD = re.compile(r"^-?\d+(?:\.\d+)?$")

Word = namedtuple("Word", "x xm y t p")   # xm = right edge, p = page ordinal


def _pages(pdf):
    """Every word with its box, page by page, from pdftotext's bbox output."""
    xml = subprocess.run(["pdftotext", "-bbox-layout", pdf, "-"], check=True,
                         capture_output=True, text=True).stdout
    root = ET.fromstring(xml.replace(' xmlns="http://www.w3.org/1999/xhtml"', ""))
    for i, page in enumerate(root.iter("page")):
        words = [Word(float(w.get("xMin")), float(w.get("xMax")),
                      (float(w.get("yMin")) + float(w.get("yMax"))) / 2, (w.text or "").strip(), i)
                 for w in page.iter("word")]
        words = [w for w in words if w.t]
        if words:
            yield words


def _banners(words):
    """y bands holding the block of column headings. It sits at the top of
    every page and is reprinted mid-page wherever the table restarts."""
    bands = []
    for w in sorted((w for w in words if w.t in HDR), key=lambda w: w.y):
        if bands and w.y - bands[-1][0].y <= 25: bands[-1].append(w)
        else: bands.append([w])
    return [b for b in bands if len({w.t for w in b}) >= 5]


def _grid(pages, pdf):
    """Column left edges, taken as the leftmost heading position seen on any
    page: the table is re-laid-out per page and a row's cells can start left
    of the heading above them, so only the document-wide minimum is a safe
    boundary."""
    seen = {}
    for words in pages:
        band = _banners(words)
        if not band: continue
        lo, hi = band[0][0].y, band[0][-1].y
        for w in sorted(words, key=lambda w: (w.y, w.x)):
            if lo <= w.y <= hi and w.t in HDR:
                seen[w.t] = min(seen.get(w.t, w.x), w.x)
    missing = [k for k in HDR if k not in seen]
    if missing: sys.exit(f"could not find column heading(s) {missing} in {pdf!r}")
    return [seen[k] for k in HDR]


def _text(words):
    """Join a cell's words back into a string. A cell wraps over several lines
    and an accented letter is its own word, set a hair higher than the rest of
    its line, so group by line before reading left to right and close up runs
    that touch ("S" + "rodmies" + "cie").
    """
    lines = []
    for w in sorted(words, key=lambda w: (w.p, w.y, w.x)):
        if lines and abs(w.y - lines[-1][0]) <= 3: lines[-1][1].append(w)
        else: lines.append((w.y, [w]))
    out = []
    for _, ws in lines:
        s, prev = "", None
        for w in sorted(ws, key=lambda w: w.x):
            s += w.t if prev is not None and w.x - prev <= 1.5 else (" " + w.t if s else w.t)
            prev = w.xm
        out.append(s)
    return " ".join(out)


def _coords(words):
    """Latitude and longitude. A negative value is often split across lines
    into a bare '-' and its digits, and a cell clipped by a page break puts
    the digits at the top of the next page, so read each column top to bottom
    and glue a lone minus onto the number below it."""
    cols, toks = [], []
    for w in sorted(words, key=lambda w: w.x):          # latitude, then longitude
        if cols and w.x - cols[-1][-1].x <= 15: cols[-1].append(w)
        else: cols.append([w])
    for c in cols:
        got = []
        for w in sorted(c, key=lambda w: (w.p, w.y)):   # wrapped lines, page by page
            if got and got[-1] == "-": got[-1] = "-" + w.t
            else: got.append(w.t)
        toks += got                                     # a bare '-' never crosses columns
    return [float(t) for t in toks if COORD.match(t)]


def parse_pdf(pdf):
    """Read the city table out of the PDF.

    `pdftotext -layout` is no help here: it reflows a long cell into its
    neighbour, so a row like "11 Baton Rouge U.S.A." loses the boundary
    between city and country. The bbox output keeps every word's box, which
    the column grid turns back into cells.
    """
    pages = list(_pages(pdf))
    grid = _grid(pages, pdf)
    col = lambda x: max(i for i, c in enumerate(grid) if x >= c - 2) if x >= grid[0] - 2 else 0
    out = []
    for words in pages:
        bands = [(b[0].y - 12, b[-1].y + 12) for b in _banners(words)]
        body = [w for w in words
                if not (w.t in HDR_WORDS and any(lo <= w.y <= hi for lo, hi in bands))]
        recs = sorted([w for w in body if col(w.x) == 0 and w.t.isdigit()], key=lambda w: w.y)
        if not recs: continue
        pitch = statistics.median([b.y - a.y for a, b in zip(recs, recs[1:])]) if len(recs) > 1 else 40
        cells = {r.t: [] for r in recs}
        for w in body:
            near = min(recs, key=lambda r: abs(r.y - w.y))
            if abs(near.y - w.y) <= pitch * 0.45:
                cells[near.t].append(w)
            elif w.y < recs[0].y and out:
                out[-1][1].append(w)          # a cell clipped by the page break, continued here
            else:
                print(f"stray text {w.t!r} at y={w.y:.0f}", file=sys.stderr)
        for r in recs:
            out.append((int(r.t), [w for w in cells[r.t] if w is not r]))
    rows = []
    for n, mine in out:
        row = {"n": n}
        for i, k in enumerate(KEYS[1:], start=1):
            row[k] = _text([w for w in mine if col(w.x) == i])
        nums = _coords([w for w in mine if col(w.x) >= 5])
        if len(nums) != 2:
            print(f"skip {row['n']} {row['city']}: found {len(nums)} coordinate(s)", file=sys.stderr); continue
        lat, lon = nums
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            print(f"skip {row['n']} {row['city']}: lat/lon out of range ({lat}, {lon})", file=sys.stderr); continue
        if not all(row[k] for k in KEYS[1:]):
            print(f"warning: {row['n']} {row['city']!r} has an empty cell: {row}", file=sys.stderr)
        rows.append(dict(row, lat=lat, lon=lon))
    return rows


def fetch_elevations(pts, dataset="mapzen"):
    """OpenTopoData: free, 1 request/s, 100 locations/request."""
    out = []
    for i in range(0, len(pts), 100):
        chunk = pts[i:i + 100]
        locs = "|".join(f"{a:.6f},{b:.6f}" for a, b in chunk)
        for attempt in range(5):
            try:
                with urllib.request.urlopen(
                        f"https://api.opentopodata.org/v1/{dataset}?locations={locs}", timeout=60) as r:
                    res = json.load(r)
                break
            except Exception as e:
                print(f"  elevation request failed ({e}), retrying", file=sys.stderr); time.sleep(5)
        else:
            sys.exit("elevation lookup failed repeatedly")
        out += [x["elevation"] for x in res["results"]]
        time.sleep(1.2)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--range", nargs=2, type=int, metavar=("FIRST", "LAST"), default=(1, 700))
    ap.add_argument("--out", default="cities.csv")
    ap.add_argument("--dataset", default="mapzen", help="opentopodata dataset (mapzen|srtm30m|aster30m)")
    ap.add_argument("--no-elevation", action="store_true")
    args = ap.parse_args()

    rows = parse_pdf(args.pdf)
    lo, hi = args.range
    got = {r["n"] for r in rows}
    missing = [i for i in range(lo, hi + 1) if i not in got]
    if missing: print(f"WARNING: rows not parsed: {missing}", file=sys.stderr)
    rows = [r for r in rows if lo <= r["n"] <= hi]

    if args.no_elevation:
        for r in rows: r["terrain_m"] = ""; r["poi_alt_m"] = ""
    else:
        print(f"fetching {len(rows)} elevations from opentopodata/{args.dataset} ...")
        el = fetch_elevations([(r["lat"], r["lon"]) for r in rows], args.dataset)
        for r, e in zip(rows, el):
            if e is None:
                print(f"  no elevation for {r['city']} — fill poi_alt_m by hand", file=sys.stderr)
                r["terrain_m"] = ""; r["poi_alt_m"] = ""
            else:
                r["terrain_m"] = round(e, 1); r["poi_alt_m"] = round(e + POI_ABOVE_DEM_M, 1)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["n", "city", "country", "continent", "anchor",
                                          "lat", "lon", "terrain_m", "poi_alt_m"])
        w.writeheader(); w.writerows(rows)
    print(f"wrote {len(rows)} cities to {args.out}")


if __name__ == "__main__":
    main()
