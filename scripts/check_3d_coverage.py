#!/usr/bin/env python3
"""
check_3d_coverage.py — Find ground_truth renders where Google Earth had no 3D
buildings, so the oblique orbit is just the satellite image draped over the
terrain (it looks like a skewed satellite view; see Hasselt in rows 351-700).

    python3 scripts/check_3d_coverage.py cities_351_700
    python3 scripts/check_3d_coverage.py cities_351_700 --flag     # also add suspects to inspect_flags.csv

How it works: the camera track is the same for every city, so the only
thing that distinguishes a flat render from a 3D one is parallax. In a flat
render the whole scene is one plane and any two frames of the orbit are
related by a single homography; with real buildings the facades and roofs
move differently from the ground and no single homography fits. For each
city the script matches ORB features between two neighbouring frames (on
opposite sides of the orbit: 0/1 and 30/31), fits a homography with RANSAC,
and records the inlier fraction. Flat renders score ~0.65-0.85, 3D ones
~0.25-0.5. Above --threshold + 0.06 (default 0.62, so > 0.68) a render is
called flat; within 0.06 of the threshold it is borderline, which you should
eyeball with inspect_renders.py --only.

Blind spot: the test assumes flat ground. A town with no 3D buildings on
steep terrain (Baguio, Traralgon in rows 351-700) still shows parallax from
the hills, scores like a 3D city, and is missed. Calibrated against a
hand-flagged pass over rows 351-700 (101 flat of 350): every city above 0.68
was flat, every city from 0.59 to 0.65 had 3D, and the only flat cities
below 0.56 were the five hilly ones. So: trust 'flat', eyeball 'borderline',
and expect '3d' to hide a flat town now and then when the terrain is steep.

Writes <share>/coverage_check.csv (city, per-pair scores, verdict), sorted
flattest first, and prints the suspects. Uses all cores; ~10 min for 350
cities.
"""
import argparse
import csv
import os
import sys

# One BLAS/OpenMP thread per worker process: with 24 workers each spinning up
# its own thread pool the machine hit a load average above 130 and crawled.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

VIEW = "ground_truth"
PAIRS = ((0, 1), (30, 31))


def load_gray(p: Path, size: int) -> np.ndarray:
    with Image.open(p) as im:
        im.draft("L", (size, size))
        im = im.convert("L").resize((size, size), Image.BILINEAR)
    return np.asarray(im, dtype=np.float32) / 255.0


def pair_score(fa: Path, fb: Path, size: int, n_keypoints: int):
    """(matches, inlier fraction, median reprojection error px) for one frame pair."""
    from skimage.feature import ORB, match_descriptors
    from skimage.measure import ransac
    from skimage.transform import ProjectiveTransform

    a, b = load_gray(fa, size), load_gray(fb, size)
    orb = ORB(n_keypoints=n_keypoints, fast_threshold=0.005)
    orb.detect_and_extract(a)
    ka, da = orb.keypoints, orb.descriptors
    orb.detect_and_extract(b)
    kb, db = orb.keypoints, orb.descriptors
    m = match_descriptors(da, db, cross_check=True, max_ratio=0.85)
    if len(m) < 30:
        return len(m), float("nan"), float("nan")
    src, dst = ka[m[:, 0]][:, ::-1], kb[m[:, 1]][:, ::-1]
    model, inliers = ransac((src, dst), ProjectiveTransform, min_samples=4,
                            residual_threshold=1.0 * size / 512, max_trials=3000)
    err = np.linalg.norm(model(src) - dst, axis=1)
    return len(m), float(inliers.mean()), float(np.median(err))


def check_city(args):
    share, city, size, n_keypoints = args
    share = Path(share)
    footage = share / city / VIEW / "footage"
    row = {"city_folder": city}
    fracs = []
    for i, (a, b) in enumerate(PAIRS, 1):
        fa, fb = footage / f"{VIEW}_{a:02d}.jpeg", footage / f"{VIEW}_{b:02d}.jpeg"
        if not (fa.exists() and fb.exists()):
            row.update({f"matches{i}": "", f"inlier{i}": "", f"err{i}": ""})
            continue
        try:
            n, frac, err = pair_score(fa, fb, size, n_keypoints)
        except Exception as e:  # noqa: BLE001 - one bad city must not kill the pool
            n, frac, err = 0, float("nan"), float("nan")
            row["note"] = str(e).splitlines()[0]
        row.update({f"matches{i}": n, f"inlier{i}": round(frac, 3) if frac == frac else "",
                    f"err{i}": round(err, 2) if err == err else ""})
        if frac == frac:
            fracs.append(frac)
    row["score"] = round(sum(fracs) / len(fracs), 3) if fracs else ""
    return row


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("share", type=Path)
    ap.add_argument("--only", nargs="*", help="city folder names (default: every city in the share)")
    ap.add_argument("--threshold", type=float, default=0.62,
                    help="mean homography-inlier fraction above which a render counts as flat")
    ap.add_argument("--size", type=int, default=768, help="analysis resolution (px)")
    ap.add_argument("--keypoints", type=int, default=4000)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--flag", action="store_true",
                    help="append flat verdicts to <share>/inspect_flags.csv (note: 'no 3D buildings')")
    args = ap.parse_args()

    share = args.share
    cities = args.only or sorted(p.name for p in share.iterdir()
                                 if p.is_dir() and (p / VIEW / "footage").is_dir())
    if not cities:
        sys.exit("no cities with ground_truth footage found")
    print(f"checking {len(cities)} cities ({len(PAIRS)} frame pairs each) ...", flush=True)
    rows = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for k, row in enumerate(ex.map(check_city, [(str(share), c, args.size, args.keypoints) for c in cities]), 1):
            rows.append(row)
            if k % 25 == 0 or k == len(cities):
                print(f"  {k}/{len(cities)}", flush=True)

    thr, margin = args.threshold, 0.06
    for r in rows:
        s = r["score"]
        r["verdict"] = ("unknown" if s == "" else "flat" if s > thr + margin else
                        "borderline" if s > thr - margin else "3d")
    rows.sort(key=lambda r: -(r["score"] if r["score"] != "" else -1))
    out = share / "coverage_check.csv"
    fields = ["city_folder", "score", "verdict"] + [f"{k}{i}" for i in range(1, len(PAIRS) + 1)
                                                    for k in ("inlier", "err", "matches")] + ["note"]
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows({**{k: "" for k in fields}, **r} for r in rows)

    flat = [r for r in rows if r["verdict"] == "flat"]
    border = [r for r in rows if r["verdict"] == "borderline"]
    unknown = [r for r in rows if r["verdict"] == "unknown"]
    print(f"\n{len(flat)} flat, {len(border)} borderline, {len(unknown)} unknown, "
          f"{len(rows) - len(flat) - len(border) - len(unknown)} 3d  -> {out}")
    for label, group in (("FLAT (no 3D buildings)", flat), ("BORDERLINE (check by eye)", border),
                         ("UNKNOWN (too few matches)", unknown)):
        if group:
            print(f"\n{label}:")
            for r in group:
                print(f"  {r['city_folder']:24} score={r['score']}  "
                      f"pairs={r.get('inlier1', '')}/{r.get('inlier2', '')}  {r.get('note', '')}")

    if args.flag and flat:
        flags_path = share / "inspect_flags.csv"
        existing = {}
        if flags_path.exists():
            with open(flags_path, newline="") as f:
                existing = {r["city_folder"]: r for r in csv.DictReader(f)}
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        added = 0
        for r in flat:
            if r["city_folder"] not in existing:
                existing[r["city_folder"]] = {"city_folder": r["city_folder"], "flagged_at": now,
                                              "note": "no 3D buildings"}
                added += 1
        with open(flags_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["city_folder", "flagged_at", "note"])
            w.writeheader()
            w.writerows(existing.values())
        print(f"\nadded {added} new flag(s) to {flags_path}")


if __name__ == "__main__":
    main()
