# Earth Studio city orbits

Automates the Google Earth Studio half of a city-annotation dataset: for each
city, one 2 s / 30 fps **orbit** rendered twice — a `satellite` view from
~1200 m and a `ground_truth` oblique view showing building facades — as 61
JPEG frames at 2048×2048 plus Earth Studio's JSON 3D camera track. Output is
laid out exactly like the reference dataset (`reference/`), so the folders
can be merged straight into it.

Every step takes a city range, so the same three commands work whether your
share is cities 10–50 or 351–700.

## Layout

```
ges_esp.py            .esp schema + orbit geometry (library used by the two scripts below)
build_city_list.py    "700 cities.pdf" -> CSV with lat/lon and terrain-based target altitude
batch_generate.py     CSV -> <projects>/<city>/{satellite,ground_truth}/<view>.esp
render_all.py         drives Earth Studio in Chrome, renders every project, unpacks results

data/                 700 cities.pdf (the master list), the CSVs you generate from it
projects/             generated .esp inputs + metadata.csv, manifest.json, render_order.txt
cities_<A>_<B>/       rendered output for one share (not for git: ~290 MB per view)
reference/            the existing dataset used to reverse-engineer parameters:
                        Cities/Koblenz  – one hand-made city with real frames
                        city_satellite/ – 300 cities (frames are git-LFS stubs, .esp/.json real)
archive/              earlier session transcripts, the first generator draft, Koblenz test renders
.ges-chrome-profile/  Chrome profile render_all.py signs in with (local only, created on first run)
```

## Quick start: render your share

```bash
pip install -r requirements.txt   # playwright; also needs Google Chrome and poppler-utils (pdftotext)

FIRST=10; LAST=50                 # the rows of "700 cities.pdf" that are yours
SHARE=cities_${FIRST}_${LAST}

# 1. PDF -> CSV for your rows, with terrain elevation from OpenTopoData
python3 build_city_list.py "data/700 cities.pdf" --range $FIRST $LAST --out data/$SHARE.csv

# 2. CSV -> two .esp files per city (+ metadata.csv, manifest.json, render_order.txt)
python3 batch_generate.py data/$SHARE.csv --out projects

# 3. Render every project in Earth Studio (Chrome driven by Playwright).
#    A Chrome window opens; sign in to Google the first time and leave it open.
#    Resumable: already-complete <city>/<view> folders are skipped.
python3 render_all.py projects --out $SHARE
```

Step 1 prints a warning if any PDF row failed to parse; step 2 warns about
rows with missing coordinates or altitude. Check both before rendering.

To run step 3 unattended and keep it alive after closing the terminal:

```bash
setsid nohup python3 render_all.py projects --out $SHARE >> $SHARE/render_all.log 2>&1 < /dev/null &
tail -f $SHARE/render_all.log                 # progress
ls -d $SHARE/*/*/footage | wc -l              # finished views (2 per city)
pkill -f "python3 render_all[.]py"; pkill -f "ges-chrome-profile"   # stop; rerun to resume
```

Useful options: `render_all.py --only koblenz trier` renders named cities
only, `--limit 2` stops after two views (good for a first test), and
`batch_generate.py --out projects_test` keeps an experiment separate from
the main queue.

### Using your own city list instead of the PDF

`batch_generate.py` only needs a CSV with `city, lat, lon, poi_alt_m`
(optional: `country, sat_radius_m, sat_height_m, gnd_radius_m, gnd_height_m,
world_time_utc`). `poi_alt_m` is the absolute altitude of the orbit target:
terrain elevation + 27 m (see *Target altitude*). `data/cities_sample.csv`
is a minimal example. To hand-pick a building instead of the PDF's downtown
anchor, edit `lat`/`lon` in the CSV and rerun step 2.

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

## Rendering (`render_all.py`)

Per project the script opens Earth Studio in a Chrome it launches with the
profile in `.ges-chrome-profile/` (sign in once; the profile keeps it), drops
the `.esp` onto the page (same as drag-and-drop import), clicks Render, then
Start, watches the "Rendered: n / 61" counter, catches the zip, and
unpacks it into `<share>/<city>/<view>/`. It checks 61 frames and 61
camera poses in the tracking JSON before marking a render done.

* **Destination folder.** Current Earth Studio asks for an output *folder*
  through the browser's native folder picker (File System Access API), which
  a script cannot drive. The script deletes `window.showOpenFilePicker` and
  friends before the page loads; Earth Studio's support check is
  `"showOpenFilePicker" in window`, so with them gone it falls back to
  packaging the render as a zip download (what Brave did for the Koblenz test
  renders). The properties must be *deleted*, not set to `undefined`.
* **Tab visibility.** Local renders pause when the tab is hidden. The script
  pins `document.hidden`/`visibilityState` to visible and starts Chrome with
  background throttling disabled, but keep the window un-minimised.
* **Stalls.** No progress for `--stall-minutes` (default 4) reloads the page
  and retries the project, up to `--retries` (3). The 3D-node preload is the
  usual place it hangs.
* **Zip hand-off.** Earth Studio zips the frames in-page and pushes the Blob
  through Chrome's download subsystem via `SafeDownloader.download`; Chrome
  crashed (SIGTRAP) on that download here. The script replaces that function
  so the Blob stays in memory, then copies it out in 16 MB chunks over the
  automation channel. If Chrome does die, the script relaunches it and retries.
* ~65–80 s per view on the machine this was developed on (about 13 h for 350
  cities), roughly 290 MB per view → ~600 MB per city. Budget disk
  accordingly before starting a large share.

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
