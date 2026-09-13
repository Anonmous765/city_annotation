#!/usr/bin/env python3
"""
build_city_list.py — Parse "data/700 cities.pdf" into the CSV that batch_generate.py
consumes, adding terrain elevation from OpenTopoData (Mapzen, global 30 m).

    python3 build_city_list.py "data/700 cities.pdf" --range 351 700 --out data/my_cities_351_700.csv

Columns written: n, city, country, continent, anchor, lat, lon, terrain_m,
poi_alt_m (= terrain_m + POI_ABOVE_DEM_M, matching where Earth Studio's Orbit
quickstart puts its target). Requires `pdftotext` (poppler-utils) on PATH.
"""
import argparse, csv, json, re, subprocess, sys, tempfile, time, urllib.request
from ges_esp import POI_ABOVE_DEM_M

NUM = re.compile(r"-?\d+\.\d+"); DASH = re.compile(r"(?<!\S)-(?!\S)"); MAIN = re.compile(r"^\s*(\d+)\s+\S")
FIX_COUNTRY = {"S.A.": "U.S.A.", "A.": "U.S.A.", "anada": "Canada", "exico": "Mexico", "azil": "Brazil",
               "United": "United Kingdom", "hilippines": "Philippines", "South Africa A": "South Africa",
               "ustralia": "Australia"}
FIX_CONT = {"North": "North America", "frica": "Africa", "South": "South America"}


def parse_pdf(pdf):
    """The PDF table wraps long cells onto the lines above/below a row and
    breaks negative latitudes into a lone '-' plus the digits on the next
    line, so this walks `pdftotext -layout` output with column offsets taken
    from each page's header."""
    txt = subprocess.run(["pdftotext", "-layout", pdf, "-"], check=True,
                         capture_output=True, text=True).stdout
    lines, cols, prev = [], None, ""
    for line in txt.splitlines():
        line = line.replace("\f", "")
        if re.search(r"#\s+City", line):
            cols = {"city": line.index("City"), "country": prev.index("Country"),
                    "continent": line.index("Continent"), "anchor": prev.index("Downtown"),
                    "lat": line.index("Latitude")}
            prev = line; continue
        if "Country /" in line and "Downtown" in line: prev = line; continue
        if "Region" in line and "anchor" in line: continue
        prev = line
        if cols: lines.append((line, cols))
    main = [i for i, (l, c) in enumerate(lines) if MAIN.match(l)]
    out = []
    for i in main:
        line, c = lines[i]; n = int(MAIN.match(line).group(1))
        nums = [float(x) for x in NUM.findall(line)]
        if len(nums) == 2:
            lat, lon = nums
        elif len(nums) == 1:
            lon = nums[0]
            dl = i if DASH.search(line) else (i - 1 if i > 0 and DASH.search(lines[i-1][0]) else None)
            if dl is None: print("skip (no dash)", n, line, file=sys.stderr); continue
            lat = None
            for j in range(dl + 1, min(dl + 4, len(lines))):
                l = lines[j][0]
                if j == i or not l.strip() or MAIN.match(l): continue
                xs = NUM.findall(l)
                if len(xs) == 1: lat = -abs(float(xs[0])); break
            if lat is None: print("skip (no lat)", n, line, file=sys.stderr); continue
        else:
            print("skip", n, line, file=sys.stderr); continue
        parts = {}
        spans = {"city": (c["city"] - 4, c["country"]), "country": (c["country"], c["continent"]),
                 "continent": (c["continent"], c["anchor"]), "anchor": (c["anchor"], c["lat"])}
        for k, (a, b) in spans.items():
            segs = []
            for j in (i - 1, i, i + 1):
                if not 0 <= j < len(lines): continue
                l = lines[j][0]
                if j != i and MAIN.match(l): continue
                l = NUM.sub(lambda m: " " * len(m.group()), l); l = DASH.sub(" ", l)
                s = l[a:b].strip() if len(l) > a else ""
                if j == i: s = re.sub(r"^\d+\s*", "", s)
                if s: segs.append(s)
            parts[k] = " ".join(segs)
        parts["country"] = FIX_COUNTRY.get(parts["country"], parts["country"])
        parts["continent"] = FIX_CONT.get(parts["continent"], parts["continent"])
        parts["city"] = re.sub(r"\s+(U\.S\.|A)$", "", parts["city"]).strip()
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            print("out of range", n, parts, lat, lon, file=sys.stderr); continue
        out.append(dict(n=n, **parts, lat=lat, lon=lon))
    return out


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
    got = {r["n"] for r in rows}
    missing = [i for i in range(1, 701) if i not in got]
    if missing: print(f"WARNING: rows not parsed: {missing}", file=sys.stderr)
    rows = [r for r in rows if args.range[0] <= r["n"] <= args.range[1]]

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
