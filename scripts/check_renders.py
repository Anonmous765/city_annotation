#!/usr/bin/env python3
"""
check_renders.py — Sanity-check every rendered view in a share. render_all.py
only checks that 61 frames and the tracking JSON arrived, so a render of open
sea or of an empty blue globe (imagery never loaded) is logged "ok"; this
script catches those, plus anything broken on disk.

    python3 scripts/check_renders.py cities_251_500 --csv data/cities_251_500/snapped.csv
    python3 scripts/check_renders.py cities_251_500 --only muscat oran

Per view it checks:
  * files: footage/ holds exactly <view>_00..60.jpeg at 2048x2048, <view>.json
    has 61 camera frames, <view>.esp is there, no leftover .zip;
  * camera path: the tracking JSON follows projects/manifest.json's orbit to
    within 50 m (a bigger miss means the render is from an older project);
  * content: frames that are one flat colour (Earth Studio's blue placeholder
    globe) or identical to the previous frame, and views with almost no
    detail (median edge strength < 0.03), which on 2026-09-25 was open water
    for 30 of 36 sea views and no land view;
  * render time: views that rendered in under 30 s (normal is ~60 s) per
    <share>/render_log.csv;
  * with --csv: targets with terrain_m <= 0, i.e. OpenTopoData returned sea
    depth for the target (the rest of the sea renders).
Texture and render time are hints, not proof: look at what they flag with
inspect_renders.py --only.

Writes <share>/render_check.csv (city_folder, view, problems) and prints a
summary. Uses all cores; ~1-2 min for 250 cities.
"""
import argparse
import csv
import json
import math
import os
import sys

for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

from inspect_renders import cities_from_csv

VIEWS = ("satellite", "ground_truth")
N = 61
LOW_DETAIL = 0.03
FAST_S = 30
TRACK_TOL_M = 50


def dist_m(a_lat, a_lon, b_lat, b_lon):
    k = math.pi / 180
    return 6371000 * math.hypot((b_lon - a_lon) * k * math.cos((a_lat + b_lat) / 2 * k), (b_lat - a_lat) * k)


def check_view(job):
    share, city, view, track = job
    d = Path(share) / city / view
    p = []
    if not d.is_dir():
        return city, view, ["not rendered"]
    names = sorted(x.name for x in (d / "footage").glob("*")) if (d / "footage").is_dir() else []
    want = [f"{view}_{i:02d}.jpeg" for i in range(N)]
    if names != want:
        p.append(f"frame set: {len(names)} files, missing {sorted(set(want) - set(names))[:3]}, "
                 f"extra {sorted(set(names) - set(want))[:3]}")
    prev, flat, frozen, edges = None, [], [], []
    for i in range(N):
        f = d / "footage" / f"{view}_{i:02d}.jpeg"
        if not f.exists():
            continue
        try:
            with Image.open(f) as im:
                if im.size != (2048, 2048):
                    p.append(f"frame {i:02d} is {im.size[0]}x{im.size[1]}")
                im.draft("L", (256, 256))
                a = np.asarray(im.convert("L").resize((256, 256)), dtype=np.float32) / 255
        except Exception as e:
            p.append(f"frame {i:02d} unreadable: {e}")
            prev = None
            continue
        if a.std() < 0.02:
            flat.append(i)
        if prev is not None and np.abs(a - prev).mean() < 0.002:
            frozen.append(i)
        edges.append(np.abs(np.diff(a, axis=0)).mean() + np.abs(np.diff(a, axis=1)).mean())
        prev = a
    if flat:
        p.append(f"{len(flat)} frames are one flat colour (imagery never loaded?)")
    if frozen:
        p.append(f"{len(frozen)} frames identical to the previous one")
    if edges and not flat and np.median(edges) < LOW_DETAIL:
        p.append(f"almost no detail (edge strength {np.median(edges):.3f}): open water?")
    j = d / f"{view}.json"
    if not j.exists():
        p.append("tracking json missing")
    else:
        try:
            cf = json.load(open(j)).get("cameraFrames", [])
            if len(cf) != N:
                p.append(f"tracking json has {len(cf)} camera frames")
            if track and cf:
                err = max(dist_m(t["lat"], t["lon"], c["coordinate"]["latitude"], c["coordinate"]["longitude"])
                          for t, c in zip(track, cf))
                if err > TRACK_TOL_M:
                    p.append(f"camera path up to {err:.0f} m off the project's orbit (rendered from an older project?)")
        except (ValueError, KeyError) as e:
            p.append(f"tracking json unreadable: {e}")
    if not (d / f"{view}.esp").exists():
        p.append(".esp missing")
    if list(d.glob("*.zip")):
        p.append("leftover .zip")
    return city, view, p


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("share", type=Path)
    ap.add_argument("--csv", type=Path, help="the snapped CSV the share was generated from (adds the terrain check)")
    ap.add_argument("--projects", type=Path, default=Path("projects"))
    ap.add_argument("--only", nargs="*", metavar="CITY")
    args = ap.parse_args()

    manifest = {e["city_folder"]: e for e in json.load(open(args.projects / "manifest.json"))}
    cities = args.only or sorted(manifest)
    missing = [c for c in cities if c not in manifest]
    if missing:
        sys.exit(f"not in {args.projects}/manifest.json: {', '.join(missing)}")
    jobs = [(str(args.share), c, v, manifest[c]["views"][v]["camera_track_approx"]) for c in cities for v in VIEWS]
    print(f"checking {len(jobs)} views ...", flush=True)
    with ProcessPoolExecutor() as ex:
        found = {(c, v): p for c, v, p in ex.map(check_view, jobs, chunksize=4)}

    log = args.share / "render_log.csv"
    if log.exists():
        last = {}
        for r in csv.DictReader(open(log, newline="")):
            last[(r["city"], r["view"])] = r
        for (c, v), r in last.items():
            if (c, v) in found and found[(c, v)] != ["not rendered"] and r["status"] == "ok" \
                    and int(r["seconds"]) < FAST_S:
                found[(c, v)].append(f"rendered in {r['seconds']} s (normal ~60 s)")
    if args.csv:
        for folder, row in cities_from_csv(args.csv):
            t = (row.get("terrain_m") or "").strip()
            if folder in cities and t and float(t) <= 0:
                for v in VIEWS:
                    found[(folder, v)].append(f"target terrain_m {t}: probably offshore")

    a = args.share / "metadata.csv"
    b = args.projects / "metadata.csv"
    if a.exists() and a.read_text() != b.read_text():
        print(f"NOTE: {a} differs from {b} (render_all.py copies it at the start of a run)")

    out = args.share / "render_check.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["city_folder", "view", "problems"])
        for (c, v), p in sorted(found.items()):
            if p:
                w.writerow([c, v, "; ".join(p)])
    bad = sorted({c for (c, v), p in found.items() if p})
    print(f"{len(found) - sum(1 for p in found.values() if p)}/{len(found)} views clean; "
          f"{len(bad)} cities with problems -> {out}")
    for c in bad:
        for v in VIEWS:
            if found[(c, v)]:
                print(f"  {c}/{v}: " + "; ".join(found[(c, v)]))
    if bad:
        print(f"\nlook at them: python3 scripts/inspect_renders.py {args.share} --only {' '.join(bad)}")


if __name__ == "__main__":
    main()
