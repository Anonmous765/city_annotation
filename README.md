# Earth Studio city orbits

Automates the Google Earth Studio half of a city-annotation dataset: for each
city, one 2 s / 30 fps **orbit** rendered twice — a `satellite` view from
~1200 m and a `ground_truth` oblique view showing building facades — as 61
JPEG frames at 2048×2048 plus Earth Studio's JSON 3D camera track. Output is
laid out exactly like the reference dataset (`reference/`), so the folders
can be merged straight into it.

Every step takes a city range, so the same commands work whether your
share is cities 10–50 or 351–700. *How the pipeline works* below walks
through each stage in detail.

## Layout

```
scripts/
  ges_esp.py            .esp schema + orbit geometry (library used by the scripts below)
  build_city_list.py    "700 cities.pdf" -> CSV with lat/lon and terrain-based target altitude
  snap_to_buildings.py  optional: move each target from the PDF's downtown point onto a real
                        building nearby (OpenStreetMap footprints) + a review sheet
  batch_generate.py     CSV -> <projects>/<city>/{satellite,ground_truth}/<view>.esp
  render_all.py         drives Earth Studio in Chrome, renders every project, unpacks results
                        (--parallel N runs N Chrome windows at once)
  inspect_renders.py    viewer: all 61 frames of both views per city side by side, Prev/Next
                        buttons, click a tile to enlarge, flag bad cities

data/                   700 cities.pdf (the master list), the CSVs you generate from it,
                        handpick_needed.md, the Overpass cache
projects/               generated .esp inputs + metadata.csv, manifest.json, render_order.txt
                        (not tracked: regenerated from the CSV by batch_generate.py)
cities_<A>_<B>/         rendered output for one share (not for git: ~600 MB per city);
                        also holds render_log.csv, render_all*.log, debug/ screenshots and
                        the viewer's .inspect/ cache and inspect_flags.csv
reference/              the existing dataset used to reverse-engineer parameters:
                          Cities/Koblenz  – one hand-made city with real frames
                          city_satellite/ – 300 cities (frames are git-LFS stubs, .esp/.json real)
archive/                earlier session transcripts, the first generator draft, Koblenz test renders
.ges-chrome-profile*/   Chrome profiles render_all.py signs in with (local only, created on first run)
```

All commands below are run from the repo root. The scripts import each
other by bare name, which works because Python puts the script's own folder
on the import path.

## Quick start: render your share

```bash
pip install -r requirements.txt   # playwright; also needs Google Chrome and poppler-utils (pdftotext)

FIRST=10; LAST=50                 # the rows of "700 cities.pdf" that are yours
SHARE=cities_${FIRST}_${LAST}

# 1. PDF -> CSV for your rows, with terrain elevation from OpenTopoData
python3 scripts/build_city_list.py "data/700 cities.pdf" --range $FIRST $LAST --out data/$SHARE.csv

# 2. CSV -> two .esp files per city (+ metadata.csv, manifest.json, render_order.txt)
python3 scripts/batch_generate.py data/$SHARE.csv --out projects

# 3. Render every project in Earth Studio (Chrome driven by Playwright).
#    A Chrome window opens; sign in to Google the first time and leave it open.
#    Resumable: already-complete <city>/<view> folders are skipped.
python3 scripts/render_all.py projects --out $SHARE
```

Step 1 prints a warning if any PDF row failed to parse; step 2 warns about
rows with missing coordinates or altitude. Check both before rendering.

To run step 3 unattended and keep it alive after closing the terminal:

```bash
setsid nohup python3 scripts/render_all.py projects --out $SHARE >> $SHARE/render_all.log 2>&1 < /dev/null &
tail -f $SHARE/render_all.log                 # progress
ls -d $SHARE/*/*/footage | wc -l              # finished views (2 per city)
```

### Stopping and resuming

It is safe to stop at any point. A view is only counted as done once its
`<view>.json` and all frames are unpacked into `<city>/<view>/footage/`,
so the view that was mid-render is simply rendered again next time;
nothing that is already complete is touched.

**Stop.** How depends on how you started step 3:

* Foreground (`python3 scripts/render_all.py ...` in a terminal): press `Ctrl+C`.
  The script and the Chrome it launched exit together.
* Background (the `setsid nohup ...` line above): `Ctrl+C` does nothing,
  because the script is detached from the terminal. Closing the Chrome
  window does not help either: the script treats that as a crash and
  relaunches Chrome. Kill both explicitly:

  ```bash
  pkill -f "python3 render_all[.]py"; pkill -f "ges-chrome-profile"
  pgrep -af "render_all|ges-chrome-profile"   # should print nothing
  ```

**Resume.** Run the same command you started with, from the project folder.
`$SHARE` is a plain shell variable and is empty in a new terminal, so set it
again first:

```bash
SHARE=cities_351_700                          # your share
setsid nohup python3 scripts/render_all.py projects --out $SHARE >> $SHARE/render_all.log 2>&1 < /dev/null &
tail -f $SHARE/render_all.log
```

The first log line reads `N projects listed, M already rendered, K to do`,
which confirms it picked up where it left off. Pass the same `--only` /
`--limit` options as before if you used any. Your Google sign-in is kept in
`.ges-chrome-profile/`, so Chrome should not ask you to log in again.

Useful options: `scripts/render_all.py --only koblenz trier` renders named cities
only, `--limit 2` stops after two views (good for a first test), and
`scripts/batch_generate.py --out projects_test` keeps an experiment separate from
the main queue.

### Checking the renders

```bash
python3 scripts/inspect_renders.py $SHARE --csv data/${SHARE}_snapped.csv   # or data/$SHARE.csv
```

Opens a window with every frame of the satellite view tiled on the left and
every frame of the ground_truth view on the right, one city at a time, with
the city's row number, target coordinates and snap status in the title.
`Next` / `Prev` (or `→` / `←`) step through the cities in list order, the
text box jumps to a folder name or row number, and `f` flags the current
city as wrong; flags land in `<share>/inspect_flags.csv`. To revisit
them, `]` / `[` jump to the next / previous flagged city, `--flagged`
opens the viewer on the flagged cities only, and `--list-flagged` just
prints them. Click any tile
to open that frame at full 2048 px in its own window (click more tiles
for more windows; `←` / `→` there step through the frames, the toolbar
zooms, `Esc` closes). The first run
builds one downscaled contact sheet per view into `<share>/.inspect/`
(about a minute for 350 cities on all cores); later runs are instant, and a
sheet is rebuilt automatically when its frames are newer. Cities with fewer
than 61 frames in a view are marked INCOMPLETE in the title.

### Rendering in parallel

One Earth Studio tab renders one view at a time and spends most of it waiting
on Earth Studio's own JavaScript (tile loading, zipping), so the GPU sits
mostly idle. `--parallel N` runs N copies of the script at once, each in its
own Chrome window on its own share of the cities:

```bash
setsid nohup python3 scripts/render_all.py projects --out $SHARE --parallel 3 >> $SHARE/render_all.log 2>&1 < /dev/null &
tail -f $SHARE/render_all.*.log                 # one log per window
```

* Each window needs its own Chrome profile (Chrome refuses to open one
  profile twice). The extra profiles `.ges-chrome-profile-1`, `-2`, ... are
  copied from `.ges-chrome-profile` on first use and keep the Google sign-in.
* Cities are dealt out round-robin, so no two windows ever write the same
  `<city>/` folder. Stopping and resuming works exactly as above; kill all the
  `render_all.py` processes and the Chromes, then rerun the same command.
* Measured on an RTX 5080 with 30 GB RAM: 3 windows give ~2.3x the
  throughput of one (a view takes ~85 s instead of ~65 s when three run at
  once). Each window uses ~3 GB RAM at peak, so 3–4 is the practical limit.
  More concurrent tile traffic may also make position-specific stalls (see
  *Known constraints*) a little more common; they are retried as usual.
* Windows are tiled rather than maximised. Keep them on screen; a minimised
  window can be throttled by the OS.

### Using your own city list instead of the PDF

`batch_generate.py` only needs a CSV with `city, lat, lon, poi_alt_m`
(optional: `country, sat_radius_m, sat_height_m, gnd_radius_m, gnd_height_m,
world_time_utc`). `poi_alt_m` is the absolute altitude of the orbit target:
terrain elevation + 27 m (see *Target altitude*). `data/cities_sample.csv`
is a minimal example. To hand-pick a building instead of the PDF's downtown
anchor, edit `lat`/`lon` in the CSV and rerun step 2.

### Putting the orbit target on a building

The PDF's "downtown anchor" is usually a road junction or a square, so the
orbit often circles pavement (Venice: the Piazzale Roma bridgehead). Only
about a fifth of the anchors in rows 351–700 sit on a building footprint.
`snap_to_buildings.py` moves each target onto a real building nearby using
OpenStreetMap footprints, preferring landmarks (town hall, cathedral,
station, castle, museum, ...) and otherwise the largest footprint, discounted
by distance so the target stays downtown:

```bash
python3 scripts/snap_to_buildings.py data/$SHARE.csv --out data/${SHARE}_snapped.csv
# review data/${SHARE}_snapped_review.csv (building, distance moved, map links),
# fix any row by hand, then:
python3 scripts/batch_generate.py data/${SHARE}_snapped.csv --out projects
```

Terrain elevation is re-fetched for the moved points. Rows where no building
was found within 400 m are left unchanged and listed at the end; pick those
by hand; for rows 351–700 those six are listed in `data/handpick_needed.md` and
are skipped until picked. Overpass results are cached in `data/osm_buildings_cache.json`.
The public Overpass servers rate-limit aggressively; the script retries and
rotates mirrors, but a full 350-city run can take the better part of an
hour. Note the reference dataset itself mostly used Earth Studio's default
city coordinate, so snapping is an improvement over it, not a requirement
for matching it.

## Output layout

What the dataset expects, same as `reference/Cities/Koblenz` and
`reference/city_satellite/cities/<city>`:

```
projects/<city_folder>/satellite/satellite.esp          # generated inputs
projects/<city_folder>/ground_truth/ground_truth.esp
projects/metadata.csv                                    # city_folder,country,latitude,longitude

<share>/<city_folder>/satellite/footage/satellite_00.jpeg … satellite_60.jpeg
<share>/<city_folder>/satellite/satellite.esp, satellite.json, ImagerySources.txt
<share>/<city_folder>/ground_truth/…                     # same four things
<share>/metadata.csv                                     # copy of projects/metadata.csv
<share>/render_log.csv                                   # one row per render attempt
<share>/debug/                                           # screenshots taken when something goes wrong
```

`city_folder` is the lower-case ASCII slug of the city name (`saarbrucken`);
cities that share a name get the country appended (`cordoba_argentina`),
as the reference dataset does.

## How the pipeline works

Four scripts, run in order. Each one reads the previous one's output file
and nothing else, so any stage can be rerun on its own. `ges_esp.py` is a
library the middle two import; it holds the orbit geometry and the `.esp`
schema. Only the last stage talks to Earth Studio.

```
"700 cities.pdf"
   │  build_city_list.py        (pdftotext + OpenTopoData)
   ▼
data/<share>.csv                 n, city, country, continent, anchor, lat, lon, terrain_m, poi_alt_m
   │  snap_to_buildings.py      (Overpass / OpenStreetMap + OpenTopoData)   ← optional
   ▼
data/<share>_snapped.csv         same columns, lat/lon moved onto a building + snap_* review columns
   │  batch_generate.py         (ges_esp.build_esp)
   ▼
projects/<city>/{satellite,ground_truth}/<view>.esp   + metadata.csv, manifest.json, render_order.txt
   │  render_all.py             (Playwright → Chrome → Earth Studio)
   ▼
<share>/<city>/<view>/footage/<view>_00..60.jpeg + <view>.json + ImagerySources.txt + <view>.esp
```

### Stage 1 — PDF to CSV (`build_city_list.py`)

1. **Text extraction.** Runs `pdftotext -layout` on the PDF and walks the
   output line by line. Each page's header row gives the character offsets
   of the City / Country / Continent / Downtown anchor / Latitude columns,
   and cells are cut at those offsets.
2. **Row reassembly.** A row starts with a line whose first token is the
   city number. The PDF wraps long cells onto the lines above and below, and
   prints negative latitudes as a lone `-` with the digits on the next line,
   so the parser looks at neighbouring lines to collect the wrapped text and
   the sign. A small table of truncated country names (`anada` → `Canada`)
   repairs cells the layout cut in half. Rows it cannot parse are printed
   with `skip` and dropped, so check stderr.
3. **Range.** `--range FIRST LAST` keeps only your rows.
4. **Elevation.** Sends the anchors to OpenTopoData (`mapzen` dataset, 30 m
   global DEM) in batches of 100 with a 1 s pause, and writes
   `terrain_m` plus `poi_alt_m = terrain_m + 27`. The 27 m is the measured
   offset between a bare-earth DEM and Earth Studio's own 3D surface, where
   its Orbit quickstart puts the target (see *Target altitude*). No
   coverage leaves `poi_alt_m` blank for you to fill in by hand.

### Stage 2 — move targets onto buildings (`snap_to_buildings.py`, optional)

The PDF anchor is usually a road junction, so the ground_truth orbit would
circle pavement. This stage moves it onto a building:

1. **Fetch footprints.** One Overpass query per batch of cities asks for
   every `way["building"]` within `--radius` (400 m) of each anchor. Results
   are cached in `data/osm_buildings_cache.json`, so a rerun is instant; the
   script retries with backoff and rotates between three public mirrors
   because they rate-limit aggressively.
2. **Filter.** Footprints smaller than `--min-area` (300 m²) and structures
   that are not real buildings (canopies, car parks, sheds, construction
   sites) are dropped.
3. **Score.** Each remaining footprint gets
   `min(area, 15 000 m²) × (4 if landmark else 1) × name bonus / (1 + distance / 250 m)`.
   "Landmark" means a building or amenity tag like town hall, cathedral,
   church, mosque, castle, palace, station, museum, theatre, university,
   courthouse, or anything tagged `historic=*` / `tourism=attraction`. The
   distance divisor halves the score every 250 m so the target stays
   downtown even when a large mall sits on the edge of the radius.
4. **Pick a point.** The target is the footprint's centroid, or the nearest
   interior point if the centroid falls outside an L-shaped building.
5. **Classify.** If the anchor already lies inside a usable footprint the
   row is marked `kept` and left as is. If nothing scored, it is marked
   `no_building` and left unchanged (those are the hand-pick cases in
   `data/handpick_needed.md`). Otherwise it is `snapped`.
6. **Re-fetch elevation** for every moved point so `poi_alt_m` is still
   terrain + 27 m at the new location.
7. **Write** the snapped CSV (same columns, `anchor` replaced by the building
   description) with review columns `orig_lat, orig_lon, snap_building,
   snap_dist_m, snap_area_m2, snap_landmark, snap_osm, snap_status`, plus a
   separate `_review.csv` with map links for eyeballing.

Nothing downstream depends on the `snap_*` columns; `batch_generate.py`
reads the CSV exactly as it would the stage 1 file. That is also why the
six `no_building` rows still render: their coordinates are simply the PDF
anchor, and the generator does not look at the status.

### Stage 3 — CSV to Earth Studio projects (`batch_generate.py` + `ges_esp.py`)

1. **Parse rows.** Requires `city, lat, lon, poi_alt_m`; rejects rows with
   unparseable or out-of-range values and warns about latitudes beyond 85°,
   where orbit longitude spacing degenerates. Optional columns
   `sat_radius_m, sat_height_m, gnd_radius_m, gnd_height_m` override the
   orbit for that city (how the Lublin and Toruń fixes are recorded), and
   `world_time_utc` sets the sun clock.
2. **Folder names.** Each city becomes a lower-case ASCII slug
   (`saarbrucken`); cities that share a slug get the country appended
   (`cordoba_argentina`), matching the reference dataset.
3. **Two views per city.** `satellite` orbits at 688 m radius, 1190 m above
   the target (pitch ≈ 60° down); `ground_truth` at 624 m radius, 312 m up
   (pitch ≈ 27°). These are the modal values across the 300 reference cities
   (see *Parameters*).
4. **Orbit geometry** (`ges_esp.orbit_keyframes`). The radius is converted to
   degrees (`radius / 111 320 m` for latitude, divided again by `cos(lat)`
   for longitude) and five camera keyframes are placed at normalised times
   0, ¼, ½, ¾, 1 at north, west, south, east, north of the target. Longitude
   keyframes use Earth Studio's `auto` bezier tangents at their extrema and
   `linear` at the zero crossings; latitude is the same one keyframe out of
   phase. Camera altitude is `round(poi_alt_m + height)` on every keyframe.
   A `cameraTargetEffect` block pins the look-at to the target's lat / lon /
   altitude, so heading and pitch follow automatically.
5. **Encoding** (`ges_esp.build_esp`). Every value is normalised to 0–1 the
   way Earth Studio stores it (`(lon+180)/360`, `(lat+90)/180`,
   `(alt+500)/65 117 981`; see *`.esp` schema notes*). The JSON is assembled
   to mirror a hand-authored `modelVersion 18` quickstart project key for
   key, including the untouched environment groups, so the file imports by
   drag-and-drop without any warning. Render settings baked in: 2048×2048,
   30 fps, duration 60 frames (which Earth Studio renders as 61 images,
   `00`–`60`), project name `satellite` / `ground_truth` so the frames get
   the right file prefix.
6. **Outputs.** `projects/<city>/<view>/<view>.esp` for every view;
   `metadata.csv` (city_folder, country, latitude, longitude, later copied
   into the share); `manifest.json` (per-view radius, height, camera
   altitude, pitch, slant range and an ideal-circle per-frame camera track
   for sanity checks); `render_order.txt` (the queue stage 4 walks).

### Stage 4 — render in Earth Studio (`render_all.py`)

`render_all.py` drives Earth Studio's web UI through Playwright. Per view it
opens Earth Studio in a Chrome it launches with the profile in
`.ges-chrome-profile/` (sign in once; the profile keeps it), drops the `.esp`
onto the page, clicks Render, then Start, watches the "Rendered: n / 61"
counter, catches the zip, and unpacks it into `<share>/<city>/<view>/`.

**Setup and queue**

* Copies `projects/metadata.csv` into the share folder and reads
  `render_order.txt` (falling back to a glob of `*/<view>/<view>.esp`).
* Skips every view that is already done. "Done" means `<view>.json` exists
  and `footage/` holds at least 61 `<view>_*.jpeg`. This is what makes the
  run resumable: stopping mid-render only costs the view in flight.
* `--only`, `--limit` and `--shard k/N` narrow the queue; `--parallel N`
  spawns N copies of the script on disjoint shards, each with its own cloned
  Chrome profile and its own `render_all.<k>.log`.
* Chrome is launched with background throttling disabled and the automation
  banner suppressed. Two init scripts run before every page load: one pins
  `document.hidden` / `visibilityState` to visible (Earth Studio pauses local
  renders in a hidden tab), the other deletes `showOpenFilePicker`,
  `showDirectoryPicker` and `chooseFileSystemEntries` from `window`.

**Per view** (`render_one`)

1. Navigate to `earth.google.com/studio` and wait for the start screen. On
   the first run this is where you sign in; the script waits up to
   `--login-timeout` minutes.
2. Confirm the folder-picker API is really gone. Current Earth Studio asks
   for a *destination folder* through the browser's native picker, which a
   script cannot drive; its support check is `"showOpenFilePicker" in
   window`, so with the properties deleted (not set to `undefined`) it falls
   back to packaging the render as a zip.
3. Patch the page's `SafeDownloader.download` so the zip Blob stays in
   memory instead of going through Chrome's download subsystem, which
   crashed with SIGTRAP on it here.
4. Dismiss the "Uh-oh! Something went wrong… recover?" modal if present.
5. Inject the `.esp` text as a synthetic drag-and-drop event (the same code
   path as importing a file) and wait for the tab title to change to the
   project name, then for "Loading Earth" to disappear.
6. Click Render. The dialog is pre-filled from the project (2048×2048,
   frames 0–60, JPEG, JSON 3D tracking). Dismiss the first-run "Choose
   folder" tip if it covers the dialog, click Start, and check that the
   Start button went away.
7. Poll the page text every 4 s for `Rendered: n / 61` (or `Loading 3D
   nodes: n / m`). Click "Continue Rendering" if Earth Studio paused
   itself. If the counter does not move for `--stall-minutes` (1.5), raise a
   stall; if the whole render passes `--max-minutes` (40), give up.
8. When the in-page Blob appears, read it out in 16 MB base64 chunks over
   the automation channel and write `<view>.zip` next to the output folder.

**After each view** (`unzip_into`)

* Extracts the zip (`footage/`, `<view>.json`, `ImagerySources.txt`, and
  usually Earth Studio's own re-serialised `<view>.esp`; if the zip has no
  `.esp` the generated one is copied in).
* Verifies 61 frames and 61 `cameraFrames` entries in the tracking JSON
  before deleting the zip and marking the view `ok`. Anything short is a
  failure, the partial `footage/` is deleted, and the view is retried.

**Failure handling**

* Ordinary errors (import timeout, Start not accepted, bad zip) are retried
  immediately up to `--retries` (3) times. If Chrome has died the script
  relaunches it and continues, up to `--max-crashes` (50) times.
* A stall is treated differently. Earth Studio sometimes freezes at one
  specific frame of one project, every time, in any browser, while other
  cities keep rendering, which looks like Google's 3D tile data for that
  spot being unavailable. Retrying right away only burns time, so the view
  is logged as `stalled` and the script moves on. After the first sweep it
  waits `--pass-wait-minutes` (15) and retries only the stalled views, up
  to `--passes` (4) sweeps in total. Views still stalled at the end are
  listed in the log; rerun the same command later, or nudge that city's
  target or radius in the CSV and regenerate its project.
* Every attempt is appended to `<share>/render_log.csv`
  (`finished_at, city, view, status, seconds, attempts, note`), and a
  screenshot is written to `<share>/debug/` whenever something goes wrong.

**Cost.** About 65–80 s per view single-window (roughly 13 h for 350
cities; `--parallel 3` gives ~2.3×), and ~290 MB per view, so ~600 MB per
city. Budget disk before starting a large share.

## How the generator was verified

The project name inside each `.esp` is `satellite` / `ground_truth`, so the
renderer names frames `satellite_00.jpeg … satellite_60.jpeg` like the
existing data (duration 60 frames @ 30 fps = 2 s = **61** images).

* Regenerating the hand-made Koblenz and Zurich projects from their measured
  parameters reproduces the real `.esp` files key-for-key; every keyframe
  lands within 8 cm of the original. The only differences are the sun
  `worldTime` window (stamped with the creation time; the sun is off by
  default so renders are unaffected — Koblenz was rendered at 18:38 local
  and is plain daylight imagery) and three `rotationX/Y/Z` values that the
  camera-target effect overrides anyway.
* Orbit direction (N → W → S → E) and radius match Earth Studio's own JSON
  3D-tracking export for Koblenz.

## Parameters (measured across the 300 reference cities)

| View | Radius | Camera height above target | Notes |
|---|---|---|---|
| satellite | 688 m | 1190 m | dataset README: "688 m, ≈1200 m"; 688 m is the modal radius, 1190 m the modal height |
| ground_truth | 624 m | 312 m | modal values (133/300 and 124/300); the reference chose these per city, 375–750 m / 190–460 m |

Earth Studio's radius slider steps in 62.5 m increments; camera altitude is
stored as a whole number of metres. Override per city with the optional CSV
columns `sat_radius_m`, `sat_height_m`, `gnd_radius_m`, `gnd_height_m`.

## Target altitude

Earth Studio stores the orbit target as an absolute altitude. Across the 300
reference cities it sits **27 m above bare-earth DEM elevation** (median;
p10 +17 m, p90 +31 m), so `build_city_list.py` writes
`poi_alt_m = terrain_m + 27`. The generated Koblenz target comes out at 99 m
vs 99.98 m in the hand-made project. For the satellite view a 10 m error is
irrelevant; for ground_truth it shifts the pitch by ~1–2°.

## Things the reference dataset does that you should know

* `metadata.csv` coordinates in `reference/city_satellite/` are **not** the orbit
  target: they sit ~670 m north of it (Earth Studio offsets the target when
  you type a location). The generator puts the target exactly on the PDF
  coordinate and writes that coordinate to `metadata.csv`.
* Reference `.esp` files say 1920×1080 but were rendered at 2048×2048 from the
  render dialog. The generator writes 2048×2048 into the project so the render
  dialog is pre-filled. Framing was checked against `reference/Cities/Koblenz`:
  a 2048×2048 project renders identically to the reference workflow.
* `manifest.json` contains an *approximate* per-frame camera track (ideal
  circle). Earth Studio's bezier interpolation deviates up to ~20 m between
  keyframes; the JSON tracking file written at render time is the truth.

## `.esp` schema notes

All attribute values are normalised to `[0, 1]`:

| Attribute | Encoding |
|---|---|
| `longitude` | `(lon + 180) / 360` |
| `latitude` | `(lat + 90) / 180` |
| `altitude` / `altitudePOI` | `(alt_m + 500) / 65_117_981` — absolute, MSL |
| `worldTime` | fraction of the author-defined `[min, max]` epoch-ms window |

Keyframe `time` is normalised across scene duration (`0 … 1`), not frames.
The orbit is five keyframes at `t = 0, .25, .5, .75, 1`; longitude uses
`auto` transitions at its extrema (odd indices) and `linear` at zero
crossings, latitude is phase-shifted by one. `cameraTargetEffect` pins the
look-at to the POI.

## Known constraints

* Rendering happens in the Earth Studio browser UI one project at a time
  (`render_all.py` automates it, but the Chrome window has to stay open).
  Cloud rendering only produces video.
* OpenTopoData is rate-limited (1 req/s, 100 points/req); a few hundred
  cities take a few seconds. Cities with no coverage get a blank `poi_alt_m`
  to fill by hand.
* Earth Studio's internals (drop import, visibility check, zip hand-off) were
  read from its minified bundle and may change with a new release; if a step
  stops working, those hooks in `render_all.py` are the first place to look.
* Earth Studio sometimes freezes mid-render at one specific frame of one
  project (frame counter stops, "00:00 remaining", no error). It follows the
  camera position, not the browser, so it appears to be Google's 3D tile
  data for that spot being unavailable or mid-update. `render_all.py` logs
  the view as `stalled`, moves on, and sweeps stalled views again in later
  passes; see *Failure handling* under Stage 4. If one never clears, nudge
  that city's target or radius in the CSV and regenerate its project.
* After a stall reload Earth Studio shows an "Uh-oh! Something went wrong"
  recovery modal that blocks every click; the script dismisses it
  automatically. If a run logs repeated `could not click 'Render'`
  failures, look at `<out>/debug/render_debug_click_render_blocked.png`.
