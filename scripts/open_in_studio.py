#!/usr/bin/env python3
"""
open_in_studio.py — Open .esp projects in Google Earth Studio, one tab each,
for looking at a city by hand: scrub the timeline, move the camera, check
what the orbit target sits on.

    python3 scripts/open_in_studio.py cities_251_500/muscat/ground_truth/ground_truth.esp
    python3 scripts/open_in_studio.py projects/oran/*/*.esp            # both views of a city
    python3 scripts/open_in_studio.py --stdin                           # read .esp paths, one per line

inspect_renders.py runs it with --stdin and sends it a path whenever you
press s / g (or click the Studio buttons), so all projects open as tabs in
the same window.

Chrome runs with its own profile, .ges-chrome-profile-inspect/, copied from
.ges-chrome-profile/ on first use so it keeps the Google sign-in, and so it
never collides with a render_all.py run using the main profile. Nothing is
rendered; the projects are imported exactly as a drag-and-drop would, and any
edits you make stay in that tab. The script exits when you close the window.
"""
import argparse
import sys
import time
from pathlib import Path

import render_all
from render_all import (ALWAYS_VISIBLE_JS, DROP_ESP_JS, STUDIO_URL, clone_profile, dismiss_recovery_prompt,
                        log, wait_for_start_screen)
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent  # profiles live in the repo root, wherever this is run from


def open_project(context, esp: Path, login_timeout_s: float):
    page = context.pages[0] if len(context.pages) == 1 and context.pages[0].url == "about:blank" \
        else context.new_page()
    page.on("dialog", lambda d: d.accept())
    page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=120_000)
    if not wait_for_start_screen(page, timeout_s=login_timeout_s):
        log(f"  {esp}: Earth Studio start screen never appeared (not signed in?)")
        return
    dismiss_recovery_prompt(page)
    name = esp.stem
    page.evaluate(DROP_ESP_JS, [esp.read_text(), esp.name])
    t0 = time.time()
    while name not in page.title():
        if time.time() - t0 > 60:
            log(f"  {esp}: project did not import (title unchanged)")
            return
        time.sleep(1)
    dismiss_recovery_prompt(page)
    log(f"  opened {esp}")


def browser_open(context):
    try:
        return any(not p.is_closed() for p in context.pages)
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("esp", nargs="*", type=Path, help=".esp files to open")
    ap.add_argument("--stdin", action="store_true", help="also read .esp paths from stdin, one per line")
    ap.add_argument("--profile", type=Path, default=ROOT / ".ges-chrome-profile-inspect",
                    help="Chrome profile to use (copied from --base-profile on first use)")
    ap.add_argument("--base-profile", type=Path, default=ROOT / ".ges-chrome-profile",
                    help="signed-in profile to copy the sign-in from")
    ap.add_argument("--login-timeout", type=float, default=15, help="minutes to wait for sign-in / start screen")
    args = ap.parse_args()
    if not args.esp and not args.stdin:
        ap.error("give .esp files or --stdin")
    for e in args.esp:
        if not e.is_file():
            sys.exit(f"{e}: no such file")

    clone_profile(args.base_profile, args.profile)
    render_all.DEBUG_DIR = args.profile / "debug"  # start-screen screenshots, not the repo root
    with sync_playwright() as pw:
        state = {"context": None}

        def context():
            if state["context"] is None or not browser_open(state["context"]):
                state["context"] = pw.chromium.launch_persistent_context(
                    user_data_dir=str(args.profile.resolve()), channel="chrome", headless=False,
                    viewport=None, args=["--start-maximized", "--disable-blink-features=AutomationControlled",
                          "--hide-crash-restore-bubble"],
                    ignore_default_args=["--enable-automation"])
                state["context"].add_init_script(ALWAYS_VISIBLE_JS)
            return state["context"]

        def handle(esp: Path):
            try:
                open_project(context(), esp, args.login_timeout * 60)
            except Exception as e:  # a closed window mid-import must not end the session
                log(f"  {esp}: {str(e).splitlines()[0]}")

        for e in args.esp:
            handle(e)
        if args.stdin:
            for line in sys.stdin:
                p = Path(line.strip())
                if p.is_file():
                    handle(p)
                elif line.strip():
                    log(f"  {p}: no such file")
        # keep Chrome up until the user closes it
        while state["context"] is not None and browser_open(state["context"]):
            time.sleep(1)


if __name__ == "__main__":
    main()
