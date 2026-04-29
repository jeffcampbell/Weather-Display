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

# Weather background palette: zone colors updated per-condition
# Weather background — black everywhere except tide zone for waves
wx_bg_bmp = displayio.Bitmap(64, 32, 2)
wx_bg_pal = displayio.Palette(2)
wx_bg_pal[0] = 0x000000   # black
wx_bg_pal[1] = 0x020608   # tide zone (dark blue, behind waves)

for y in range(32):
    for x in range(64):
        wx_bg_bmp[x, y] = 1 if y >= 18 else 0

wx_bg_tg = displayio.TileGrid(wx_bg_bmp, pixel_shader=wx_bg_pal, x=0, y=0)

# Wave overlay: 128px wide bitmap that scrolls horizontally for animation
# Only covers the tide zone (rows 18-30, mapped to wave bitmap rows 0-12)
import math
WAVE_W = 128  # wider than display so we can scroll
WAVE_H = 13   # rows 18-30
wave_bmp = displayio.Bitmap(WAVE_W, WAVE_H, 4)
wave_pal = displayio.Palette(4)
wave_pal[0] = 0x000000
wave_pal.make_transparent(0)
wave_pal[1] = 0x0E2840   # wave crest
wave_pal[2] = 0x061830   # wave body
wave_pal[3] = 0x030C18   # wave deep

for x in range(WAVE_W):
    wave = math.sin(x * 0.14) * 2 + math.sin(x * 0.09 + 2.0) * 1.5
    surface = 3 + wave  # relative to wave bitmap top
    for y in range(WAVE_H):
        if 3 <= y <= 11:
            # Darkened strip for tide text — skip wave drawing
            wave_bmp[x, y] = 0  # transparent (shows bg color beneath)
        elif y < surface:
            wave_bmp[x, y] = 1  # crest
        elif y < surface + 2:
            wave_bmp[x, y] = 2  # body
        else:
            wave_bmp[x, y] = 3  # deep

wave_tg = displayio.TileGrid(wave_bmp, pixel_shader=wave_pal, x=0, y=18)
_wave_offset = 0

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

# Background first (behind everything)
weather_group.append(wx_bg_tg)

# Wave overlay (tide zone animation)
weather_group.append(wave_tg)

# Weather icon (8x8, will be rebuilt when condition changes)
wx_icon_tg, wx_icon_bmp, wx_icon_pal = make_icon_tg(ICON_SUN, 8, 8, 0xFFCC00, x=2, y=1)
weather_group.append(wx_icon_tg)

# Temperature label
temp_label = Label(FONT, text="", color=0xFFFF00, x=12, y=5)
weather_group.append(temp_label)

# Condition label
cond_label = Label(FONT, text="", color=0xAAAAAA, x=1, y=13)
weather_group.append(cond_label)

# Tide arrow (5x7) — positioned in wave zone
tide_arrow_tg, tide_arrow_bmp, tide_arrow_pal = make_icon_tg(ARROW_UP, 5, 7, 0x00EEFF, x=3, y=22)
weather_group.append(tide_arrow_tg)

# Tide label — in wave zone
tide_label = Label(FONT, text="", color=0x00EEFF, x=10, y=25)
weather_group.append(tide_label)

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

# Row 2: Airline + type — small font
airline_label = Label(FONT_SMALL, text="", color=0x00FF00, x=16, y=13)
plane_group.append(airline_label)

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
    global weather_str, weather_cond, weather_cond_main
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
        print("Weather:", weather_str, "-", weather_cond)
    except Exception as e:
        print("Weather err:", e)
        if not weather_str:
            weather_str = "N/A"
            weather_cond = "No Data"
            weather_cond_main = ""
    gc.collect()


def fetch_tides():
    global tide_str, tide_type_val
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
        found = False
        for p in preds:
            time_part = p["t"].split(" ")[1]
            h_str, m_str = time_part.split(":")
            h, m = int(h_str), int(m_str)
            if h * 60 + m >= now_mins:
                tide_type_val = p.get("type", "")
                label = "HI" if tide_type_val == "H" else "LO"
                ampm = "A" if h < 12 else "P"
                h12 = h % 12 or 12
                tide_str = "{} {}:{}{}".format(label, h12, m_str, ampm)
                found = True
                print("Tide:", tide_str)
                break
        if not found:
            tide_str = "DONE"
            tide_type_val = ""
    except Exception as e:
        print("Tide err:", e)
        if not tide_str:
            tide_str = "N/A"
            tide_type_val = ""
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

def show_weather_tides():
    switch_screen("weather")
    # Update icon
    update_weather_icon(weather_cond_main)
    # Update temp
    temp_label.text = weather_str
    # Condition text always fits (max 10 chars from conditions.csv)
    cond_label.text = weather_cond
    # Update tide
    if tide_type_val:
        update_tide_arrow(tide_type_val)
        tide_arrow_tg.hidden = False
        tide_label.x = 9
    else:
        tide_arrow_tg.hidden = True
        tide_label.x = 2
    tide_label.text = tide_str


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

    # Row 2: Airline + aircraft type — small
    if ac_type:
        airline_label.text = "{}  {}".format(name[:8], ac_type)
    else:
        airline_label.text = name[:8]
    airline_label.color = color

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

    # --- Weather + Tides refresh ---
    if now - last_weather_fetch >= WEATHER_INTERVAL:
        fetch_weather()
        fetch_tides()
        last_weather_fetch = now
        if not showing_planes:
            show_weather_tides()

    # --- OpenSky check ---
    if now - last_sky_fetch >= OPENSKY_INTERVAL:
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

    # Animate wave scroll on weather screen (shift 1px every tick)
    if not showing_planes:
        _wave_offset = (_wave_offset + 1) % (WAVE_W - 64)
        wave_tg.x = -_wave_offset

    # --- Button handling ---
    if not btn_down.value:  # pressed (active low)
        inject_test_plane()
        time.sleep(0.3)  # debounce
    if not btn_up.value:
        clear_test_planes()
        time.sleep(0.3)

    time.sleep(1)
