#!/usr/bin/env python3
"""
inspect_renders.py — Eyeball every rendered city: all 61 frames of the
satellite view and all 61 of the ground_truth view, tiled into two grids side
by side, with Prev / Next buttons to step through the cities.

    python3 scripts/inspect_renders.py cities_351_700
    python3 scripts/inspect_renders.py cities_351_700 --csv data/cities_351_700/snapped.csv
    python3 scripts/inspect_renders.py cities_351_700 --start torun
    python3 scripts/inspect_renders.py cities_351_700 --only leiden villach   # just these
    python3 scripts/inspect_renders.py cities_351_700 --flagged          # revisit only flagged cities
    python3 scripts/inspect_renders.py cities_351_700 --list-flagged     # just print them

Loading 122 full-size 2048x2048 JPEGs per city is far too slow to browse, so
the first run builds one downscaled contact sheet per <city>/<view> into
<share>/.inspect/ (all cores, a few minutes for 350 cities) and later runs
reuse them. A sheet is rebuilt when its frames are newer than it, or with
--rebuild.

Controls
    Next / Prev buttons, or  →  ←        step through cities
    Home / End                           first / last city
    f                                    flag the current city as wrong (toggles);
                                         flags are written to <share>/inspect_flags.csv
    ]  /  [                              jump to the next / previous flagged city
    s  /  g                              open this city's satellite / ground_truth project
                                         in Google Earth Studio (a tab in one Chrome window,
                                         via open_in_studio.py); also the Studio buttons
    type a city folder name in the box   jump to it
    click a tile                         open that frame full size (2048 px) in its own
                                         window; click more tiles for more windows.
                                         In that window: ← → step frames, scroll wheel or
                                         the toolbar zooms, Esc / q closes it
    q                                    quit

With --csv the cities come in list order (row n) and the title shows the
anchor / building, snap status and target coordinates from the CSV; without
it they are alphabetical by folder name.
"""
import argparse
import csv
import math
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from batch_generate import slug

VIEWS = ("satellite", "ground_truth")
N_FRAMES = 61


# --- contact sheets ---------------------------------------------------------

def sheet_path(share: Path, city: str, view: str) -> Path:
    return share / ".inspect" / f"{city}_{view}.jpg"


def frames_of(share: Path, city: str, view: str):
    return sorted((share / city / view / "footage").glob(f"{view}_*.jpeg"))


def sheet_is_stale(share: Path, city: str, view: str) -> bool:
    out = sheet_path(share, city, view)
    frames = frames_of(share, city, view)
    if not frames:
        return False  # nothing to build from
    if not out.exists():
        return True
    return out.stat().st_mtime < max(f.stat().st_mtime for f in frames)


def build_sheet(args):
    """Tile every frame of one view into a cols-wide grid of thumb-px squares
    with the frame number in the corner. Runs in a worker process."""
    share, city, view, thumb, cols = args
    share = Path(share)
    frames = frames_of(share, city, view)
    if not frames:
        return city, view, "no frames"
    rows = math.ceil(len(frames) / cols)
    sheet = Image.new("RGB", (cols * thumb, rows * thumb), (24, 24, 24))
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", max(10, thumb // 10))
    except OSError:
        font = ImageFont.load_default()
    for i, f in enumerate(frames):
        with Image.open(f) as im:
            # JPEG draft mode decodes at 1/2, 1/4 or 1/8 scale directly:
            # 2048 -> 256 px costs a fraction of a full decode.
            im.draft("RGB", (thumb, thumb))
            im = im.convert("RGB").resize((thumb, thumb), Image.BILINEAR)
        x, y = (i % cols) * thumb, (i // cols) * thumb
        sheet.paste(im, (x, y))
        label = f.stem.rsplit("_", 1)[-1]
        draw.text((x + 3, y + 1), label, fill=(255, 255, 0), font=font,
                  stroke_width=2, stroke_fill=(0, 0, 0))
    out = sheet_path(share, city, view)
    out.parent.mkdir(exist_ok=True)
    sheet.save(out, quality=85)
    return city, view, f"{len(frames)} frames"


def build_all(share: Path, cities, thumb: int, cols: int, rebuild: bool):
    jobs = [(str(share), c, v, thumb, cols) for c in cities for v in VIEWS
            if frames_of(share, c, v) and (rebuild or sheet_is_stale(share, c, v))]
    if not jobs:
        return
    print(f"building {len(jobs)} contact sheets into {share / '.inspect'} ...", flush=True)
    done = 0
    with ProcessPoolExecutor() as ex:
        for city, view, note in ex.map(build_sheet, jobs, chunksize=2):
            done += 1
            if done % 20 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}  ({city}/{view}: {note})", flush=True)


# --- city list ----------------------------------------------------------------

def cities_from_csv(csv_path: Path):
    """CSV rows in list order -> [(folder, info dict)], using the same folder
    naming as batch_generate.py (duplicate names get the country appended)."""
    from collections import Counter
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    base = [slug(r["city"]) for r in rows]
    dup = {b for b, n in Counter(base).items() if n > 1}
    out, seen = [], Counter()
    for r, b in zip(rows, base):
        if b in dup and r.get("country"):
            b = f"{b}_{slug(r['country'])}"
        seen[b] += 1
        out.append((b if seen[b] == 1 else f"{b}_{seen[b]}", r))
    return out


def cities_from_folders(share: Path):
    return [(p.name, {}) for p in sorted(share.iterdir())
            if p.is_dir() and not p.name.startswith(".") and p.name != "debug"
            and any((p / v).is_dir() for v in VIEWS)]


def title_for(i, n, folder, info, share, flagged):
    bits = [f"[{i + 1}/{n}]  {folder}"]
    if info:
        bits[0] = f"[{i + 1}/{n}]  #{info.get('n', '?')}  {info.get('city', folder)}, {info.get('country', '')}"
        status = info.get("snap_status")
        line2 = f"target {info.get('lat')}, {info.get('lon')}  alt {info.get('poi_alt_m')} m"
        if status:
            line2 += f"   snap: {status}"
            if info.get("snap_dist_m"):
                line2 += f" ({info['snap_dist_m']} m)"
            if info.get("anchor_source") == "handpicked":
                line2 += "   hand-picked anchor"
        bits.append(line2)
        anchor = (info.get("anchor") or "").strip()
        if anchor:
            bits.append(anchor if len(anchor) <= 90 else anchor[:87] + "...")
    missing = [v for v in VIEWS if len(frames_of(share, folder, v)) < N_FRAMES]
    if missing:
        bits.append("INCOMPLETE: " + ", ".join(missing))
    if flagged:
        bits[0] += "     *** FLAGGED ***"
    return "\n".join(bits)


def load_flags(flags_path: Path) -> dict:
    """<share>/inspect_flags.csv -> {city_folder: note}."""
    flags = {}
    if flags_path.exists():
        with open(flags_path, newline="") as f:
            for r in csv.DictReader(f):
                flags[r["city_folder"]] = r.get("note", "")
    return flags


# --- viewer -------------------------------------------------------------------

VIEWER_KEYS = {"f", "s", "g", "q", "n", "p", " ", "left", "right", "backspace", "home", "end", "[", "]"}


def esp_for(share: Path, projects: Path, folder: str, view: str):
    """The .esp that produced the frames (<share>/<city>/<view>/), else the
    generated project (projects/<city>/<view>/) for a view not rendered yet."""
    for base, label in ((share, "rendered"), (projects, "project")):
        p = base / folder / view / f"{view}.esp"
        if p.is_file():
            return p, label
    return None, None


class Studio:
    """One open_in_studio.py --stdin process shared by every request, so all
    projects open as tabs of the same Chrome window."""

    def __init__(self):
        self.proc = None

    def open(self, esp: Path):
        import subprocess
        for _ in range(2):
            if self.proc is None or self.proc.poll() is not None:
                self.proc = subprocess.Popen(
                    [sys.executable, str(Path(__file__).with_name("open_in_studio.py")), "--stdin"],
                    stdin=subprocess.PIPE, text=True)
            try:
                self.proc.stdin.write(f"{esp.resolve()}\n")
                self.proc.stdin.flush()
                return
            except (BrokenPipeError, OSError):
                self.proc = None

    def close(self):
        # EOF lets the helper keep Chrome open until you close it yourself.
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass


def run_viewer(share: Path, cities, start: int, flags_path: Path, cols: int = 8, projects: Path = Path("projects")):
    import matplotlib
    import matplotlib.pyplot as plt
    from matplotlib.widgets import Button, TextBox

    # matplotlib's own shortcuts would fire too (f = fullscreen, s = save, g = grid, p = pan, ...)
    for k in [k for k in matplotlib.rcParams if k.startswith("keymap.")]:
        matplotlib.rcParams[k] = [x for x in matplotlib.rcParams[k] if x not in VIEWER_KEYS]

    flags = load_flags(flags_path)
    studio = Studio()

    def save_flags():
        with open(flags_path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["city_folder", "flagged_at", "note"])
            for c, note in flags.items():
                w.writerow([c, datetime.now().isoformat(timespec="seconds"), note])

    fig = plt.figure(figsize=(18, 10))
    fig.canvas.manager.set_window_title(f"inspect {share}")
    axes = [fig.add_axes([0.01, 0.10, 0.485, 0.78]), fig.add_axes([0.505, 0.10, 0.485, 0.78])]
    for ax in axes:
        ax.set_axis_off()
    ax_prev = fig.add_axes([0.30, 0.02, 0.10, 0.05])
    ax_next = fig.add_axes([0.60, 0.02, 0.10, 0.05])
    ax_flag = fig.add_axes([0.45, 0.02, 0.10, 0.05])
    ax_jump = fig.add_axes([0.80, 0.02, 0.15, 0.05])
    ax_sat = fig.add_axes([0.01, 0.02, 0.11, 0.05])
    ax_gt = fig.add_axes([0.13, 0.02, 0.11, 0.05])
    b_prev = Button(ax_prev, "◀ Prev")
    b_next = Button(ax_next, "Next ▶")
    b_flag = Button(ax_flag, "flag (f)")
    b_sat = Button(ax_sat, "Studio: satellite (s)")
    b_gt = Button(ax_gt, "Studio: ground truth (g)")
    t_jump = TextBox(ax_jump, "go to ", initial="")
    status = fig.text(0.01, 0.075, "", fontsize=9, color="dimgray")
    state = {"i": start}
    cache = {}

    def load(folder, view):
        key = (folder, view)
        if key not in cache:
            p = sheet_path(share, folder, view)
            cache[key] = Image.open(p).convert("RGB") if p.exists() else None
            if len(cache) > 12:  # keep a handful of neighbours only
                cache.pop(next(iter(cache)))
        return cache[key]

    def show():
        i = state["i"]
        folder, info = cities[i]
        for ax, view in zip(axes, VIEWS):
            ax.clear()
            ax.set_axis_off()
            im = load(folder, view)
            if im is None:
                ax.text(0.5, 0.5, f"{view}: no render", ha="center", va="center",
                        transform=ax.transAxes, fontsize=16, color="red")
            else:
                ax.imshow(im, interpolation="bilinear")
            ax.set_title(view, fontsize=11)
        fig.suptitle(title_for(i, len(cities), folder, info, share, folder in flags),
                     fontsize=12, y=0.995, va="top", color="red" if folder in flags else "black")
        b_flag.label.set_text("unflag (f)" if folder in flags else "flag (f)")
        fig.canvas.draw_idle()
        # pre-warm the next city's sheets so Next is instant
        if i + 1 < len(cities):
            for v in VIEWS:
                load(cities[i + 1][0], v)

    def step(d):
        state["i"] = max(0, min(len(cities) - 1, state["i"] + d))
        status.set_text("")
        show()

    def open_studio(view):
        folder = cities[state["i"]][0]
        esp, label = esp_for(share, projects, folder, view)
        if esp is None:
            status.set_text(f"no {view}.esp for {folder} in {share} or {projects}")
        else:
            studio.open(esp)
            status.set_text(f"opening {esp} ({label}) in Earth Studio; a Chrome window opens, "
                            "the first one takes ~10 s")
        fig.canvas.draw_idle()

    def toggle_flag():
        folder = cities[state["i"]][0]
        if folder in flags:
            del flags[folder]
        else:
            flags[folder] = ""
        save_flags()
        show()

    def jump(text):
        text = text.strip().lower()
        if not text:
            return
        for j, (folder, info) in enumerate(cities):
            if folder == text or text == str(info.get("n", "")) or slug(info.get("city", "")) == slug(text):
                state["i"] = j
                break
        else:
            hits = [j for j, (folder, _) in enumerate(cities) if folder.startswith(text)]
            if hits:
                state["i"] = hits[0]
        t_jump.set_val("")
        show()

    def open_frame(folder, view, idx):
        """Pop the full-resolution frame up in its own window. Each click makes
        a new window, so two frames can sit side by side; inside a window
        ← / → step through that view's frames and Escape / q close it."""
        frames = frames_of(share, folder, view)
        if not frames:
            return
        pos = {"i": max(0, min(len(frames) - 1, idx))}
        pf = plt.figure(figsize=(10, 10.4))
        pax = pf.add_axes([0.005, 0.005, 0.99, 0.955])
        pax.set_axis_off()

        def draw():
            f = frames[pos["i"]]
            pax.clear()
            pax.set_axis_off()
            with Image.open(f) as im:
                pax.imshow(im.convert("RGB"), interpolation="lanczos")
            pf.suptitle(f"{folder} / {view} / frame {f.stem.rsplit('_', 1)[-1]} of {len(frames) - 1}"
                        "     (← → step, scroll/drag to zoom, Esc closes)", fontsize=11, y=0.99, va="top")
            pf.canvas.manager.set_window_title(f"{folder} {view} {f.stem.rsplit('_', 1)[-1]}")
            pf.canvas.draw_idle()

        def pkey(ev):
            if ev.key in ("right", "n", " "):
                pos["i"] = min(len(frames) - 1, pos["i"] + 1); draw()
            elif ev.key in ("left", "p", "backspace"):
                pos["i"] = max(0, pos["i"] - 1); draw()
            elif ev.key in ("escape", "q"):
                plt.close(pf)

        pf.canvas.mpl_connect("key_press_event", pkey)
        draw()
        pf.show()

    def on_click(ev):
        # Left click on a tile -> that frame full size. Ignored while the
        # toolbar's zoom/pan tool is active so panning the sheet still works.
        if ev.button != 1 or ev.inaxes not in axes or ev.xdata is None:
            return
        tb = getattr(fig.canvas, "toolbar", None)
        if tb is not None and getattr(tb, "mode", ""):
            return
        view = VIEWS[axes.index(ev.inaxes)]
        folder = cities[state["i"]][0]
        im = load(folder, view)
        if im is None:
            return
        tile = im.width / cols
        idx = int(ev.ydata // tile) * cols + int(ev.xdata // tile)
        if 0 <= idx < len(frames_of(share, folder, view)):
            open_frame(folder, view, idx)

    def on_key(ev):
        if t_jump.capturekeystrokes:
            return
        if ev.key in ("right", "n", " "):
            step(1)
        elif ev.key in ("left", "p", "backspace"):
            step(-1)
        elif ev.key == "home":
            state["i"] = 0; show()
        elif ev.key == "end":
            state["i"] = len(cities) - 1; show()
        elif ev.key == "f":
            toggle_flag()
        elif ev.key == "s":
            open_studio("satellite")
        elif ev.key == "g":
            open_studio("ground_truth")
        elif ev.key in ("]", "["):
            step_flagged(1 if ev.key == "]" else -1)
        elif ev.key == "q":
            plt.close(fig)

    def step_flagged(d):
        """Jump to the next (d=1) / previous (d=-1) flagged city, wrapping."""
        n = len(cities)
        for k in range(1, n + 1):
            j = (state["i"] + d * k) % n
            if cities[j][0] in flags:
                state["i"] = j
                show()
                return

    b_prev.on_clicked(lambda _: step(-1))
    b_next.on_clicked(lambda _: step(1))
    b_flag.on_clicked(lambda _: toggle_flag())
    b_sat.on_clicked(lambda _: open_studio("satellite"))
    b_gt.on_clicked(lambda _: open_studio("ground_truth"))
    t_jump.on_submit(jump)
    fig.canvas.mpl_connect("key_press_event", on_key)
    fig.canvas.mpl_connect("button_press_event", on_click)
    show()
    plt.show()
    studio.close()
    if flags:
        print(f"{len(flags)} flagged: {', '.join(flags)}  -> {flags_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("share", type=Path, help="rendered share folder, e.g. cities_351_700")
    ap.add_argument("--csv", type=Path, help="the CSV the share was generated from (list order + titles)")
    ap.add_argument("--start", help="city folder name (or row number with --csv) to open first")
    ap.add_argument("--only", nargs="*", metavar="CITY",
                    help="show only these city folders (e.g. the disagreements from check_3d_coverage.py)")
    ap.add_argument("--flagged", action="store_true",
                    help="show only the cities flagged in <share>/inspect_flags.csv")
    ap.add_argument("--list-flagged", action="store_true",
                    help="print the flagged cities and exit")
    ap.add_argument("--thumb", type=int, default=192, help="thumbnail size in px (default 192)")
    ap.add_argument("--cols", type=int, default=8, help="frames per row in each grid (default 8)")
    ap.add_argument("--rebuild", action="store_true", help="regenerate all contact sheets")
    ap.add_argument("--build-only", action="store_true", help="build the sheets and exit (no window)")
    ap.add_argument("--projects", type=Path, default=Path("projects"),
                    help="generated projects folder; its .esp is opened in Earth Studio when a view has "
                         "not been rendered (default: projects)")
    args = ap.parse_args()

    if not args.share.is_dir():
        sys.exit(f"{args.share} is not a folder")
    cities = cities_from_csv(args.csv) if args.csv else cities_from_folders(args.share)
    if not cities:
        sys.exit("no cities found")
    if args.only:
        want = {c.lower() for c in args.only}
        cities = [(c, info) for c, info in cities if c in want]
        if not cities:
            sys.exit("none of the --only cities were found")
    flags_path = args.share / "inspect_flags.csv"
    if args.flagged or args.list_flagged:
        flags = load_flags(flags_path)
        cities = [(c, info) for c, info in cities if c in flags]
        if not cities:
            sys.exit(f"nothing flagged yet in {flags_path}")
        if args.list_flagged:
            for c, info in cities:
                n = f"#{info['n']:>4} " if info.get("n") else ""
                print(f"{n}{c}  {info.get('city', '')} {('(' + info['country'] + ')') if info.get('country') else ''}"
                      f"{('  ' + flags[c]) if flags[c] else ''}")
            return
    build_all(args.share, [c for c, _ in cities], args.thumb, args.cols, args.rebuild)
    if args.build_only:
        return
    start = 0
    if args.start:
        s = args.start.strip().lower()
        for j, (folder, info) in enumerate(cities):
            if folder == s or s == str(info.get("n", "")):
                start = j
                break
    run_viewer(args.share, cities, start, flags_path, args.cols, args.projects)


if __name__ == "__main__":
    main()
