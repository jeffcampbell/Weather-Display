#!/usr/bin/env python3
"""Take screenshots comparing aircraft icon styles."""
from playwright.sync_api import sync_playwright

OUT_DIR = "bugs"

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 900, "height": 700})

    for style in ["A", "B", "C", "D"]:
        page.goto("http://localhost:8000")
        page.wait_for_timeout(3000)
        # Reassign the mutable reference via window
        page.evaluate(f"""
            window._overrideJet = ICON_JET_{style};
            // Patch getAircraftIcon to use override
            window._origGetAircraftIcon = getAircraftIcon;
            getAircraftIcon = function(tc, cat) {{
                const r = window._origGetAircraftIcon(tc, cat);
                return (r === ICON_JET_A || r === ICON_JET_B || r === ICON_JET_C || r === ICON_JET_D)
                    ? window._overrideJet : r;
            }};
        """)
        page.click("button:text('Clear Planes')")
        page.wait_for_timeout(200)
        page.click("button:text('Inject Test Plane')")
        page.wait_for_timeout(1500)
        page.locator("#wrapper").screenshot(path=f"{OUT_DIR}/style_{style}.png")
        print(f"Saved style {style}")

    browser.close()
    print("Done!")
