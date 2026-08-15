# Matrix Portal — launcher
# ---------------------------------------------------------------------------
# Selects the display layout based on the `display` panel size in secrets.py
# and hands off to the matching layout program. Each layout module is a full,
# self-contained program (fetch loop + rendering) authored for one panel size:
#
#   layout_64x32.py   native 64x32 layout   (default)
#   layout_128x64.py  native 128x64 layout  (scale=2 + wide astronomy)
#
# Feature flags (enable_weather / enable_tide / enable_astronomy /
# enable_planes / enable_boats) live inside both layouts and behave the same
# either way — see either module's configuration block.
#
# Importing a layout module runs it (its main loop never returns), so this
# file does nothing else after the import.
# ---------------------------------------------------------------------------
try:
    from secrets import secrets
except ImportError:
    secrets = {}

_display = str(secrets.get("display", "64x32")).lower()
print("Launcher: display =", _display)

if _display == "128x64":
    import layout_128x64  # noqa: F401 — runs the 128x64 program
else:
    import layout_64x32  # noqa: F401 — runs the 64x32 program (default)
