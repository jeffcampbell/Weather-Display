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
import terminalio
import displayio
from adafruit_matrixportal.matrixportal import MatrixPortal
from adafruit_display_text.label import Label

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

# ---------------------------------------------------------------------------
# Display setup — 64x32, using displayio directly for icons + text
# ---------------------------------------------------------------------------
mp = MatrixPortal(status_neopixel=board.NEOPIXEL, bit_depth=4, debug=False)

# Clear MatrixPortal's default group so we manage our own layout
while len(mp.splash) > 0:
    mp.splash.pop()

display = mp.display
FONT = terminalio.FONT

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

# Airline ICAO prefix -> (name, IATA code, color)
AIRLINE_INFO = {
    "UAL": ("United",     "UA", 0x0055A4),
    "DAL": ("Delta",      "DL", 0xCC2222),
    "SWA": ("SouthWst",   "WN", 0xF9A01B),
    "AAL": ("American",   "AA", 0xCC2222),
    "JBU": ("JetBlue",    "B6", 0x0033CC),
    "FFT": ("Frontier",   "F9", 0x008040),
    "NKS": ("Spirit",     "NK", 0xF0CB00),
    "SKW": ("SkyWest",    "OO", 0x6666AA),
    "ASA": ("Alaska",     "AS", 0x01426A),
    "RPA": ("Republic",   "YX", 0x3366BB),
    "ENY": ("Envoy",      "MQ", 0xCC3333),
    "EJA": ("NetJets",    "EJ", 0x999999),
    "FDX": ("FedEx",      "FX", 0xFF6600),
    "UPS": ("UPS",        "5X", 0x6B3600),
    "BAW": ("British",    "BA", 0x1B3D6D),
    "AFR": ("AirFrnce",   "AF", 0x002157),
    "DLH": ("Lufthnsa",   "LH", 0x00337F),
    "ACA": ("AirCanda",   "AC", 0xDD1122),
    "UAE": ("Emirates",   "EK", 0xCC0000),
    "EDV": ("Endeavor",   "9E", 0xCC3333),
    "GJS": ("GoJet",      "G7", 0x4477AA),
    "JIA": ("PSA",        "OH", 0x3366BB),
    "PDT": ("Piedmont",   "PT", 0x3366BB),
    "AAY": ("Allegiant",  "G4", 0xF68C1E),
    "MXY": ("Breeze",     "MX", 0x44BBEE),
    "HAL": ("Hawaiian",   "HA", 0x7722AA),
    "QXE": ("Horizon",    "QX", 0x009DB0),
}

def get_airline_info(callsign):
    prefix = callsign[:3].upper()
    return AIRLINE_INFO.get(prefix, (prefix, prefix[:2], 0x00AA44))

def icao_to_display(icao):
    """Convert ICAO airport code to 3-letter display code."""
    if not icao:
        return "???"
    if len(icao) == 4 and icao[0] == "K":
        return icao[1:]
    if len(icao) == 4 and icao[:2] == "CY":
        return icao[1:]
    return icao[:4]

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
wx_bg_bmp = displayio.Bitmap(64, 32, 5)
wx_bg_pal = displayio.Palette(5)
wx_bg_pal[0] = 0x000000   # border
wx_bg_pal[1] = 0x0A0503   # top zone (warm default)
wx_bg_pal[2] = 0x080808   # condition zone
wx_bg_pal[3] = 0x101820   # separator
wx_bg_pal[4] = 0x050301   # tide zone

# Paint the weather background zones
for y in range(32):
    for x in range(64):
        if x == 0 or x == 63 or y == 0 or y == 31:
            wx_bg_bmp[x, y] = 0  # border
        elif y <= 9:
            wx_bg_bmp[x, y] = 1  # top zone
        elif y <= 18:
            wx_bg_bmp[x, y] = 2  # condition zone
        elif y == 19:
            wx_bg_bmp[x, y] = 3  # separator
        else:
            wx_bg_bmp[x, y] = 4  # tide zone

wx_bg_tg = displayio.TileGrid(wx_bg_bmp, pixel_shader=wx_bg_pal, x=0, y=0)

# Plane background palette
pl_bg_bmp = displayio.Bitmap(64, 32, 5)
pl_bg_pal = displayio.Palette(5)
pl_bg_pal[0] = 0x050810   # border
pl_bg_pal[1] = 0x020412   # callsign zone
pl_bg_pal[2] = 0x08101E   # separator lines
pl_bg_pal[3] = 0x02060E   # alt zone
pl_bg_pal[4] = 0x01030A   # speed zone

for y in range(32):
    for x in range(64):
        if x == 0 or x == 63 or y == 0 or y == 31:
            pl_bg_bmp[x, y] = 0
        elif y <= 9:
            pl_bg_bmp[x, y] = 1
        elif y == 10 or y == 21:
            pl_bg_bmp[x, y] = 2
        elif y <= 20:
            pl_bg_bmp[x, y] = 3
        else:
            pl_bg_bmp[x, y] = 4

pl_bg_tg = displayio.TileGrid(pl_bg_bmp, pixel_shader=pl_bg_pal, x=0, y=0)

# Weather condition -> background color palette mapping
WEATHER_BG = {
    "Clear":        (0x191003, 0x0D0A05, 0x140E05, 0x32230A, 0x191003),
    "Clouds":       (0x080A12, 0x04050A, 0x0C0F19, 0x141928, 0x080A12),
    "Rain":         (0x030614, 0x01030C, 0x050A1E, 0x0A1432, 0x030614),
    "Drizzle":      (0x030614, 0x01030C, 0x050A1E, 0x0A1432, 0x030614),
    "Snow":         (0x0A0C12, 0x05060A, 0x0F1219, 0x191C28, 0x0A0C12),
    "Thunderstorm": (0x0F0514, 0x06020A, 0x14081C, 0x230F2D, 0x0F0514),
}

def update_weather_bg(cond_main):
    """Update weather background palette colors for the condition."""
    colors = WEATHER_BG.get(cond_main, (0x080808, 0x040405, 0x0C0C0F, 0x141419, 0x080808))
    for i in range(5):
        wx_bg_pal[i] = colors[i]

def update_plane_bg(airline_color):
    """Tint the plane background with a dim version of the airline color."""
    r = ((airline_color >> 16) & 0xFF)
    g = ((airline_color >> 8) & 0xFF)
    b = (airline_color & 0xFF)
    # Very dim tint for airline zone
    pl_bg_pal[1] = ((r >> 4) << 16) | ((g >> 4) << 8) | (b >> 4)

# Route cache: callsign -> {"origin": "BOS", "dest": "JFK"}
flight_cache = {}

def fetch_route(callsign):
    """Fetch route info for a callsign from OpenSky. Caches results."""
    if callsign in flight_cache:
        return flight_cache[callsign]
    gc.collect()
    url = "https://opensky-network.org/api/routes?callsign={}".format(callsign)
    headers = {}
    if OPENSKY_USER and OPENSKY_PASS:
        import binascii
        cred = binascii.b2a_base64(
            "{}:{}".format(OPENSKY_USER, OPENSKY_PASS).encode()
        ).decode().strip()
        headers["Authorization"] = "Basic " + cred
    info = {"origin": "???", "dest": "???"}
    try:
        if headers:
            resp = mp.network.fetch(url, headers=headers)
        else:
            resp = mp.network.fetch(url)
        data = resp.json()
        resp.close()
        route = data.get("route", [])
        if route:
            info["origin"] = icao_to_display(route[0])
            info["dest"] = icao_to_display(route[-1])
        print("Route {}: {} -> {}".format(callsign, info["origin"], info["dest"]))
    except Exception as e:
        print("Route err for {}: {}".format(callsign, e))
    flight_cache[callsign] = info
    gc.collect()
    return info

# --- Weather screen group ---
weather_group = displayio.Group()

# Background first (behind everything)
weather_group.append(wx_bg_tg)

# Weather icon (8x8, will be rebuilt when condition changes)
wx_icon_tg, wx_icon_bmp, wx_icon_pal = make_icon_tg(ICON_SUN, 8, 8, 0xFFCC00, x=1, y=1)
weather_group.append(wx_icon_tg)

# Temperature label
temp_label = Label(FONT, text="", color=0xFFFF00, x=11, y=5)
weather_group.append(temp_label)

# Condition label (scrolling handled manually)
cond_label = Label(FONT, text="", color=0xBBBBBB, x=1, y=14)
weather_group.append(cond_label)

# Tide arrow (5x7)
tide_arrow_tg, tide_arrow_bmp, tide_arrow_pal = make_icon_tg(ARROW_UP, 5, 7, 0x00CCFF, x=2, y=22)
weather_group.append(tide_arrow_tg)

# Tide label
tide_label = Label(FONT, text="", color=0x00CCFF, x=9, y=25)
weather_group.append(tide_label)

# --- Plane screen group ---
plane_group = displayio.Group()

# Background first
plane_group.append(pl_bg_tg)

# Row 1: Route (right of logo area)
route_label = Label(FONT, text="", color=0xFFFFFF, x=16, y=4)
plane_group.append(route_label)

# Row 2: Airline name
airline_label = Label(FONT, text="", color=0x00FF00, x=16, y=12)
plane_group.append(airline_label)

# Row 3: Aircraft type
type_label = Label(FONT, text="", color=0x55AADD, x=16, y=20)
plane_group.append(type_label)

# Row 4: Altitude (left) + Callsign (right)
alt_label = Label(FONT, text="", color=0x44AA44, x=16, y=28)
plane_group.append(alt_label)

call_label = Label(FONT, text="", color=0x555566, x=40, y=28)
plane_group.append(call_label)

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

# Scrolling state for condition text
scroll_offset = 0
scroll_phase = "pause_start"
scroll_timer = 0.0

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
        color = 0xAAAAAAA
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

def fetch_weather():
    global weather_str, weather_cond, weather_cond_main, scroll_offset, scroll_phase, scroll_timer
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
        weather_cond = data["weather"][0].get("description", weather_cond_main)
        # Capitalize words
        weather_cond = " ".join(w.capitalize() for w in weather_cond.split())
        weather_str = "{}{}F".format(temp, chr(176))  # degree symbol
        print("Weather:", weather_str, "-", weather_cond)
        # Reset scrolling
        scroll_offset = 0
        scroll_phase = "pause_start"
        scroll_timer = 0.0
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
    url = (
        "https://opensky-network.org/api/states/all"
        "?lamin={}&lomin={}&lamax={}&lomax={}"
    ).format(lamin, lomin, lamax, lomax)
    headers = {}
    if OPENSKY_USER and OPENSKY_PASS:
        import binascii
        cred = binascii.b2a_base64(
            "{}:{}".format(OPENSKY_USER, OPENSKY_PASS).encode()
        ).decode().strip()
        headers["Authorization"] = "Basic " + cred
    try:
        if headers:
            resp = mp.network.fetch(url, headers=headers)
        else:
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
            planes.append({
                "call": callsign[:8],
                "alt": alt_ft,
                "spd": vel_kt,
                "hdg": hdg,
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
    # Update background colors for weather condition
    update_weather_bg(weather_cond_main)
    # Update icon
    update_weather_icon(weather_cond_main)
    # Update temp
    temp_label.text = weather_str
    # Update condition (truncate for now; scrolling handled in main loop)
    if len(weather_cond) <= 10:
        cond_label.text = weather_cond
    else:
        cond_label.text = weather_cond[:10]
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
            fetch_route(p["call"])
        if has_route(p["call"]):
            result.append(p)
    return result


def show_plane(plane):
    switch_screen("plane")
    callsign = plane["call"]
    name, iata, color = get_airline_info(callsign)

    # Tint background
    update_plane_bg(color)

    route = flight_cache.get(callsign, {})
    origin = route.get("origin", "")
    dest = route.get("dest", "")

    # Row 1: Route
    route_label.text = "{} > {}".format(origin, dest)
    route_label.color = 0xFFFFFF

    # Row 2: Airline name
    airline_label.text = name[:8]
    airline_label.color = color

    # Row 3: Altitude + heading
    alt_k = plane["alt"] // 1000
    compass = heading_to_compass(plane["hdg"])
    type_label.text = "{}Kft {}".format(alt_k, compass)

    # Row 4: Callsign
    alt_label.text = ""
    call_label.text = callsign
    call_label.x = max(16, 64 - len(callsign) * 6)


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

    # --- Scroll condition text (weather screen only) ---
    if not showing_planes and len(weather_cond) > 10:
        scroll_timer += 1.0  # ~1 sec per tick
        max_scroll = len(weather_cond) - 10
        if scroll_phase == "pause_start":
            cond_label.text = weather_cond[:10]
            if scroll_timer > 2:
                scroll_phase = "scrolling"
                scroll_timer = 0
        elif scroll_phase == "scrolling":
            scroll_offset = min(scroll_offset + 1, max_scroll)
            cond_label.text = weather_cond[scroll_offset:scroll_offset + 10]
            if scroll_offset >= max_scroll:
                scroll_phase = "pause_end"
                scroll_timer = 0
        elif scroll_phase == "pause_end":
            if scroll_timer > 2:
                scroll_phase = "pause_start"
                scroll_timer = 0
                scroll_offset = 0

    time.sleep(1)
