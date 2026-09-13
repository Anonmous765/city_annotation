#!/usr/bin/env python3
"""
batch_generate.py — Turn a CSV of cities into paired Earth Studio projects laid
out exactly like the reference dataset (reference/city_satellite/ and reference/Cities/):

    <out>/<city_folder>/satellite/satellite.esp
    <out>/<city_folder>/ground_truth/ground_truth.esp
    <out>/metadata.csv          # city_folder,country,latitude,longitude
    <out>/manifest.json         # per-city params + approximate camera track
    <out>/render_order.txt      # flat queue list

The project *name* inside each .esp is "satellite" / "ground_truth" so that
Earth Studio's renderer names the frames satellite_00.jpeg ... exactly as the
existing data does. Render each project into its own folder's footage/ dir.

Input CSV columns
-----------------
Required : city, lat, lon, poi_alt_m
Optional : country, sat_radius_m, sat_height_m, gnd_radius_m, gnd_height_m,
           world_time_utc   (ISO 8601, e.g. 2026-06-21T10:00:00+00:00)

poi_alt_m is the ABSOLUTE altitude (m, MSL) of the orbit target. Earth Studio
places the target ~27 m above bare-earth DEM elevation, so use
DEM + 27 (build_city_list.py does this for you).

Usage
-----
    python3 build_city_list.py "data/700 cities.pdf" --range 351 700 --out data/my_cities_351_700.csv
    python3 batch_generate.py data/my_cities_351_700.csv --out ./projects
"""

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from ges_esp import Site, SATELLITE, GROUND, build_esp, camera_track


def slug(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", s.strip().lower()).strip("_")


def fnum(row, key, default=None):
    v = (row.get(key) or "").strip()
    return float(v) if v else default


def parse_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    missing = {"city", "lat", "lon", "poi_alt_m"} - set(rows[0].keys() if rows else [])
    if missing:
        sys.exit(f"CSV is missing required column(s): {', '.join(sorted(missing))}")

    sites, problems = [], []
    for i, row in enumerate(rows, start=2):
        try:
            lat, lon = float(row["lat"]), float(row["lon"])
            alt = float(row["poi_alt_m"])
        except (ValueError, TypeError):
            problems.append(f"row {i} ({row.get('city','?')}): unparseable lat/lon/alt")
            continue
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            problems.append(f"row {i} ({row['city']}): lat/lon out of range")
            continue
        if abs(lat) > 85:
            problems.append(f"row {i} ({row['city']}): |lat|>85, orbit longitude "
                            f"spacing degenerates near the poles")

        wt = (row.get("world_time_utc") or "").strip()
        sites.append(Site(
            city=row["city"].strip(),
            lat=lat, lon=lon, poi_alt_m=alt,
            world_time_utc=datetime.fromisoformat(wt) if wt else None,
            meta={"country": (row.get("country") or "").strip(),
                  "sat_radius_m": fnum(row, "sat_radius_m", SATELLITE.orbit_radius_m),
                  "sat_height_m": fnum(row, "sat_height_m", SATELLITE.height_above_poi_m),
                  "gnd_radius_m": fnum(row, "gnd_radius_m", GROUND.orbit_radius_m),
                  "gnd_height_m": fnum(row, "gnd_height_m", GROUND.height_above_poi_m)},
        ))
    return sites, problems


def folder_names(sites):
    """city slug; duplicates get the country appended (as the reference
    dataset does, e.g. cordoba_argentina)."""
    base = [slug(s.city) for s in sites]
    dup = {b for b, n in Counter(base).items() if n > 1}
    names, seen = [], Counter()
    for s, b in zip(sites, base):
        if b in dup and s.meta["country"]:
            b = f"{b}_{slug(s.meta['country'])}"
        seen[b] += 1
        names.append(b if seen[b] == 1 else f"{b}_{seen[b]}")
    return names


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--out", default="./projects")
    ap.add_argument("--res", type=int, default=2048, help="square output resolution")
    ap.add_argument("--frames", type=int, default=60,
                    help="scene duration in frames (60 = 2 s @ 30 fps -> 61 images 00..60)")
    ap.add_argument("--fps", type=int, default=30)
    args = ap.parse_args()

    sites, problems = parse_rows(args.csv)
    for p in problems:
        print(f"  warn: {p}", file=sys.stderr)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    manifest, order, meta_rows = [], [], []
    for site, folder in zip(sites, folder_names(sites)):
        views = [
            replace(SATELLITE, orbit_radius_m=site.meta["sat_radius_m"],
                    height_above_poi_m=site.meta["sat_height_m"]),
            replace(GROUND, orbit_radius_m=site.meta["gnd_radius_m"],
                    height_above_poi_m=site.meta["gnd_height_m"]),
        ]

        entry = {"city": site.city, "city_folder": folder, "country": site.meta["country"],
                 "poi": {"lat": site.lat, "lon": site.lon, "alt_m": site.poi_alt_m},
                 "views": {}}

        for v in views:
            d = out / folder / v.name
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{v.name}.esp"
            with open(path, "w") as f:
                json.dump(build_esp(site, v, width=args.res, height=args.res,
                                    fps=args.fps, n_frames=args.frames,
                                    project_name=v.name), f, separators=(",", ":"))
            entry["views"][v.name] = {
                "esp": str(path.relative_to(out)),
                "orbit_radius_m": v.orbit_radius_m,
                "height_above_poi_m": v.height_above_poi_m,
                "camera_alt_m": round(site.poi_alt_m + v.height_above_poi_m),
                "pitch_deg": round(v.pitch_deg, 3),
                "slant_range_m": round(v.slant_range_m, 1),
                "camera_track_approx": camera_track(site, v, args.frames),
            }
            order.append(f"{folder}/{v.name}\t{path.relative_to(out)}")

        manifest.append(entry)
        meta_rows.append({"city_folder": folder, "country": site.meta["country"],
                          "latitude": site.lat, "longitude": site.lon})

    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    (out / "render_order.txt").write_text("\n".join(order) + "\n")
    with open(out / "metadata.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["city_folder", "country", "latitude", "longitude"])
        w.writeheader()
        w.writerows(meta_rows)

    n = len(manifest)
    total = n * 2 * (args.frames + 1)
    print(f"{n} cities -> {n*2} projects -> {total:,} frames at "
          f"{args.res}x{args.res} ({args.frames + 1} images per project)")
    print(f"wrote {out}/metadata.csv, manifest.json and render_order.txt")


if __name__ == "__main__":
    main()
