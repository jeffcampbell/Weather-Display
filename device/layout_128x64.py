# Matrix Portal — Tides, Weather, Aircraft, and Ship Tracker
# Hardware: Adafruit MatrixPortal S3 + 128x64 RGB LED Matrix
# (M4 also works but S3 is recommended for the 128x64 panel — more RAM/PSRAM.)
# See device/SETUP.md for the full library list and setup walkthrough.

import random
import time
import gc
import json
import math
import board
import microcontroller
import digitalio
import terminalio
import displayio
from adafruit_matrixportal.matrixportal import MatrixPortal
from adafruit_display_text.label import Label
from adafruit_bitmap_font import bitmap_font
try:
    from watchdog import WatchDogMode
    _WATCHDOG_OK = True
except ImportError:
    _WATCHDOG_OK = False

try:
    from secrets import secrets
except ImportError:
    raise RuntimeError("Missing secrets.py -- see template")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NOAA_STATION = secrets["noaa_station"]
LAT = float(secrets["latitude"])
LON = float(secrets["longitude"])
OWM_KEY = secrets["openweather_key"]
# Static UTC offset used at boot. After the first weather fetch, the
# OpenWeatherMap response carries the accurate offset (DST-aware) and the
# RTC re-syncs automatically, so this only needs to be roughly right.
TZ_OFFSET_HOURS = int(secrets.get("tz_offset_hours", -5))

WEATHER_INTERVAL = 600
FORECAST_INTERVAL = 3600    # 3-day forecast — proxy already caches for 1h
OPENSKY_INTERVAL = 60
HEALTH_INTERVAL = 300       # poll proxy /api/health every 5 minutes
WATCHDOG_TIMEOUT = 90       # hard-reset if the main loop hasn't fed for this long
PLANE_CYCLE_SECS = 5
PLANE_MAX_SECS = 600          # max continuous time on plane screen
PLANE_COOLDOWN_SECS = 60      # weather break after PLANE_MAX_SECS hits
PLANE_QUIET_START_HR = 1      # local hour to stop fetching planes (saves API)
PLANE_QUIET_END_HR = 5        # local hour to resume fetching planes
STATUS_INTERVAL = 180         # poll proxy /api/status every 3 min (proxy caches 180s)
STATUS_SHOW_EVERY = 45        # rest-screen seconds before the status board shows again
STATUS_DWELL_SECS = 10        # how long the summary + each incident card stays up
# ---------------------------------------------------------------------------
# Feature + display configuration (per-device, from secrets.py)
#
# Feature flags turn each capability on/off independently. Defaults preserve
# the previous behavior (which was derived from `basin_mode`) so existing
# secrets.py files keep working unchanged.
#
#   enable_weather    weather text (temp / condition / wind) + 3-day forecast
#   enable_tide       tide basin water column + tide time  (coastal displays)
#   enable_astronomy  moon/planet sky card + sky map/zoom   (inland displays)
#   enable_planes     overhead aircraft screen
#   enable_boats      nearby AIS vessel screen              (coastal displays)
#   enable_status     cloud/dev provider outage board       (128x64 only)
#
# Tide and astronomy share the left basin / sky card, so on a single panel
# they are alternatives — if both are enabled, astronomy takes the basin.
# ---------------------------------------------------------------------------
_LEGACY_BASIN = secrets.get("basin_mode", "tides")   # back-compat default source

def _flag(name, default):
    return bool(secrets.get(name, default))

ENABLE_WEATHER   = _flag("enable_weather", True)
ENABLE_ASTRONOMY = _flag("enable_astronomy", _LEGACY_BASIN == "sky")
ENABLE_TIDE      = _flag("enable_tide", _LEGACY_BASIN != "sky")
ENABLE_PLANES    = _flag("enable_planes", True)
ENABLE_BOATS     = _flag("enable_boats", _LEGACY_BASIN != "sky")
ENABLE_STATUS    = _flag("enable_status", False)   # 128x64-only outage board

# --- Display panel size ---
# "128x64" (default, verified) renders the 64x32 layout at scale=2 plus
# native-width astronomy. "64x32" targets a native 64x32 panel. NOTE: full
# 64x32 layout support is still in progress; 128x64 is the tested config.
DISPLAY = secrets.get("display", "128x64")
if DISPLAY == "64x32":
    MATRIX_WIDTH, MATRIX_HEIGHT, DISPLAY_SCALE = 64, 32, 1
else:
    MATRIX_WIDTH, MATRIX_HEIGHT, DISPLAY_SCALE = 128, 64, 2

# Internal knobs the rest of the file already understands, derived from the
# flags above. Astronomy wins the shared basin/sky area when both are on.
BASIN_MODE = "sky" if ENABLE_ASTRONOMY else "tides"
PLANES_ENABLED = ENABLE_PLANES
SHIPS_ENABLED = ENABLE_BOATS
SHIPS_TEST = False
SHIP_INTERVAL = 60      # poll for ships every 60 sec
SHIP_WEATHER_SECS = 30  # show weather for 30s in cycle
DEMO_MODE = False       # Set True to auto-cycle test fixtures (no network needed)
DEMO_INTERVAL = 30      # seconds per view in demo mode

# Display runs at bit_depth=2 to keep per-row PWM bursts short enough that
# the panel power rail doesn't sag mid-scan (the symptom: flicker on bright
# rows). At bit_depth=2 each channel has 4 PWM levels (0x00 / 0x40 / 0x80 /
# 0xC0), giving a 64-color palette total. _dim() snaps any 0xRRGGBB color
# to that palette so the values in code match what the panel actually shows.
# A handful of source literals that would round down to 0 are rescued
# manually (water-deep, dim-star, vsep) to preserve visual intent.
#
# PANEL_BGR: this specific panel is wired BGR — sending red shows as blue,
# sending blue shows as red. _dim() swaps R<->B post-quantization so source
# code uses normal 0xRRGGBB values. If you ever swap in an RGB-wired panel,
# set PANEL_BGR = False.
PANEL_BGR = True

def _dim(c):
    c = c & 0xC0C0C0
    if PANEL_BGR:
        c = ((c & 0xFF) << 16) | (c & 0x00FF00) | ((c >> 16) & 0xFF)
    return c

# HTTP proxy on Raspberry Pi — bypasses ESP32 TLS limitation for OpenSky
PROXY_HOST = secrets.get("proxy_host", "")       # e.g. "http://YOUR_PI_IP:6590"
# Shared secret sent as X-Device-Secret on POST /api/devicelog. Must match
# the proxy's device_secret. Empty here = device sends no header (proxy
# only enforces if its config also has device_secret set).
DEVICE_SECRET = secrets.get("device_secret", "")
# Named location for the proxy's v2 API. When set, planes are fetched from
# /api/v2/planes?loc=<name> using the location's lat/lon/bbox configured in
# the proxy's config.json `locations` block. When empty, the device uses the
# original /api/planes endpoint (proxy's home location).
LOCATION_NAME = secrets.get("location", "")
# BASIN_MODE, PLANES_ENABLED, and SHIPS_ENABLED are derived from the feature
# flags in the configuration block near the top of this file.

# ---------------------------------------------------------------------------
# Demo fixtures — varied conditions to exercise all display paths
# (temp_str, cond_str, cond_main, wind_spd, wind_dir, tide_level, tide_type)
_DEMO_WEATHER = (
    ("72\xb0F",  "Clear Sky",  "Clear",        5, "SW", 0.8, "H"),
    ("-5\xb0F",  "Heavy Snow", "Snow",         18, "NW", 0.5, "L"),
    ("95\xb0F",  "Thndrstm",   "Thunderstorm", 28, "S",  0.2, "L"),
    ("55\xb0F",  "Heavy Rain", "Rain",         22, "NE", 0.6, "H"),
    ("68\xb0F",  "Fog",        "Fog",           3, "W",  0.4, "L"),
    ("82\xb0F",  "Sctd Cloud", "Clouds",       12, "E",  0.9, "H"),
)
# (callsign, alt_ft, spd_kt, hdg, origin, dest, actype, reg)
_DEMO_PLANES = (
    ("UAL1234", 35000, 450, 270, "BOS", "SFO", "B739", "N12345"),
    ("DAL567",  28000, 420, 180, "BOS", "ATL", "A321", "N567DL"),
    ("JBU42",   18000, 380,  90, "BOS", "FLL", "A320", "N42JB"),
    ("BAW213",  38000, 490,  45, "BOS", "LHR", "B789", "G-ZBKA"),
    ("AAL100",  32000, 440, 250, "BOS", "DFW", "B738", "N100AA"),
)
_DEMO_SHIPS = (
    {"name": "IYANOUGH", "type": 40, "type_name": "HighSpeed",
     "destination": "NANTUCKET", "length": 47,  "distance_mi": 2.3, "heading": 135},
    {"name": "MSC FLORA", "type": 70, "type_name": "Cargo",
     "destination": "NEW YORK",  "length": 280, "distance_mi": 8.1, "heading": 220},
    {"name": "SEA TITAN", "type": 80, "type_name": "Tanker",
     "destination": "HOUSTON",   "length": 220, "distance_mi": 5.7, "heading": 45},
    {"name": "FREEDOM",   "type": 50, "type_name": "Special",
     "destination": "BOSTON",    "length": 80,  "distance_mi": 5.1, "heading": 315},
)

# ---------------------------------------------------------------------------
# Buttons — UP and DOWN on the Matrix Portal M4
# ---------------------------------------------------------------------------
btn_up = digitalio.DigitalInOut(board.BUTTON_UP)
btn_up.switch_to_input(pull=digitalio.Pull.UP)
btn_down = digitalio.DigitalInOut(board.BUTTON_DOWN)
btn_down.switch_to_input(pull=digitalio.Pull.UP)

# BTN_UP forces the weather screen back on, even if a plane is currently
# being shown. Defined here so the button-poll block at the bottom of the
# file can reach it; show_weather_tides is defined before that block runs.
def force_weather_screen():
    global planes, showing_planes, _forecast_showing, _forecast_pending
    planes = []
    showing_planes = False
    _forecast_showing = False
    _forecast_pending = False
    show_weather_tides()

# ---------------------------------------------------------------------------
# Display setup — 128x64 panel, rendered at scale=2 from a 64x32 coordinate
# space. The root groups below all use scale=2, so every x/y coordinate in
# this file is in the original 64x32 layout and gets pixel-doubled by
# displayio. This avoids rewriting hundreds of hardcoded coordinates.
# ---------------------------------------------------------------------------
mp = MatrixPortal(
    status_neopixel=board.NEOPIXEL,
    bit_depth=2,
    debug=False,
    width=MATRIX_WIDTH,
    height=MATRIX_HEIGHT,
)

# Clear MatrixPortal's default group so we manage our own layout
root = mp.display.root_group
while len(root) > 0:
    root.pop()

display = mp.display
FONT = terminalio.FONT
FONT_SMALL = bitmap_font.load_font("4x6.bdf")
FONT_MID = bitmap_font.load_font("5x8.bdf")


# Airline lookup — reads from airlines.csv on disk, caches only recent entries
# Saves ~6 KB RAM vs inline dict on SAMD51
_airline_cache = {}  # max 5 entries
_AIRLINE_CACHE_MAX = 5

def get_airline_info(callsign):
    """Look up airline by ICAO prefix. Returns (name, iata, color)."""
    prefix = callsign[:3].upper()
    if prefix in _airline_cache:
        return _airline_cache[prefix]
    # Scan CSV file for matching ICAO code
    try:
        with open("airlines.csv", "r") as f:
            f.readline()  # skip header
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if parts[0] == prefix:
                    name = parts[2][:8]
                    iata = parts[1]
                    color = int(parts[3], 16)
                    result = (name, iata, color)
                    # Evict oldest if cache full
                    if len(_airline_cache) >= _AIRLINE_CACHE_MAX:
                        _airline_cache.pop(next(iter(_airline_cache)))
                    _airline_cache[prefix] = result
                    return result
    except Exception as e:
        print("Airline CSV err:", e)
    return (prefix, prefix[:2], 0x00AA44)

def icao_to_display(icao):
    """Convert ICAO airport code to 3-letter display code."""
    if not icao:
        return "???"
    # US airports: KJFK → JFK
    if len(icao) == 4 and icao[0] == "K":
        return icao[1:]
    # Canadian: CYYZ → YYZ
    if len(icao) == 4 and icao[:2] == "CY":
        return icao[1:]
    # Lookup from airports.csv on disk
    try:
        with open("airports.csv", "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(icao + ","):
                    return line.split(",")[1]
    except Exception:
        pass
    # Fallback: truncate to 3 chars
    return icao[:3]

COMPASS_DIRS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

def heading_to_compass(hdg):
    return COMPASS_DIRS[round(hdg / 45) % 8]

# ---------------------------------------------------------------------------
# Display groups and labels
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Background bitmaps — colored zones to fill the display
# Uses a small palette (4 colors) mapped to zones on a full-screen bitmap.
# This avoids the RAM cost of a per-pixel-color framebuffer.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Tide water column (x=0-13, full height y=0-31)
# Water fills from bottom based on tide level, no border/walls
# ---------------------------------------------------------------------------
BASIN_W = 20   # full left column width
BASIN_H = 32   # full display height
# Tide current particles: (x, y_phase_offset) — staggered so they never clump
_TIDE_PARTICLES = ((3, 0), (11, 11), (17, 22))
# Fixed star positions (col, row) for clear-sky night sky in weather basin
_STAR_POSITIONS = ((2, 1), (7, 4), (14, 2), (4, 7), (11, 5), (17, 3), (8, 8), (1, 6))
# Palette: 0=black sky, 1=water deep, 2=water mid, 3=water surface,
#          4=ship hull (gray), 5=ship superstructure (amber), 6=dim star,
#          7=sun/lightning yellow, 8=cloud/snow gray, 9=rain blue
basin_bmp = displayio.Bitmap(BASIN_W, BASIN_H, 12)
basin_pal = displayio.Palette(12)
basin_pal[0] = 0x000000
basin_pal[1] = 0x000040   # water deep (navy) — rescued from 0x001237 (would round to 0)
basin_pal[2] = 0x003264   # water mid (ocean blue)
basin_pal[3] = 0x125A96   # water surface/crest (bright blue)
basin_pal[4] = 0xBBBBBB   # ship hull (light gray) / Saturn ring / Mercury body
basin_pal[5] = 0xFF8822   # ship superstructure (amber) / Jupiter body
basin_pal[6] = 0x404040   # dim star (night sky) — rescued from 0x232335 (would round to 0)
basin_pal[7] = 0xFFCC00   # sun / lightning yellow / Saturn body
basin_pal[8] = 0xBBBBCC   # cloud / snow gray
basin_pal[9] = 0x2255AA   # rain blue
basin_pal[10] = 0xCC4422  # Mars rust (sky-mode only) — iron-oxide, not fire-engine red
basin_pal[11] = 0xFFEECC  # Venus cream (sky-mode only)

basin_tg = displayio.TileGrid(basin_bmp, pixel_shader=basin_pal, x=0, y=0)

_tide_level = 0.5      # 0.0 = empty, 1.0 = full
_basin_anim_tick = 0    # for surface wave animation
_tide_predictions = []  # store all today's predictions for interpolation
_sep_pixel_y = 16       # current y of the tide direction indicator pixel

BRIGHTNESS_MAX = 1.0
BRIGHTNESS_MIN = 0.08   # dimmest without going off
BRIGHTNESS_RAMP = 60    # minutes to ramp up/down

def update_brightness():
    """Adjust display brightness based on sun position."""
    t = time.localtime()
    now_mins = t.tm_hour * 60 + t.tm_min

    if _sunrise_mins <= now_mins <= _sunset_mins:
        # Daytime — check if we're in the ramp-up or ramp-down window
        mins_after_sunrise = now_mins - _sunrise_mins
        mins_before_sunset = _sunset_mins - now_mins

        if mins_after_sunrise < BRIGHTNESS_RAMP:
            # Ramping up after sunrise
            frac = mins_after_sunrise / BRIGHTNESS_RAMP
            b = BRIGHTNESS_MIN + (BRIGHTNESS_MAX - BRIGHTNESS_MIN) * frac
        elif mins_before_sunset < BRIGHTNESS_RAMP:
            # Ramping down before sunset
            frac = mins_before_sunset / BRIGHTNESS_RAMP
            b = BRIGHTNESS_MIN + (BRIGHTNESS_MAX - BRIGHTNESS_MIN) * frac
        else:
            b = BRIGHTNESS_MAX
    else:
        # Nighttime
        b = BRIGHTNESS_MIN

    display.brightness = max(BRIGHTNESS_MIN, min(BRIGHTNESS_MAX, b))

# ---------------------------------------------------------------------------
# Weather sky art helpers — draw into basin_bmp sky area (y < water_top)
# ---------------------------------------------------------------------------

def _sp(x, y, c, wt):
    if 0 <= x < BASIN_W and 0 <= y < wt:
        basin_bmp[x, y] = c


def _sky_sun(cx, cy, c, wt):
    """5×5 circle body + 4 cardinal rays + 4 diagonal rays."""
    for dx in (-1, 0, 1):
        _sp(cx + dx, cy - 2, c, wt)
        _sp(cx + dx, cy + 2, c, wt)
    for dx in (-2, -1, 0, 1, 2):
        _sp(cx + dx, cy - 1, c, wt)
        _sp(cx + dx, cy,     c, wt)
        _sp(cx + dx, cy + 1, c, wt)
    _sp(cx,      cy - 3, c, wt); _sp(cx,      cy + 3, c, wt)
    _sp(cx - 3,  cy,     c, wt); _sp(cx + 3,  cy,     c, wt)
    _sp(cx - 2,  cy - 3, c, wt); _sp(cx + 2,  cy - 3, c, wt)
    _sp(cx - 3,  cy - 2, c, wt); _sp(cx + 3,  cy - 2, c, wt)
    _sp(cx - 3,  cy + 2, c, wt); _sp(cx + 3,  cy + 2, c, wt)
    _sp(cx - 2,  cy + 3, c, wt); _sp(cx + 2,  cy + 3, c, wt)


def _sky_moon(cx, cy, c, wt):
    """Left-facing crescent moon: filled oval, right side bitten out."""
    for dx in (-1, 0, 1):
        _sp(cx + dx, cy - 2, c, wt)
        _sp(cx + dx, cy + 2, c, wt)
    for dx in (-2, -1, 0, 1, 2):
        _sp(cx + dx, cy - 1, c, wt)
        _sp(cx + dx, cy,     c, wt)
        _sp(cx + dx, cy + 1, c, wt)
    # Bite out right side
    for dy in (-1, 0, 1):
        _sp(cx + 1, cy + dy, 0, wt)
        _sp(cx + 2, cy + dy, 0, wt)


def _sky_cloud(x, y, w, c, wt):
    """Fluffy cloud: narrow bumpy top, two solid rows below."""
    for dx in range(1, w - 1):
        _sp(x + dx, y, c, wt)
    for dx in range(w):
        _sp(x + dx, y + 1, c, wt)
        _sp(x + dx, y + 2, c, wt)


def _sky_rain(x, y, count, c, wt):
    """Diagonal rain streaks: each streak is 2 pixels at 45°."""
    for i in range(count):
        xx = x + i * 3
        _sp(xx,     y,     c, wt)
        _sp(xx + 1, y + 1, c, wt)


def _sky_lightning(x, y, c, wt):
    """Classic zigzag lightning bolt, 4 rows tall."""
    _sp(x + 2, y,     c, wt)
    _sp(x + 1, y + 1, c, wt); _sp(x + 2, y + 1, c, wt)
    _sp(x + 1, y + 2, c, wt)
    _sp(x,     y + 3, c, wt); _sp(x + 1, y + 3, c, wt)


def _sky_snow(x, y, wt, count=3):
    """+ pattern snowflakes in a row (count flakes, default 3)."""
    c = 8
    for i in range(count):
        xx = x + i * 5
        _sp(xx + 1, y,     c, wt)
        _sp(xx,     y + 1, c, wt); _sp(xx + 1, y + 1, c, wt); _sp(xx + 2, y + 1, c, wt)
        _sp(xx + 1, y + 2, c, wt)


def _draw_weather_sky(water_top):
    """Draw weather-appropriate art in the sky portion of the basin."""
    if not weather_cond_main or water_top < 4:
        return
    SUN = 7; CLD = 8; RN = 9
    t = time.localtime()
    now_mins = t.tm_hour * 60 + t.tm_min
    night = now_mins < _sunrise_mins or now_mins > _sunset_mins
    cond = weather_cond_main

    if cond == "Clear":
        if night:
            _sky_moon(5, 3, SUN, water_top)
            for sx, sy in _STAR_POSITIONS:
                if sy < water_top - 1:
                    basin_bmp[sx, sy] = 6  # dim star
        else:
            _sky_sun(9, 3, SUN, water_top)

    elif cond == "Clouds":
        if night:
            _sky_moon(4, 2, SUN, water_top)
        else:
            _sky_sun(4, 2, SUN, water_top)
        _sky_cloud(8, 0, 11, CLD, water_top)

    elif cond in ("Rain", "Drizzle"):
        if cond == "Drizzle":
            _sky_cloud(2, 0, 16, CLD, water_top)
            for i in range(6):
                _sp(2 + i * 3, 5, RN, water_top)
        else:
            _sky_cloud(1, 0, 18, CLD, water_top)
            _sky_rain(2, 4, 6, RN, water_top)
            _sky_rain(3, 6, 5, RN, water_top)

    elif cond == "Snow":
        _sky_cloud(1, 0, 18, CLD, water_top)
        _sky_snow(2, 4, water_top, 4)

    elif cond == "Thunderstorm":
        _sky_cloud(1, 0, 18, CLD, water_top)
        _sky_rain(4, 6, 5, RN, water_top)
        _sky_lightning(8, 3, SUN, water_top)

    else:
        # Fog / Mist / Haze / Smoke — horizontal dot lines
        for y_off in range(3):
            for x in range(1, BASIN_W - 1, 2):
                _sp(x, 2 + y_off * 2, CLD, water_top)


_last_water_top = -1
_last_weather_cond_drawn = None
_last_has_ship_drawn = None
_last_night_drawn = None


# ---------------------------------------------------------------------------
# Sky map (basin_mode="sky") — full-panel-width horizon view of tonight's
# visible planets. The top ~52 rows are sky (altitude 90° at top, horizon
# at the bottom of the area); planets plotted as colored 2x2 dots at their
# (azimuth, altitude) positions. Cardinal direction labels are intentionally
# left off for cleanliness; weather/time strip lives below the chart.
# ---------------------------------------------------------------------------
_HORIZON_Y     = 43      # row of the horizon line in sky_card_bmp coords
_HORIZON_COLOR = 6       # palette slot 6: dim gray
_SUN_COLOR     = 7       # palette slot 7: yellow
_MOON_BRIGHT   = 4       # palette slot 4: light gray (full-ish moon)
_MOON_MED      = 8       # palette slot 8: cool gray (half-ish)
_MOON_DIM      = 6       # palette slot 6: dim gray (thin crescent)
_STAR_DIM      = 6       # palette slot 6: dim gray
_STAR_BRIGHT   = 8       # palette slot 8: cool gray
_NIGHT_SUN_ALT = -6      # sun must be below this (deg) to render stars

# Per-planet dot colors (basin_pal indices)
_PLANET_COLOR = {
    "Mercury": 4,    # light gray
    "Venus":   11,   # cream
    "Mars":    10,   # red
    "Jupiter": 5,    # amber
    "Saturn":  7,    # yellow
}

# Hex versions of planet colors for label tinting in the list view.
_PLANET_COLOR_HEX = {
    "Mercury": 0xBBBBBB,
    "Venus":   0xFFEECC,
    "Mars":    0xCC4422,
    "Jupiter": 0xFF8822,
    "Saturn":  0xFFCC00,
}

# Cycle: 30s zoomed-out map → 30s zoom on each visible planet (one at a
# time), then back to map. Independent 4.5-min timer interrupts for the
# list (text info) view, which itself runs 30s before normal cycle resumes.
_VIEW_DWELL_SECS  = 30
_LIST_DWELL_SECS  = 30
_LIST_INTERVAL    = 4 * 60 + 30     # 270s — every 4.5 min the list shows
_ZOOM_AZ_SPAN     = 60              # degrees of azimuth in zoom window
_ZOOM_ALT_SPAN    = 30              # degrees of altitude in zoom window
_sky_view_mode      = "map"
_sky_view_last_flip = 0
_zoom_idx           = 0             # which planet the zoom view focuses on
_last_list_time     = 0             # monotonic time the list was last shown


def _put(bmp, x, y, color_idx):
    """Bounds-checked single pixel write."""
    if 0 <= x < SKY_CARD_W and 0 <= y < SKY_CARD_H:
        bmp[x, y] = color_idx


def _az_to_x(az):
    """Map azimuth (degrees) to sky_card x. 360° spans 128 px (~2.8°/px).
    North wraps at x=0/128, E=32, S=64, W=96."""
    return int(az * SKY_CARD_W / 360) % SKY_CARD_W


def _alt_to_y(alt):
    """Map altitude (degrees) to sky_card y. 90° = top, 0° = horizon row."""
    y = _HORIZON_Y - int(alt * _HORIZON_Y / 90)
    if y < 0:
        return 0
    if y > _HORIZON_Y - 1:
        return _HORIZON_Y - 1
    return y


def _dot_2x2(x, y, color):
    """Small block — top-left at (x, y)."""
    for dx in (0, 1):
        for dy in (0, 1):
            _put(sky_card_bmp, x + dx, y + dy, color)


def _dot_plus(cx, cy, color):
    """5-pixel plus shape centered on (cx, cy) — bigger than 2x2 without
    being a fat square."""
    _put(sky_card_bmp, cx,     cy,     color)
    _put(sky_card_bmp, cx - 1, cy,     color)
    _put(sky_card_bmp, cx + 1, cy,     color)
    _put(sky_card_bmp, cx,     cy - 1, color)
    _put(sky_card_bmp, cx,     cy + 1, color)


def _dot_disk(cx, cy, r, color):
    """Round-ish filled disk of radius r centered on (cx, cy). Uses the
    same tip/equator trim as the planet glyphs so small disks read clean."""
    for dy in range(-r, r + 1):
        ry2 = r * r - dy * dy
        if ry2 < 1:
            continue
        rx = int(math.sqrt(ry2))
        if rx == r:
            rx -= 1
        for dx in range(-rx, rx + 1):
            _put(sky_card_bmp, cx + dx, cy + dy, color)


def _draw_glow_2x2(cx, cy, core_color, halo_color):
    """2x2 bright core with a 1-pixel halo ring around it (12 halo pixels
    forming a 4x4 outline, corners NOT included for a softer-edged look).
    Core occupies (cx..cx+1, cy..cy+1)."""
    # Halo top/bottom rows (3 pixels each, skipping outer corners)
    for dx in (0, 1):
        _put(sky_card_bmp, cx + dx, cy - 1, halo_color)
        _put(sky_card_bmp, cx + dx, cy + 2, halo_color)
    # Halo side columns
    for dy in (0, 1):
        _put(sky_card_bmp, cx - 1, cy + dy, halo_color)
        _put(sky_card_bmp, cx + 2, cy + dy, halo_color)
    _dot_2x2(cx, cy, core_color)


def _draw_glow_3x3(cx, cy, core_color, halo_color):
    """3x3 bright core centered on (cx, cy) with a soft 1-pixel halo ring.
    5x5 footprint with corners trimmed for a rounded glow look."""
    for dx in (-1, 0, 1):
        _put(sky_card_bmp, cx + dx, cy - 2, halo_color)
        _put(sky_card_bmp, cx + dx, cy + 2, halo_color)
    for dy in (-1, 0, 1):
        _put(sky_card_bmp, cx - 2, cy + dy, halo_color)
        _put(sky_card_bmp, cx + 2, cy + dy, halo_color)
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            _put(sky_card_bmp, cx + dx, cy + dy, core_color)


def _draw_glow_5x5(cx, cy, core_color, halo_color):
    """Big glowy planet glyph for the zoom view: rounded 5x5 core in
    core_color, 7x7-footprint halo ring in halo_color."""
    # Halo top + bottom rows (5 px each, no outer corners)
    for dx in range(-2, 3):
        _put(sky_card_bmp, cx + dx, cy - 3, halo_color)
        _put(sky_card_bmp, cx + dx, cy + 3, halo_color)
    # Halo side columns (5 px each)
    for dy in range(-2, 3):
        _put(sky_card_bmp, cx - 3, cy + dy, halo_color)
        _put(sky_card_bmp, cx + 3, cy + dy, halo_color)
    # 5x5 core with the 4 outer corners cut for a rounder look
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            if dx * dx + dy * dy > 5:
                continue
            _put(sky_card_bmp, cx + dx, cy + dy, core_color)


def _draw_saturn_glyph(cx, cy, body_color, ring_color):
    """Iconic mini-Saturn: 3x2 body with 1-pixel ring extensions on each
    side at the equator row. Reads as 'planet with rings' even at this
    pixel scale."""
    # Body 3x2 (offset so (cx, cy) is the center of the upper row)
    for dx in (-1, 0, 1):
        _put(sky_card_bmp, cx + dx, cy,     body_color)
        _put(sky_card_bmp, cx + dx, cy + 1, body_color)
    # Ring extensions — 1 pixel on each side at the equator row (cy)
    _put(sky_card_bmp, cx - 2, cy, ring_color)
    _put(sky_card_bmp, cx + 2, cy, ring_color)


# ---------------------------------------------------------------------------
# Iconic per-planet glyphs for the ZOOM view. Each is hand-designed at the
# 30-second zoom scale to feel like a portrait of that planet rather than
# yet another colored dot. Map view keeps the smaller generic glow shapes.
# ---------------------------------------------------------------------------

def _draw_jupiter_zoom(cx, cy):
    """7-row banded gas-giant. Amber body, two dark bands, equator slightly
    wider for a faintly oblate look. Centered on (cx, cy)."""
    A = _PLANET_COLOR["Jupiter"]   # amber
    B = 6                          # dim gray bands
    # (dy, half_width, color)
    rows = (
        (-3, 2, A),
        (-2, 3, A),
        (-1, 3, B),   # north equatorial band
        ( 0, 4, A),   # equator (widest)
        ( 1, 3, B),   # south equatorial band
        ( 2, 3, A),
        ( 3, 2, A),
    )
    for dy, w, color in rows:
        for dx in range(-w, w + 1):
            _put(sky_card_bmp, cx + dx, cy + dy, color)


def _draw_saturn_zoom(cx, cy):
    """5-row Saturn with a thick ring crossing through the body. Ring is 2 px
    tall on the extensions (one above + one below the equator) so the ring
    shape dominates at glance instead of reading as a single thin line."""
    Y = _PLANET_COLOR["Saturn"]    # yellow
    R = 4                          # light gray ring
    # Body 5x5, rounded
    body_rows = ((-2, 1), (-1, 2), (0, 2), (1, 2), (2, 1))
    for dy, w in body_rows:
        for dx in range(-w, w + 1):
            _put(sky_card_bmp, cx + dx, cy + dy, Y)
    # Ring extensions on both sides — 2px tall (equator + row above) so the
    # ring is unmistakable. Outer tip stays single-pixel for a tapered look.
    for dx in (-3, 3):
        _put(sky_card_bmp, cx + dx, cy - 1, R)
        _put(sky_card_bmp, cx + dx, cy,     R)
    for dx in (-4, 4):
        _put(sky_card_bmp, cx + dx, cy, R)
    # Ring crosses through body at equator — single dim pixel at center
    # to suggest the ring passing in front
    _put(sky_card_bmp, cx, cy, R)


def _draw_mars_zoom(cx, cy):
    """5x5 red Mars with cool-gray polar caps top and bottom — the most
    immediately recognizable Mars feature even at this pixel scale."""
    R = _PLANET_COLOR["Mars"]      # red
    W = 8                          # cool gray polar cap
    # Body 5x5 rounded
    body_rows = ((-1, 2), (0, 2), (1, 2))
    for dy, w in body_rows:
        for dx in range(-w, w + 1):
            _put(sky_card_bmp, cx + dx, cy + dy, R)
    # Top + bottom edge slightly narrower with white center for polar caps
    for dx in (-1, 0, 1):
        _put(sky_card_bmp, cx + dx, cy - 2, R)
        _put(sky_card_bmp, cx + dx, cy + 2, R)
    _put(sky_card_bmp, cx, cy - 2, W)   # north polar cap (replaces center red)
    _put(sky_card_bmp, cx, cy + 2, W)   # south polar cap


def _draw_venus_zoom(cx, cy):
    """Big bright Venus — 5x5 cream core with halo ring, plus 4 single-pixel
    rays in cardinal directions for the iconic 'brightest object' look."""
    _draw_glow_5x5(cx, cy, _PLANET_COLOR["Venus"], 8)
    # 4 ray extensions one pixel past the halo
    for dx, dy in ((-5, 0), (5, 0), (0, -5), (0, 5)):
        _put(sky_card_bmp, cx + dx, cy + dy, 8)


def _draw_mercury_zoom(cx, cy):
    """Small 3x3 Mercury with a 2-pixel sun-tint gradient on its sun-facing
    edge — hints at Mercury's perpetual nearness to the sun. Two pixels read
    as deliberate tinting; one alone can look like a stray glitch."""
    G = _PLANET_COLOR["Mercury"]   # light gray
    Y = 7                          # yellow sun-tint
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            _put(sky_card_bmp, cx + dx, cy + dy, G)
    _put(sky_card_bmp, cx + 2, cy,     Y)   # equator (brightest sun-facing point)
    _put(sky_card_bmp, cx + 1, cy - 1, Y)   # upper-right edge (gradient)


_PLANET_ZOOM_GLYPH = {
    "Mercury": _draw_mercury_zoom,
    "Venus":   _draw_venus_zoom,
    "Mars":    _draw_mars_zoom,
    "Jupiter": _draw_jupiter_zoom,
    "Saturn":  _draw_saturn_zoom,
}


def _draw_moon_zoom(cx, cy, phase):
    """Big phase-correct moon disk, radius 12 (25px diameter).

    phase: 0..1 synodic position (0=new, 0.25=1st quarter waxing,
    0.5=full, 0.75=last quarter waning, →1=new again).

    Renders the lit hemisphere in light gray (palette 4), and outlines the
    dark hemisphere's perimeter in dim gray (palette 6) so the disk is
    always visible even at new moon."""
    r = 12
    LIT  = 4
    RIM  = 6
    cos_pa = math.cos(math.pi * (1 - 2 * phase))   # 1 at full, -1 at new
    waxing = phase <= 0.5
    for dy in range(-r, r + 1):
        ry2 = r * r - dy * dy
        if ry2 < 1:
            continue
        rx = int(math.sqrt(ry2))
        if rx == r:
            rx -= 1
        for dx in range(-rx, rx + 1):
            if waxing:
                lit = dx > -cos_pa * rx - 0.5
            else:
                lit = dx <  cos_pa * rx + 0.5
            if lit:
                _put(sky_card_bmp, cx + dx, cy + dy, LIT)
            elif dx == -rx or dx == rx:
                # Rim outline of un-lit side so the disk shape is always
                # readable, even when illum is very low.
                _put(sky_card_bmp, cx + dx, cy + dy, RIM)


def _clear_sky_card():
    for y in range(SKY_CARD_H):
        for x in range(SKY_CARD_W):
            sky_card_bmp[x, y] = 0


# Pseudo-random star scatter. Generated once at first night render with a
# fixed seed so the pattern is stable across reboots — feels like the same
# sky, not a different random one each time.
_STARS = None

def _generate_stars(count=40):
    random.seed(42)
    out = []
    for _ in range(count):
        x = random.randint(0, SKY_CARD_W - 1)
        y = random.randint(1, _HORIZON_Y - 2)   # above horizon, not on top edge
        bright = (random.randint(0, 4) == 0)    # ~1 in 5 brighter
        out.append((x, y, bright))
    return out


def _is_night():
    sun = _sky_data.get("sun") or {}
    return sun.get("alt", 90) < _NIGHT_SUN_ALT


def render_sky_map():
    """Draw the horizon sky map at full panel width. Layers (bottom to top):
       1. Stars (only when the sun is below civil twilight)
       2. Horizon line
       3. Sun (if above horizon)
       4. Moon (size + brightness scaled by illumination)
       5. Planets (size by magnitude)"""
    global _STARS
    _clear_sky_card()

    # 1) Stars — pseudo-random scatter, only visible at night
    if _is_night():
        if _STARS is None:
            _STARS = _generate_stars()
        for sx, sy, bright in _STARS:
            sky_card_bmp[sx, sy] = _STAR_BRIGHT if bright else _STAR_DIM

    # 2) Horizon line — dim, doesn't compete with the dots
    for x in range(SKY_CARD_W):
        sky_card_bmp[x, _HORIZON_Y] = _HORIZON_COLOR

    # 3) Sun — big yellow disk with amber halo + 4 cardinal rays when above
    # horizon. Bigger than the planets so it visually dominates the map.
    sun = _sky_data.get("sun") or {}
    if sun.get("alt", -90) > 0:
        sx = _az_to_x(sun["az"])
        sy = _alt_to_y(sun["alt"])
        _draw_glow_5x5(sx, sy, _SUN_COLOR, 5)  # 5x5 core + 7x7 halo
        # 4 short rays extending out 1 px past the halo
        _put(sky_card_bmp, sx - 5, sy, _SUN_COLOR)
        _put(sky_card_bmp, sx + 5, sy, _SUN_COLOR)
        _put(sky_card_bmp, sx, sy - 5, _SUN_COLOR)
        _put(sky_card_bmp, sx, sy + 5, _SUN_COLOR)

    # 4) Moon — bigger glowy for full-ish, smaller dim for crescent
    moon = _sky_data.get("moon") or {}
    m_alt = moon.get("alt", -90)
    if m_alt > 0:
        mx = _az_to_x(moon["az"])
        my = _alt_to_y(m_alt)
        illum = moon.get("illum", 0)
        if illum > 0.7:
            _draw_glow_3x3(mx, my, _MOON_BRIGHT, _MOON_DIM)
        elif illum > 0.3:
            _draw_glow_2x2(mx, my, _MOON_MED, _MOON_DIM)
        elif illum > 0.05:
            _dot_2x2(mx, my, _MOON_DIM)

    # 5) Planets — iconic glyphs with halos. Brighter planets get bigger
    #    cores and softer halos so they read as "this one really pops"
    #    even at the cost of pin-point positional accuracy.
    planets = _sky_data.get("planets") or []
    for p in planets:
        px = _az_to_x(p.get("best_az", 0))
        py = _alt_to_y(p.get("best_alt", 0))
        name = p.get("name", "")
        color = _PLANET_COLOR.get(name, 4)
        mag = p.get("mag", 0)
        if name == "Saturn":
            _draw_saturn_glyph(px, py, color, _STAR_DIM)
        elif mag < -3:        # Venus (mag ~-4)
            _draw_glow_3x3(px, py, color, _STAR_DIM)
        elif mag < -1:        # Jupiter (mag ~-2)
            _draw_glow_2x2(px, py, color, _STAR_DIM)
        elif name == "Mars":
            _draw_glow_2x2(px, py, color, _STAR_DIM)
        else:                 # Mercury — small + simple
            _dot_2x2(px, py, color)


# Sky-map state. We only redraw when the planet list changes.
_sky_data = {"planets": [], "cloud_score": "Clear", "cond": "Clear"}
_sky_last_drawn = ""    # marker string: "<count>:<names>"
# Unused legacy globals retained so update_basin_planets has stable signature
_sky_idx = 0
_sky_last_cycle = 0


def fetch_sky():
    """Pull tonight's planet visibility from the proxy. Refreshed once per
    weather cycle (~5 min on the device, 6h cache on the proxy)."""
    global _sky_data
    if not (LOCATION_NAME and PROXY_HOST):
        return
    try:
        url = "{}/api/v2/sky?loc={}".format(PROXY_HOST, LOCATION_NAME)
        data = fetch_json(url)
        _sky_data = data
        device_log("Sky:{} cloud={}".format(
            len(data.get("planets", [])),
            data.get("cloud_score", "?"),
        ))
    except Exception as e:
        device_log("Sky err:{}".format(e))


def _zoom_targets():
    """Names of objects we can zoom on, in cycle order: each visible
    planet plus "Moon" if it's above the horizon."""
    planets = _sky_data.get("planets") or []
    names = [p.get("name", "") for p in planets]
    moon = _sky_data.get("moon") or {}
    if moon.get("alt", -90) > 0:
        names.append("Moon")
    return names


def render_zoom_view():
    """Dispatcher for the zoom slot. Targets cycle through visible planets
    and (if up) the moon. Planet zooms show a 60°×30° patch of sky around
    the focus; the moon zoom dedicates the whole card to a big phase-
    correct moon disk."""
    targets = _zoom_targets()
    if not targets:
        _clear_sky_card()
        return
    idx = _zoom_idx if 0 <= _zoom_idx < len(targets) else 0
    name = targets[idx]
    if name == "Moon":
        _render_moon_zoom()
    else:
        _render_planet_zoom_at(name)


def _render_moon_zoom():
    """Full-card moon portrait — big phase-correct disk centered."""
    _clear_sky_card()
    moon = _sky_data.get("moon") or {}
    phase = moon.get("phase", 0)
    illum = moon.get("illum", 0)
    cx = SKY_CARD_W // 2          # 64
    cy = SKY_CARD_H // 2 - 1      # 21
    _draw_moon_zoom(cx, cy, phase)
    if sky_zoom_label is not None:
        sky_zoom_label.text = "ZOOM: Moon {}%".format(int(round(illum * 100)))
        sky_zoom_label.color = _dim(0xDDCCAA)   # warm cream


def _render_planet_zoom_at(focus_name):
    """Planet-focused zoom: 60°×30° window around the planet's position."""
    _clear_sky_card()
    planets = _sky_data.get("planets") or []
    focus = None
    for p in planets:
        if p.get("name", "") == focus_name:
            focus = p
            break
    if focus is None:
        return
    cen_az  = focus.get("best_az", 0)
    cen_alt = focus.get("best_alt", 30)

    half_az  = _ZOOM_AZ_SPAN  / 2
    half_alt = _ZOOM_ALT_SPAN / 2

    def to_screen(az, alt):
        # Signed delta az in [-180, 180] handles wraparound at 0/360
        d_az = (az - cen_az + 540) % 360 - 180
        if abs(d_az) > half_az or abs(alt - cen_alt) > half_alt:
            return None
        x = int(SKY_CARD_W * (d_az + half_az) / _ZOOM_AZ_SPAN)
        # Higher altitude → smaller y (top of screen)
        y = int(SKY_CARD_H * (half_alt - (alt - cen_alt)) / _ZOOM_ALT_SPAN)
        return x, y

    # Crosshair at the view center — subtle, dim
    ccx, ccy = SKY_CARD_W // 2, SKY_CARD_H // 2
    for d in (-3, 3):
        _put(sky_card_bmp, ccx + d, ccy, 6)
        _put(sky_card_bmp, ccx, ccy + d, 6)

    # Sun if in window (rare at night, but include for completeness)
    sun = _sky_data.get("sun") or {}
    if sun.get("alt", -90) > 0:
        pos = to_screen(sun["az"], sun["alt"])
        if pos:
            _draw_glow_5x5(pos[0], pos[1], _SUN_COLOR, 5)

    # Moon if in window
    moon = _sky_data.get("moon") or {}
    if moon.get("alt", -90) > 0:
        pos = to_screen(moon["az"], moon["alt"])
        if pos:
            illum = moon.get("illum", 0)
            if illum > 0.5:
                _draw_glow_5x5(pos[0], pos[1], _MOON_BRIGHT, _MOON_DIM)
            elif illum > 0.2:
                _draw_glow_3x3(pos[0], pos[1], _MOON_MED, _MOON_DIM)
            elif illum > 0.05:
                _dot_2x2(pos[0], pos[1], _MOON_DIM)

    # Planets — iconic per-planet glyphs (see _draw_*_zoom above). Each
    # has hand-designed features: Jupiter's bands, Saturn's rings,
    # Mars's polar caps, Venus's rays, Mercury's sun-tint.
    for p in planets:
        pos = to_screen(p.get("best_az", 0), p.get("best_alt", 0))
        if not pos:
            continue
        name = p.get("name", "")
        glyph = _PLANET_ZOOM_GLYPH.get(name)
        if glyph:
            glyph(pos[0], pos[1])
        else:
            _dot_2x2(pos[0], pos[1], _PLANET_COLOR.get(name, 4))

    # Update zoom label
    if sky_zoom_label is not None:
        sky_zoom_label.text = "ZOOM: " + focus.get("name", "")
        sky_zoom_label.color = _dim(
            _PLANET_COLOR_HEX.get(focus.get("name", ""), 0xCCCCCC)
        )


def _render_list_view():
    """Populate the 3 list-view rows with planet info. Empty slots get
    blank text so the rows just disappear."""
    planets = _sky_data.get("planets") or []
    for i in range(3):
        if i < len(planets):
            p = planets[i]
            name = p.get("name", "")
            sky_list_name_labels[i].text = name
            sky_list_name_labels[i].color = _dim(
                _PLANET_COLOR_HEX.get(name, 0xCCCCCC)
            )
            sset = p.get("set", "")
            rise = p.get("rise", "")
            tail = sset or rise or ""
            sky_list_info_labels[i].text = "{} {}\xb0 {}-{}".format(
                p.get("best_dir", ""),
                p.get("best_alt", 0),
                p.get("best_time", ""),
                tail,
            )
        else:
            sky_list_name_labels[i].text = ""
            sky_list_info_labels[i].text = ""


def _set_view_mode(mode):
    """Toggle the right combination of widgets for the active view.
    map  -> sky_card_tg only
    zoom -> sky_card_tg + zoom title label
    list -> 3 name + 3 info labels (sky_card_tg hidden)"""
    is_list = (mode == "list")
    is_zoom = (mode == "zoom")
    sky_card_tg.hidden = is_list
    for lbl in sky_list_name_labels:
        lbl.hidden = not is_list
    for lbl in sky_list_info_labels:
        lbl.hidden = not is_list
    if sky_zoom_label is not None:
        sky_zoom_label.hidden = not is_zoom


def update_basin_planets():
    """Per-tick sky-area update.

    Normal cycle (each step _VIEW_DWELL_SECS):
        map → zoom on each planet in turn → map → repeat

    Independent every-_LIST_INTERVAL timer interrupts the cycle to show
    the list view for _LIST_DWELL_SECS, then normal cycle resumes from map.
    """
    global _sky_last_drawn, _sky_view_mode, _sky_view_last_flip
    global _zoom_idx, _last_list_time, _forecast_pending

    now = time.monotonic()
    planets = _sky_data.get("planets") or []

    zoom_targets = _zoom_targets()
    dwell = _LIST_DWELL_SECS if _sky_view_mode == "list" else _VIEW_DWELL_SECS
    if planets and now - _sky_view_last_flip >= dwell:
        # Decide what to switch to.
        if _sky_view_mode == "list":
            # End of list flash: back to map, restart cycle from beginning.
            # Also signal the main loop to show the forecast card next, so
            # the user gets a "planet summary → weather summary" beat. We
            # raise the flag regardless of whether forecast_days is populated
            # so the user sees the card transition even when the fetch is
            # late or failed — show_forecast() handles the empty case.
            _sky_view_mode = "map"
            _zoom_idx = 0
            _forecast_pending = True
        elif now - _last_list_time >= _LIST_INTERVAL:
            # Time for the periodic list flash. Pre-empts whatever was next.
            _sky_view_mode = "list"
            _last_list_time = now
        elif _sky_view_mode == "map":
            # Map → zoom on first target (planet or moon).
            _sky_view_mode = "zoom"
            _zoom_idx = 0
        else:                                # _sky_view_mode == "zoom"
            _zoom_idx += 1
            if _zoom_idx >= len(zoom_targets):
                _sky_view_mode = "map"
                _zoom_idx = 0
        _sky_view_last_flip = now
        _set_view_mode(_sky_view_mode)
        _sky_last_drawn = ""

    sun = _sky_data.get("sun") or {}
    moon = _sky_data.get("moon") or {}
    marker = "{}:{}:{}:{}:s{}@{}:m{}@{}/{}".format(
        _sky_view_mode, _zoom_idx,
        len(planets),
        ",".join(p.get("name", "") for p in planets),
        sun.get("az", -1), sun.get("alt", -91),
        moon.get("az", -1), moon.get("alt", -91),
        moon.get("phase", 0),
    )
    if marker == _sky_last_drawn:
        return

    if _sky_view_mode == "map":
        render_sky_map()
    elif _sky_view_mode == "zoom":
        render_zoom_view()
    else:
        _render_list_view()
    _sky_last_drawn = marker


def update_basin_water(level, tick):
    """Redraw water column with tide level. Wave intensity driven by wind.

    Sky pixels (sun/moon/clouds/etc.) are static between weather updates,
    so we only clear and redraw them when something that affects the sky
    actually changes — tide level moved, weather flipped, ship arrived or
    departed, or day/night crossed. Without that gating, the per-tick
    clear-then-redraw briefly flashes the sky to black and the weather
    art appears to blink."""
    global _last_water_top, _last_weather_cond_drawn
    global _last_has_ship_drawn, _last_night_drawn

    water_top = int(30 - level * 22)
    water_top = max(8, min(30, water_top))

    # Wind → wave parameters
    # <5mph: flat calm, 5-15mph: moderate, 15+mph: choppy
    w = min(_wind_speed, 25)
    calm = w < 5

    if not calm:
        amplitude = 0.3 + w * 0.06
        speed = 0.3 + w * 0.02
        chop = 0.5 + w * 0.03
        threshold = 0.2 - w * 0.02
    extra_rows = 1 if w >= 15 else 0

    # Precompute ship row spans so they can be drawn in a single pass,
    # avoiding a two-pass blink where water briefly overwrites the ship.
    # Each entry: (abs_row, x1_inclusive, x2_inclusive, palette_idx)
    has_ship = bool(ships)
    if has_ship:
        cx = BASIN_W // 2  # = 10
        ship_spans = (
            (water_top - 3, cx - 1, cx,     5),  # funnel  2px  amber
            (water_top - 2, cx - 3, cx + 2, 5),  # bridge  6px  amber
            (water_top - 1, cx - 5, cx + 5, 4),  # deck   11px  gray
            (water_top,     cx - 5, cx + 5, 4),  # hull   11px  gray
            (water_top + 1, cx - 4, cx + 4, 4),  # keel    9px  gray
        )
    else:
        ship_spans = ()

    # Did anything that affects the sky change since the last call?
    _t = time.localtime()
    _now_mins = _t.tm_hour * 60 + _t.tm_min
    night = _now_mins < _sunrise_mins or _now_mins > _sunset_mins
    sky_dirty = (water_top != _last_water_top
                 or weather_cond_main != _last_weather_cond_drawn
                 or has_ship != _last_has_ship_drawn
                 or night != _last_night_drawn)

    for row in range(BASIN_H):
        # Find ship span for this row (if any)
        ship_x1 = ship_x2 = -1
        ship_pal = 0
        for sr, sx1, sx2, sp in ship_spans:
            if sr == row:
                ship_x1 = max(0, sx1)
                ship_x2 = min(BASIN_W - 1, sx2)
                ship_pal = sp
                break

        for col in range(BASIN_W):
            if ship_x1 <= col <= ship_x2:
                basin_bmp[col, row] = ship_pal
            elif row < water_top - extra_rows:
                # Sky region — only clear when something forces a redraw.
                # Otherwise leave the previously-drawn sky art alone so the
                # weather doesn't flash to black on every tick.
                if sky_dirty:
                    basin_bmp[col, row] = 0
            elif row <= water_top:
                if calm:
                    basin_bmp[col, row] = 3  # flat surface line
                else:
                    wave = math.sin(col * chop + tick * speed) * amplitude
                    if w >= 10:
                        wave += math.sin(col * 1.3 + tick * speed * 1.7) * amplitude * 0.4
                    basin_bmp[col, row] = 3 if wave > threshold else 0
            elif row == water_top + 1:
                if calm:
                    basin_bmp[col, row] = 2  # flat sub-surface
                else:
                    wave = math.sin(col * chop + tick * speed + 1.0)
                    basin_bmp[col, row] = 3 if wave > 0 else 2
            else:
                basin_bmp[col, row] = 1  # deep

    # Tide current particles — mid-tone pixels drifting up (making) or down (ebbing)
    # through the water column to suggest current direction
    if tide_type_val:
        water_depth = BASIN_H - water_top - 2  # stay below surface rows
        if water_depth > 2:
            for px, py_off in _TIDE_PARTICLES:
                if tide_type_val == "H":
                    py = water_top + 2 + (py_off - tick) % water_depth
                else:
                    py = water_top + 2 + (py_off + tick) % water_depth
                if 0 <= py < BASIN_H:
                    basin_bmp[px, py] = 2  # mid-water tone — subtle against deep

    # Weather sky art (sun/moon/clouds/rain/lightning/snow/fog) — only
    # redraw on transitions; otherwise the cleared sky pixels above keep
    # whatever was last drawn there, no flashing.
    if sky_dirty:
        _draw_weather_sky(water_top)
        _last_water_top = water_top
        _last_weather_cond_drawn = weather_cond_main
        _last_has_ship_drawn = has_ship
        _last_night_drawn = night

def interpolate_tide_level():
    """Calculate current tide basin fill (0.0-1.0) from predictions.
    Predictions are stored as absolute seconds, so this works seamlessly
    across midnight (the tomorrow-half of the 2-day fetch is in the list)."""
    global _tide_level
    if len(_tide_predictions) < 2:
        _tide_level = 0.5
        return
    now_secs = time.mktime(time.localtime())

    # Find bracketing tides (previous and next)
    prev_tide = None
    next_tide = None
    for i, p in enumerate(_tide_predictions):
        if p[0] >= now_secs:
            next_tide = p
            if i > 0:
                prev_tide = _tide_predictions[i - 1]
            break

    if not prev_tide or not next_tide:
        # Before first tide or after last — estimate
        _tide_level = 0.7 if tide_type_val == "H" else 0.3
        return

    # Progress between previous and next tide
    span = next_tide[0] - prev_tide[0]
    if span <= 0:
        _tide_level = 0.5
        return
    progress = (now_secs - prev_tide[0]) / span

    # Rising (prev=L, next=H) or falling (prev=H, next=L)
    if prev_tide[1] == "L" and next_tide[1] == "H":
        _tide_level = progress  # 0→1
    elif prev_tide[1] == "H" and next_tide[1] == "L":
        _tide_level = 1.0 - progress  # 1→0
    else:
        _tide_level = 0.5

# Plane background palette — includes logo box zone + content zones
# Palette: 0=navy dark, 1=logo fill (updated per airline), 2=logo border,
#          3=separator, 4=content zone, 5=accent bar
# Plane background — logo box on left, black everywhere else
# Indices 0-2 used for plane, 1-5 repurposed for ship ocean + hull
pl_bg_bmp = displayio.Bitmap(14, 32, 6)
pl_bg_pal = displayio.Palette(6)
pl_bg_pal[0] = 0x000000
pl_bg_pal[1] = 0x0055A4   # logo fill (plane) / ocean deep (ship)
pl_bg_pal[2] = 0x002244   # logo border (plane) / hull gray (ship)
pl_bg_pal[3] = 0x003264   # ocean mid (ship only)
pl_bg_pal[4] = 0x125A96   # ocean surface (ship only)
pl_bg_pal[5] = 0xFF8822   # ship superstructure (ship only)

for y in range(32):
    for x in range(14):
        if x == 0 or x == 13 or y == 0 or y == 31:
            pl_bg_bmp[x, y] = 2
        else:
            pl_bg_bmp[x, y] = 1

pl_bg_tg = displayio.TileGrid(pl_bg_bmp, pixel_shader=pl_bg_pal, x=0, y=0)


def update_plane_bg(airline_color):
    """Update plane background with airline branding."""
    pl_bg_pal[1] = _dim(airline_color)
    # Border used to be a >>2 darkening of airline_color, which collapses to
    # 0x000000 under bit_depth=2 quantization. Fixed dim gray reads as a
    # subtle outline on every airline color instead.
    pl_bg_pal[2] = 0x404040


def update_ship_ocean(tick):
    """Animate ocean waves in ship left panel (14×32). Called every second."""
    if not _ship_hull_params:
        return
    y_start, ship_h, bow_len, ship_w, cx = _ship_hull_params
    super_rows = max(2, ship_h * 3 // 10)
    for y in range(32):
        for x in range(14):
            w1 = math.sin(x * 0.8 + tick * 1.2 + y * 0.5) * 0.6
            w2 = math.sin(x * 1.3 - tick * 0.7 + y * 0.4) * 0.4
            v = w1 + w2
            pl_bg_bmp[x, y] = 4 if v > 0.4 else (3 if v > 0 else 1)
    # Redraw ship silhouette over ocean
    for i in range(ship_h):
        y = y_start + i
        if y < 0 or y > 31:
            continue
        if i < bow_len:
            hw = max(1, ship_w * (i + 1) // (bow_len + 1) // 2)
        elif i >= ship_h - 2:
            hw = ship_w // 2 - 1
        else:
            hw = ship_w // 2
        pal_idx = 5 if bow_len <= i < bow_len + super_rows else 2
        for x in range(cx - hw, cx + hw + 1):
            if 0 <= x < 14:
                pl_bg_bmp[x, y] = pal_idx


# Route cache: callsign -> {"origin": "BOS", "dest": "JFK", "type":..., "reg":...}
# Sized for a full day of traffic in the bbox — too small and fetch_route
# starts evicting the currently-displayed plane mid-iteration of
# get_displayable_planes(), leaving show_plane() with an empty lookup.
flight_cache = {}
_FLIGHT_CACHE_MAX = 50


_consecutive_fetch_errs = 0
_FETCH_ERR_RESET_THRESHOLD = 12  # auto-reboot after this many fetch/render errors in a row

def fetch_json(url):
    """Fetch a URL and return parsed JSON. Always closes the socket — without
    try/finally, a MemoryError mid-parse leaks the socket and the next fetch
    fails with 'existing socket already connected' until reboot.

    Also tracks a consecutive-error counter; if a fetch raises (caller's
    except block calls fetch_failed), repeated failures trigger a hard
    reboot — adafruit_requests can get into an unrecoverable SSL/socket
    state that only a CPU reset clears."""
    global _consecutive_fetch_errs
    headers = None
    if DEVICE_SECRET and PROXY_HOST and url.startswith(PROXY_HOST):
        headers = {"X-Device-Secret": DEVICE_SECRET}
    resp = mp.network.fetch(url, headers=headers) if headers else mp.network.fetch(url)
    try:
        data = resp.json()
        _consecutive_fetch_errs = 0  # success resets the counter
        return data
    finally:
        resp.close()


def fetch_failed():
    """Caller's except block invokes this so we count the error."""
    global _consecutive_fetch_errs
    _consecutive_fetch_errs += 1
    if _consecutive_fetch_errs >= _FETCH_ERR_RESET_THRESHOLD:
        device_log("Too many errs ({}), reset".format(_consecutive_fetch_errs))
        time.sleep(1)
        microcontroller.reset()


def device_log(msg):
    """Timestamp and buffer a log entry; also prints to serial."""
    global _log_buffer
    t = time.localtime()
    entry = "[{:02d}:{:02d}:{:02d}] {}".format(t.tm_hour, t.tm_min, t.tm_sec, msg)
    print(entry)
    _log_buffer.append(entry)
    if len(_log_buffer) > 30:
        _log_buffer.pop(0)


def flush_device_log():
    """POST buffered log entries to the Pi proxy. Throttled to once per 5 min."""
    global _log_buffer, _last_log_flush
    if not _log_buffer:
        return
    now = time.monotonic()
    if now - _last_log_flush < 300:
        return
    _last_log_flush = now
    msgs = _log_buffer[:]
    _log_buffer = []
    try:
        gc.collect()
        body = json.dumps({"msgs": msgs}).encode()
        headers = {"Content-Type": "application/json"}
        if DEVICE_SECRET:
            headers["X-Device-Secret"] = DEVICE_SECRET
        resp = mp.network.requests.post(
            "{}/api/devicelog".format(PROXY_HOST),
            data=body,
            headers=headers,
        )
        resp.close()
        del body
        gc.collect()
        print("Log flushed: {} msgs".format(len(msgs)))
    except Exception as e:
        print("Log flush err:", e)
        _log_buffer = msgs + _log_buffer
        if len(_log_buffer) > 50:
            _log_buffer = _log_buffer[-30:]


def fetch_route(callsign, icao24=""):
    """Fetch route + aircraft type via proxy. Caches results."""
    if callsign in flight_cache:
        return flight_cache[callsign]
    gc.collect()
    info = {"origin": "???", "dest": "???", "type": "", "reg": ""}
    try:
        url = "{}/api/route?callsign={}".format(PROXY_HOST, callsign)
        if icao24:
            url += "&icao24={}".format(icao24)
        data = fetch_json(url)
        route = data.get("route", [])
        if route:
            info["origin"] = icao_to_display(route[0])
            info["dest"] = icao_to_display(route[-1])
        info["type"] = data.get("typecode", "")
        info["reg"] = data.get("registration", "")
        # operatorIata intentionally not stored — device never uses it
        print("Route {}: {} -> {} ({})".format(
            callsign, info["origin"], info["dest"], info["type"]))
    except Exception as e:
        print("Route err for {}: {}".format(callsign, e))
        fetch_failed()
    # Evict oldest if cache full
    if len(flight_cache) >= _FLIGHT_CACHE_MAX:
        flight_cache.pop(next(iter(flight_cache)))
    flight_cache[callsign] = info
    gc.collect()
    return info

# --- Weather screen group ---
# Two-layer composition. The outer group renders at panel-native scale=1;
# its scale=2 child carries everything that was originally laid out on the
# 64x32 logical grid (tide basin, weather labels, clock, etc.). The sky-mode
# planet card overlays the basin area at native resolution so glyphs and
# labels get the full 128x64 pixel density.
weather_group = displayio.Group()                 # native-resolution outer
weather_group_scaled = displayio.Group(scale=DISPLAY_SCALE)   # all existing scale-2 widgets
weather_group.append(weather_group_scaled)

# LEFT COLUMN: tide water fill (full column, animated)
weather_group_scaled.append(basin_tg)

# Tide time at bottom of column — tiny font
tide_time_label = Label(FONT_SMALL, text="", color=0x00CCDD, x=1, y=29)
weather_group_scaled.append(tide_time_label)

# Vertical separator line at x=14
vsep_bmp = displayio.Bitmap(1, 32, 2)
vsep_pal = displayio.Palette(2)
vsep_pal[0] = 0x000000
vsep_pal.make_transparent(0)
vsep_pal[1] = 0x404040   # vertical separator — rescued from 0x222233 (would round to 0)
for r in range(32):
    vsep_bmp[0, r] = 1
vsep_tg = displayio.TileGrid(vsep_bmp, pixel_shader=vsep_pal, x=20, y=0)
weather_group_scaled.append(vsep_tg)

# Tide direction indicator — white pixel sliding up (rising) or down (ebbing)
# along the separator line
sep_pixel_bmp = displayio.Bitmap(1, 1, 2)
sep_pixel_pal = displayio.Palette(2)
sep_pixel_pal[0] = 0x000000
sep_pixel_pal.make_transparent(0)
sep_pixel_pal[1] = 0xFFFFFF
sep_pixel_bmp[0, 0] = 1
sep_pixel_tg = displayio.TileGrid(sep_pixel_bmp, pixel_shader=sep_pixel_pal, x=20, y=16)
weather_group_scaled.append(sep_pixel_tg)

# RIGHT SIDE — 4 rows

# Native-resolution weather labels. In tide mode they live in the right
# column (x=44+); in sky mode they live in a horizontal two-row strip at
# the bottom — row 1 = clock + temp, row 2 = condition + wind.
if BASIN_MODE == "sky":
    clock_label = Label(FONT_MID,   text="", color=0xFFFFFF, x=2,  y=51)
    temp_label  = Label(FONT_MID,   text="", color=0xFFDD00, x=80, y=51)
    cond_label  = Label(FONT_MID,   text="", color=0xAAAACC, x=2,  y=60)
    wind_label  = Label(FONT_SMALL, text="", color=0x88BBCC, x=80, y=60)
else:
    clock_label = Label(FONT_MID,   text="", color=0xFFFFFF, x=44, y=8,  scale=2)
    temp_label  = Label(FONT_MID,   text="", color=0xFFDD00, x=44, y=26, scale=2)
    cond_label  = Label(FONT_MID,   text="", color=0xAAAACC, x=44, y=44)
    wind_label  = Label(FONT_SMALL, text="", color=0x88BBCC, x=44, y=55)
weather_group.append(clock_label)
weather_group.append(temp_label)
weather_group.append(cond_label)
weather_group.append(wind_label)

# --- Sky card (basin_mode='sky' only) ---
# Full panel width × top 44 rows. Horizon at y=43; the bottom two rows
# (y=44..63) hold the two-row native weather strip. Hidden in tide mode.
SKY_CARD_W, SKY_CARD_H = 128, 44
sky_card_bmp = displayio.Bitmap(SKY_CARD_W, SKY_CARD_H, 12)
sky_card_tg = displayio.TileGrid(sky_card_bmp, pixel_shader=basin_pal, x=0, y=0)
sky_card_tg.hidden = (BASIN_MODE != "sky")
weather_group.append(sky_card_tg)

# List-view labels — 3 rows, each with a name (planet-colored) + info
# (direction, altitude, best/set times). Hidden by default; shown only
# while _sky_view_mode == "list".
sky_list_name_labels = []
sky_list_info_labels = []
sky_zoom_label = None
if BASIN_MODE == "sky":
    for ly in (12, 24, 36):
        name_lbl = Label(FONT_SMALL, text="", color=0xCCCCCC, x=2,  y=ly)
        info_lbl = Label(FONT_SMALL, text="", color=0x88BBCC, x=36, y=ly)
        name_lbl.hidden = True
        info_lbl.hidden = True
        weather_group.append(name_lbl)
        weather_group.append(info_lbl)
        sky_list_name_labels.append(name_lbl)
        sky_list_info_labels.append(info_lbl)
    # Tiny title shown in zoom mode: "ZOOM: <Planet>".
    sky_zoom_label = Label(FONT_SMALL, text="", color=0xCCCCCC, x=2, y=4)
    sky_zoom_label.hidden = True
    weather_group.append(sky_zoom_label)

# In sky mode, hide the scale=2 basin + tide label + vertical separator
# since the sky card overlays them and the separator no longer marks a
# meaningful boundary between planet glyph and weather text.
if BASIN_MODE == "sky":
    basin_tg.hidden = True
    tide_time_label.hidden = True
    sep_pixel_tg.hidden = True
    vsep_tg.hidden = True

# --- Plane screen group ---
plane_group = displayio.Group(scale=DISPLAY_SCALE)

# Background first (includes logo box)
plane_group.append(pl_bg_tg)

# IATA code label inside logo box (centered in 14x32 area)
logo_label = Label(FONT, text="", color=0xFFFFFF, x=2, y=16)
plane_group.append(logo_label)

# Row 1: Route — LARGE font
route_label = Label(FONT, text="", color=0xFFFFFF, x=16, y=4)
plane_group.append(route_label)

# Row 2: Airline name — mid font (bigger, more readable)
airline_label = Label(FONT_MID, text="", color=0x00FF00, x=16, y=13)
plane_group.append(airline_label)

actype_label = Label(FONT_SMALL, text="", color=0x55AADD, x=16, y=27)
plane_group.append(actype_label)

# Row 3: Altitude + heading — mid font
alt_label = Label(FONT_MID, text="", color=0x44AA44, x=16, y=20)
plane_group.append(alt_label)

# Row 4: Registration (tail number) — small font
reg_label = Label(FONT_SMALL, text="", color=0x667788, x=16, y=27)
plane_group.append(reg_label)


# Ship screen: reuses plane_group and its labels to save RAM
# show_ship() switches to "plane" screen and repurposes the labels

# --- Loading screen group ---
loading_group = displayio.Group(scale=DISPLAY_SCALE)
loading_label = Label(FONT, text="LOADING...", color=0xFFFF00, x=4, y=12)
loading_group.append(loading_label)

# --- 3-day forecast screen group (sky mode only) ---
# Native 128×64 so the 4×6 small font lays out at 1:1 pixel density. Three
# columns × 5 text rows + 1 weather glyph each. Reuses basin_pal (sun yellow,
# cloud gray, rain blue, dim gray) so no second palette is needed.
FORECAST_W, FORECAST_H = 128, 64
_FCOL_CENTERS = (21, 64, 106)   # x-center of each column
_FCOL_LEFTS   = (1, 44, 87)     # x-left for left-anchored text
_FROW_Y       = (1, 26, 35, 44, 53)   # day, cond, hi, wind-speed, wind-dir
_FGLYPH_CY    = 16              # vertical center of glyph zone (y=8..24)
# Day-of-week labels for the third forecast column. tm_wday is 0=Mon..6=Sun.
_DOW_SHORT = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

forecast_group = displayio.Group()
forecast_bmp = displayio.Bitmap(FORECAST_W, FORECAST_H, 12)
forecast_tg = displayio.TileGrid(forecast_bmp, pixel_shader=basin_pal, x=0, y=0)
forecast_group.append(forecast_tg)

# 3 columns × 5 labels (day / cond / hi / wind-speed / wind-dir). Day label
# is centered above the glyph; the rest are left-aligned at the column's
# left margin so wider strings like "Light rain" don't recenter mid-update.
_forecast_labels = []
for _col in range(3):
    _col_labels = []
    # Day label (centered, brighter)
    _day_lbl = Label(FONT_SMALL, text="", color=0xFFFFFF)
    _day_lbl.anchor_point = (0.5, 0)
    _day_lbl.anchored_position = (_FCOL_CENTERS[_col], _FROW_Y[0])
    forecast_group.append(_day_lbl)
    _col_labels.append(_day_lbl)
    # Condition / hi / wind labels (left-anchored)
    for _i, _color in enumerate((0xAAAACC, 0xFFDD00, 0x88BBCC, 0x88BBCC)):
        _lbl = Label(FONT_SMALL, text="", color=_color,
                     x=_FCOL_LEFTS[_col], y=_FROW_Y[_i + 1])
        forecast_group.append(_lbl)
        _col_labels.append(_lbl)
    _forecast_labels.append(_col_labels)


# ---------------------------------------------------------------------------
# Service status board (128x64 only) — summary grid + incident card
# ---------------------------------------------------------------------------
# Data comes pre-normalized from the proxy's /api/status (levels 0/1/2), so
# these two screens are pure rendering. Built only when ENABLE_STATUS is set,
# to avoid the label/bitmap memory cost on displays that don't use it.
STATUS_MAX = 8                       # grid slots (2 cols x 4 rows)
_STATUS_MARK_COL_X = (8, 70)         # marker left x per column
_STATUS_LBL_COL_X  = (16, 78)        # provider label x per column
_STATUS_ROW_Y      = (20, 31, 42, 53)

# Shared 4-color marker palette: 0=off (transparent), 1=green, 2=amber, 3=red.
_status_pal = displayio.Palette(4)
_status_pal[0] = 0x000000
_status_pal[1] = _dim(0x00CC00)
_status_pal[2] = _dim(0xFFAA00)
_status_pal[3] = _dim(0xFF0000)
_status_pal.make_transparent(0)

status_group = None
status_incident_group = None
_status_marker_bmps = []
_status_markers = []
_status_name_labels = []
_status_footer = None
_sti_provider = _sti_status = _sti_comp = _sti_title1 = _sti_title2 = None

if ENABLE_STATUS:
    status_group = displayio.Group()
    _st_header = Label(FONT_SMALL, text="SERVICE STATUS", color=_dim(0xCCCCCC))
    _st_header.anchor_point = (0.5, 0.5)
    _st_header.anchored_position = (64, 7)
    status_group.append(_st_header)
    for _si in range(STATUS_MAX):
        _c, _r = _si % 2, _si // 2
        _mb = displayio.Bitmap(5, 5, 4)
        _mtg = displayio.TileGrid(_mb, pixel_shader=_status_pal,
                                  x=_STATUS_MARK_COL_X[_c], y=_STATUS_ROW_Y[_r])
        _mtg.hidden = True
        status_group.append(_mtg)
        _snl = Label(FONT_SMALL, text="", color=_dim(0xCCCCCC),
                     x=_STATUS_LBL_COL_X[_c], y=_STATUS_ROW_Y[_r] + 2)
        status_group.append(_snl)
        _status_marker_bmps.append(_mb)
        _status_markers.append(_mtg)
        _status_name_labels.append(_snl)
    _status_footer = Label(FONT_SMALL, text="", color=_dim(0x00CC00))
    _status_footer.anchor_point = (0.5, 0.5)
    _status_footer.anchored_position = (64, 61)
    status_group.append(_status_footer)

    status_incident_group = displayio.Group()
    _sti_provider = Label(FONT_MID, text="", color=_dim(0xFFFFFF))
    _sti_provider.anchor_point = (0.5, 0.5)
    _sti_provider.anchored_position = (64, 9)
    status_incident_group.append(_sti_provider)
    _sti_status = Label(FONT_MID, text="", color=_dim(0xFFAA00))
    _sti_status.anchor_point = (0.5, 0.5)
    _sti_status.anchored_position = (64, 22)
    status_incident_group.append(_sti_status)
    _sti_comp = Label(FONT_SMALL, text="", color=_dim(0x00CCDD))
    _sti_comp.anchor_point = (0.5, 0.5)
    _sti_comp.anchored_position = (64, 34)
    status_incident_group.append(_sti_comp)
    _sti_title1 = Label(FONT_SMALL, text="", color=_dim(0xCCCCCC))
    _sti_title1.anchor_point = (0.5, 0.5)
    _sti_title1.anchored_position = (64, 46)
    status_incident_group.append(_sti_title1)
    _sti_title2 = Label(FONT_SMALL, text="", color=_dim(0xCCCCCC))
    _sti_title2.anchor_point = (0.5, 0.5)
    _sti_title2.anchored_position = (64, 55)
    status_incident_group.append(_sti_title2)


def _set_status_marker(i, level):
    """Fill marker bitmap i with the palette index for a 0/1/2 status level."""
    idx = (level + 1) if level in (0, 1, 2) else 1
    bmp = _status_marker_bmps[i]
    for _y in range(5):
        for _x in range(5):
            bmp[_x, _y] = idx


def _status_wrap2(text, width):
    """Split text into two lines of <= width chars, breaking on a space."""
    text = text or ""
    if len(text) <= width:
        return text, ""
    cut = text.rfind(" ", 0, width + 1)
    if cut <= 0:
        cut = width
    return text[:cut].rstrip(), text[cut:].lstrip()[: width]


def show_status_summary():
    """Render the always-in-rotation provider grid: a colored marker + name per
    provider, with a footer summarizing overall health."""
    try:
        switch_screen("status")
        n_issues = 0
        for i in range(STATUS_MAX):
            if i < len(status_providers):
                p = status_providers[i]
                lvl = p.get("level", 0)
                if lvl >= 1:
                    n_issues += 1
                _status_markers[i].hidden = False
                _set_status_marker(i, lvl)
                _status_name_labels[i].text = str(p.get("name", ""))[:11]
            else:
                _status_markers[i].hidden = True
                _status_name_labels[i].text = ""
        if n_issues == 0:
            _status_footer.text = "all operational"
            _status_footer.color = _dim(0x00CC00)
        else:
            _status_footer.text = "{} incident{}".format(
                n_issues, "" if n_issues == 1 else "s")
            _status_footer.color = _dim(0xFF0000 if status_worst >= 2 else 0xFFAA00)
    except MemoryError as _e:
        print("show_status_summary MemoryError:", _e)
        gc.collect()


def _fmt_status_updated(epoch):
    """UTC epoch -> last-update string for the incident card. A bare 'h:mmp'
    clock is only meaningful within the last day; once the update is more than
    24h old the wall-clock time is ambiguous (no date shown), so collapse it to
    '>24H'. Empty string when unknown/unparseable."""
    if not epoch:
        return ""
    try:
        epoch = int(epoch)
        # RTC holds local time, so mktime(localtime()) is a local epoch; back out
        # the tz offset to compare against the UTC epoch the proxy sent.
        now_utc = time.mktime(time.localtime()) - _tz_offset_secs
        if now_utc - epoch > 86400:
            return ">24H"
        t = time.localtime(epoch + _tz_offset_secs)
        return "{}:{:02d}{}".format(
            t.tm_hour % 12 or 12, t.tm_min, "p" if t.tm_hour >= 12 else "a")
    except (ValueError, OverflowError):
        return ""


def show_status_incident(p):
    """Render the detail card for one degraded/down provider."""
    try:
        switch_screen("status_incident")
        lvl = p.get("level", 0)
        col = _dim(0xFF0000 if lvl >= 2 else 0xFFAA00)
        _sti_provider.text = str(p.get("name", ""))[:20]
        _upd = _fmt_status_updated(p.get("updated"))
        _sti_status.text = ("OUTAGE" if lvl >= 2 else "DEGRADED") + (
            "  " + _upd if _upd else "")
        _sti_status.color = col
        _sti_comp.text = str(p.get("component", ""))[:30]
        _l1, _l2 = _status_wrap2(str(p.get("title", "")), 30)
        _sti_title1.text = _l1
        _sti_title2.text = _l2
    except MemoryError as _e:
        print("show_status_incident MemoryError:", _e)
        gc.collect()


def _fp(x, y, c):
    """Bounds-checked single pixel write into forecast_bmp."""
    if 0 <= x < FORECAST_W and 0 <= y < FORECAST_H:
        forecast_bmp[x, y] = c


def _fc_cloud_shape(cx, cy, color=8):
    """Cloud silhouette centered at (cx, cy). ~13 wide × 5 tall: bumpy top,
    rounded body. Used as the base for rain/snow/storm/drizzle glyphs too."""
    C = color
    for dx in (-3, -2, 2, 3, 4):
        _fp(cx + dx, cy - 2, C)
    for dx in (-5, -4, -3, -2, -1, 1, 2, 3, 4, 5):
        _fp(cx + dx, cy - 1, C)
    for dx in range(-6, 7):
        _fp(cx + dx, cy,     C)
        _fp(cx + dx, cy + 1, C)
    for dx in range(-5, 6):
        _fp(cx + dx, cy + 2, C)


def _fc_sun(cx, cy):
    """Round 5×5 disk + 4 cardinal 2-px rays + 4 diagonal 1-px rays."""
    Y = 7
    for dy in (-2, -1, 0, 1, 2):
        for dx in (-2, -1, 0, 1, 2):
            if abs(dx) == 2 and abs(dy) == 2:
                continue
            _fp(cx + dx, cy + dy, Y)
    for d in (4, 5):
        _fp(cx + d, cy, Y); _fp(cx - d, cy, Y)
        _fp(cx, cy + d, Y); _fp(cx, cy - d, Y)
    _fp(cx + 3, cy + 3, Y); _fp(cx - 3, cy + 3, Y)
    _fp(cx + 3, cy - 3, Y); _fp(cx - 3, cy - 3, Y)


def _fc_partly_cloudy(cx, cy):
    """Small sun upper-left + cloud overlapping lower-right."""
    Y = 7
    sx, sy = cx - 4, cy - 4
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            _fp(sx + dx, sy + dy, Y)
    _fp(sx - 2, sy, Y); _fp(sx + 2, sy, Y)
    _fp(sx, sy - 2, Y); _fp(sx, sy + 2, Y)
    _fc_cloud_shape(cx + 1, cy + 2)


def _fc_rain(cx, cy):
    """Cloud + 3 diagonal rain streaks below."""
    _fc_cloud_shape(cx, cy - 2)
    B = 9
    for sx in (-4, 0, 4):
        _fp(cx + sx,     cy + 4, B)
        _fp(cx + sx - 1, cy + 5, B)


def _fc_drizzle(cx, cy):
    """Cloud + 3 single dots below (lighter than rain)."""
    _fc_cloud_shape(cx, cy - 2)
    B = 9
    for sx in (-4, 0, 4):
        _fp(cx + sx, cy + 4, B)


def _fc_thunderstorm(cx, cy):
    """Cloud + yellow zigzag bolt centered below."""
    _fc_cloud_shape(cx, cy - 2)
    L = 7
    _fp(cx + 1, cy + 3, L)
    _fp(cx,     cy + 4, L); _fp(cx + 1, cy + 4, L)
    _fp(cx - 1, cy + 5, L); _fp(cx,     cy + 5, L)
    _fp(cx - 2, cy + 6, L); _fp(cx - 1, cy + 6, L)


def _fc_snow(cx, cy):
    """Cloud + 3 plus-pattern flakes below."""
    _fc_cloud_shape(cx, cy - 2)
    W = 4
    for sx in (-4, 0, 4):
        _fp(cx + sx,     cy + 4, W)
        _fp(cx + sx - 1, cy + 5, W); _fp(cx + sx, cy + 5, W); _fp(cx + sx + 1, cy + 5, W)
        _fp(cx + sx,     cy + 6, W)


def _fc_fog(cx, cy):
    """4 horizontal gray bars suggesting layered fog."""
    G = 6
    G2 = 8
    bars = ((cy - 5, G2, 11), (cy - 2, G, 14), (cy + 1, G2, 12), (cy + 4, G, 13))
    for by, color, w in bars:
        for dx in range(-(w // 2), w - w // 2):
            _fp(cx + dx, by, color)


def _draw_forecast_glyph(cx, cy, cond_id):
    """Dispatch glyph by OpenWeatherMap condition code group."""
    if cond_id is None:
        cond_id = 800
    if 200 <= cond_id < 300:
        _fc_thunderstorm(cx, cy)
    elif 300 <= cond_id < 400:
        _fc_drizzle(cx, cy)
    elif 500 <= cond_id < 600:
        _fc_rain(cx, cy)
    elif 600 <= cond_id < 700:
        _fc_snow(cx, cy)
    elif 700 <= cond_id < 800:
        _fc_fog(cx, cy)
    elif cond_id == 800:
        _fc_sun(cx, cy)
    elif cond_id == 801:
        _fc_partly_cloudy(cx, cy)
    else:
        _fc_cloud_shape(cx, cy)


def _forecast_day_label(idx):
    """Index 0 = TODAY, 1 = TMRW, 2 = weekday name."""
    if idx == 0:
        return "TODAY"
    if idx == 1:
        return "TMRW"
    return _DOW_SHORT[(time.localtime().tm_wday + idx) % 7]

# --- Health indicator: 1 px red dot at (63, 31) ---
# Visible when /api/health reports a non-empty `issues` list (or when the
# proxy is unreachable). One TileGrid per group because displayio doesn't
# allow a TileGrid to be a child of multiple parents — they share the
# same bitmap and palette so this stays cheap.
_health_bmp = displayio.Bitmap(1, 1, 2)
_health_pal = displayio.Palette(2)
_health_pal[0] = 0x000000
_health_pal.make_transparent(0)
_health_pal[1] = 0xFF0000
_health_bmp[0, 0] = 1
_health_pixels = []
for _grp in (weather_group, plane_group, loading_group):
    _tg = displayio.TileGrid(_health_bmp, pixel_shader=_health_pal, x=63, y=31)
    _tg.hidden = True
    _grp.append(_tg)
    _health_pixels.append(_tg)
# forecast_group is native 128×64 (not scale=2), so position the health pixel
# at the native bottom-right corner directly.
_fcst_health_tg = displayio.TileGrid(_health_bmp, pixel_shader=_health_pal, x=127, y=63)
_fcst_health_tg.hidden = True
forecast_group.append(_fcst_health_tg)
_health_pixels.append(_fcst_health_tg)

def set_health_indicator(visible):
    """Show or hide the bottom-right red pixel across all screens."""
    for _tg in _health_pixels:
        _tg.hidden = not visible

# --- Apply PANEL_BRIGHTNESS to every static palette and label color ---
# HUB75 has no hardware dimming, so we scale RGB values in place.
# Dynamic writes (update_plane_bg, show_ship, show_plane, etc.) call
# _dim() inline; the scan below catches everything statically defined above.
for _pal in (basin_pal, pl_bg_pal, vsep_pal, sep_pixel_pal, _health_pal):
    for _i in range(len(_pal)):
        _pal[_i] = _dim(_pal[_i])
for _lbl in (tide_time_label, clock_label, temp_label, cond_label, wind_label,
             logo_label, route_label, airline_label, actype_label,
             alt_label, reg_label, loading_label):
    _lbl.color = _dim(_lbl.color)
for _col_labels in _forecast_labels:
    for _lbl in _col_labels:
        _lbl.color = _dim(_lbl.color)

# Start with loading screen
display.root_group = loading_group


# ---------------------------------------------------------------------------
# Time sync — single source of truth is the Pi proxy's /api/time endpoint.
# The Pi is NTP-synced via systemd-timesyncd, so it's authoritative; HTTP
# over LAN avoids the UDP-NTP-blocked-by-Wi-Fi failure mode and is more
# current than OWM's slightly-stale `dt` field. TZ_OFFSET_HOURS from
# secrets.py is a static fallback used until the first proxy sync lands.
# ---------------------------------------------------------------------------
_tz_offset_secs = TZ_OFFSET_HOURS * 3600
_rtc_known = False


def _try_proxy_time_sync():
    """Set the device RTC from the proxy's /api/time response and update
    _tz_offset_secs from the proxy's reported local offset (which already
    bakes in DST). Returns True on success. Safe to call repeatedly —
    each call absolutely re-syncs the RTC, so DST flips and small drift
    are corrected for free without any delta-nudge logic."""
    global _rtc_known, _tz_offset_secs
    try:
        data = fetch_json(PROXY_HOST + "/api/time")
        utc = int(data["utc"])
        tz_off = int(data["tz_offset_secs"])
        import rtc as _rtc_mod
        _rtc_mod.RTC().datetime = time.localtime(utc + tz_off)
        _tz_offset_secs = tz_off
        _rtc_known = True
        return True
    except Exception as e:
        print("Proxy time sync failed:", e)
        return False


print("Syncing time from proxy...")
if _try_proxy_time_sync():
    print("Time synced (UTC{:+d}):".format(_tz_offset_secs // 3600), time.localtime())

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
_log_buffer = []
_last_log_flush = 0.0
weather_str = ""
weather_cond = ""
weather_cond_main = ""
tide_str = ""
tide_type_val = ""
wind_str = ""
_wind_speed = 0
_sunrise_mins = 5 * 60 + 30
_sunset_mins = 19 * 60 + 30
ships = []
ship_idx = 0
last_ship_fetch = -SHIP_INTERVAL
_ship_cycle_start = 0
_showing_ship = False
_ship_hull_params = None   # (y_start, ship_h, bow_len, ship_w, cx) for ship ocean animation
_ship_anim_tick = 0
_ship_name_full = ""       # full ship name for character-window marquee
_ship_name_phase = 0       # marquee tick counter
# Row-2 (type / length) alternation. When the AIS feed gives a real type AND
# we know the vessel's length, we alternate between "Cargo" and "525ft" every
# 3 s on the type row. When only one of the two is known, we show that one
# fixed. "Vessel" is the AIS placeholder for type=0 — never displayed when we
# have a length to show instead.
_ship_alt_enabled = False
_ship_alt_type_text = ""
_ship_alt_length_text = ""
_ship_alt_showing_type = True
_ship_alt_last_switch = 0.0
planes = []
showing_planes = False
plane_screen_started_at = 0   # ts when plane screen first appeared (for max-duration safeguard)
plane_cooldown_until = 0      # don't re-show plane screen before this ts
plane_idx = 0
last_weather_fetch = -WEATHER_INTERVAL
last_forecast_fetch = -FORECAST_INTERVAL
last_sky_fetch = -OPENSKY_INTERVAL
last_health_fetch = -HEALTH_INTERVAL
# 3-day forecast state. Populated by fetch_forecast() from /api/forecast.
# Each entry: {"hi", "lo", "cond", "cond_id", "date", "wind", "wind_deg"}.
forecast_days = []
# Sky-mode-only: signals the main loop to flip to the forecast card once
# the planet list view dwell ends. Cleared when forecast card is shown or
# pre-empted by planes/ships.
_forecast_pending = False
_forecast_showing = False
_forecast_started_at = 0
FORECAST_DWELL_SECS = 30      # how long the forecast card stays on screen
# Service status board state. status_providers/status_worst are refreshed by
# fetch_status() from /api/status. The rotation shows the summary card every
# STATUS_SHOW_EVERY seconds of rest, then walks each degraded/down provider.
status_providers = []
status_worst = 0
last_status_fetch = -STATUS_INTERVAL
_last_status_summary = ""
_showing_status = False        # True while any status card owns the screen
_status_started_at = 0         # monotonic when the current status card appeared
_status_last_shown = 0         # monotonic when the last status rotation ended
_status_phase = 0              # 0 = summary; 1..N = incident cards
_status_incidents = []         # provider indices with level >= 1 for this rotation
last_plane_cycle = 0
current_screen = "loading"

_demo_step         = 2   # _demo_advance() increments first, so step 0 = weather fires first
_demo_weather_idx  = 0
_demo_plane_idx    = 0
_demo_ship_idx     = 0
_demo_last_switch  = 0

device_log("Boot OK")

# Watchdog — hard-resets the device if the main loop hasn't fed it for
# WATCHDOG_TIMEOUT seconds. Recovers automatically from cases where a
# network call hangs indefinitely (the web workflow stays responsive at
# supervisor level even when user code is blocked, so a hung fetch can
# silently freeze the display until manual intervention).
if _WATCHDOG_OK:
    try:
        microcontroller.watchdog.timeout = WATCHDOG_TIMEOUT
        microcontroller.watchdog.mode = WatchDogMode.RESET
        device_log("Watchdog: {}s".format(WATCHDOG_TIMEOUT))
    except Exception as _e:
        print("Watchdog setup failed:", _e)
        _WATCHDOG_OK = False


def switch_screen(name):
    """Switch which display group is shown."""
    global current_screen
    if current_screen == name:
        return
    current_screen = name
    if name == "weather":
        display.root_group = weather_group
    elif name == "plane":
        display.root_group = plane_group
    elif name == "forecast":
        display.root_group = forecast_group
    elif name == "status":
        display.root_group = status_group
    elif name == "status_incident":
        display.root_group = status_incident_group
    elif name == "loading":
        display.root_group = loading_group

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def get_condition_text(cond_id, fallback):
    """Look up short condition text from conditions.csv on disk.
    The display column fits 10 small-font chars (4 px/char in a 43 px panel)."""
    cid = str(cond_id)
    try:
        with open("conditions.csv", "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(cid + ","):
                    return line.split(",", 1)[1]
    except Exception:
        pass
    return fallback[:10]


def fetch_weather():
    global weather_str, weather_cond, weather_cond_main, wind_str, _wind_speed
    global _sunrise_mins, _sunset_mins, _tz_offset_secs, _rtc_known
    gc.collect()
    try:
        url = (
            "https://api.openweathermap.org/data/2.5/weather"
            "?lat={}&lon={}&appid={}&units=imperial"
        ).format(LAT, LON, OWM_KEY)
        data = fetch_json(url)
        temp = int(round(data["main"]["temp"]))
        weather_cond_main = data["weather"][0]["main"]
        cond_id = data["weather"][0].get("id", 0)
        raw_desc = data["weather"][0].get("description", weather_cond_main)
        weather_cond = get_condition_text(cond_id, raw_desc)
        weather_str = "{}{}F".format(temp, chr(176))  # degree symbol
        # Wind: speed in mph + compass direction
        _wind_speed = int(round(data.get("wind", {}).get("speed", 0)))
        wind_deg = data.get("wind", {}).get("deg", 0)
        wind_dir = heading_to_compass(wind_deg)
        wind_str = "{}mph {}".format(_wind_speed, wind_dir)
        # Time + DST sync via the proxy. Each call absolute-resets the RTC,
        # so DST flips and small drift are picked up automatically. If the
        # proxy is unreachable the existing _tz_offset_secs (either the
        # static secrets value or the last-known-good proxy value) is good
        # enough until the next attempt.
        prev_tz = _tz_offset_secs
        if _try_proxy_time_sync() and _tz_offset_secs != prev_tz:
            device_log("TZ shift (UTC{:+d})".format(_tz_offset_secs // 3600))
        # Sunrise/sunset for brightness control (local time as minutes)
        sr = data.get("sys", {}).get("sunrise", 0)
        ss = data.get("sys", {}).get("sunset", 0)
        if sr and ss:
            sr_local = (sr + _tz_offset_secs) % 86400  # seconds into local day
            ss_local = (ss + _tz_offset_secs) % 86400
            _sunrise_mins = sr_local // 60
            _sunset_mins = ss_local // 60
        device_log("Wx:{} {} {}".format(weather_str, weather_cond, wind_str))
    except Exception as e:
        device_log("Wx err:{}".format(e))
        fetch_failed()
        if not weather_str:
            weather_str = "N/A"
            weather_cond = "No Data"
            weather_cond_main = ""
            wind_str = ""
    gc.collect()


def fetch_forecast():
    """Pull today/tomorrow/day-after high/low/cond/wind from the proxy.
    The proxy already caches OpenWeatherMap's 5-day endpoint for 1h, so the
    device just mirrors that cadence."""
    global forecast_days
    gc.collect()
    try:
        if LOCATION_NAME:
            url = "{}/api/v2/forecast?loc={}".format(PROXY_HOST, LOCATION_NAME)
        else:
            url = "{}/api/forecast".format(PROXY_HOST)
        data = fetch_json(url)
        forecast_days = data.get("days") or []
        device_log("Fcst:{}d".format(len(forecast_days)))
    except Exception as e:
        device_log("Fcst err:{}".format(e))
        fetch_failed()
    gc.collect()


def fetch_tides():
    """Fetch today + tomorrow's tide predictions from NOAA in one request and
    store them as (abs_secs, type, hour, minute_str). Using a 2-day window
    means the next upcoming tide is always in the list (no fall-through to a
    second request) and the basin-level interpolation works across midnight.

    NOTE: NOAA's `date` param only accepts `today`, `latest`, `recent`. To get
    a specific day or range you must use `begin_date`/`end_date` — passing
    `date=tomorrow` silently returns today's data."""
    global tide_str, tide_type_val, _tide_predictions
    gc.collect()
    try:
        now = time.localtime()
        today_str = "{:04d}{:02d}{:02d}".format(now.tm_year, now.tm_mon, now.tm_mday)
        tmr = time.localtime(time.mktime(now) + 86400)
        tmr_str = "{:04d}{:02d}{:02d}".format(tmr.tm_year, tmr.tm_mon, tmr.tm_mday)
        url = (
            "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
            "?begin_date={}&end_date={}&station={}&product=predictions&datum=MLLW"
            "&time_zone=lst_ldt&interval=hilo&units=english&format=json"
        ).format(today_str, tmr_str, NOAA_STATION)
        preds = fetch_json(url).get("predictions", [])
        now_secs = time.mktime(now)

        _tide_predictions = []
        for p in preds:
            ts = p["t"]                        # e.g. "2026-05-09 18:12"
            d_part, t_part = ts.split(" ")
            y, mo, d = [int(x) for x in d_part.split("-")]
            h_str, m_str = t_part.split(":")
            h, m = int(h_str), int(m_str)
            secs = time.mktime((y, mo, d, h, m, 0, 0, 0, 0))
            _tide_predictions.append((secs, p.get("type", ""), h, m_str))

        next_p = None
        for p in _tide_predictions:
            if p[0] >= now_secs:
                next_p = p
                break
        if next_p:
            tide_type_val = next_p[1]
            h12 = next_p[2] % 12 or 12
            tide_str = "{}:{}".format(h12, next_p[3])
            device_log("Tide:{} {}".format(tide_type_val, tide_str))
        else:
            tide_str = "N/A"
            tide_type_val = ""
        # Calculate basin level — the per-tick block redraws it each frame
        interpolate_tide_level()
    except Exception as e:
        device_log("Tide err:{}".format(e))
        fetch_failed()
        if not tide_str:
            tide_str = "N/A"
            tide_type_val = ""
    gc.collect()



def fetch_planes():
    global planes
    gc.collect()
    try:
        if LOCATION_NAME:
            url = "{}/api/v2/planes?loc={}".format(PROXY_HOST, LOCATION_NAME)
        else:
            url = "{}/api/planes".format(PROXY_HOST)
        data = fetch_json(url)
        # Proxy returns positional arrays: [call, icao24, alt, spd, hdg, vrate]
        # Avoids ~180 bytes/plane of string-key interning vs named-key dicts.
        planes = data.get("planes") or []
        if data.get("rate_limited"):
            device_log("Planes:rate-limited")
        elif data.get("upstream_error"):
            device_log("Planes:upstream {}".format(data["upstream_error"]))
        else:
            device_log("Planes:{}".format(len(planes)))
    except MemoryError:
        device_log("Planes: response too large")
        fetch_failed()
        planes = []
    except Exception as e:
        device_log("Planes err:{}".format(e))
        fetch_failed()
        planes = []
    gc.collect()


_last_health_issues = None  # last known issue list — used to log only on change
_consecutive_bad_polls = 0  # pixel only lights after 2 in a row, to absorb blips

def fetch_health():
    """Poll /api/health and toggle the bottom-right red pixel based on the
    proxy's reported issues. Requires 2 consecutive bad polls before lighting
    the pixel; one good poll clears it. Logs every state change."""
    global _last_health_issues, _consecutive_bad_polls
    try:
        url = "{}/api/health".format(PROXY_HOST)
        data = fetch_json(url)
        issues = data.get("issues") or []
        if issues:
            _consecutive_bad_polls += 1
        else:
            _consecutive_bad_polls = 0
        set_health_indicator(_consecutive_bad_polls >= 2)
        if issues != _last_health_issues:
            device_log("Health:{}".format(",".join(issues) if issues else "ok"))
            _last_health_issues = issues
    except Exception as e:
        # Can't reach the proxy → that's also a problem worth flagging.
        _consecutive_bad_polls += 1
        set_health_indicator(_consecutive_bad_polls >= 2)
        marker = ["proxy_unreachable"]
        if marker != _last_health_issues:
            device_log("Health err:{}".format(e))
            _last_health_issues = marker


def fetch_status():
    """Poll /api/status and cache the pre-normalized provider levels the status
    board renders. A failure just leaves the last-known data in place (and logs)
    rather than counting toward the fetch-error reboot threshold — a status feed
    hiccup should never reboot the display."""
    global status_providers, status_worst, _last_status_summary
    try:
        data = fetch_json("{}/api/status".format(PROXY_HOST))
        status_providers = data.get("providers") or []
        status_worst = data.get("worst", 0)
        summary = "{}p/w{}".format(len(status_providers), status_worst)
        if summary != _last_status_summary:
            device_log("Status:{}".format(summary))
            _last_status_summary = summary
    except Exception as e:
        device_log("Status err:{}".format(e))


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

# Centering helpers for the right panel (x=17 to x=63, 47px wide)
# Right-column panel coordinates (sky-mode native layout). The planet card
# ends at native x=39 so we leave a 4 px gap before weather text starts.
_RIGHT_START = 44
_RIGHT_W = 128 - _RIGHT_START   # = 84 px

# Per-label effective glyph width. Each entry is (font_w * label_scale) so
# the centering helpers don't have to introspect the Label object.
_CHAR_W_CLOCK = 10   # FONT_MID (5) × scale=2
_CHAR_W_TEMP  = 10
_CHAR_W_COND  = 5    # FONT_MID native
_CHAR_W_WIND  = 4    # FONT_SMALL native


def _center_right(label, text, char_w):
    """Center a label in the right column at native pixel scale."""
    label.text = text
    label.x = _RIGHT_START + (_RIGHT_W - len(text) * char_w) // 2


# Legacy names — preserved so older call sites keep working. The two ship
# helpers still operate on the 64×32 scale=2 plane group (unchanged).
def _center_mid(label, text):
    if BASIN_MODE == "sky":
        # Bottom-strip labels live at fixed x positions; just update text.
        label.text = text
        return
    if label is clock_label or label is temp_label:
        _center_right(label, text, _CHAR_W_CLOCK)
    else:
        _center_right(label, text, _CHAR_W_COND)

def _center_small(label, text):
    if BASIN_MODE == "sky":
        label.text = text
        return
    if label is cond_label:
        _center_right(label, text, _CHAR_W_COND)
    else:
        _center_right(label, text, _CHAR_W_WIND)

def _center_ship(label, text):
    label.text = text
    label.x = 16 + (48 - len(text) * 4) // 2

def _center_ship_mid(label, text):
    label.text = text
    label.x = 16 + (48 - len(text) * 5) // 2

# ---------------------------------------------------------------------------
# Ship type colors and display
# ---------------------------------------------------------------------------
SHIP_TYPE_COLORS = {
    3: 0x44AA44,   # Fishing — green
    4: 0xFF8800,   # High-speed — orange
    5: 0xAAAA00,   # Special (tugs, pilots) — olive
    6: 0x44AAFF,   # Passenger — blue
    7: 0xCC8844,   # Cargo — brown
    8: 0xFF4444,   # Tanker — red
    9: 0x888888,   # Other — gray
}

def get_ship_type_color(type_code):
    decade = type_code // 10 if type_code else 0
    return SHIP_TYPE_COLORS.get(decade, 0x666688)


def _ship_display_secs(name):
    """Seconds to show this ship: enough for the name to fully scroll + 2s end pause."""
    n = len(name)
    if n <= 9:
        return 15
    # 2s start pause + one tick per scroll step + 2s end pause
    return max(15, 2 + (n - 9) + 2)


def fetch_ships():
    """Fetch nearby ships from proxy."""
    global ships
    gc.collect()
    try:
        url = "{}/api/ships".format(PROXY_HOST)
        data = fetch_json(url)
        ships = data.get("ships", [])
        device_log("Ships:{}".format(len(ships)))
    except Exception as e:
        device_log("Ships err:{}".format(e))
        fetch_failed()
        ships = []
    gc.collect()


def show_ship(ship):
    """Display a ship — reuses the plane screen group to save RAM.
    Wrapped in try/except so a label-realloc MemoryError just skips this
    render instead of crashing the device."""
    global _ship_hull_params, _ship_anim_tick
    try:
        gc.collect()
        switch_screen("plane")
        name = ship.get("name", "UNKNOWN")
        type_name = ship.get("type_name", "Vessel")
        type_code = ship.get("type", 0)
        dest = ship.get("destination", "")
        color = get_ship_type_color(type_code)
        length = ship.get("length", 50)

        # Draw ship silhouette in left column, scaled by length.
        # Map 30-300m → 10-28px tall, centered vertically.
        ship_h = max(10, min(28, int(10 + (length - 30) * 18 / 270)))
        ship_w = max(4, min(10, ship_h // 3 + 2))
        y_start = (32 - ship_h) // 2
        bow_len = max(2, ship_h // 5)
        cx = 7
        super_rows = max(2, ship_h * 3 // 10)

        pl_bg_pal[1] = _dim(0x001237)   # ocean deep
        pl_bg_pal[2] = _dim(0xBBBBCC)   # hull (light blue-gray)
        pl_bg_pal[3] = _dim(0x003264)   # ocean mid
        pl_bg_pal[4] = _dim(0x125A96)   # ocean surface
        pl_bg_pal[5] = _dim(color)      # superstructure (ship type color)

        # Fill ocean background, then draw hull — update_ship_ocean animates it
        for y in range(32):
            for x in range(14):
                pl_bg_bmp[x, y] = 1
        for i in range(ship_h):
            y = y_start + i
            if y < 0 or y > 31:
                continue
            if i < bow_len:
                hw = max(1, ship_w * (i + 1) // (bow_len + 1) // 2)
            elif i >= ship_h - 2:
                hw = ship_w // 2 - 1
            else:
                hw = ship_w // 2
            pal_idx = 5 if bow_len <= i < bow_len + super_rows else 2
            for x in range(cx - hw, cx + hw + 1):
                if 0 <= x < 14:
                    pl_bg_bmp[x, y] = pal_idx

        _ship_hull_params = (y_start, ship_h, bow_len, ship_w, cx)
        _ship_anim_tick = 0

        actype_label.text = ""
        logo_label.text = ""
        route_label.text = ""

        global _ship_name_full, _ship_name_phase
        # Upgrade to mid font for ship name/type/dest rows
        airline_label.font = FONT_MID
        alt_label.font = FONT_MID
        reg_label.font = FONT_MID

        _ship_name_full = name
        _ship_name_phase = 0
        _n = len(name)
        if _n <= 9:
            airline_label.text = name
            airline_label.x = 16 + (48 - _n * 5) // 2
        else:
            airline_label.text = name[:9]
            airline_label.x = 16
        airline_label.color = _dim(0xFFFFFF)
        airline_label.y = 5

        # Row 2: type name and/or length in feet. AIS reports length in
        # meters; we convert and round. "Vessel" is the placeholder for
        # type=0/unknown — when length is known we show that instead.
        global _ship_alt_enabled, _ship_alt_type_text, _ship_alt_length_text
        global _ship_alt_showing_type, _ship_alt_last_switch
        length_ft = int(length * 3.28084) if length else 0
        type_known = bool(type_name) and type_name != "Vessel" and type_code != 0
        if type_known and length_ft:
            _ship_alt_enabled = True
            _ship_alt_type_text = type_name[:9]
            _ship_alt_length_text = "{}ft".format(length_ft)
            _ship_alt_showing_type = True
            _ship_alt_last_switch = time.monotonic()
            _center_ship_mid(reg_label, _ship_alt_type_text)
        elif length_ft:
            _ship_alt_enabled = False
            _center_ship_mid(reg_label, "{}ft".format(length_ft))
        else:
            _ship_alt_enabled = False
            _center_ship_mid(reg_label, type_name[:9])
        reg_label.color = _dim(color)
        reg_label.y = 13

        if dest:
            _center_ship_mid(alt_label, dest[:9])
        else:
            alt_label.text = ""
        alt_label.color = _dim(0x8899AA)
        alt_label.y = 21

        dist = ship.get("distance_mi", 0)
        hdg = ship.get("heading", 0)
        compass = heading_to_compass(hdg)
        info = "{}mi {}".format(dist, compass) if dist else compass
        actype_label.font = FONT_SMALL
        _center_ship(actype_label, info)
        actype_label.color = _dim(0x6699AA)
        actype_label.y = 29
    except MemoryError as _e:
        print("show_ship MemoryError:", _e)
        gc.collect()


def show_forecast():
    """Render the 3-day forecast card. Vertical columns: day name, weather
    glyph, condition text, high temp, wind speed, wind direction."""
    try:
        switch_screen("forecast")
        device_log("Fcst show:{}d".format(len(forecast_days)))
        # Wipe glyph zone + redraw separator lines. Text labels self-update.
        for y in range(FORECAST_H):
            for x in range(FORECAST_W):
                forecast_bmp[x, y] = 0
        for sy in range(FORECAST_H):
            forecast_bmp[42, sy] = 6
            forecast_bmp[85, sy] = 6

        # Empty-data fallback: clear all labels and surface a single
        # "NO FORECAST DATA" line centered on the panel. This still confirms
        # the rotation slot is firing — without it, an empty forecast_days
        # would render as a near-black card and feel like a no-op.
        if not forecast_days:
            for col_labels in _forecast_labels:
                for lbl in col_labels:
                    lbl.text = ""
            _forecast_labels[1][0].text = "NO DATA"
            return

        for col in range(3):
            day_lbl, cond_lbl, hi_lbl, wind_s_lbl, wind_d_lbl = _forecast_labels[col]
            if col < len(forecast_days):
                d = forecast_days[col]
                cond_id = d.get("cond_id", 800)
                day_lbl.text = _forecast_day_label(col)
                cond_lbl.text = get_condition_text(cond_id, d.get("cond", "Clear"))[:10]
                hi_lbl.text = "{}{}F".format(d.get("hi", "?"), chr(176))
                wind_s_lbl.text = "{}mph".format(d.get("wind", 0))
                wind_d_lbl.text = heading_to_compass(d.get("wind_deg", 0))
                _draw_forecast_glyph(_FCOL_CENTERS[col], _FGLYPH_CY, cond_id)
            else:
                day_lbl.text = ""
                cond_lbl.text = ""
                hi_lbl.text = ""
                wind_s_lbl.text = ""
                wind_d_lbl.text = ""
    except MemoryError as _e:
        print("show_forecast MemoryError:", _e)
        gc.collect()


def show_weather_tides():
    """Render the current weather + tide screen. Wrapped in try/except so
    a label-realloc MemoryError just skips this render instead of crashing."""
    try:
        switch_screen("weather")
        _center_mid(temp_label, weather_str)
        try:
            temp_val = int(weather_str.split(chr(176))[0])
        except (ValueError, IndexError):
            temp_val = 60
        if temp_val >= 90:   tc = 0xFF2222
        elif temp_val >= 70: tc = 0xFFDD00
        elif temp_val >= 50: tc = 0x88FFCC
        elif temp_val >= 30: tc = 0x44AAFF
        else:                tc = 0x2255CC
        temp_label.color = _dim(tc)
        _center_small(cond_label, weather_cond[:10])
        _center_small(wind_label, wind_str)
        if BASIN_MODE == "sky":
            # Planet-card labels are owned by update_basin_planets() — it
            # writes them on every glyph swap. Nothing to do here.
            pass
        else:
            # Tide time / HIGH / LOW at bottom of left column. Slack window
            # is ±15 minutes, expressed in seconds since predictions are absolute.
            now_secs = time.mktime(time.localtime())
            slack_label = ""
            for p in _tide_predictions:
                if abs(p[0] - now_secs) <= 900:
                    slack_label = "HIGH" if p[1] == "H" else "LOW"
                    break
            tide_time_label.text = slack_label if slack_label else tide_str
            tide_time_label.color = _dim(0xFFFFFF if (slack_label or _tide_level < 0.2) else 0x00CCDD)
    except MemoryError as _e:
        print("show_weather_tides MemoryError:", _e)
        gc.collect()


def has_route(callsign):
    """Check if a plane has route data in the cache."""
    route = flight_cache.get(callsign, {})
    return route.get("origin", "???") != "???" and route.get("dest", "???") != "???"


def get_displayable_planes():
    """Return only planes that have route data.
    Plane format from proxy: [call, icao24, alt, spd, hdg, vrate]"""
    result = []
    for p in planes:
        call = p[0]
        if call not in flight_cache:
            fetch_route(call, p[1])
        if has_route(call):
            result.append(p)
    return result


def show_plane(plane):
    """Render plane info. Wrapped in try/except so a label-realloc MemoryError
    just skips this render instead of crashing.
    Plane format from proxy: [call, icao24, alt, spd, hdg, vrate]"""
    try:
        gc.collect()
        switch_screen("plane")
        # Reset bg pixels (show_ship may have rewritten them) + label layout
        for _y in range(32):
            for _x in range(14):
                pl_bg_bmp[_x, _y] = 2 if (_x == 0 or _x == 13 or _y == 0 or _y == 31) else 1
        # Restore fonts (show_ship may have changed them)
        airline_label.font = FONT_MID
        alt_label.font = FONT_MID
        reg_label.font = FONT_SMALL
        actype_label.font = FONT_SMALL

        airline_label.y = 13; airline_label.x = 16
        alt_label.y     = 20; alt_label.x     = 16; alt_label.color = _dim(0x44AA44)
        actype_label.y  = 27; actype_label.x  = 16
        reg_label.y     = 27; reg_label.x     = 16; reg_label.color = _dim(0x667788)
        logo_label.y    = 16; logo_label.x    = 2

        callsign = plane[0]
        name, iata, color = get_airline_info(callsign)
        update_plane_bg(color)

        logo_label.text = iata
        logo_label.x = 1 + (14 - len(iata) * 6) // 2
        bright = ((color >> 16) & 0xFF) * 0.299 + ((color >> 8) & 0xFF) * 0.587 + (color & 0xFF) * 0.114
        logo_label.color = _dim(0x111111 if bright > 140 else 0xFFFFFF)

        # Safety net: if the cache entry is gone (evicted by a sibling
        # fetch_route call during get_displayable_planes' iteration) or
        # empty for any other reason, repopulate it synchronously so the
        # display never shows a callsign with blank origin/dest/type/reg.
        route = flight_cache.get(callsign, {})
        if not route.get("origin") or route.get("origin") == "???":
            fetch_route(callsign, plane[1])
            route = flight_cache.get(callsign, {})
        route_label.text = "{}>{}".format(route.get("origin", ""), route.get("dest", ""))

        airline_label.text = name[:8]
        airline_label.color = _dim(color)

        alt_k = plane[2] // 1000
        alt_label.text = "{}k {}".format(alt_k, heading_to_compass(plane[4])) if alt_k > 0 else ""

        # Row 4: type (left) + registration (right-aligned)
        ac_type = route.get("type", "")
        actype_label.text = ac_type
        actype_label.color = _dim(0x55AADD)
        reg = route.get("reg", "") or ""
        reg_label.text = reg
        if reg:
            reg_label.x = max(16, 64 - len(reg) * 4)
    except MemoryError as _e:
        print("show_plane MemoryError:", _e)
        gc.collect()


def _demo_advance():
    """Advance to the next demo view: weather → plane → ship → weather…"""
    global _demo_step, _demo_weather_idx, _demo_plane_idx, _demo_ship_idx
    global weather_str, weather_cond, weather_cond_main, wind_str, _wind_speed
    global tide_str, tide_type_val, _tide_level, _tide_predictions
    global planes, ships, showing_planes, _showing_ship
    _demo_step = (_demo_step + 1) % 3
    if _demo_step == 0:                        # weather
        w = _DEMO_WEATHER[_demo_weather_idx % len(_DEMO_WEATHER)]
        _demo_weather_idx += 1
        weather_str = w[0]; weather_cond = w[1]; weather_cond_main = w[2]
        _wind_speed = w[3]; wind_str = "{}mph {}".format(w[3], w[4])
        _tide_level = w[5]; tide_type_val = w[6]
        tide_str = "4:30"; _tide_predictions = []
        planes = []; ships = []
        showing_planes = False; _showing_ship = False
        show_weather_tides()
        print("Demo weather:", weather_str, weather_cond)
    elif _demo_step == 1:                      # plane
        p = _DEMO_PLANES[_demo_plane_idx % len(_DEMO_PLANES)]
        _demo_plane_idx += 1
        call = p[0]
        planes = [[call, "", p[1], p[2], p[3], 0]]
        flight_cache[call] = {"origin": p[4], "dest": p[5], "type": p[6], "reg": p[7]}
        showing_planes = True; _showing_ship = False
        show_plane(planes[0])
        print("Demo plane:", call, p[4], ">", p[5])
    else:                                      # ship
        s = _DEMO_SHIPS[_demo_ship_idx % len(_DEMO_SHIPS)]
        _demo_ship_idx += 1
        ships = [s]; planes = []
        showing_planes = False; _showing_ship = True
        show_ship(s)
        print("Demo ship:", s["name"])



# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

if SHIPS_TEST:
    ships = [
        {"name": "IYANOUGH", "type": 40, "type_name": "HighSpeed",
         "destination": "NANTUCKET", "length": 47, "distance_mi": 2.3, "heading": 135},
        {"name": "MSC FLORA", "type": 70, "type_name": "Cargo",
         "destination": "NEW YORK", "length": 280, "distance_mi": 8.1, "heading": 220},
        {"name": "SEA TITAN", "type": 80, "type_name": "Tanker",
         "destination": "HOUSTON", "length": 220, "distance_mi": 5.7, "heading": 45},
    ]
    _ship_cycle_start = time.monotonic() - SHIP_WEATHER_SECS  # skip to ship phase
    gc.collect()
    print("Free mem:", gc.mem_free())
    show_ship(ships[0])
    display.brightness = 1.0
    print("SHIPS_TEST: injected", len(ships), "test ships, showing first")

if DEMO_MODE:
    print("DEMO MODE — cycling test fixtures, no network needed")
    display.brightness = 1.0
    _demo_advance()
    _demo_last_switch = time.monotonic()

while True:
    gc.collect()
    now = time.monotonic()

    # --- Daily 03:30 reboot — clears accumulated BDF glyph cache, socket
    # state, and other gradual leaks. Uptime > 1h check prevents a reboot
    # loop if the device starts inside the 03:30 window.
    _t = time.localtime()
    if _t.tm_hour == 3 and _t.tm_min == 30 and now > 3600:
        device_log("Daily reboot")
        flush_device_log()
        time.sleep(1)
        microcontroller.reset()

    if DEMO_MODE:
        if now - _demo_last_switch >= DEMO_INTERVAL:
            _demo_advance()
            _demo_last_switch = now
    else:
        # --- Weather + Tides/Sky refresh ---
        if now - last_weather_fetch >= WEATHER_INTERVAL:
            if ENABLE_WEATHER:
                fetch_weather()
            if BASIN_MODE == "sky":       # sky == astronomy enabled
                fetch_sky()
            elif ENABLE_TIDE:
                fetch_tides()
            flush_device_log()
            last_weather_fetch = now
            if not showing_planes and not _forecast_showing and not _showing_status:
                show_weather_tides()

        # --- 3-day forecast refresh (sky mode only — the only rotation slot
        # that surfaces it). Independent cadence from current-weather since
        # the daily outlook only changes a few times a day.
        if BASIN_MODE == "sky" and now - last_forecast_fetch >= FORECAST_INTERVAL:
            fetch_forecast()
            last_forecast_fetch = now

        # --- Forecast card show/hide. Sky list view raises _forecast_pending
        # on exit; we honour it here unless a plane or ship is currently
        # owning the screen. Dwell expires → return to weather/tides.
        if _forecast_pending and not showing_planes and not _showing_ship and not _showing_status:
            _forecast_pending = False
            _forecast_showing = True
            _forecast_started_at = now
            show_forecast()
        if _forecast_showing and now - _forecast_started_at >= FORECAST_DWELL_SECS:
            _forecast_showing = False
            show_weather_tides()

        # --- Proxy health check (drives the bottom-right red pixel) ---
        if PROXY_HOST and now - last_health_fetch >= HEALTH_INTERVAL:
            fetch_health()
            last_health_fetch = now

        # --- Service status board refresh (128x64 only) ---
        if ENABLE_STATUS and PROXY_HOST and now - last_status_fetch >= STATUS_INTERVAL:
            fetch_status()
            last_status_fetch = now

        # --- OpenSky check ---
        # Skip plane fetches during quiet hours to save FlightAware API calls.
        # Clears any cached planes so the display falls back to weather/tides.
        _hr = time.localtime().tm_hour
        _quiet = PLANE_QUIET_START_HR <= _hr < PLANE_QUIET_END_HR
        if PLANES_ENABLED and not _quiet and now - last_sky_fetch >= OPENSKY_INTERVAL:
            fetch_planes()
            last_sky_fetch = now
        elif _quiet and planes:
            planes = []

        # Only show planes that have route data
        display_planes = get_displayable_planes() if PLANES_ENABLED else []

        # Safeguard: if a plane has been on screen for PLANE_MAX_SECS straight
        # (e.g. fetch issue, hovering aircraft, stale ADS-B data), force a
        # weather break so the user is never permanently stuck on a plane.
        if showing_planes and now - plane_screen_started_at >= PLANE_MAX_SECS:
            device_log("Plane max, weather break")
            showing_planes = False
            plane_cooldown_until = now + PLANE_COOLDOWN_SECS
            show_weather_tides()

        if display_planes and not showing_planes and now >= plane_cooldown_until:
            showing_planes = True
            # If a ship was on screen when the plane preempted it, clear the
            # flag — otherwise the ship per-tick block keeps animating
            # update_ship_ocean over pl_bg_bmp, clobbering the plane logo.
            _showing_ship = False
            _forecast_showing = False    # plane takes over the screen group
            _showing_status = False      # plane preempts the status board too
            plane_idx = 0
            last_plane_cycle = now
            plane_screen_started_at = now
            device_log("Plane:{}".format(display_planes[0][0]))
            show_plane(display_planes[0])
        elif not display_planes and showing_planes:
            showing_planes = False
            show_weather_tides()

        # --- Cycle through multiple planes ---
        if showing_planes and len(display_planes) > 1:
            if now - last_plane_cycle >= PLANE_CYCLE_SECS:
                plane_idx = (plane_idx + 1) % len(display_planes)
                show_plane(display_planes[plane_idx])
                last_plane_cycle = now

        # --- Ship tracking ---
        if SHIPS_ENABLED and not showing_planes:
            if not SHIPS_TEST and now - last_ship_fetch >= SHIP_INTERVAL:
                fetch_ships()
                last_ship_fetch = now
                if ships and _ship_cycle_start == 0:
                    _ship_cycle_start = now

            # Weather/ship cycling: 30s weather, then each ship for its computed duration, repeat
            if ships:
                _ship_secs = [_ship_display_secs(s.get("name", "")) for s in ships]
                ship_display_total = sum(_ship_secs)
                cycle_pos = (now - _ship_cycle_start) % (SHIP_WEATHER_SECS + ship_display_total)
                if cycle_pos < SHIP_WEATHER_SECS:
                    # Weather phase
                    if _showing_ship:
                        _showing_ship = False
                        show_weather_tides()
                else:
                    # Ship phase — find which ship based on cumulative display time
                    ship_phase_elapsed = cycle_pos - SHIP_WEATHER_SECS
                    cumulative = 0
                    expected_idx = len(ships) - 1
                    for _i, _d in enumerate(_ship_secs):
                        if ship_phase_elapsed < cumulative + _d:
                            expected_idx = _i
                            break
                        cumulative += _d
                    if not _showing_ship:
                        _showing_ship = True
                        _forecast_showing = False    # ship takes over the screen group
                        _showing_status = False      # ship preempts the status board too
                        ship_idx = expected_idx
                        device_log("Ship:{} {}mi".format(ships[ship_idx].get("name","?")[:12], ships[ship_idx].get("distance_mi","?")))
                        show_ship(ships[ship_idx])
                    elif expected_idx != ship_idx:
                        ship_idx = expected_idx
                        show_ship(ships[ship_idx])
            elif _showing_ship:
                # Ship sailed out of range. The cycling logic above only runs
                # while `ships` is non-empty, so without this branch we'd be
                # stuck on the ship screen forever — the per-tick clock/basin
                # block is gated on `not _showing_ship`, so the display would
                # freeze on the last-rendered ship frame.
                _showing_ship = False
                _ship_cycle_start = 0
                show_weather_tides()
                device_log("Ship gone, weather")

        # --- Service status board rotation (128x64 only) ---
        # The summary card enters the rotation every STATUS_SHOW_EVERY seconds of
        # rest; when providers are degraded/down, each one's incident card is
        # walked after the summary. Planes and ships preempt (handled above), so
        # this only runs on the resting screen.
        if (ENABLE_STATUS and status_providers and not showing_planes
                and not _showing_ship and not _forecast_showing):
            if not _showing_status and now - _status_last_shown >= STATUS_SHOW_EVERY:
                _showing_status = True
                _status_phase = 0
                _status_incidents = [i for i, p in enumerate(status_providers)
                                     if p.get("level", 0) >= 1]
                _status_started_at = now
                show_status_summary()
            elif _showing_status and now - _status_started_at >= STATUS_DWELL_SECS:
                if _status_phase < len(_status_incidents):
                    _p = status_providers[_status_incidents[_status_phase]]
                    _status_phase += 1
                    _status_started_at = now
                    show_status_incident(_p)
                else:
                    _showing_status = False
                    _status_last_shown = now
                    show_weather_tides()

    # Per-tick updates: clock + basin wave animation + tide direction pixel.
    # Wrapped in try/except so a transient MemoryError just skips this frame
    # instead of propagating to the top-level loop and crashing the device.
    # gc.collect() first to maximize the largest contiguous free block.
    # NOTE: do NOT call fetch_failed() here — render MemoryErrors are normal
    # and must not count toward the auto-reboot threshold.
    if not showing_planes and not _showing_ship and not _forecast_showing and not _showing_status:
        try:
            gc.collect()
            t = time.localtime()
            h12 = t.tm_hour % 12 or 12
            ampm = "A" if t.tm_hour < 12 else "P"
            _center_mid(clock_label, "{}:{:02d} {}M".format(h12, t.tm_min, ampm))
            _basin_anim_tick += 1
            if BASIN_MODE == "sky":
                update_basin_planets()
                sep_pixel_tg.hidden = True
            else:
                update_basin_water(_tide_level, _basin_anim_tick)
                _at_slack = tide_time_label.text in ("HIGH", "LOW")
                if _at_slack:
                    _now_secs = time.mktime(t)
                    _still_slack = False
                    for _p in _tide_predictions:
                        if abs(_p[0] - _now_secs) <= 900:
                            _still_slack = True
                            break
                    if not _still_slack:
                        tide_time_label.text = tide_str
                        tide_time_label.color = _dim(0x00CCDD)
                        _at_slack = False
                sep_pixel_tg.hidden = _at_slack
                if not _at_slack:
                    if tide_type_val == "H":
                        _sep_pixel_y = (_sep_pixel_y - 1) % 32
                    elif tide_type_val == "L":
                        _sep_pixel_y = (_sep_pixel_y + 1) % 32
                    sep_pixel_tg.y = _sep_pixel_y
            update_brightness()
        except MemoryError as _e:
            print("per-tick MemoryError:", _e)
            gc.collect()

    if _showing_ship:
        try:
            gc.collect()
            _ship_anim_tick += 1
            update_ship_ocean(_ship_anim_tick)
            if _ship_alt_enabled and time.monotonic() - _ship_alt_last_switch >= 3.0:
                _ship_alt_showing_type = not _ship_alt_showing_type
                _ship_alt_last_switch = time.monotonic()
                _center_ship_mid(
                    reg_label,
                    _ship_alt_type_text if _ship_alt_showing_type else _ship_alt_length_text,
                )
            _n = len(_ship_name_full)
            if _n > 9:
                _ship_name_phase += 1
                _scroll_steps = _n - 9
                _cycle_len = 2 + _scroll_steps + 2
                _pos = _ship_name_phase % _cycle_len
                if _pos < 2:
                    _char_start = 0
                elif _pos < 2 + _scroll_steps:
                    _char_start = _pos - 2
                else:
                    _char_start = _scroll_steps
                _new_text = _ship_name_full[_char_start:_char_start + 9]
                if airline_label.text != _new_text:
                    airline_label.text = _new_text
        except MemoryError as _e:
            print("ship-anim MemoryError:", _e)
            gc.collect()

    # --- Button handling ---
    if not btn_down.value and DEMO_MODE:  # pressed (active low)
        _demo_advance()
        _demo_last_switch = now
        time.sleep(0.3)  # debounce
    if not btn_up.value:                  # pressed (active low)
        force_weather_screen()
        time.sleep(0.3)                   # debounce

    # Pet the watchdog. If we never get here (network hang, infinite loop,
    # etc.), the device hard-resets after WATCHDOG_TIMEOUT seconds.
    if _WATCHDOG_OK:
        try:
            microcontroller.watchdog.feed()
        except Exception:
            pass

    time.sleep(1)
