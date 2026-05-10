# Matrix Portal — Tides, Weather, Aircraft, and Ship Tracker
# Hardware: Adafruit MatrixPortal (M4 or S3) + 64x32 RGB LED Matrix
# See device/SETUP.md for the full library list and setup walkthrough.

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
OPENSKY_INTERVAL = 60
HEALTH_INTERVAL = 300       # poll proxy /api/health every 5 minutes
WATCHDOG_TIMEOUT = 90       # hard-reset if the main loop hasn't fed for this long
PLANE_CYCLE_SECS = 5
PLANE_MAX_SECS = 600          # max continuous time on plane screen
PLANE_COOLDOWN_SECS = 60      # weather break after PLANE_MAX_SECS hits
PLANE_QUIET_START_HR = 1      # local hour to stop fetching planes (saves API)
PLANE_QUIET_END_HR = 5        # local hour to resume fetching planes
PLANES_ENABLED = True
SHIPS_ENABLED = True    # Set True to enable ship tracking
SHIPS_TEST = False
SHIP_INTERVAL = 60      # poll for ships every 60 sec
SHIP_WEATHER_SECS = 30  # show weather for 30s in cycle
DEMO_MODE = False       # Set True to auto-cycle test fixtures (no network needed)
DEMO_INTERVAL = 30      # seconds per view in demo mode

# HTTP proxy on Raspberry Pi — bypasses ESP32 TLS limitation for OpenSky
PROXY_HOST = secrets.get("proxy_host", "")       # e.g. "http://YOUR_PI_IP:6590"
# Shared secret sent as X-Device-Secret on POST /api/devicelog. Must match
# the proxy's device_secret. Empty here = device sends no header (proxy
# only enforces if its config also has device_secret set).
DEVICE_SECRET = secrets.get("device_secret", "")

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
    global planes, showing_planes
    planes = []
    showing_planes = False
    show_weather_tides()

# ---------------------------------------------------------------------------
# Display setup — 64x32, using displayio directly for icons + text
# ---------------------------------------------------------------------------
mp = MatrixPortal(status_neopixel=board.NEOPIXEL, bit_depth=4, debug=False)

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
basin_bmp = displayio.Bitmap(BASIN_W, BASIN_H, 10)
basin_pal = displayio.Palette(10)
basin_pal[0] = 0x000000
basin_pal[1] = 0x001237   # water deep (navy)
basin_pal[2] = 0x003264   # water mid (ocean blue)
basin_pal[3] = 0x125A96   # water surface/crest (bright blue)
basin_pal[4] = 0xBBBBBB   # ship hull (light gray)
basin_pal[5] = 0xFF8822   # ship superstructure (amber)
basin_pal[6] = 0x232335   # dim star (night sky)
basin_pal[7] = 0xFFCC00   # sun / lightning yellow
basin_pal[8] = 0xBBBBCC   # cloud / snow gray
basin_pal[9] = 0x2255AA   # rain blue

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
    r = ((airline_color >> 16) & 0xFF)
    g = ((airline_color >> 8) & 0xFF)
    b = (airline_color & 0xFF)
    pl_bg_pal[1] = airline_color
    pl_bg_pal[2] = ((r >> 2) << 16) | ((g >> 2) << 8) | (b >> 2)


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


# Route cache: callsign -> {"origin": "BOS", "dest": "JFK"}
flight_cache = {}
_FLIGHT_CACHE_MAX = 10


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
weather_group = displayio.Group()

# LEFT COLUMN: tide water fill (full column, animated)
weather_group.append(basin_tg)

# Tide time at top of column — tiny font
tide_time_label = Label(FONT_SMALL, text="", color=0x00CCDD, x=1, y=29)
weather_group.append(tide_time_label)

# Vertical separator line at x=14
vsep_bmp = displayio.Bitmap(1, 32, 2)
vsep_pal = displayio.Palette(2)
vsep_pal[0] = 0x000000
vsep_pal.make_transparent(0)
vsep_pal[1] = 0x222233
for r in range(32):
    vsep_bmp[0, r] = 1
vsep_tg = displayio.TileGrid(vsep_bmp, pixel_shader=vsep_pal, x=20, y=0)
weather_group.append(vsep_tg)

# Tide direction indicator — white pixel sliding up (rising) or down (ebbing)
# along the separator line
sep_pixel_bmp = displayio.Bitmap(1, 1, 2)
sep_pixel_pal = displayio.Palette(2)
sep_pixel_pal[0] = 0x000000
sep_pixel_pal.make_transparent(0)
sep_pixel_pal[1] = 0xFFFFFF
sep_pixel_bmp[0, 0] = 1
sep_pixel_tg = displayio.TileGrid(sep_pixel_bmp, pixel_shader=sep_pixel_pal, x=20, y=16)
weather_group.append(sep_pixel_tg)

# RIGHT SIDE — 4 rows

# Row 1 (y=4): Clock — mid font, white, prominent
clock_label = Label(FONT_MID, text="", color=0xFFFFFF, x=22, y=4)
weather_group.append(clock_label)

# Row 2 (y=12): Weather icon + temperature — mid font, bright yellow

temp_label = Label(FONT_MID, text="", color=0xFFDD00, x=32, y=12)
weather_group.append(temp_label)

# Row 3 (y=20): Condition — small font, gray
cond_label = Label(FONT_SMALL, text="", color=0x888899, x=22, y=20)
weather_group.append(cond_label)

# Row 4 (y=28): Wind — small font, light blue
wind_label = Label(FONT_SMALL, text="", color=0x6699AA, x=22, y=28)
weather_group.append(wind_label)

# --- Plane screen group ---
plane_group = displayio.Group()

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
loading_group = displayio.Group()
loading_label = Label(FONT, text="LOADING...", color=0xFFFF00, x=4, y=12)
loading_group.append(loading_label)

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

def set_health_indicator(visible):
    """Show or hide the bottom-right red pixel across all screens."""
    for _tg in _health_pixels:
        _tg.hidden = not visible

# Start with loading screen
display.root_group = loading_group


# ---------------------------------------------------------------------------
# Time sync — NTP at boot if reachable; otherwise we wait until the first
# weather fetch and use OWM's authoritative `dt` (UTC unix timestamp) to
# set the RTC. _rtc_known tracks whether the RTC's current offset matches
# _tz_offset_secs — only then is the delta-based DST resync safe.
# ---------------------------------------------------------------------------
_tz_offset_secs = TZ_OFFSET_HOURS * 3600
_rtc_known = False
print("Syncing time via NTP...")
try:
    import socketpool, wifi as _wifi_mod, adafruit_ntp, rtc as _rtc_mod
    _pool = socketpool.SocketPool(_wifi_mod.radio)
    _ntp = adafruit_ntp.NTP(_pool, tz_offset=0)
    _local_secs = time.mktime(_ntp.datetime) + _tz_offset_secs
    _rtc_mod.RTC().datetime = time.localtime(_local_secs)
    _rtc_known = True
    print("Time synced (UTC{:+d}):".format(TZ_OFFSET_HOURS), time.localtime())
except Exception as e:
    print("NTP sync failed:", e)

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
planes = []
showing_planes = False
plane_screen_started_at = 0   # ts when plane screen first appeared (for max-duration safeguard)
plane_cooldown_until = 0      # don't re-show plane screen before this ts
plane_idx = 0
last_weather_fetch = -WEATHER_INTERVAL
last_sky_fetch = -OPENSKY_INTERVAL
last_health_fetch = -HEALTH_INTERVAL
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
        # Time / DST sync. Two cases:
        #   - _rtc_known=False (NTP failed at boot, or this is the first
        #     fetch after a soft-reload that lost track of the RTC offset):
        #     use OWM's `dt` (UTC unix timestamp) to set the RTC absolutely.
        #     Authoritative, no dependence on prior state.
        #   - _rtc_known=True: the RTC's offset matches _tz_offset_secs, so
        #     the DST flip can be handled with a small delta nudge — avoids
        #     the small backward jump from re-reading OWM's slightly-stale dt.
        tz_off = data.get("timezone", _tz_offset_secs)
        utc_secs = data.get("dt", 0)
        if not _rtc_known:
            if utc_secs:
                try:
                    import rtc as _rtc_mod
                    _rtc_mod.RTC().datetime = time.localtime(utc_secs + tz_off)
                    _tz_offset_secs = tz_off
                    _rtc_known = True
                    device_log("RTC sync (UTC{:+d})".format(tz_off // 3600))
                except Exception as e:
                    print("RTC sync err:", e)
        elif tz_off != _tz_offset_secs:
            delta = tz_off - _tz_offset_secs
            try:
                import rtc as _rtc_mod
                _rtc_mod.RTC().datetime = time.localtime(time.mktime(time.localtime()) + delta)
                _tz_offset_secs = tz_off
                device_log("TZ shift {}s (UTC{:+d})".format(delta, tz_off // 3600))
            except Exception as e:
                print("TZ resync err:", e)
        # Sunrise/sunset for brightness control (local time as minutes)
        sr = data.get("sys", {}).get("sunrise", 0)
        ss = data.get("sys", {}).get("sunset", 0)
        if sr and ss:
            sr_local = (sr + tz_off) % 86400  # seconds into local day
            ss_local = (ss + tz_off) % 86400
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


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

# Centering helpers for the right panel (x=17 to x=63, 47px wide)
_RIGHT_START = 21
_RIGHT_W = 43

def _center_mid(label, text):
    """Center a mid-font label (5px/char) in the right panel."""
    label.text = text
    tw = len(text) * 5
    label.x = _RIGHT_START + (_RIGHT_W - tw) // 2

def _center_ship(label, text):
    label.text = text
    label.x = 16 + (48 - len(text) * 4) // 2

def _center_ship_mid(label, text):
    label.text = text
    label.x = 16 + (48 - len(text) * 5) // 2

def _center_small(label, text):
    """Center a small-font label (4px/char) in the right panel."""
    label.text = text
    tw = len(text) * 4
    label.x = _RIGHT_START + (_RIGHT_W - tw) // 2

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

        pl_bg_pal[1] = 0x001237   # ocean deep
        pl_bg_pal[2] = 0xBBBBCC   # hull (light blue-gray)
        pl_bg_pal[3] = 0x003264   # ocean mid
        pl_bg_pal[4] = 0x125A96   # ocean surface
        pl_bg_pal[5] = color       # superstructure (ship type color)

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
        airline_label.color = 0xFFFFFF
        airline_label.y = 5

        _center_ship_mid(reg_label, type_name[:9])
        reg_label.color = color
        reg_label.y = 13

        if dest:
            _center_ship_mid(alt_label, dest[:9])
        else:
            alt_label.text = ""
        alt_label.color = 0x8899AA
        alt_label.y = 21

        dist = ship.get("distance_mi", 0)
        hdg = ship.get("heading", 0)
        compass = heading_to_compass(hdg)
        info = "{}mi {}".format(dist, compass) if dist else compass
        actype_label.font = FONT_SMALL
        _center_ship(actype_label, info)
        actype_label.color = 0x6699AA
        actype_label.y = 29
    except MemoryError as _e:
        print("show_ship MemoryError:", _e)
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
        temp_label.color = tc
        _center_small(cond_label, weather_cond[:10])
        _center_small(wind_label, wind_str)
        # Tide time / HIGH / LOW at bottom of left column. Slack window
        # is ±15 minutes, expressed in seconds since predictions are absolute.
        now_secs = time.mktime(time.localtime())
        slack_label = ""
        for p in _tide_predictions:
            if abs(p[0] - now_secs) <= 900:
                slack_label = "HIGH" if p[1] == "H" else "LOW"
                break
        tide_time_label.text = slack_label if slack_label else tide_str
        tide_time_label.color = 0xFFFFFF if (slack_label or _tide_level < 0.2) else 0x00CCDD
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
        alt_label.y     = 20; alt_label.x     = 16; alt_label.color = 0x44AA44
        actype_label.y  = 27; actype_label.x  = 16
        reg_label.y     = 27; reg_label.x     = 16; reg_label.color = 0x667788
        logo_label.y    = 16; logo_label.x    = 2

        callsign = plane[0]
        name, iata, color = get_airline_info(callsign)
        update_plane_bg(color)

        logo_label.text = iata
        logo_label.x = 1 + (14 - len(iata) * 6) // 2
        bright = ((color >> 16) & 0xFF) * 0.299 + ((color >> 8) & 0xFF) * 0.587 + (color & 0xFF) * 0.114
        logo_label.color = 0x111111 if bright > 140 else 0xFFFFFF

        route = flight_cache.get(callsign, {})
        route_label.text = "{}>{}".format(route.get("origin", ""), route.get("dest", ""))

        airline_label.text = name[:8]
        airline_label.color = color

        alt_k = plane[2] // 1000
        alt_label.text = "{}k {}".format(alt_k, heading_to_compass(plane[4])) if alt_k > 0 else ""

        # Row 4: type (left) + registration (right-aligned)
        ac_type = route.get("type", "")
        actype_label.text = ac_type
        actype_label.color = 0x55AADD
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
        # --- Weather + Tides refresh ---
        if now - last_weather_fetch >= WEATHER_INTERVAL:
            fetch_weather()
            fetch_tides()
            flush_device_log()
            last_weather_fetch = now
            if not showing_planes:
                show_weather_tides()

        # --- Proxy health check (drives the bottom-right red pixel) ---
        if PROXY_HOST and now - last_health_fetch >= HEALTH_INTERVAL:
            fetch_health()
            last_health_fetch = now

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

    # Per-tick updates: clock + basin wave animation + tide direction pixel.
    # Wrapped in try/except so a transient MemoryError just skips this frame
    # instead of propagating to the top-level loop and crashing the device.
    # gc.collect() first to maximize the largest contiguous free block.
    # NOTE: do NOT call fetch_failed() here — render MemoryErrors are normal
    # and must not count toward the auto-reboot threshold.
    if not showing_planes and not _showing_ship:
        try:
            gc.collect()
            t = time.localtime()
            h12 = t.tm_hour % 12 or 12
            ampm = "A" if t.tm_hour < 12 else "P"
            _center_mid(clock_label, "{}:{:02d} {}M".format(h12, t.tm_min, ampm))
            _basin_anim_tick += 1
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
                    tide_time_label.color = 0x00CCDD
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
