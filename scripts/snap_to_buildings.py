#!/usr/bin/env python3
"""
snap_to_buildings.py — Move each city's orbit target from the generic
"downtown" anchor onto a real building nearby, using OpenStreetMap building
footprints (Overpass API), so the ground_truth orbit circles a building
instead of a road junction or a square.

    python3 scripts/snap_to_buildings.py data/cities_351_700/cities.csv --out data/cities_351_700/snapped.csv

For every city it fetches all building footprints within --radius metres of
the anchor and picks one:

  * landmark buildings score highest (town hall, cathedral/church, castle,
    palace, station, museum, theatre, university, courthouse, mosque, temple,
    anything tagged historic=* or tourism=attraction/museum),
  * otherwise the largest footprint, with a small bonus for having a name,
  * both discounted by distance from the anchor so the target stays downtown
    (score = capped area x landmark factor x name bonus / (1 + distance / --distance-scale)).
  * canopies, car parks, sheds, sites under construction etc. are skipped.

The target is a point inside the chosen footprint (its centroid, or the
nearest interior point if the centroid falls outside an L-shaped building).
Terrain elevation is re-fetched from OpenTopoData for the new point so
poi_alt_m stays terrain + 27 m, exactly as build_city_list.py does.

Output CSV = input columns with lat/lon/terrain_m/poi_alt_m updated and the
anchor replaced by the building name, plus review columns:
  orig_lat, orig_lon, snap_building, snap_dist_m, snap_area_m2,
  snap_landmark, snap_osm (way id), snap_status
snap_status is "snapped", "kept" (anchor already inside a building), or
"no_building" (nothing usable within --radius; row left unchanged — pick
one by hand or widen the radius). A --review CSV with just the review
columns and map links is written next to it for eyeballing.

Hand-picked anchors: if --anchors (default: handpicked_anchors.csv next to
the input CSV) exists, each of its rows (columns n, city, lat, lon, anchor,
snap, note; matched on n) replaces that city's PDF anchor before snapping.
snap=yes searches for a building around the new point as usual; snap=no uses
the point exactly as given (snap_status "handpicked"). Elevation is always
re-fetched for these rows, and anchor_source says "handpicked" or "pdf".

Rows whose final terrain_m is <= 0 are listed as a warning: that is
OpenTopoData returning sea depth, i.e. the target is offshore and the render
will show open water.

Overpass results are cached in --cache (JSON) so a rerun is instant; delete
the cache to refetch. The public Overpass servers are rate-limited, so the
script queries --batch cities per request, retries with backoff and rotates
between mirrors.
"""

import argparse
import csv
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from build_city_list import fetch_elevations
from ges_esp import POI_ABOVE_DEM_M

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
UA = "cities-annotation snap_to_buildings/1.0"
M_PER_DEG = 111_320.0

LANDMARK_BUILDING = {"cathedral", "church", "chapel", "mosque", "temple", "synagogue", "town_hall", "townhall",
                     "civic", "government", "public", "train_station", "station", "palace", "castle",
                     "university", "college", "museum", "theatre", "hospital", "courthouse",
                     "parliament", "stadium", "library", "monastery", "basilica"}
LANDMARK_AMENITY = {"townhall", "place_of_worship", "theatre", "library", "university", "courthouse",
                    "cinema", "arts_centre", "community_centre", "hospital", "marketplace", "conference_centre"}
# Footprints that are not buildings you would orbit: canopies, car parks, sheds, sites under construction.
NOT_A_BUILDING = {"roof", "canopy", "parking", "garage", "garages", "carport", "shed", "greenhouse",
                  "construction", "hangar", "silo", "storage_tank", "ruins", "bridge", "grandstand",
                  "shelter", "tent", "container"}


def is_landmark(tags):
    return (tags.get("building") in LANDMARK_BUILDING or tags.get("amenity") in LANDMARK_AMENITY
            or "historic" in tags or tags.get("tourism") in ("attraction", "museum")
            or tags.get("railway") == "station" or "castle_type" in tags)


def usable(tags):
    return tags.get("building") not in NOT_A_BUILDING and tags.get("amenity") != "parking" \
        and tags.get("parking") is None


def describe(tags, area):
    name = tags.get("name") or tags.get("name:en")
    kind = (tags.get("building") if tags.get("building") not in (None, "yes") else None) \
        or tags.get("amenity") or tags.get("tourism") or tags.get("historic") or "building"
    kind = kind.replace("_", " ")
    return f"{name} ({kind})" if name else f"unnamed {kind}, {area:,.0f} m²"


# --- geometry (local metres around the anchor) -------------------------------

def to_xy(lat, lon, lat0, lon0):
    return ((lon - lon0) * M_PER_DEG * math.cos(math.radians(lat0)), (lat - lat0) * M_PER_DEG)


def area_centroid(xy):
    a = cx = cy = 0.0
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        f = x1 * y2 - x2 * y1
        a += f
        cx += (x1 + x2) * f
        cy += (y1 + y2) * f
    if abs(a) < 1e-9:
        return 0.0, (sum(p[0] for p in xy) / len(xy), sum(p[1] for p in xy) / len(xy))
    return abs(a) / 2, (cx / (3 * a), cy / (3 * a))


def inside(p, xy):
    x, y = p
    c = False
    for i in range(len(xy)):
        x1, y1 = xy[i]
        x2, y2 = xy[(i + 1) % len(xy)]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            c = not c
    return c


def interior_point(xy, c):
    """Centroid if it is inside, else the interior grid point nearest to it."""
    if inside(c, xy):
        return c
    xs, ys = [p[0] for p in xy], [p[1] for p in xy]
    step = max(2.0, (max(xs) - min(xs)) / 40)
    best = None
    y = min(ys)
    while y <= max(ys):
        x = min(xs)
        while x <= max(xs):
            if inside((x, y), xy):
                d = math.hypot(x - c[0], y - c[1])
                if best is None or d < best[0]:
                    best = (d, (x, y))
            x += step
        y += step
    return best[1] if best else c


# --- Overpass -----------------------------------------------------------------

def overpass(query, url, tries=8):
    for attempt in range(tries):
        req = urllib.request.Request(url, data=urllib.parse.urlencode({"data": query}).encode(),
                                     headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                return json.load(r)["elements"]
        except Exception as e:
            wait = 10 * (attempt + 1)
            print(f"    {url.split('/')[2]}: {str(e)[:50]}; retrying in {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)
    return None


def cache_key(r):
    # Hand-picked rows are searched around a different point than the PDF
    # anchor that the plain city-name entry was fetched for.
    if r.get("anchor_source") == "handpicked":
        return f"{r['city']} @ {float(r['lat']):.5f},{float(r['lon']):.5f}"
    return r["city"]


def load_handpicked(path):
    if not path.exists():
        return {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {r["n"].strip(): r for r in csv.DictReader(f) if not r["n"].strip().startswith("#")}


def fetch_buildings(rows, radius, batch, cache_path):
    """One worker thread per Overpass mirror, each pulling batches of cities
    off a shared queue; a batch a mirror cannot get is put back for another.
    The cache file is rewritten after every batch so a rerun resumes."""
    import queue
    import threading
    cache = json.load(open(cache_path)) if cache_path.exists() else {}
    todo = [r for r in rows if cache_key(r) not in cache]
    q = queue.Queue()
    for i in range(0, len(todo), batch):
        q.put((0, todo[i:i + batch]))
    lock = threading.Lock()
    done = [0]

    def worker(url):
        while True:
            try:
                tries, chunk = q.get_nowait()
            except queue.Empty:
                return
            query = "[out:json][timeout:120];(" + "".join(
                f'way(around:{radius},{float(r["lat"]):.6f},{float(r["lon"]):.6f})["building"];' for r in chunk
            ) + ");out tags geom;"
            els = overpass(query, url)
            if els is None:
                if tries < 4:
                    q.put((tries + 1, chunk))
                else:
                    print(f"  giving up on {', '.join(r['city'] for r in chunk)} (rerun later)", flush=True)
                q.task_done()
                continue
            with lock:
                for r in chunk:
                    lat0, lon0 = float(r["lat"]), float(r["lon"])
                    mine = []
                    for e in els:
                        g = e.get("geometry")
                        if not g or len(g) < 4:
                            continue
                        # a footprint belongs to this city if any vertex is within the radius of its anchor
                        if any(math.hypot(*to_xy(p["lat"], p["lon"], lat0, lon0)) <= radius
                               for p in g[:: max(1, len(g) // 8)] + [g[0]]):
                            mine.append({"id": e["id"], "tags": e.get("tags", {}),
                                         "geom": [(p["lat"], p["lon"]) for p in g]})
                    cache[cache_key(r)] = mine
                done[0] += len(chunk)
                print(f"  overpass: {done[0]}/{len(todo)} cities fetched ({url.split('/')[2]})", flush=True)
                json.dump(cache, open(cache_path, "w"))
            q.task_done()
            time.sleep(1)

    threads = [threading.Thread(target=worker, args=(u,), daemon=True) for u in OVERPASS]
    for t in threads:
        t.start()
        time.sleep(3)
    for t in threads:
        t.join()
    missing = [r["city"] for r in rows if cache_key(r) not in cache]
    if missing:
        print(f"WARNING: no Overpass answer for {len(missing)} cities (treated as no_building; rerun to retry): "
              + ", ".join(missing), file=sys.stderr)
    return cache


# --- choosing ----------------------------------------------------------------

def choose(row, buildings, args):
    lat0, lon0 = float(row["lat"]), float(row["lon"])
    best = None
    for b in buildings:
        if not usable(b["tags"]):
            continue
        xy = [to_xy(la, lo, lat0, lon0) for la, lo in b["geom"]]
        if xy[0] == xy[-1]:
            xy = xy[:-1]
        area, c = area_centroid(xy)
        if area < args.min_area:
            continue
        if inside((0.0, 0.0), xy):
            return b, area, 0.0, (lat0, lon0), True  # anchor already on a building: keep it
        p = interior_point(xy, c)
        dist = math.hypot(*p)
        if dist > args.radius:
            continue
        landmark = is_landmark(b["tags"])
        named = bool(b["tags"].get("name") or b["tags"].get("name:en"))
        score = (min(area, args.area_cap) * (args.landmark_factor if landmark else 1.0)
                 * (1.5 if named else 1.0) / (1 + dist / args.distance_scale))
        if best is None or score > best[0]:
            lat = lat0 + p[1] / M_PER_DEG
            lon = lon0 + p[0] / (M_PER_DEG * math.cos(math.radians(lat0)))
            best = (score, b, area, dist, (lat, lon))
    if best is None:
        return None
    _, b, area, dist, latlon = best
    return b, area, dist, latlon, False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="city list from build_city_list.py")
    ap.add_argument("--out", required=True, help="snapped CSV for batch_generate.py")
    ap.add_argument("--review", help="review sheet CSV (default: <out> with _review suffix)")
    ap.add_argument("--cache", default="data/cache/osm_buildings_cache.json")
    ap.add_argument("--radius", type=float, default=400, help="search radius around the anchor, m")
    ap.add_argument("--min-area", type=float, default=300, help="ignore footprints smaller than this, m²")
    ap.add_argument("--area-cap", type=float, default=15000, help="area above this counts no extra, m²")
    ap.add_argument("--landmark-factor", type=float, default=4.0, help="score multiplier for landmark tags")
    ap.add_argument("--distance-scale", type=float, default=250, help="score halves at this distance, m")
    ap.add_argument("--batch", type=int, default=8, help="cities per Overpass request")
    ap.add_argument("--no-elevation", action="store_true", help="keep the old poi_alt_m (flat cities only)")
    ap.add_argument("--anchors", help="hand-picked anchors CSV (default: handpicked_anchors.csv next to the input)")
    args = ap.parse_args()

    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    fields = list(rows[0].keys())
    extra = ["orig_lat", "orig_lon", "snap_building", "snap_dist_m", "snap_area_m2", "snap_landmark", "snap_osm",
             "snap_status", "anchor_source"]
    for c in extra:
        if c not in fields:
            fields.append(c)

    anchors_path = Path(args.anchors) if args.anchors else Path(args.csv).with_name("handpicked_anchors.csv")
    hand = load_handpicked(anchors_path)
    unknown = set(hand) - {r.get("n", "") for r in rows}
    if unknown:
        sys.exit(f"{anchors_path}: row numbers not in {args.csv}: {', '.join(sorted(unknown))}")
    for r in rows:
        h = hand.get(r.get("n", ""))
        r["anchor_source"] = "handpicked" if h else "pdf"
        if h:
            if h["city"].strip() != r["city"]:
                sys.exit(f"{anchors_path}: row {h['n']} is {r['city']!r} in {args.csv}, not {h['city']!r}")
            r["lat"], r["lon"] = h["lat"].strip(), h["lon"].strip()
            r["anchor"] = h.get("anchor", "").strip() or r.get("anchor", "")
    if hand:
        print(f"{len(hand)} hand-picked anchor(s) from {anchors_path}")

    print(f"{len(rows)} cities; fetching building footprints within {args.radius:g} m of each anchor")
    cache = fetch_buildings(rows, args.radius, args.batch, Path(args.cache))

    moved = []
    for r in rows:
        r["orig_lat"], r["orig_lon"] = r["lat"], r["lon"]
        h = hand.get(r.get("n", ""))
        if h and h.get("snap", "yes").strip().lower() == "no":
            r.update(snap_building="", snap_dist_m=0, snap_area_m2="", snap_landmark="", snap_osm="",
                     snap_status="handpicked")
            moved.append(r)
            continue
        pick = choose(r, cache.get(cache_key(r), []), args)
        if pick is None:
            r.update(snap_building="", snap_dist_m="", snap_area_m2="", snap_landmark="", snap_osm="", snap_status="no_building")
            continue
        b, area, dist, (lat, lon), kept = pick
        r.update(snap_building=describe(b["tags"], area), snap_dist_m=round(dist), snap_area_m2=round(area),
                 snap_landmark="yes" if is_landmark(b["tags"]) else "", snap_osm=f"way/{b['id']}",
                 snap_status="kept" if kept else "snapped")
        if not kept:
            r["lat"], r["lon"] = f"{lat:.6f}", f"{lon:.6f}"
            r["anchor"] = b["tags"].get("name") or b["tags"].get("name:en") or r.get("anchor", "")
            moved.append(r)
        elif h:
            moved.append(r)  # the PDF anchor's terrain_m does not apply to a hand-picked point

    if moved and not args.no_elevation:
        print(f"fetching terrain elevation for {len(moved)} moved targets ...")
        el = fetch_elevations([(float(r["lat"]), float(r["lon"])) for r in moved])
        for r, e in zip(moved, el):
            if e is not None:
                r["terrain_m"] = round(e, 1)
                r["poi_alt_m"] = round(e + POI_ABOVE_DEM_M, 1)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    review = args.review or str(Path(args.out).with_name(Path(args.out).stem + "_review.csv"))
    with open(review, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["n", "city", "country", "status", "building", "landmark", "moved_m", "area_m2",
                    "old_lat", "old_lon", "new_lat", "new_lon", "osm", "map"])
        for r in rows:
            w.writerow([r.get("n", ""), r["city"], r.get("country", ""), r["snap_status"], r["snap_building"],
                        r["snap_landmark"], r["snap_dist_m"], r["snap_area_m2"], r["orig_lat"], r["orig_lon"],
                        r["lat"], r["lon"],
                        f"https://www.openstreetmap.org/{r['snap_osm']}" if r["snap_osm"] else "",
                        f"https://www.google.com/maps/@{r['lat']},{r['lon']},200m/data=!3m1!1e3"])

    n = {s: sum(1 for r in rows if r["snap_status"] == s) for s in ("snapped", "kept", "no_building", "handpicked")}
    print(f"snapped {n['snapped']}, kept {n['kept']} (anchor already on a building), "
          f"handpicked {n['handpicked']} (used as given), no building found {n['no_building']}")
    print(f"wrote {args.out} and {review}")
    if n["no_building"]:
        print("no building within radius (add them to handpicked_anchors.csv): "
              + ", ".join(r["city"] for r in rows if r["snap_status"] == "no_building"))
    sea = [r for r in rows if r.get("terrain_m") not in (None, "") and float(r["terrain_m"]) <= 0]
    if sea:
        print(f"WARNING: {len(sea)} target(s) with terrain_m <= 0, probably offshore (the render would be open "
              "water); hand-pick them: " + ", ".join(f"{r.get('n', '')} {r['city']} ({r['terrain_m']})" for r in sea))


if __name__ == "__main__":
    main()
