# Matrix Portal M4 — Tides + Weather + Overhead Aircraft Tracker
# Hardware: Matrix Portal M4 (SAMD51 + ESP32) + 64x32 RGB LED Matrix
#
# Enhanced display:
#   Weather screen: weather icon + temp | scrolling condition | tide arrow + time
#   Plane screen:   plane icon + callsign (airline color) | alt + compass | speed
#
# Required libs (copy from Adafruit CircuitPython Bundle to CIRCUITPY/lib):
#   adafruit_matrixportal, adafruit_portalbase, adafruit_esp32spi,
#   adafruit_bus_device, adafruit_requests, adafruit_connection_manager,
#   adafruit_display_text, adafruit_io, adafruit_fakerequests, neopixel,

import time
import gc
import math
import board
import digitalio
import terminalio
import displayio
from adafruit_matrixportal.matrixportal import MatrixPortal
from adafruit_display_text.label import Label
from adafruit_bitmap_font import bitmap_font

try:
    from secrets import secrets
except ImportError:
    raise RuntimeError("Missing secrets.py -- see template")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
NOAA_STATION = secrets.get("noaa_station", "8445425")
LAT = float(secrets.get("latitude", 42.142039))
LON = float(secrets.get("longitude", -70.693353))
OWM_KEY = secrets["openweather_key"]
TIMEZONE = secrets.get("timezone", "America/New_York")
OPENSKY_USER = secrets.get("opensky_user", "")
OPENSKY_PASS = secrets.get("opensky_pass", "")

BBOX = 0.1
WEATHER_INTERVAL = 600
OPENSKY_INTERVAL = 60
PLANE_CYCLE_SECS = 5
PLANE_MAX_SECS = 600          # max continuous time on plane screen
PLANE_COOLDOWN_SECS = 60      # weather break after PLANE_MAX_SECS hits
PLANE_QUIET_START_HR = 1      # local hour to stop fetching planes (saves API)
PLANE_QUIET_END_HR = 5        # local hour to resume fetching planes
PLANES_ENABLED = True
SHIPS_ENABLED = True    # Set True to enable ship tracking
SHIPS_TEST = False
SHIP_INTERVAL = 30      # poll for ships every 30 sec
SHIP_WEATHER_SECS = 30  # show weather for 30s in cycle

# HTTP proxy on Raspberry Pi — bypasses ESP32 TLS limitation for OpenSky
PROXY_HOST = "http://YOUR_PROXY_HOST:6590"

# ---------------------------------------------------------------------------
# Buttons — UP and DOWN on the Matrix Portal M4
# ---------------------------------------------------------------------------
btn_up = digitalio.DigitalInOut(board.BUTTON_UP)
btn_up.switch_to_input(pull=digitalio.Pull.UP)
btn_down = digitalio.DigitalInOut(board.BUTTON_DOWN)
btn_down.switch_to_input(pull=digitalio.Pull.UP)

# Button handlers — no-ops in production, saves ~3KB RAM
def inject_test_weather(): pass
def inject_test_plane(): pass
def reset_to_live():
    global last_weather_fetch
    last_weather_fetch = -WEATHER_INTERVAL
def clear_test_planes():
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

# ---------------------------------------------------------------------------
# Icon bitmaps (8x8, 2 colors: transparent + icon color)
# Each icon is stored as 8 bytes, one per row, 8 bits per row
# ---------------------------------------------------------------------------

# fmt: off
ICON_SUN = bytes([
    0b00010000,
    0b10010010,
    0b01000100,
    0b00111000,
    0b10111010,
    0b01000100,
    0b10010010,
    0b00010000,
])
ICON_CLOUD = bytes([
    0b00000000,
    0b00110000,
    0b01111000,
    0b01111100,
    0b11111110,
    0b11111110,
    0b01111100,
    0b00000000,
])
ICON_RAIN = bytes([
    0b00110000,
    0b01111000,
    0b11111100,
    0b01111100,
    0b00000000,
    0b01010100,
    0b10101000,
    0b00010000,
])
ICON_SNOW = bytes([
    0b00110000,
    0b01111000,
    0b11111100,
    0b01111100,
    0b00000000,
    0b01001000,
    0b00100100,
    0b01001000,
])
ICON_STORM = bytes([
    0b00110000,
    0b01111000,
    0b11111100,
    0b11111110,
    0b00011000,
    0b00110000,
    0b00010000,
    0b00001000,
])
ICON_FOG = bytes([
    0b00000000,
    0b11111110,
    0b00000000,
    0b01111100,
    0b00000000,
    0b11111110,
    0b00000000,
    0b01111100,
])
# fmt: on

# Weather condition -> icon mapping
def _get_icon_for(cond):
    if cond == "Clear": return ICON_SUN
    if cond == "Clouds": return ICON_CLOUD
    if cond in ("Rain", "Drizzle"): return ICON_RAIN
    if cond == "Snow": return ICON_SNOW
    if cond == "Thunderstorm": return ICON_STORM
    return ICON_FOG

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

def temp_color(t):
    """Return display color for a temperature value (°F)."""
    if t >= 90: return 0xFF2222
    if t >= 80: return 0xFF8800
    if t >= 60: return 0xFFDD00
    if t >= 40: return 0x88FFCC
    if t >= 20: return 0x44AAFF
    return 0x2255CC

def _icon_color_for(cond_main):
    """Return icon color for a weather condition string."""
    if cond_main == "Clear":       return 0xFFCC00
    if cond_main in ("Rain", "Drizzle"): return 0x4488FF
    if cond_main == "Snow":        return 0xCCDDFF
    if cond_main == "Thunderstorm": return 0xAAAA00
    if cond_main in ("Mist", "Fog", "Haze", "Smoke", "Dust"): return 0x888888
    return 0xAAAAAA

def heading_to_compass(hdg):
    return COMPASS_DIRS[round(hdg / 45) % 8]

# ---------------------------------------------------------------------------
# Display groups and labels
# ---------------------------------------------------------------------------

def make_icon_tg(icon_data, width, height, color, x=0, y=0):
    """Create a TileGrid from 1-bit icon data."""
    bmp = displayio.Bitmap(width, height, 2)
    pal = displayio.Palette(2)
    pal[0] = 0x000000
    pal.make_transparent(0)
    pal[1] = color
    for row in range(height):
        byte = icon_data[row]
        for col in range(width):
            if byte & (1 << (7 - col)):
                bmp[col, row] = 1
    tg = displayio.TileGrid(bmp, pixel_shader=pal, x=x, y=y)
    return tg, bmp, pal

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
# Palette: 0=black, 1=water deep, 2=water mid, 3=water surface,
#          4=ship hull (gray), 5=ship superstructure (amber)
basin_bmp = displayio.Bitmap(BASIN_W, BASIN_H, 6)
basin_pal = displayio.Palette(6)
basin_pal[0] = 0x000000
basin_pal[1] = 0x031420   # water deep
basin_pal[2] = 0x062838   # water mid
basin_pal[3] = 0x0C3850   # water surface/crest
basin_pal[4] = 0xBBBBBB   # ship hull (light gray)
basin_pal[5] = 0xFF8822   # ship superstructure (amber)

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

def update_basin_water(level, tick):
    """Redraw water column with tide level. Wave intensity driven by wind."""
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
    if ships:
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
                basin_bmp[col, row] = 0  # air
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

def interpolate_tide_level():
    """Calculate current tide basin fill (0.0-1.0) from predictions."""
    global _tide_level
    if len(_tide_predictions) < 2:
        _tide_level = 0.5
        return
    now = time.localtime()
    now_mins = now.tm_hour * 60 + now.tm_min

    # Find bracketing tides (previous and next)
    prev_tide = None
    next_tide = None
    for i, p in enumerate(_tide_predictions):
        if p[0] >= now_mins:
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
    progress = (now_mins - prev_tide[0]) / span

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
pl_bg_bmp = displayio.Bitmap(14, 32, 3)
pl_bg_pal = displayio.Palette(3)
pl_bg_pal[0] = 0x000000
pl_bg_pal[1] = 0x0055A4   # logo fill (updated per airline)
pl_bg_pal[2] = 0x002244   # logo border (updated per airline)

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

# Route cache: callsign -> {"origin": "BOS", "dest": "JFK"}
flight_cache = {}
_FLIGHT_CACHE_MAX = 10


def fetch_json(url):
    """Fetch a URL and return parsed JSON. Always closes the socket — without
    try/finally, a MemoryError mid-parse leaks the socket and the next fetch
    fails with 'existing socket already connected' until reboot."""
    resp = mp.network.fetch(url)
    try:
        return resp.json()
    finally:
        resp.close()


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
wx_icon_tg, wx_icon_bmp, wx_icon_pal = make_icon_tg(ICON_SUN, 8, 8, 0xFFCC00, x=22, y=9)
weather_group.append(wx_icon_tg)

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

# Row 2: Airline (left) + type (right) — small font
airline_label = Label(FONT_SMALL, text="", color=0x00FF00, x=16, y=13)
plane_group.append(airline_label)

actype_label = Label(FONT_SMALL, text="", color=0x55AADD, x=48, y=13)
plane_group.append(actype_label)

# Row 3: Altitude + heading — small font
alt_label = Label(FONT_SMALL, text="", color=0x44AA44, x=16, y=20)
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

# Start with loading screen
display.root_group = loading_group

# ---------------------------------------------------------------------------
# Time sync (Adafruit IO)
# ---------------------------------------------------------------------------
print("Syncing time...")
try:
    mp.network.get_local_time(TIMEZONE)
    print("Time synced:", time.localtime())
except Exception as e:
    print("Time sync failed:", e)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
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
planes = []
showing_planes = False
plane_screen_started_at = 0   # ts when plane screen first appeared (for max-duration safeguard)
plane_cooldown_until = 0      # don't re-show plane screen before this ts
plane_idx = 0
last_weather_fetch = -WEATHER_INTERVAL
last_sky_fetch = -OPENSKY_INTERVAL
last_plane_cycle = 0
current_screen = "loading"


# ---------------------------------------------------------------------------
# Icon update helper
# ---------------------------------------------------------------------------

def update_weather_icon(cond_main):
    """Rebuild the weather icon bitmap for the given condition."""
    icon_data = _get_icon_for(cond_main)
    wx_icon_pal[1] = _icon_color_for(cond_main)
    for row in range(8):
        byte = icon_data[row]
        for col in range(8):
            wx_icon_bmp[col, row] = 1 if (byte & (1 << (7 - col))) else 0

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
    """Look up short condition text from conditions.csv on disk."""
    cid = str(cond_id)
    try:
        with open("conditions.csv", "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith(cid + ","):
                    return line.split(",", 1)[1]
    except Exception:
        pass
    return fallback[:8]


def fetch_weather():
    global weather_str, weather_cond, weather_cond_main, wind_str, _wind_speed, _sunrise_mins, _sunset_mins
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
        # Sunrise/sunset for brightness control (local time as minutes)
        tz_off = data.get("timezone", -14400)  # seconds offset from UTC
        sr = data.get("sys", {}).get("sunrise", 0)
        ss = data.get("sys", {}).get("sunset", 0)
        if sr and ss:
            sr_local = (sr + tz_off) % 86400  # seconds into local day
            ss_local = (ss + tz_off) % 86400
            _sunrise_mins = sr_local // 60
            _sunset_mins = ss_local // 60
        print("Weather:", weather_str, "-", weather_cond, "Wind:", wind_str)
    except Exception as e:
        print("Weather err:", e)
        if not weather_str:
            weather_str = "N/A"
            weather_cond = "No Data"
            weather_cond_main = ""
            wind_str = ""
    gc.collect()


def fetch_tides():
    global tide_str, tide_type_val, _tide_predictions
    gc.collect()
    try:
        url = (
            "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
            "?date=today&station={}&product=predictions&datum=MLLW"
            "&time_zone=lst_ldt&interval=hilo&units=english&format=json"
        ).format(NOAA_STATION)
        data = fetch_json(url)
        preds = data.get("predictions", [])
        now = time.localtime()
        now_mins = now.tm_hour * 60 + now.tm_min

        # Store all predictions for basin interpolation
        _tide_predictions = []
        for p in preds:
            time_part = p["t"].split(" ")[1]
            h_str, m_str = time_part.split(":")
            h, m = int(h_str), int(m_str)
            _tide_predictions.append((h * 60 + m, p.get("type", ""), h, m_str))

        # Find next upcoming tide
        found = False
        for p in _tide_predictions:
            if p[0] >= now_mins:
                tide_type_val = p[1]
                h12 = p[2] % 12 or 12
                tide_str = "{}:{}".format(h12, p[3])
                found = True
                print("Tide:", tide_type_val, tide_str)
                break
        if not found:
            tide_str = "DONE"
            tide_type_val = ""
        # Calculate basin level
        interpolate_tide_level()
    except Exception as e:
        print("Tide err:", e)
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
        states = data.get("states") or []
        planes = []
        for s in states:
            if s[8]:
                continue
            callsign = (s[1] or "").strip()
            if not callsign:
                continue
            alt_m = s[7] or s[13] or 0
            alt_ft = int(alt_m * 3.281)
            vel_kt = int((s[9] or 0) * 1.944)
            hdg = int(s[10] or 0)
            vrate = s[11] or 0  # vertical rate m/s
            planes.append({
                "call": callsign[:8],
                "icao24": s[0] or "",
                "alt": alt_ft,
                "spd": vel_kt,
                "hdg": hdg,
                "vrate": int(vrate),
            })
        print("Planes overhead:", len(planes))
    except MemoryError:
        print("OpenSky: response too large, try smaller BBOX")
        planes = []
    except Exception as e:
        print("OpenSky err:", e)
        planes = []
    gc.collect()


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


def fetch_ships():
    """Fetch nearby ships from proxy."""
    global ships
    gc.collect()
    try:
        url = "{}/api/ships".format(PROXY_HOST)
        data = fetch_json(url)
        ships = data.get("ships", [])
        print("Ships nearby:", len(ships))
    except Exception as e:
        print("Ships err:", e)
        ships = []
    gc.collect()


def show_ship(ship):
    try:
        _show_ship_inner(ship)
    except MemoryError as _e:
        print("show_ship MemoryError:", _e)
        gc.collect()


def _show_ship_inner(ship):
    """Display a ship — reuses the plane screen group to save RAM."""
    gc.collect()
    switch_screen("plane")
    name = ship.get("name", "UNKNOWN")
    type_name = ship.get("type_name", "Vessel")
    type_code = ship.get("type", 0)
    dest = ship.get("destination", "")

    # Update left column color with ship type
    color = get_ship_type_color(type_code)
    length = ship.get("length", 50)

    # Draw ship silhouette in left column, scaled by length
    # Map 30-300m → 10-28px tall, centered vertically
    ship_h = max(10, min(28, int(10 + (length - 30) * 18 / 270)))
    ship_w = max(4, min(10, ship_h // 3 + 2))  # width proportional
    y_start = (32 - ship_h) // 2
    bow_len = max(2, ship_h // 5)  # pointed bow section
    cx = 7  # center x of 14px column

    # Fill column with ocean blue
    pl_bg_pal[1] = 0x0A2A40  # ocean blue
    pl_bg_pal[2] = 0xDDDDDD  # ship hull (white/light gray)
    for y in range(32):
        for x in range(14):
            pl_bg_bmp[x, y] = 1  # ocean

    # Draw white ship hull
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
        for x in range(cx - hw, cx + hw + 1):
            if 0 <= x < 14:
                pl_bg_bmp[x, y] = 2  # white hull

    actype_label.text = ""
    logo_label.text = ""

    # Clear large-font label
    route_label.text = ""

    # Row 1: Ship name (small font, centered)
    logo_label.text = ""
    _center_ship(airline_label, name[:12])
    airline_label.color = 0xFFFFFF
    airline_label.y = 5

    # Row 2: Vessel type (centered)
    _center_ship(reg_label, type_name)
    reg_label.color = color
    reg_label.y = 12

    # Row 3: Destination (centered)
    if dest:
        _center_ship(alt_label, dest[:12])
    else:
        alt_label.text = ""
    alt_label.color = 0x888899
    alt_label.y = 19

    # Row 4: Distance + heading (centered, reuse route_label but keep short)
    dist = ship.get("distance_mi", 0)
    hdg = ship.get("heading", 0)
    compass = heading_to_compass(hdg)
    if dist:
        info = "{}mi {}".format(dist, compass)
    else:
        info = compass
    route_label.text = ""
    # Row 4: distance + heading (small font, centered)
    _center_ship(actype_label, info)
    actype_label.color = 0x6699AA
    actype_label.y = 26



def show_weather_tides():
    try:
        _show_weather_tides_inner()
    except MemoryError as _e:
        print("show_weather_tides MemoryError:", _e)
        gc.collect()


def _show_weather_tides_inner():
    switch_screen("weather")
    # Weather icon — center in right panel
    update_weather_icon(weather_cond_main)
    icon_x = _RIGHT_START + (_RIGHT_W - 8) // 2
    wx_icon_tg.x = icon_x - 10  # offset left of temp
    # Temperature — centered, color based on temp value
    _center_mid(temp_label, weather_str)
    try:
        temp_val = int(weather_str.split(chr(176))[0])
    except (ValueError, IndexError):
        temp_val = 60
    temp_label.color = temp_color(temp_val)
    # Nudge icon to left of centered temp
    tw = len(weather_str) * 5
    temp_center = _RIGHT_START + (_RIGHT_W - tw) // 2
    wx_icon_tg.x = temp_center - 10
    # Condition — centered (small font), cap to 8 chars to fit pre-sized label
    _center_small(cond_label, weather_cond[:8])
    # Wind — centered (small font)
    _center_small(wind_label, wind_str)
    # Tide time at bottom of left column
    # Show "HIGH" or "LOW" when within 15 min of slack tide
    now_m = time.localtime()
    now_mins = now_m.tm_hour * 60 + now_m.tm_min
    slack_label = ""
    for p in _tide_predictions:
        if abs(p[0] - now_mins) <= 15:
            slack_label = "HIGH" if p[1] == "H" else "LOW"
            break
    tide_time_label.text = slack_label if slack_label else tide_str
    tide_time_label.color = 0xFFFFFF if (slack_label or _tide_level < 0.2) else 0x00CCDD
    # Basin + clock updated in main loop


def has_route(callsign):
    """Check if a plane has route data in the cache."""
    route = flight_cache.get(callsign, {})
    return route.get("origin", "???") != "???" and route.get("dest", "???") != "???"


def get_displayable_planes():
    """Return only planes that have route data."""
    result = []
    for p in planes:
        if p["call"] not in flight_cache:
            fetch_route(p["call"], p.get("icao24", ""))
        if has_route(p["call"]):
            result.append(p)
    return result


def show_plane(plane):
    try:
        _show_plane_inner(plane)
    except MemoryError as _e:
        print("show_plane MemoryError:", _e)
        gc.collect()


def _show_plane_inner(plane):
    gc.collect()
    switch_screen("plane")
    # Reset pl_bg_bmp to plane border layout (show_ship rewrites every pixel)
    for _y in range(32):
        for _x in range(14):
            pl_bg_bmp[_x, _y] = 2 if (_x == 0 or _x == 13 or _y == 0 or _y == 31) else 1
    # Reset label positions/colors to plane layout (show_ship mutates these)
    airline_label.y = 13
    airline_label.x = 16
    actype_label.y = 13
    actype_label.x = 48
    alt_label.y = 20
    alt_label.x = 16
    alt_label.color = 0x44AA44
    reg_label.y = 27
    reg_label.x = 16
    reg_label.color = 0x667788
    logo_label.y = 16
    logo_label.x = 2
    callsign = plane["call"]
    name, iata, color = get_airline_info(callsign)

    # Update background with airline branding
    update_plane_bg(color)

    # Logo: IATA code centered in logo box
    logo_label.text = iata
    logo_label.x = 1 + (14 - len(iata) * 6) // 2
    bright = ((color >> 16) & 0xFF) * 0.299 + ((color >> 8) & 0xFF) * 0.587 + (color & 0xFF) * 0.114
    logo_label.color = 0x111111 if bright > 140 else 0xFFFFFF

    route = flight_cache.get(callsign, {})
    origin = route.get("origin", "")
    dest = route.get("dest", "")
    ac_type = route.get("type", "")
    reg = route.get("reg", "")

    # Row 1: Route — LARGE, next to plane icon
    route_label.text = "{}>{}".format(origin, dest)

    # Row 2: Airline (left) + type (right-aligned)
    airline_label.text = name[:8]
    airline_label.color = color
    actype_label.text = ac_type
    # Right-align type: 4px per char in small font
    actype_label.x = max(18, 64 - len(ac_type) * 4) if ac_type else 48

    # Row 3: Altitude + climb/descend arrow + heading — small
    alt_k = plane["alt"] // 1000
    compass = heading_to_compass(plane["hdg"])
    if alt_k > 0:
        alt_label.text = "{}k {}".format(alt_k, compass)
    else:
        alt_label.text = ""

    # Row 4: Registration (tail number) — small, dim
    reg_label.text = reg or ""





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

while True:
    gc.collect()
    now = time.monotonic()

    # --- Weather + Tides refresh ---
    if now - last_weather_fetch >= WEATHER_INTERVAL:
        fetch_weather()
        fetch_tides()
        last_weather_fetch = now
        if not showing_planes:
            show_weather_tides()

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
        print("Plane screen max duration reached, weather break")
        showing_planes = False
        plane_cooldown_until = now + PLANE_COOLDOWN_SECS
        show_weather_tides()

    if display_planes and not showing_planes and now >= plane_cooldown_until:
        showing_planes = True
        plane_idx = 0
        last_plane_cycle = now
        plane_screen_started_at = now
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

        # Weather/ship cycling: 30s weather, 5s per ship, repeat
        if ships:
            ship_display_total = len(ships) * 15
            cycle_pos = (now - _ship_cycle_start) % (SHIP_WEATHER_SECS + ship_display_total)
            if cycle_pos < SHIP_WEATHER_SECS:
                # Weather phase
                if _showing_ship:
                    _showing_ship = False
                    show_weather_tides()
            else:
                # Ship phase
                if not _showing_ship:
                    _showing_ship = True
                    ship_idx = 0
                    show_ship(ships[ship_idx])
                elif len(ships) > 1:
                    ship_phase_elapsed = cycle_pos - SHIP_WEATHER_SECS
                    expected_idx = int(ship_phase_elapsed / 15) % len(ships)
                    if expected_idx != ship_idx:
                        ship_idx = expected_idx
                        show_ship(ships[ship_idx])

    # Weather screen per-tick updates: clock + basin animation
    if not showing_planes and not _showing_ship:
        t = time.localtime()
        h12 = t.tm_hour % 12 or 12
        ampm = "A" if t.tm_hour < 12 else "P"
        _center_mid(clock_label, "{}:{:02d} {}M".format(h12, t.tm_min, ampm))
        # Animate tide basin surface
        _basin_anim_tick += 1
        update_basin_water(_tide_level, _basin_anim_tick)
        # Slide tide direction pixel up (rising) or down (ebbing) along separator
        if tide_type_val == "H":
            _sep_pixel_y = (_sep_pixel_y - 1) % 32
        elif tide_type_val == "L":
            _sep_pixel_y = (_sep_pixel_y + 1) % 32
        sep_pixel_tg.y = _sep_pixel_y
        # Adjust brightness based on sunrise/sunset
        update_brightness()

    # --- Button handling ---
    if not btn_down.value:  # pressed (active low)
        if PLANES_ENABLED:
            inject_test_plane()
        else:
            inject_test_weather()
        time.sleep(0.3)  # debounce
    if not btn_up.value:
        if PLANES_ENABLED:
            clear_test_planes()
        else:
            reset_to_live()
        time.sleep(0.3)

    time.sleep(1)
