#!/usr/bin/env python3
"""Take screenshots of the simulator for visual testing."""
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

URL = "http://localhost:8000"
OUT_DIR = Path(__file__).parent.parent / "bugs"

OUT_DIR.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.goto(URL)
    page.wait_for_timeout(4000)
    wrapper = page.locator("#wrapper")

    # Weather screen
    page.click("button:text('Clear Planes')")
    page.wait_for_timeout(500)
    wrapper.screenshot(path=str(OUT_DIR / "weather.png"))
    print("Saved weather")

    # Test planes
    for i in range(4):
        page.click("button:text('Clear Planes')")
        page.wait_for_timeout(200)
        page.click("button:text('Inject Test Plane')")
        page.wait_for_timeout(1500)
        wrapper.screenshot(path=str(OUT_DIR / f"plane_{i+1}.png"))
        log_text = page.locator("#log div:last-child").text_content()
        print(f"Saved plane {i+1}: {log_text}")

    browser.close()
    print("Done!")
