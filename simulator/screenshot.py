#!/usr/bin/env python3
"""
Playwright screenshot tester for the Matrix Portal Simulator.

Starts the dev server, injects every test fixture (including worst-case),
and screenshots the display canvas after each one.  With --update-refs the
current run becomes the new reference baseline.  Without it, each screenshot
is pixel-diffed against its reference and failures are reported.

Setup (first time):
    pip install playwright Pillow
    playwright install chromium

Usage:
    python3 screenshot.py                  # diff against references
    python3 screenshot.py --update-refs    # accept current output as baseline
    python3 screenshot.py --port 8888      # use a specific server port
    python3 screenshot.py --no-server      # assume server is already running
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
SHOTS_DIR  = SCRIPT_DIR / "screenshots"
REFS_DIR   = SHOTS_DIR / "reference"

# (js_function, fixture_count, filename_prefix, cycle_index_var)
FIXTURE_SEQUENCES = [
    ("injectTestWeather",  6, "weather",       "_twIdx"),
    ("injectTestPlane",    5, "plane",          "_tpIdx"),
    ("injectTestShip",     5, "ship",           "_tsIdx"),
    ("injectWorstWeather", 4, "worst_weather",  "_wwIdx"),
    ("injectWorstPlane",   4, "worst_plane",    "_wpIdx"),
    ("injectWorstShip",    4, "worst_ship",     "_wsIdx"),
]


def take_screenshots(page, with_guides=False):
    """Cycle through every fixture, screenshot after each one.
    Returns {name: Path} for every screenshot saved."""
    shots = {}
    suffix = "_guides" if with_guides else ""

    # Freeze live clock so weather screenshots are deterministic
    page.evaluate("testClockStr = '10:30 AM'")

    if with_guides:
        page.evaluate("if (!overlayEnabled) toggleOverlay()")
    else:
        page.evaluate("if (overlayEnabled) toggleOverlay()")

    for func, count, prefix, idx_var in FIXTURE_SEQUENCES:
        page.evaluate("clearAll()")               # clean slate before each sequence
        page.evaluate(f"{idx_var} = 0")           # reset cycle to fixture 0
        for i in range(count):
            # Freeze animation state so every shot is deterministic regardless
            # of how long the previous fixture took to render.
            page.evaluate("_basin_tick = 0; _sep_pixel_y = 16")
            page.evaluate(f"{func}()")
            page.wait_for_timeout(80)             # settle one render at tick=0
            page.evaluate("_basin_tick = 0; _sep_pixel_y = 16")  # hold frozen
            page.wait_for_timeout(40)
            name = f"{prefix}_{i + 1}{suffix}"
            path = SHOTS_DIR / f"{name}.png"
            page.locator("#wrapper").screenshot(path=str(path))
            shots[name] = path

    # Restore live clock
    page.evaluate("testClockStr = null")
    return shots


def diff_shots(shots):
    """Pixel-diff each screenshot against its reference.  Returns failure count."""
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        print("  (skip diff — install Pillow + numpy for pixel comparison)")
        return 0

    failures = 0
    for name, path in sorted(shots.items()):
        ref = REFS_DIR / path.name
        if not ref.exists():
            print(f"  [NEW]  {name}")
            continue
        cur = np.array(Image.open(path).convert("RGB"))
        base = np.array(Image.open(ref).convert("RGB"))
        if cur.shape != base.shape:
            print(f"  [FAIL] {name}  — size changed {base.shape} → {cur.shape}")
            failures += 1
            continue
        diff = np.abs(cur.astype(int) - base.astype(int))
        changed = int((diff.sum(axis=2) > 8).sum())   # >8 to ignore JPEG noise
        if changed:
            print(f"  [FAIL] {name}  — {changed} pixels differ")
            failures += 1
        else:
            print(f"  [ok]   {name}")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port",        type=int,  default=8889)
    parser.add_argument("--update-refs", action="store_true",
                        help="Save current screenshots as new reference baseline")
    parser.add_argument("--no-server",   action="store_true",
                        help="Skip starting the server (assumes it is already running)")
    args = parser.parse_args()

    SHOTS_DIR.mkdir(exist_ok=True)
    REFS_DIR.mkdir(exist_ok=True)

    server = None
    if not args.no_server:
        print(f"Starting simulator on port {args.port}…")
        server = subprocess.Popen(
            [sys.executable, str(SCRIPT_DIR / "server.py"), str(args.port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1.5)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        if server:
            server.terminate()
        sys.exit(
            "playwright is not installed.\n"
            "Run:  pip install playwright Pillow && playwright install chromium"
        )

    shots = {}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1000, "height": 750})
            page.goto(f"http://localhost:{args.port}")
            page.wait_for_selector("#matrix")
            page.wait_for_timeout(400)

            print("Screenshotting fixtures (guides off)…")
            shots.update(take_screenshots(page, with_guides=False))

            print("Screenshotting fixtures (guides on)…")
            shots.update(take_screenshots(page, with_guides=True))

            browser.close()
    finally:
        if server:
            server.terminate()

    total = len(shots)
    print(f"\n{total} screenshots → {SHOTS_DIR}/")

    if args.update_refs:
        import shutil
        for path in shots.values():
            shutil.copy(path, REFS_DIR / path.name)
        print(f"Reference baseline updated ({total} images) → {REFS_DIR}/")
        sys.exit(0)

    print("Diffing against references…")
    failures = diff_shots(shots)
    if failures:
        print(f"\n{failures} failure(s).  Run with --update-refs to accept as new baseline.")
        sys.exit(1)
    print("All OK.")


if __name__ == "__main__":
    main()
