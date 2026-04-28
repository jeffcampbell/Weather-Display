#!/usr/bin/env python3
"""Take screenshots of the simulator in different modes."""
import sys
from playwright.sync_api import sync_playwright

URL = "http://localhost:8000"
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "bugs"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 900, "height": 700})
    page.goto(URL)
    page.wait_for_timeout(4000)

    wrapper = page.locator("#wrapper")

    # 1. Weather screen
    page.click("button:text('Clear Planes')")
    page.wait_for_timeout(500)
    wrapper.screenshot(path=f"{OUT_DIR}/check_weather.png")
    print("Saved weather")

    # 2. Test planes (have cached route data)
    for i in range(3):
        page.click("button:text('Clear Planes')")
        page.wait_for_timeout(200)
        page.click("button:text('Inject Test Plane')")
        page.wait_for_timeout(1500)
        wrapper.screenshot(path=f"{OUT_DIR}/check_plane_{i+1}.png")
        print(f"Saved test plane {i+1}")

    # 3. Wait for real planes (no cached route) to appear
    page.click("button:text('Clear Planes')")
    page.click("button:text('Force Refresh')")
    page.wait_for_timeout(8000)  # wait for OpenSky fetch
    wrapper.screenshot(path=f"{OUT_DIR}/check_real.png")
    print("Saved real plane (if overhead)")

    browser.close()
    print("Done!")
