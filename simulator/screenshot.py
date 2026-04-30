#!/usr/bin/env python3
"""Take screenshots of all three display modes."""
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path(__file__).parent.parent / "bugs"
OUT.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.goto("http://localhost:8000")
    page.wait_for_timeout(4000)
    w = page.locator("#wrapper")

    # Weather
    page.click("button:text('Clear / Weather')")
    page.wait_for_timeout(1000)
    w.screenshot(path=str(OUT / "weather.png"))
    print("Saved weather")

    # Cycle through test weather presets
    for i in range(3):
        page.click("button:text('Cycle Weather')")
        page.wait_for_timeout(800)
        w.screenshot(path=str(OUT / f"weather_{i+1}.png"))
        print(f"Saved weather preset {i+1}")

    # Planes
    for i in range(2):
        page.click("button:text('Inject Plane')")
        page.wait_for_timeout(1000)
        w.screenshot(path=str(OUT / f"plane_{i+1}.png"))
        print(f"Saved plane {i+1}")

    # Ships
    for i in range(3):
        page.click("button:text('Inject Ship')")
        page.wait_for_timeout(1000)
        w.screenshot(path=str(OUT / f"ship_{i+1}.png"))
        print(f"Saved ship {i+1}")

    browser.close()
    print("Done!")
