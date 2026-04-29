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
#   adafruit_display_text, adafruit_io, adafruit_fakerequests, neopixel

import time
import gc
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
NOAA_STATION = secrets.get("noaa_station", "8443970")
LAT = float(secrets.get("latitude", 42.36))
LON = float(secrets.get("longitude", -71.06))
OWM_KEY = secrets["openweather_key"]
TIMEZONE = secrets.get("timezone", "America/New_York")
OPENSKY_USER = secrets.get("opensky_user", "")
OPENSKY_PASS = secrets.get("opensky_pass", "")

BBOX = 0.1
WEATHER_INTERVAL = 600
OPENSKY_INTERVAL = 60
PLANE_CYCLE_SECS = 5
PLANES_ENABLED = False  # Set True to enable flight tracking

# HTTP proxy on Raspberry Pi — bypasses ESP32 TLS limitation for OpenSky
PROXY_HOST = "http://YOUR_PROXY_HOST:6590"

# ---------------------------------------------------------------------------
# Buttons — UP and DOWN on the Matrix Portal M4
# ---------------------------------------------------------------------------
btn_up = digitalio.DigitalInOut(board.BUTTON_UP)
btn_up.switch_to_input(pull=digitalio.Pull.UP)
btn_down = digitalio.DigitalInOut(board.BUTTON_DOWN)
btn_down.switch_to_input(pull=digitalio.Pull.UP)

# Test plane data for button injection
# Debug weather presets — cycle with DOWN button when PLANES_ENABLED=False
TEST_WEATHER = [
    {"temp": "72°F", "cond": "Clear Sky", "main": "Clear",
     "wind": "5mph SW", "wind_spd": 5, "tide_level": 0.8},
    {"temp": "55°F", "cond": "Heavy Rain", "main": "Rain",
     "wind": "22mph NE", "wind_spd": 22, "tide_level": 0.3},
    {"temp": "28°F", "cond": "Snow", "main": "Snow",
     "wind": "12mph N", "wind_spd": 12, "tide_level": 0.5},
    {"temp": "65°F", "cond": "Sctd Cloud", "main": "Clouds",
     "wind": "8mph E", "wind_spd": 8, "tide_level": 0.9},
    {"temp": "80°F", "cond": "Thndrstm", "main": "Thunderstorm",
     "wind": "25mph S", "wind_spd": 25, "tide_level": 0.1},
    {"temp": "60°F", "cond": "Fog", "main": "Fog",
     "wind": "2mph W", "wind_spd": 2, "tide_level": 0.6},
]
_test_wx_idx = 0

def inject_test_weather():
    """Cycle through test weather presets."""
    global _test_wx_idx, weather_str, weather_cond, weather_cond_main
    global wind_str, _wind_speed, _tide_level
    tw = TEST_WEATHER[_test_wx_idx % len(TEST_WEATHER)]
    _test_wx_idx += 1
    weather_str = tw["temp"]
    weather_cond = tw["cond"]
    weather_cond_main = tw["main"]
    wind_str = tw["wind"]
    _wind_speed = tw["wind_spd"]
    _tide_level = tw["tide_level"]
    show_weather_tides()
    print("Test weather:", tw["temp"], tw["cond"], "wind:", tw["wind"],
          "tide:", tw["tide_level"])

def reset_to_live():
    """Force a live data refresh."""
    global last_weather_fetch
    last_weather_fetch = -WEATHER_INTERVAL
    print("Reset to live data — will refresh next tick")

TEST_PLANES = [
    {"call": "UAL1234", "alt": 35000, "spd": 450, "hdg": 270,
     "origin": "BOS", "dest": "SFO", "type": "B739"},
    {"call": "DAL567",  "alt": 28000, "spd": 420, "hdg": 180,
     "origin": "BOS", "dest": "ATL", "type": "A321"},
    {"call": "JBU42",   "alt": 18000, "spd": 380, "hdg": 90,
     "origin": "BOS", "dest": "FLL", "type": "A320"},
    {"call": "BAW213",  "alt": 38000, "spd": 490, "hdg": 45,
     "origin": "BOS", "dest": "LHR", "type": "B789"},
    {"call": "AAL100",  "alt": 32000, "spd": 440, "hdg": 250,
     "origin": "BOS", "dest": "DFW", "type": "B738"},
]
_test_idx = 0

def inject_test_plane():
    """Inject a test plane via button press."""
    global _test_idx, planes, showing_planes, plane_idx, last_plane_cycle
    tp = TEST_PLANES[_test_idx % len(TEST_PLANES)]
    _test_idx += 1
    planes.append({
        "call": tp["call"], "icao24": "", "alt": tp["alt"],
        "spd": tp["spd"], "hdg": tp["hdg"], "vrate": 5,
    })
    flight_cache[tp["call"]] = {
        "origin": tp["origin"], "dest": tp["dest"],
        "type": tp["type"], "reg": tp.get("reg", "N12345"),
    }
    showing_planes = True
    plane_idx = len(planes) - 1
    last_plane_cycle = time.monotonic()
    show_plane(planes[plane_idx])
    print("Injected test plane:", tp["call"], tp["origin"], ">", tp["dest"])

def clear_test_planes():
    """Clear all planes and return to weather."""
    global planes, showing_planes
    planes = []
    showing_planes = False
    show_weather_tides()
    print("Cleared planes — back to weather")

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
FONT_SMALL = bitmap_font.load_font("tom-thumb.bdf")
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
ICON_PLANE = bytes([
    0b00010000,
    0b00010000,
    0b00111000,
    0b01111100,
    0b11111110,
    0b00010000,
    0b00111000,
    0b00010000,
])
# Arrow bitmaps (5 wide in 8-bit byte, left-aligned)
ARROW_UP = bytes([
    0b00100000,
    0b01110000,
    0b11111000,
    0b00100000,
    0b00100000,
    0b00100000,
    0b00100000,
])
ARROW_DOWN = bytes([
    0b00100000,
    0b00100000,
    0b00100000,
    0b00100000,
    0b11111000,
    0b01110000,
    0b00100000,
])
# fmt: on

# Weather condition -> icon mapping
WEATHER_ICONS = {
    "Clear": ICON_SUN,
    "Clouds": ICON_CLOUD,
    "Rain": ICON_RAIN,
    "Drizzle": ICON_RAIN,
    "Snow": ICON_SNOW,
    "Thunderstorm": ICON_STORM,
    "Mist": ICON_FOG,
    "Smoke": ICON_FOG,
    "Haze": ICON_FOG,
    "Dust": ICON_FOG,
    "Fog": ICON_FOG,
    "Sand": ICON_FOG,
    "Ash": ICON_FOG,
    "Squall": ICON_FOG,
    "Tornado": ICON_FOG,
}

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
import math

BASIN_W = 16   # full left column width
BASIN_H = 32   # full display height
# Palette: 0=black, 1=water deep, 2=water mid, 3=water surface, 4=arrow(white)
basin_bmp = displayio.Bitmap(BASIN_W, BASIN_H, 5)
basin_pal = displayio.Palette(5)
basin_pal[0] = 0x000000
basin_pal[1] = 0x031420   # water deep
basin_pal[2] = 0x062838   # water mid
basin_pal[3] = 0x0C3850   # water surface/crest
basin_pal[4] = 0xFFFFFF   # arrow (white, visible above and below water)

basin_tg = displayio.TileGrid(basin_bmp, pixel_shader=basin_pal, x=0, y=0)

_tide_level = 0.5      # 0.0 = empty, 1.0 = full
_basin_anim_tick = 0    # for surface wave animation
_tide_predictions = []  # store all today's predictions for interpolation

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
    # 0-5mph: gentle ripple, 10-15mph: moderate, 20+mph: choppy
    w = min(_wind_speed, 25)
    amplitude = 0.3 + w * 0.06      # 0.3 (calm) to 1.8 (stormy)
    speed = 0.3 + w * 0.02          # animation speed
    chop = 0.5 + w * 0.03           # frequency (higher = choppier)
    threshold = 0.2 - w * 0.02      # surface visibility threshold
    extra_rows = 1 if w >= 15 else 0 # extra row of surface foam in high wind

    for row in range(BASIN_H):
        for col in range(BASIN_W):
            if row < water_top - extra_rows:
                basin_bmp[col, row] = 0  # air
            elif row <= water_top:
                # Surface zone — wave shape driven by wind
                wave = math.sin(col * chop + tick * speed) * amplitude
                if w >= 10:
                    wave += math.sin(col * 1.3 + tick * speed * 1.7) * amplitude * 0.4
                basin_bmp[col, row] = 3 if wave > threshold else 0
            elif row == water_top + 1:
                wave = math.sin(col * chop + tick * speed + 1.0)
                basin_bmp[col, row] = 3 if wave > 0 else 2
            else:
                basin_bmp[col, row] = 1  # deep

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
        if p["mins"] >= now_mins:
            next_tide = p
            if i > 0:
                prev_tide = _tide_predictions[i - 1]
            break

    if not prev_tide or not next_tide:
        # Before first tide or after last — estimate
        _tide_level = 0.7 if tide_type_val == "H" else 0.3
        return

    # Progress between previous and next tide
    span = next_tide["mins"] - prev_tide["mins"]
    if span <= 0:
        _tide_level = 0.5
        return
    progress = (now_mins - prev_tide["mins"]) / span

    # Rising (prev=L, next=H) or falling (prev=H, next=L)
    if prev_tide["type"] == "L" and next_tide["type"] == "H":
        _tide_level = progress  # 0→1
    elif prev_tide["type"] == "H" and next_tide["type"] == "L":
        _tide_level = 1.0 - progress  # 1→0
    else:
        _tide_level = 0.5

# Plane background palette — includes logo box zone + content zones
# Palette: 0=navy dark, 1=logo fill (updated per airline), 2=logo border,
#          3=separator, 4=content zone, 5=accent bar
# Plane background — logo box on left, black everywhere else
pl_bg_bmp = displayio.Bitmap(64, 32, 3)
pl_bg_pal = displayio.Palette(3)
pl_bg_pal[0] = 0x000000   # black background
pl_bg_pal[1] = 0x0055A4   # logo fill (updated per airline)
pl_bg_pal[2] = 0x002244   # logo border (updated per airline)

for y in range(32):
    for x in range(64):
        if x < 14:
            # Logo box
            if x == 0 or x == 13 or y == 0 or y == 31:
                pl_bg_bmp[x, y] = 2  # border
            else:
                pl_bg_bmp[x, y] = 1  # fill
        else:
            pl_bg_bmp[x, y] = 0  # black

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

def fetch_route(callsign, icao24=""):
    """Fetch route + aircraft type via proxy. Caches results."""
    if callsign in flight_cache:
        return flight_cache[callsign]
    gc.collect()
    url = "{}/api/route?callsign={}".format(PROXY_HOST, callsign)
    if icao24:
        url += "&icao24={}".format(icao24)
    info = {"origin": "???", "dest": "???", "type": "", "reg": ""}
    try:
        resp = mp.network.fetch(url)
        data = resp.json()
        resp.close()
        route = data.get("route", [])
        if route:
            info["origin"] = icao_to_display(route[0])
            info["dest"] = icao_to_display(route[-1])
        info["type"] = data.get("typecode", "")
        info["reg"] = data.get("registration", "")
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
vsep_tg = displayio.TileGrid(vsep_bmp, pixel_shader=vsep_pal, x=16, y=0)
weather_group.append(vsep_tg)

# RIGHT SIDE — 4 rows

# Row 1 (y=4): Clock — mid font, white, prominent
clock_label = Label(FONT_MID, text="", color=0xFFFFFF, x=18, y=4)
weather_group.append(clock_label)

# Row 2 (y=12): Weather icon + temperature — mid font, bright yellow
wx_icon_tg, wx_icon_bmp, wx_icon_pal = make_icon_tg(ICON_SUN, 8, 8, 0xFFCC00, x=18, y=9)
weather_group.append(wx_icon_tg)

temp_label = Label(FONT_MID, text="", color=0xFFDD00, x=28, y=12)
weather_group.append(temp_label)

# Row 3 (y=20): Condition — small font, gray
cond_label = Label(FONT_SMALL, text="", color=0x888899, x=18, y=20)
weather_group.append(cond_label)

# Row 4 (y=28): Wind — small font, light blue
wind_label = Label(FONT_SMALL, text="", color=0x6699AA, x=18, y=28)
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
_wind_speed = 0  # mph, drives wave animation intensity
_sunrise_mins = 5 * 60 + 30   # default 5:30 AM
_sunset_mins = 19 * 60 + 30   # default 7:30 PM
forecast_hi = ""
forecast_lo = ""
forecast_cond = ""
planes = []
showing_planes = False
plane_idx = 0
last_weather_fetch = -WEATHER_INTERVAL
last_sky_fetch = -OPENSKY_INTERVAL
last_plane_cycle = 0
current_screen = "loading"  # "loading", "weather", "plane"


# ---------------------------------------------------------------------------
# Icon update helper
# ---------------------------------------------------------------------------

def update_weather_icon(cond_main):
    """Rebuild the weather icon bitmap for the given condition."""
    icon_data = WEATHER_ICONS.get(cond_main, ICON_CLOUD)
    # Determine icon color based on condition
    if cond_main == "Clear":
        color = 0xFFCC00
    elif cond_main in ("Rain", "Drizzle"):
        color = 0x4488FF
    elif cond_main == "Snow":
        color = 0xCCDDFF
    elif cond_main == "Thunderstorm":
        color = 0xAAAA00
    elif cond_main in ("Mist", "Fog", "Haze", "Smoke", "Dust"):
        color = 0x888888
    else:
        color = 0xAAAAAA
    wx_icon_pal[1] = color
    for row in range(8):
        byte = icon_data[row]
        for col in range(8):
            wx_icon_bmp[col, row] = 1 if (byte & (1 << (7 - col))) else 0

def update_tide_arrow(t_type):
    """Update tide arrow to up or down."""
    arrow_data = ARROW_UP if t_type == "H" else ARROW_DOWN
    tide_arrow_pal[1] = 0x00CCFF
    for row in range(7):
        byte = arrow_data[row]
        for col in range(5):
            tide_arrow_bmp[col, row] = 1 if (byte & (1 << (7 - col))) else 0

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
    return fallback[:10]


def fetch_weather():
    global weather_str, weather_cond, weather_cond_main, wind_str, _wind_speed, _sunrise_mins, _sunset_mins
    gc.collect()
    url = (
        "https://api.openweathermap.org/data/2.5/weather"
        "?lat={}&lon={}&appid={}&units=imperial"
    ).format(LAT, LON, OWM_KEY)
    try:
        resp = mp.network.fetch(url)
        data = resp.json()
        resp.close()
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
    url = (
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
        "?date=today&station={}&product=predictions&datum=MLLW"
        "&time_zone=lst_ldt&interval=hilo&units=english&format=json"
    ).format(NOAA_STATION)
    try:
        resp = mp.network.fetch(url)
        data = resp.json()
        resp.close()
        preds = data.get("predictions", [])
        now = time.localtime()
        now_mins = now.tm_hour * 60 + now.tm_min

        # Store all predictions for basin interpolation
        _tide_predictions = []
        for p in preds:
            time_part = p["t"].split(" ")[1]
            h_str, m_str = time_part.split(":")
            h, m = int(h_str), int(m_str)
            _tide_predictions.append({
                "mins": h * 60 + m,
                "type": p.get("type", ""),
                "h": h, "m_str": m_str,
            })

        # Find next upcoming tide
        found = False
        for p in _tide_predictions:
            if p["mins"] >= now_mins:
                tide_type_val = p["type"]
                label = "HI" if tide_type_val == "H" else "LO"
                ampm = "A" if p["h"] < 12 else "P"
                h12 = p["h"] % 12 or 12
                tide_str = "{}:{}".format(h12, p["m_str"])
                found = True
                print("Tide:", label, tide_str)
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


def fetch_forecast():
    """Fetch tomorrow's forecast from proxy."""
    global forecast_hi, forecast_lo, forecast_cond
    gc.collect()
    url = "{}/api/forecast".format(PROXY_HOST)
    try:
        resp = mp.network.fetch(url)
        data = resp.json()
        resp.close()
        forecast_hi = str(data.get("hi", ""))
        forecast_lo = str(data.get("lo", ""))
        forecast_cond = get_condition_text(
            data.get("cond_id", 0),
            data.get("cond", "")
        )
        print("Forecast:", forecast_hi, "/", forecast_lo, forecast_cond)
    except Exception as e:
        print("Forecast err:", e)
    gc.collect()


def fetch_planes():
    global planes
    gc.collect()
    lamin = LAT - BBOX
    lamax = LAT + BBOX
    lomin = LON - BBOX
    lomax = LON + BBOX
    url = "{}/api/planes".format(PROXY_HOST)
    try:
        resp = mp.network.fetch(url)
        data = resp.json()
        resp.close()
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
_RIGHT_START = 17
_RIGHT_W = 47

def _center_mid(label, text):
    """Center a mid-font label (5px/char) in the right panel."""
    label.text = text
    tw = len(text) * 5
    label.x = _RIGHT_START + (_RIGHT_W - tw) // 2

def _center_small(label, text):
    """Center a small-font label (4px/char) in the right panel."""
    label.text = text
    tw = len(text) * 4
    label.x = _RIGHT_START + (_RIGHT_W - tw) // 2

def show_weather_tides():
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
    if temp_val >= 90:
        temp_label.color = 0xFF2222
    elif temp_val >= 80:
        temp_label.color = 0xFF8800
    elif temp_val >= 60:
        temp_label.color = 0xFFDD00
    elif temp_val >= 40:
        temp_label.color = 0x88FFCC
    elif temp_val >= 20:
        temp_label.color = 0x44AAFF
    else:
        temp_label.color = 0x2255CC
    # Nudge icon to left of centered temp
    tw = len(weather_str) * 5
    temp_center = _RIGHT_START + (_RIGHT_W - tw) // 2
    wx_icon_tg.x = temp_center - 10
    # Condition — centered (small font)
    _center_small(cond_label, weather_cond)
    # Wind — centered (small font)
    _center_small(wind_label, wind_str)
    # Tide time at bottom of left column
    tide_time_label.text = tide_str
    tide_time_label.color = 0xFFFFFF if _tide_level < 0.2 else 0x00CCDD
    # Basin + clock updated in main loop


def has_route(callsign):
    """Check if a plane has route data in the cache."""
    route = flight_cache.get(callsign, {})
    return route.get("origin", "???") != "???" and route.get("dest", "???") != "???"


def get_displayable_planes():
    """Return only planes that have route data."""
    result = []
    for p in planes:
        # Try fetching route if not cached
        if p["call"] not in flight_cache:
            fetch_route(p["call"], p.get("icao24", ""))
        if has_route(p["call"]):
            result.append(p)
    return result


def show_plane(plane):
    switch_screen("plane")
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
        alt_label.text = "Alt: {}k ft {}".format(alt_k, compass)
    else:
        alt_label.text = ""

    # Row 4: Registration (tail number) — small, dim
    reg_label.text = "Reg: {}".format(reg) if reg else ""



# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
print("Starting main loop")

while True:
    now = time.monotonic()

    # --- Weather + Tides + Forecast refresh ---
    if now - last_weather_fetch >= WEATHER_INTERVAL:
        fetch_weather()
        fetch_tides()
        fetch_forecast()
        last_weather_fetch = now
        if not showing_planes:
            show_weather_tides()

    # --- OpenSky check ---
    if PLANES_ENABLED and now - last_sky_fetch >= OPENSKY_INTERVAL:
        fetch_planes()
        last_sky_fetch = now

    # Only show planes that have route data
    display_planes = get_displayable_planes()

    if display_planes and not showing_planes:
        showing_planes = True
        plane_idx = 0
        last_plane_cycle = now
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

    # Weather screen per-tick updates: clock + basin animation
    if not showing_planes:
        t = time.localtime()
        h12 = t.tm_hour % 12 or 12
        ampm = "A" if t.tm_hour < 12 else "P"
        _center_mid(clock_label, "{}:{:02d} {}M".format(h12, t.tm_min, ampm))
        # Animate tide basin surface
        _basin_anim_tick += 1
        update_basin_water(_tide_level, _basin_anim_tick)
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
