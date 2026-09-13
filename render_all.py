#!/usr/bin/env python3
"""
render_all.py — Drive Google Earth Studio in Chrome to render every project
under projects/, unzipping each result so that every <out>/<city>/<view>/
ends up with footage/, <view>.json, ImagerySources.txt and the .esp, exactly
like reference/Cities/Koblenz/.

    pip install playwright            # one-time
    python3 render_all.py projects --out cities_10_50     # renders everything not yet done

Output layout (same as reference/Cities/Koblenz and reference/city_satellite/cities/<city>):

    <out>/<city_folder>/satellite/footage/satellite_00.jpeg ... satellite_60.jpeg
    <out>/<city_folder>/satellite/satellite.esp, satellite.json, ImagerySources.txt
    <out>/<city_folder>/ground_truth/...
    <out>/metadata.csv                # copied from projects/metadata.csv
    (<out> is whatever you pass to --out, one folder per share of the city list)

First run: a Chrome window opens on Earth Studio. Sign in to Google there
(the script waits until the Earth Studio start screen appears) and leave the
window open — the profile is kept in .ges-chrome-profile/ so later runs are
already signed in.

If Google refuses to sign in inside the script-launched Chrome ("This browser
may not be secure"), start Chrome yourself and attach instead:

    google-chrome --remote-debugging-port=9222 --user-data-dir=$HOME/.ges-profile &
    # sign in to earth.google.com/studio in that window, then:
    python3 render_all.py projects --cdp http://localhost:9222

What it does per project: open Earth Studio, drop the .esp onto the page
(same as drag-and-drop import), click Render (the dialog is pre-filled from
the .esp: 2048x2048, frames 0-60, JPEG, JSON 3D tracking), click Start, wait
for the zip, save + unzip it, verify 61 frames + json. Progress is polled
every few seconds; if it stops moving for --stall-minutes the view is marked
stalled and retried on a later pass (see below); other errors are retried up
to --retries times right away. Already-complete projects are skipped, so you
can stop and restart at any time.

Stalls: sometimes Earth Studio stops advancing the frame counter at one
specific frame of one project ("00:00 remaining", nothing in flight, no
error) and never recovers. Seen on 2026-09-13 for several Polish cities while
German ones kept rendering: it follows the camera position, not the frame
count or the browser, so it looks like Google's 3D tile data being updated
for that area. The script gives up after --stall-minutes, moves on, and
sweeps the stalled views again in later passes (--passes, --pass-wait-minutes).
After a stall reload Earth Studio shows an "Uh-oh! Something went wrong. Do
you want to recover ...?" modal that blocks all clicks; it is dismissed
automatically.

Keep the Chrome window on screen. Earth Studio pauses local renders when it
believes the tab is hidden; the script tells the page it is always visible,
but a minimised window can still be throttled by the OS.

Output folder picker: newer Earth Studio asks for a "Destination" folder
through the browser's native folder picker (File System Access API), which
cannot be driven from a script. The script hides that API from the page, so
Earth Studio falls back to packaging the render as a zip. The zip is then
captured in-page (Chrome crashed on the actual download here) and copied out
through the automation channel, then unpacked into <out>/<city>/<view>/.
"""

import argparse
import base64
import csv
import json
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit("playwright is not installed:  pip install playwright")

STUDIO_URL = "https://earth.google.com/studio/"
VIEWS = ("satellite", "ground_truth")
N_FRAMES = 61  # duration 60 frames -> images 00..60

# Earth Studio pauses when document.hidden; pin visibility so a window that
# is merely behind another one keeps rendering.
ALWAYS_VISIBLE_JS = """
Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
Object.defineProperty(document, 'hidden', {get: () => false});
document.hasFocus = () => true;
document.addEventListener('visibilitychange', e => e.stopImmediatePropagation(), true);
window.addEventListener('blur', e => e.stopImmediatePropagation(), true);
"""

# Hide the File System Access API so the render dialog has no "Destination"
# folder button (which needs a native picker) and Earth Studio instead
# downloads the finished render as a zip.
# Earth Studio's check is `"showOpenFilePicker" in window` (lib_compiled.js,
# FileSystemAccess.isSupported), so the properties must be *deleted* — merely
# setting them to undefined leaves the `in` test true.
NO_FOLDER_PICKER_JS = """
for (const k of ['chooseFileSystemEntries', 'showDirectoryPicker', 'showOpenFilePicker', 'showSaveFilePicker']) {
  try { delete window[k]; } catch (e) {}
}
"""
PICKER_PRESENT_JS = "['chooseFileSystemEntries','showOpenFilePicker','showDirectoryPicker'].filter(k => k in window).join(',')"

# Same synthetic drag-and-drop that was verified interactively.
DROP_ESP_JS = """
([text, name]) => {
  const file = new File([text], name, {type: 'application/json'});
  const dt = new DataTransfer(); dt.items.add(file);
  for (const t of ['dragenter', 'dragover', 'drop']) {
    document.body.dispatchEvent(new DragEvent(t, {bubbles: true, cancelable: true, dataTransfer: dt}));
  }
  return true;
}
"""

# Earth Studio zips the frames in-page and hands the Blob to
# SafeDownloader.download(blob, name) (google3 safe_downloader, exported as
# window.SafeDownloader), which pushes it through Chrome's download
# subsystem. On this machine Chrome dies with SIGTRAP while doing that, so we
# swap the function for one that keeps a copy of the Blob in memory and pull
# the bytes out over the automation channel instead.
CAPTURE_ZIP_JS = """
() => {
  if (!window.SafeDownloader) return false;
  window.__renderZip = null;
  window.SafeDownloader.download = async (blob, name) => {
    // The Blob may be backed by a sandboxed FileSystem entry that Earth
    // Studio deletes ~6 s later; copy it into memory first.
    const buf = await blob.arrayBuffer();
    window.__renderZip = {blob: new Blob([buf]), name: name, size: buf.byteLength};
  };
  return true;
}
"""
READ_ZIP_CHUNK_JS = """
async ([off, len]) => {
  const buf = await window.__renderZip.blob.slice(off, off + len).arrayBuffer();
  const u = new Uint8Array(buf); let s = '';
  for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
  return btoa(s);
}
"""
ZIP_CHUNK = 16 * 1024 * 1024

PROGRESS_RE = re.compile(r"(Rendered|Loading 3D nodes):\s*(\d+)\s*/\s*(\d+)")


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# --- project bookkeeping ---------------------------------------------------

def is_done(view_dir: Path, view: str) -> bool:
    footage = view_dir / "footage"
    if not (view_dir / f"{view}.json").exists() or not footage.is_dir():
        return False
    return len(list(footage.glob(f"{view}_*.jpeg"))) >= N_FRAMES


def list_jobs(root: Path, out: Path, only=None):
    """Yield (city, view, esp_path, output_dir) in render_order.txt order."""
    order = root / "render_order.txt"
    if order.exists():
        rel = [line.split("\t")[-1].strip() for line in order.read_text().splitlines() if line.strip()]
        esps = [root / r for r in rel]
    else:
        esps = sorted(p for v in VIEWS for p in root.glob(f"*/{v}/{v}.esp"))
    jobs = []
    for esp in esps:
        view = esp.stem
        city = esp.parent.parent.name
        if only and city not in only:
            continue
        jobs.append((city, view, esp, out / city / view))
    return jobs


def unzip_into(zip_path: Path, view_dir: Path, view: str, esp: Path):
    """Earth Studio zips contain footage/, <view>.esp, <view>.json,
    ImagerySources.txt at the top level (verified on the Koblenz renders)."""
    view_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if not any(n.startswith("footage/") for n in names):
            raise RuntimeError(f"unexpected zip layout: {names[:5]}")
        z.extractall(view_dir)
    if not (view_dir / f"{view}.esp").exists():
        shutil.copy2(esp, view_dir / f"{view}.esp")
    frames = sorted((view_dir / "footage").glob(f"{view}_*.jpeg"))
    n = len(frames)
    if n < N_FRAMES or not (view_dir / f"{view}.json").exists():
        raise RuntimeError(f"unzipped render incomplete: {n} frames, json={(view_dir / f'{view}.json').exists()}")
    try:
        with open(view_dir / f"{view}.json") as f:
            meta = json.load(f)
        if len(meta.get("cameraFrames", [])) < N_FRAMES:
            raise RuntimeError(f"tracking json has {len(meta.get('cameraFrames', []))} camera frames")
    except json.JSONDecodeError as e:
        raise RuntimeError(f"tracking json unreadable: {e}")
    return n


# --- browser driving -------------------------------------------------------

def body_text(page) -> str:
    try:
        return page.evaluate("document.body ? document.body.innerText : ''")
    except Exception:
        return ""


def wait_for_start_screen(page, timeout_s):
    """Start screen = 'Blank Project' button present. Also handles the
    first-run Google sign-in by simply waiting for the user."""
    t0 = time.time()
    warned = False
    while time.time() - t0 < timeout_s:
        try:
            if page.get_by_role("button", name="Blank Project").count():
                return True
        except Exception:
            pass
        if not warned and ("accounts.google.com" in page.url or "Sign in" in body_text(page)):
            log("Earth Studio wants a Google sign-in. Sign in inside the Chrome window; waiting...")
            warned = True
        if int(time.time() - t0) % 30 == 0:
            log(f"  waiting for start screen: url={page.url[:80]} title={page.title()!r}")
            snap(page, "start_screen")
        time.sleep(2)
    return False


DEBUG_DIR = Path(".")  # set to <out>/debug by main()


def snap(page, tag):
    """Debug screenshot -> <out>/debug/render_debug_<tag>.png (overwritten each time)."""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(DEBUG_DIR / f"render_debug_{tag}.png"))
    except Exception:
        pass


def click_button(page, name, timeout_ms=60_000):
    btn = page.get_by_role("button", name=name, exact=True)
    btn.first.wait_for(state="visible", timeout=timeout_ms)
    try:
        btn.first.click(timeout=30_000)
    except PWTimeout:
        # Usually something is sitting over the button (a modal); keep the
        # screenshot so the blocker is visible, and name the button in the log.
        snap(page, f"click_{name.lower().replace(' ', '_')}_blocked")
        raise RuntimeError(f"could not click '{name}' (blocked by an overlay? see debug screenshot)")


def dismiss_recovery_prompt(page) -> bool:
    """After a reload mid-render Earth Studio shows a modal: "Uh-oh! Something
    went wrong. Do you want to recover what you were working on?" with
    Dismiss / Yes buttons. It sits over the whole page and swallows every
    click, so Render can never be pressed. We import our own project each
    time, so Dismiss is always the right answer."""
    try:
        if "Uh-oh!" not in body_text(page) and "recover what you were working on" not in body_text(page):
            return False
        btn = page.get_by_role("button", name="Dismiss", exact=True)
        if not btn.count():
            btn = page.get_by_text("Dismiss", exact=True)
        btn.first.click(timeout=5_000)
        log("  dismissed Earth Studio's 'Something went wrong' recovery prompt")
        time.sleep(1)
        return True
    except Exception as e:
        log(f"  could not dismiss the recovery prompt: {str(e).splitlines()[0]}")
        snap(page, "recovery_prompt")
        return False


def save_zip_from_page(page, zip_path: Path) -> int:
    """Copy the captured zip Blob out of the page in chunks; returns bytes."""
    size = page.evaluate("window.__renderZip.size")
    with open(zip_path, "wb") as f:
        for off in range(0, size, ZIP_CHUNK):
            f.write(base64.b64decode(page.evaluate(READ_ZIP_CHUNK_JS, [off, ZIP_CHUNK])))
    page.evaluate("window.__renderZip = null")
    got = zip_path.stat().st_size
    if got != size:
        raise RuntimeError(f"zip copy incomplete: {got} of {size} bytes")
    return got


def render_one(page, downloads, esp: Path, view: str, args, zip_path: Path) -> int:
    """Import, render, and write the finished zip to zip_path; returns bytes."""
    downloads.clear()
    page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=120_000)
    if not wait_for_start_screen(page, timeout_s=args.login_timeout * 60):
        raise RuntimeError("Earth Studio start screen never appeared (not signed in?)")
    # Belt and braces: make sure the folder-picker API really is gone before
    # Earth Studio builds its render dialog, otherwise Start needs a folder.
    picker = page.evaluate(PICKER_PRESENT_JS)
    if picker:
        log(f"  folder picker API still present ({picker}); hiding it")
        page.evaluate(NO_FOLDER_PICKER_JS)
        if page.evaluate(PICKER_PRESENT_JS):
            raise RuntimeError("could not hide the File System Access API")
    if not page.evaluate(CAPTURE_ZIP_JS):
        raise RuntimeError("window.SafeDownloader not found; cannot capture the zip")

    dismiss_recovery_prompt(page)
    page.evaluate(DROP_ESP_JS, [esp.read_text(), esp.name])
    # Title becomes "<project name> - Google Earth Studio" once imported.
    t0 = time.time()
    while view not in page.title():
        if time.time() - t0 > 60:
            raise RuntimeError("project did not import (title unchanged)")
        time.sleep(1)

    # Let the globe finish loading before opening the render dialog.
    t0 = time.time()
    while "Loading Earth" in body_text(page) and time.time() - t0 < 120:
        time.sleep(2)

    dismiss_recovery_prompt(page)  # can also pop up a moment after the import
    click_button(page, "Render")
    start = page.get_by_role("button", name="Start", exact=True).first
    start.wait_for(state="visible", timeout=60_000)
    if "Choose an output folder" in body_text(page) or \
            page.locator(".setup-folder-output button:visible", has_text="folder").count():
        snap(page, "destination_required")
        raise RuntimeError("render dialog asks for a Destination folder (picker API not hidden)")
    for _ in range(4):
        # First-run "Choose folder" tip sits over the dialog; dismiss it.
        got_it = page.get_by_role("button", name="Got it")
        if got_it.count():
            got_it.first.click(timeout=2_000)
            time.sleep(1)
        start.click(timeout=5_000)
        time.sleep(3)
        if not start.is_visible():
            break
    else:
        snap(page, "start_not_accepted")
        raise RuntimeError("render dialog did not start (Start button still visible)")
    log(f"  rendering {view} ...")
    time.sleep(8)
    snap(page, "after_start")

    last_progress, last_change = None, time.time()
    started = time.time()
    while True:
        if page.evaluate("!!(window.__renderZip && window.__renderZip.blob)"):
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            n = save_zip_from_page(page, zip_path)
            log(f"  captured zip: {n / 1e6:.1f} MB")
            return n
        if downloads:  # only if the SafeDownloader patch somehow did not take
            dl = downloads[0]
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            dl.save_as(str(zip_path))
            if dl.failure():
                raise RuntimeError(f"download failed: {dl.failure()}")
            return zip_path.stat().st_size
        text = body_text(page)
        m = PROGRESS_RE.search(text)
        progress = m.group(0) if m else None

        if "Continue Rendering" in text:
            try:
                page.get_by_role("button", name="Continue Rendering").first.click(timeout=2_000)
                log("  resumed a paused render")
            except Exception:
                pass

        if progress != last_progress:
            last_progress, last_change = progress, time.time()
            if progress and progress.startswith("Rendered"):
                log(f"  {progress}")
        elif time.time() - last_change > args.stall_minutes * 60:
            snap(page, "stalled")
            raise RuntimeError(f"no progress for {args.stall_minutes} min (last: {progress})")

        if time.time() - started > args.max_minutes * 60:
            raise RuntimeError(f"render exceeded {args.max_minutes} min")
        time.sleep(4)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("projects", type=Path, help="folder produced by batch_generate.py")
    ap.add_argument("--out", type=Path, required=True,
                    help="where rendered <city>/<view>/ folders go, e.g. cities_10_50")
    ap.add_argument("--only", nargs="*", help="city folder names to render (default: all)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N renders (0 = no limit)")
    ap.add_argument("--profile", type=Path, default=Path(".ges-chrome-profile"),
                    help="Chrome profile dir the script launches (keeps your sign-in)")
    ap.add_argument("--cdp", help="attach to a Chrome you started with --remote-debugging-port instead")
    ap.add_argument("--stall-minutes", type=float, default=1.5,
                    help="give up on a render if the frame counter freezes this long (zip packaging "
                         "at the end takes ~30-40 s, so keep this above 1)")
    ap.add_argument("--passes", type=int, default=4,
                    help="how many times to sweep the list; views that stalled are retried on the next "
                         "pass instead of immediately (stalls are position-specific and usually clear "
                         "up on their own after a while)")
    ap.add_argument("--pass-wait-minutes", type=float, default=15, help="pause between passes when only stalled views remain")
    ap.add_argument("--max-minutes", type=float, default=40, help="give up on one render after this long")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--max-crashes", type=int, default=50,
                    help="stop if Chrome has to be relaunched more than this many times")
    ap.add_argument("--login-timeout", type=float, default=15, help="minutes to wait for sign-in / start screen")
    args = ap.parse_args()

    root = args.projects
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    global DEBUG_DIR
    DEBUG_DIR = out / "debug"
    if (root / "metadata.csv").exists():
        shutil.copy2(root / "metadata.csv", out / "metadata.csv")
    jobs = list_jobs(root, out, set(args.only) if args.only else None)
    todo = [j for j in jobs if not is_done(j[3], j[1])]
    log(f"{len(jobs)} projects listed, {len(jobs) - len(todo)} already rendered, {len(todo)} to do")
    if not todo:
        return
    if args.limit:
        todo = todo[: args.limit]

    logf = out / "render_log.csv"
    new_log = not logf.exists()
    logfh = open(logf, "a", newline="")
    logw = csv.writer(logfh)
    if new_log:
        logw.writerow(["finished_at", "city", "view", "status", "seconds", "attempts", "note"])

    chrome_args = [
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-blink-features=AutomationControlled",
        "--start-maximized",
    ]
    with sync_playwright() as pw:
        state = {"context": None, "page": None, "downloads": []}

        def open_browser():
            """(Re)launch Chrome and return a fresh page. Called at start and
            after Chrome crashes (it dies with SIGTRAP now and then while
            packaging the zip)."""
            old = state["context"]
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            if args.cdp:
                browser = pw.chromium.connect_over_cdp(args.cdp)
                context = browser.contexts[0] if browser.contexts else browser.new_context(accept_downloads=True)
            else:
                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(args.profile.resolve()),
                    channel="chrome", headless=False, accept_downloads=True, viewport=None,
                    args=chrome_args, ignore_default_args=["--enable-automation"],
                )
            context.add_init_script(ALWAYS_VISIBLE_JS)
            context.add_init_script(NO_FOLDER_PICKER_JS)
            state["context"] = context
            return new_page()

        def new_page():
            context = state["context"]
            for p in list(context.pages):
                if p.is_closed():
                    continue
                if p.url == "about:blank" or p.url.startswith("chrome://"):  # blank tab: reuse it
                    pg = p
                    break
            else:
                pg = context.new_page()
            pg.on("dialog", lambda d: d.accept())  # e.g. "leave page?" on reload
            pg.on("download", lambda d: state["downloads"].append(d))
            state["page"] = pg
            return pg

        def browser_alive():
            ctx = state["context"]
            if ctx is None:
                return False
            try:
                return any(not p.is_closed() for p in ctx.pages) or ctx.new_page() is not None
            except Exception:
                return False

        open_browser()
        crashes = 0

        durations = []
        stalled = []
        for pass_no in range(1, args.passes + 1):
          if pass_no > 1:
            todo = stalled
            stalled = []
            if not todo:
                break
            log(f"pass {pass_no}: retrying {len(todo)} stalled view(s) after {args.pass_wait_minutes:g} min")
            time.sleep(args.pass_wait_minutes * 60)
          for i, (city, view, esp, view_dir) in enumerate(todo, 1):
            eta = ""
            if durations:
                avg = sum(durations) / len(durations)
                eta = f", ETA {timedelta(seconds=int(avg * (len(todo) - i + 1)))}"
            log(f"[{i}/{len(todo)}] {city}/{view}{eta}")
            t0 = time.time()
            status, note = "failed", ""
            for attempt in range(1, args.retries + 1):
                try:
                    if not browser_alive():
                        crashes += 1
                        if crashes > args.max_crashes:
                            raise SystemExit(f"Chrome died {crashes} times; giving up")
                        log(f"  Chrome is gone (crash #{crashes}); relaunching")
                        open_browser()
                    elif state["page"].is_closed():
                        new_page()
                    page = state["page"]
                    zip_path = view_dir / f"{view}.zip"
                    render_one(page, state["downloads"], esp, view, args, zip_path)
                    n = unzip_into(zip_path, view_dir, view, esp)
                    zip_path.unlink()
                    status, note = "ok", f"{n} frames"
                    break
                except SystemExit:
                    raise
                except Exception as e:  # RuntimeError, playwright Error/TimeoutError, zipfile errors
                    note = str(e).splitlines()[0]
                    log(f"  attempt {attempt} failed: {note}")
                    shutil.rmtree(view_dir / "footage", ignore_errors=True)
                    for leftover in view_dir.glob("*.zip") if view_dir.exists() else []:
                        leftover.unlink()
                    if "no progress" in note:
                        # A frozen frame counter is Earth Studio failing to finish loading the
                        # scene at one camera position (seen 2026-09-13: same frame every time,
                        # any browser, while other cities render fine). Retrying right away just
                        # burns time; leave it for the next pass.
                        status = "stalled"
                        break
                    time.sleep(5)
            secs = time.time() - t0
            if status == "ok":
                durations.append(secs)
            elif status == "stalled":
                stalled.append((city, view, esp, view_dir))
            log(f"  {status} in {timedelta(seconds=int(secs))} ({note})")
            logw.writerow([datetime.now().isoformat(timespec="seconds"), city, view, status, int(secs), attempt, note])
            logfh.flush()
          if stalled:
            log(f"pass {pass_no} done: {len(stalled)} view(s) stalled: " + ", ".join(f"{c}/{v}" for c, v, _, _ in stalled))
        if stalled:
            log(f"still stalled after {args.passes} passes: " + ", ".join(f"{c}/{v}" for c, v, _, _ in stalled))
            log("rerun later (python3 render_all.py projects --out <out>) to try them again")

        try:
            state["context"].close()
        except Exception:
            pass
    log("done")


if __name__ == "__main__":
    main()
