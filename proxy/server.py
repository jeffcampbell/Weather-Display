#!/usr/bin/env python3
"""
Lightweight HTTP proxy for Matrix Portal M4.

The ESP32 on the Matrix Portal can't negotiate TLS with some APIs.
This proxy runs on a Raspberry Pi and forwards requests over HTTPS,
returning plain HTTP responses the device can consume.

Extensible: add new API routes by defining handler functions and
registering them in ROUTES.

Usage:
    python3 server.py                  # default port 6590
    PORT=8080 python3 server.py        # custom port
"""

import json
import os
import sqlite3
import time
import asyncio
import threading
import urllib.request
import urllib.error
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from threading import Lock

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PORT = int(os.environ.get("PORT", 6590))
CONFIG_FILE = Path(__file__).parent / "config.json"

# Load config (API keys, location, etc.)
_config = {}
if CONFIG_FILE.exists():
    with open(CONFIG_FILE) as f:
        _config = json.load(f)

OPENSKY_CLIENT_ID = _config.get("opensky_client_id", "")
OPENSKY_CLIENT_SECRET = _config.get("opensky_client_secret", "")
OWM_KEY = _config.get("openweather_key", "")
AISSTREAM_KEY = _config.get("aisstream_key", "")
FLIGHTAWARE_KEY = _config.get("flightaware_key", "")
DEVICE_SECRET = _config.get("device_secret", "")
LATITUDE = float(_config.get("latitude", 42.36))
LONGITUDE = float(_config.get("longitude", -71.06))
BBOX = float(_config.get("bbox", 0.1))

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache = {}       # key -> {"data": bytes, "time": float}
_cache_lock = Lock()
_started_at = time.time()

# Consecutive OpenSky 429s; reset on the next successful upstream fetch.
# Used by handle_planes to escalate the back-off window from 1h → 2h.
_opensky_429_streak = 0


def cache_get(key, max_age_sec):
    """Return cached bytes if fresh, else None. Respects age_override set by cache_set."""
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        ttl = entry.get("age_override") or max_age_sec
        if (time.time() - entry["time"]) < ttl:
            return entry["data"]
    return None


def cache_set(key, data, age_override=None):
    """Cache data. age_override pins the TTL regardless of what cache_get requests."""
    with _cache_lock:
        _cache[key] = {"data": data, "time": time.time(), "age_override": age_override}


# ---------------------------------------------------------------------------
# Sightings log (SQLite)
# ---------------------------------------------------------------------------

DB_PATH = Path(__file__).parent / "sightings.db"
_db_lock = Lock()
LOG_FILE = Path(__file__).parent / "device.log"
_log_lock = Lock()


def _log_proxy_event(msg):
    """Append a proxy-side event to device.log in the same format the
    device uses, so /api/devicelog tail surfaces both sources together.
    Each line is prefixed with `proxy:` so it's easy to grep."""
    now = time.localtime()
    entry = "[{:02d}:{:02d}:{:02d}] proxy: {}".format(
        now.tm_hour, now.tm_min, now.tm_sec, msg)
    line = "{} | {}\n".format(time.strftime("%Y-%m-%d %H:%M:%S"), entry)
    with _log_lock:
        try:
            with open(LOG_FILE, "a") as f:
                f.write(line)
        except Exception:
            pass
    print(entry)

def _db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS ships (
                id        INTEGER PRIMARY KEY,
                ts        INTEGER NOT NULL,
                mmsi      TEXT,
                name      TEXT,
                type_name TEXT,
                lat       REAL,
                lon       REAL,
                speed     REAL,
                heading   INTEGER,
                distance_mi REAL,
                destination TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS planes (
                id        INTEGER PRIMARY KEY,
                ts        INTEGER NOT NULL,
                callsign  TEXT,
                icao24    TEXT,
                alt_ft    INTEGER,
                speed_kt  INTEGER,
                heading   INTEGER,
                lat       REAL,
                lon       REAL,
                distance_mi REAL
            )
        """)
        # Persistent vessel static data — survives proxy restarts so MMSIs
        # we've seen before always carry full context. Destination is NOT
        # cached (voyage data, changes every trip).
        con.execute("""
            CREATE TABLE IF NOT EXISTS vessel_static (
                mmsi         TEXT PRIMARY KEY,
                name         TEXT,
                type         INTEGER,
                type_name    TEXT,
                callsign     TEXT,
                length       INTEGER,
                last_updated INTEGER
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS ships_ts  ON ships(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS planes_ts ON planes(ts)")

# Deduplicate: don't log the same vessel again within this window
_SHIP_LOG_INTERVAL  = 300   # 5 minutes
_PLANE_LOG_INTERVAL = 120   # 2 minutes
_last_ship_log  = {}  # mmsi  -> last logged ts
_last_plane_log = {}  # callsign -> last logged ts

def log_ship(s):
    mmsi = s.get("mmsi", "")
    now = int(time.time())
    if now - _last_ship_log.get(mmsi, 0) < _SHIP_LOG_INTERVAL:
        return
    _last_ship_log[mmsi] = now
    with _db_lock:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO ships (ts,mmsi,name,type_name,lat,lon,speed,heading,distance_mi,destination) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, mmsi, s.get("name",""), s.get("type_name",""),
                 s.get("lat"), s.get("lon"), s.get("speed"), s.get("heading"),
                 s.get("distance_mi"), s.get("destination",""))
            )

def log_plane(callsign, icao24, alt_ft, speed_kt, heading, lat, lon):
    now = int(time.time())
    if now - _last_plane_log.get(callsign, 0) < _PLANE_LOG_INTERVAL:
        return
    _last_plane_log[callsign] = now
    import math
    def _dist(la1, lo1, la2, lo2):
        if not la2 or not lo2:
            return None
        R = 3958.8
        phi1, phi2 = math.radians(la1), math.radians(la2)
        dphi = math.radians(la2 - la1)
        dlam = math.radians(lo2 - lo1)
        a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
        return round(R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a)), 1)
    distance_mi = _dist(LATITUDE, LONGITUDE, lat, lon)
    with _db_lock:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO planes (ts,callsign,icao24,alt_ft,speed_kt,heading,lat,lon,distance_mi) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (now, callsign, icao24, alt_ft, speed_kt, heading, lat, lon, distance_mi)
            )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def fetch(url, headers=None, timeout=15):
    """Fetch a URL and return (status, body_bytes)."""
    hdrs = {"User-Agent": "MatrixPortalProxy/1.0"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 502, json.dumps({"error": str(e)}).encode()


_OPENSKY_TOKEN_URL = (
    "https://auth.opensky-network.org/auth/realms/opensky-network"
    "/protocol/openid-connect/token"
)
_opensky_token = None        # current access token
_opensky_token_exp = 0.0     # epoch seconds when current token expires
_opensky_token_lock = Lock()


def _fetch_opensky_token():
    """Exchange client_id/client_secret for a bearer token. Returns None on failure."""
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": OPENSKY_CLIENT_ID,
        "client_secret": OPENSKY_CLIENT_SECRET,
    }).encode()
    req = urllib.request.Request(
        _OPENSKY_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read())
        return payload.get("access_token"), int(payload.get("expires_in", 1800))
    except Exception as e:
        _log_proxy_event("OpenSky token fetch failed: {}".format(e))
        return None, 0


def opensky_headers():
    """Return a Bearer auth header for OpenSky, fetching/refreshing the
    OAuth2 token as needed. Returns {} if no client credentials configured
    or token fetch fails — caller will get a 401/429 and fall through to
    the existing empty-response handling."""
    if not (OPENSKY_CLIENT_ID and OPENSKY_CLIENT_SECRET):
        return {}
    global _opensky_token, _opensky_token_exp
    with _opensky_token_lock:
        # Refresh if expired or within 60s of expiry.
        if not _opensky_token or time.time() >= _opensky_token_exp - 60:
            token, ttl = _fetch_opensky_token()
            if not token:
                return {}
            _opensky_token = token
            _opensky_token_exp = time.time() + ttl
        return {"Authorization": "Bearer {}".format(_opensky_token)}


# ---------------------------------------------------------------------------
# API route handlers
# ---------------------------------------------------------------------------
# Each handler takes (query_params: dict) and returns (status, body_bytes).
# Register new handlers in the ROUTES dict at the bottom.

def handle_planes(params):
    """Fetch aircraft in bounding box from OpenSky and return a slim
    device-friendly response — only the fields the device actually uses,
    with on-ground / no-callsign rows already filtered out. Cached 30s.

    Cuts the JSON payload roughly in half vs. raw OpenSky, which matters
    a lot to the SAMD51 device: smaller parse = less heap fragmentation."""

    cache_key = "planes"
    cached = cache_get(cache_key, max_age_sec=55)
    if cached:
        return 200, cached

    lat = float(params.get("lat", [LATITUDE])[0])
    lon = float(params.get("lon", [LONGITUDE])[0])
    bbox = float(params.get("bbox", [BBOX])[0])

    url = (
        f"https://opensky-network.org/api/states/all"
        f"?lamin={lat-bbox}&lomin={lon-bbox}"
        f"&lamax={lat+bbox}&lomax={lon+bbox}"
    )
    status, data = fetch(url, headers=opensky_headers())

    global _opensky_429_streak

    if status == 429:
        _opensky_429_streak += 1
        # First 429 in a streak: back off 1h. Successive 429s (when the
        # next upstream attempt also gets throttled) escalate to 2h to be
        # a better citizen to the OpenSky API.
        backoff_secs = 7200 if _opensky_429_streak >= 2 else 3600
        empty = json.dumps({"time": 0, "planes": [], "rate_limited": True}).encode()
        cache_set(cache_key, empty, age_override=backoff_secs)
        _log_proxy_event("OpenSky 429 #{} — backing off {}h".format(
            _opensky_429_streak, backoff_secs // 3600))
        return 200, empty

    if status != 200:
        # Always return valid JSON — a non-JSON upstream body (HTML 503, etc.)
        # would cause resp.json() to raise on the device, triggering fetch_failed().
        return 200, json.dumps({"time": 0, "planes": [], "upstream_error": status}).encode()

    if _opensky_429_streak:
        _log_proxy_event("OpenSky recovered after {} 429(s)".format(_opensky_429_streak))
        _opensky_429_streak = 0

    try:
        raw = json.loads(data)
        states = raw.get("states") or []
        # Return a slim positional-array per plane: [call, icao24, alt, spd, hdg, vrate]
        # Positional arrays avoid the ~30-byte string-key interning per field
        # that named-key dicts cost on the device's JSON parser. With ~6 fields
        # per plane, that's ~180 bytes saved per plane in device heap.
        planes = []
        for s in states:
            try:
                if s[8]:                                # on_ground
                    continue
                callsign = (s[1] or "").strip()
                if not callsign:
                    continue
                alt_m = s[7] or s[13] or 0              # baro_altitude or geo
                p_lat, p_lon = s[6] or 0, s[5] or 0
                entry = [
                    callsign[:8],
                    s[0] or "",                         # icao24
                    int(alt_m * 3.281),                 # alt (ft)
                    int((s[9] or 0) * 1.944),           # spd (kt)
                    int(s[10] or 0),                    # hdg
                    int(s[11] or 0),                    # vrate
                ]
                planes.append(entry)
                log_plane(callsign[:8], s[0] or "", entry[2], entry[3], entry[4], p_lat, p_lon)
            except Exception:
                continue                                 # skip malformed rows, keep the rest
        body = json.dumps({"time": raw.get("time", 0), "planes": planes}).encode()
        cache_set(cache_key, body, age_override=55)
        return 200, body
    except Exception as e:
        return 200, json.dumps({"time": 0, "planes": [], "error": str(e)}).encode()


def handle_route(params):
    """Proxy route + aircraft type lookup. Falls through:
        FlightAware (real-time, paid)  ->  OpenSky routes  ->  adsbdb
    FlightAware data reflects what the aircraft is *actually* doing right now,
    whereas the OpenSky/adsbdb route DBs return scheduled-callsign data which
    can be stale or wrong (e.g. callsign reused later in the day for a
    different leg). Cached for 1 hour per callsign+icao24 pair."""

    callsign = params.get("callsign", [""])[0].strip()
    icao24 = params.get("icao24", [""])[0].strip()
    if not callsign:
        return 400, json.dumps({"error": "missing callsign"}).encode()

    cache_key = f"route:{callsign}:{icao24}" if icao24 else f"route:{callsign}"
    cached = cache_get(cache_key, max_age_sec=3600)
    if cached:
        return 200, cached

    result = {"callsign": callsign, "route": [], "typecode": "", "registration": ""}

    # 1. FlightAware AeroAPI — real-time flight data. Best accuracy.
    if FLIGHTAWARE_KEY:
        fa_url = f"https://aeroapi.flightaware.com/aeroapi/flights/{callsign}"
        fa_status, fa_data = fetch(fa_url, headers={"x-apikey": FLIGHTAWARE_KEY})
        if fa_status == 200 and fa_data:
            try:
                fa = json.loads(fa_data)
                # Pick the in-progress flight, else the most recent one.
                flights = fa.get("flights", []) or []
                pick = None
                for f in flights:
                    if f.get("status", "").lower().startswith("en route") or f.get("actual_off"):
                        if not f.get("actual_on"):
                            pick = f
                            break
                if pick is None and flights:
                    pick = flights[0]
                if pick:
                    o_icao = (pick.get("origin") or {}).get("code_icao", "")
                    d_icao = (pick.get("destination") or {}).get("code_icao", "")
                    if o_icao and d_icao:
                        result["route"] = [o_icao, d_icao]
                    # FlightAware also gives aircraft type — use it if present
                    ac_type = pick.get("aircraft_type", "")
                    if ac_type and not result["typecode"]:
                        result["typecode"] = ac_type
                    reg = pick.get("registration", "")
                    if reg and not result["registration"]:
                        result["registration"] = reg
            except Exception as e:
                print(f"FlightAware parse err for {callsign}: {e}")

    # 2. OpenSky route DB (scheduled)
    if not result["route"]:
        url = f"https://opensky-network.org/api/routes?callsign={callsign}"
        status, data = fetch(url, headers=opensky_headers())
        if status == 200 and data:
            try:
                route_data = json.loads(data)
                result["route"] = route_data.get("route", [])
            except Exception:
                pass

    # 3. adsbdb (scheduled, alt source)
    if not result["route"]:
        ads_url = f"https://api.adsbdb.com/v0/callsign/{callsign}"
        ads_status, ads_data = fetch(ads_url)
        if ads_status == 200 and ads_data:
            try:
                ads = json.loads(ads_data)
                fr = ads.get("response", {}).get("flightroute", {})
                origin_icao = fr.get("origin", {}).get("icao_code", "")
                dest_icao = fr.get("destination", {}).get("icao_code", "")
                if origin_icao and dest_icao:
                    result["route"] = [origin_icao, dest_icao]
            except Exception:
                pass

    # Fetch aircraft type from hexdb.io (free, no auth, reliable)
    if icao24:
        ac_cache_key = f"aircraft:{icao24}"
        ac_cached = cache_get(ac_cache_key, max_age_sec=86400)
        if ac_cached:
            try:
                ac_data = json.loads(ac_cached)
                result["typecode"] = ac_data.get("ICAOTypeCode", "")
                result["registration"] = ac_data.get("Registration", "")
            except Exception:
                pass
        else:
            ac_url = f"https://hexdb.io/api/v1/aircraft/{icao24}"
            ac_status, ac_data = fetch(ac_url)
            if ac_status == 200 and ac_data:
                cache_set(ac_cache_key, ac_data)
                try:
                    ac_parsed = json.loads(ac_data)
                    result["typecode"] = ac_parsed.get("ICAOTypeCode", "")
                    result["registration"] = ac_parsed.get("Registration", "")
                except Exception:
                    pass

    body = json.dumps(result).encode()
    if result["route"]:
        cache_set(cache_key, body)
        return 200, body
    return 404, json.dumps({"error": "route not found", "callsign": callsign}).encode()


def handle_aircraft(params):
    """Proxy OpenSky aircraft metadata by icao24 hex.
    Cached for 24 hours (aircraft type doesn't change)."""

    icao24 = params.get("icao24", [""])[0].strip()
    if not icao24:
        return 400, json.dumps({"error": "missing icao24"}).encode()

    cache_key = f"aircraft:{icao24}"
    cached = cache_get(cache_key, max_age_sec=86400)
    if cached:
        return 200, cached

    url = f"https://opensky-network.org/api/metadata/aircraft/icao24/{icao24}"
    status, data = fetch(url, headers=opensky_headers())
    if status == 200 and data:
        cache_set(cache_key, data)
        return 200, data
    return 404, json.dumps({"error": "aircraft not found", "icao24": icao24}).encode()


def handle_forecast(params):
    """Fetch 3-day weather forecast from OpenWeatherMap 5-day forecast.
    Returns today, tomorrow, and day-after with hi/lo/condition/wind.
    Cached for 1 hour per (lat,lon)."""

    if not OWM_KEY:
        return 500, json.dumps({"error": "no openweather_key configured"}).encode()

    lat = float(params.get("lat", [LATITUDE])[0])
    lon = float(params.get("lon", [LONGITUDE])[0])

    cache_key = f"forecast:{lat},{lon}"
    cached = cache_get(cache_key, max_age_sec=3600)
    if cached:
        return 200, cached

    import datetime

    url = (
        f"https://api.openweathermap.org/data/2.5/forecast"
        f"?lat={lat}&lon={lon}&appid={OWM_KEY}&units=imperial"
    )
    status, data = fetch(url)
    if status != 200 or not data:
        return status, data or json.dumps({"error": "forecast fetch failed"}).encode()

    try:
        forecast = json.loads(data)
        items = forecast.get("list", [])

        today = datetime.date.today()
        target_dates = [today + datetime.timedelta(days=i) for i in range(3)]
        date_strings = [d.strftime("%Y-%m-%d") for d in target_dates]

        days = {}
        for item in items:
            dt_txt = item.get("dt_txt", "")
            date_str = dt_txt[:10]
            if date_str not in date_strings:
                continue
            entry = days.setdefault(date_str, {
                "hi": -999, "lo": 999,
                "conditions": {}, "cond_id": 800,
                "wind_speeds": [], "wind_degs": [],
            })
            main = item.get("main") or {}
            temp = main.get("temp")
            if temp is None:
                continue
            entry["hi"] = max(entry["hi"], temp)
            entry["lo"] = min(entry["lo"], temp)
            weather = (item.get("weather") or [{}])[0]
            cid = weather.get("id", 800)
            cmain = weather.get("main", "Clear")
            entry["conditions"][cmain] = entry["conditions"].get(cmain, 0) + 1
            entry["cond_id"] = cid
            wind = item.get("wind", {})
            if wind.get("speed"):
                entry["wind_speeds"].append(wind["speed"])
            if wind.get("deg") is not None:
                entry["wind_degs"].append(wind["deg"])

        result = []
        for ds in date_strings:
            if ds not in days:
                continue
            e = days[ds]
            if e["hi"] == -999:
                continue
            most_common = max(e["conditions"], key=e["conditions"].get) if e["conditions"] else "Clear"
            avg_wind = round(sum(e["wind_speeds"]) / len(e["wind_speeds"])) if e["wind_speeds"] else 0
            avg_deg = round(sum(e["wind_degs"]) / len(e["wind_degs"])) if e["wind_degs"] else 0
            result.append({
                "hi": round(e["hi"]),
                "lo": round(e["lo"]),
                "cond": most_common,
                "cond_id": e["cond_id"],
                "date": ds,
                "wind": avg_wind,
                "wind_deg": avg_deg,
            })

        body = json.dumps({"days": result}).encode()
        cache_set(cache_key, body)
        return 200, body
    except Exception as e:
        return 500, json.dumps({"error": str(e)}).encode()


# ---------------------------------------------------------------------------
# AIS Ship Tracking — WebSocket listener + HTTP endpoint
# ---------------------------------------------------------------------------

_ships = {}         # MMSI -> ship info dict
_ships_lock = Lock()

# Persistent static-data cache, mirrored to disk via vessel_static table.
# Keyed by MMSI. Holds name/type/type_name/callsign/length only — destination
# is voyage data and is intentionally never cached here.
_vessel_static_cache = {}
_vessel_cache_lock = Lock()


def _vessel_cache_load():
    """Populate _vessel_static_cache from disk at startup. Cheap full scan —
    the table is small (one row per unique vessel we've ever seen)."""
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT mmsi,name,type,type_name,callsign,length FROM vessel_static"
        )
        for mmsi, name, type_, type_name, callsign, length in cur:
            _vessel_static_cache[mmsi] = {
                "name": name or "",
                "type": type_ or 0,
                "type_name": type_name or "",
                "callsign": callsign or "",
                "length": length or 0,
            }
    print("Vessel static cache: {} loaded".format(len(_vessel_static_cache)))


def _vessel_cache_upsert(mmsi, fields):
    """Merge non-empty static fields into the cache for this MMSI and persist
    to disk. Empty/zero values are ignored so partial reports don't blow away
    previously-known data."""
    if not mmsi or not any(v for v in fields.values()):
        return
    with _vessel_cache_lock:
        existing = _vessel_static_cache.setdefault(mmsi, {})
        for k, v in fields.items():
            if v:
                existing[k] = v
        snapshot = dict(existing)
    with _db_lock:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT OR REPLACE INTO vessel_static "
                "(mmsi,name,type,type_name,callsign,length,last_updated) "
                "VALUES (?,?,?,?,?,?,?)",
                (mmsi, snapshot.get("name", ""), snapshot.get("type", 0),
                 snapshot.get("type_name", ""), snapshot.get("callsign", ""),
                 snapshot.get("length", 0), int(time.time()))
            )


SHIP_STALE_SECS = 600  # remove ships not seen in 10 min
SHIP_MIN_LENGTH = 30   # meters — filter out small vessels
SHIP_CENTER_LAT = LATITUDE   # center of ship search radius (same as home location)
SHIP_CENTER_LON = LONGITUDE
SHIP_MAX_MILES = 10    # only show ships within this radius

AIS_TYPE_NAMES = {
    3: "Fishing", 4: "HighSpeed", 5: "Special",
    6: "Passenger", 7: "Cargo", 8: "Tanker", 9: "Other",
}

def get_ship_type(ais_type):
    """Map AIS type integer (0-99) to category name."""
    decade = ais_type // 10 if ais_type else 0
    return AIS_TYPE_NAMES.get(decade, "Vessel")

def _distance_miles(lat1, lon1, lat2, lon2):
    """Approximate distance in miles between two lat/lon points."""
    import math
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon/2)**2)
    return 3959 * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def _ais_listener():
    """Background async loop: connect to AISStream WebSocket and track ships."""
    import websockets

    async def _listen():
        while True:
            try:
                url = "wss://stream.aisstream.io/v0/stream"
                subscribe = {
                    "APIKey": AISSTREAM_KEY,
                    "BoundingBoxes": [
                        [[LATITUDE - 1.0, LONGITUDE - 1.0],
                         [LATITUDE + 1.0, LONGITUDE + 1.0]]
                    ],
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                }
                print(f"AIS: connecting to {url}...")
                async with websockets.connect(url) as ws:
                    await ws.send(json.dumps(subscribe))
                    print("AIS: subscribed, listening for ships")
                    async for msg_json in ws:
                        try:
                            msg = json.loads(msg_json)
                            _process_ais_message(msg)
                        except Exception as e:
                            print(f"AIS parse err: {e}")
            except Exception as e:
                print(f"AIS connection err: {e}, reconnecting in 10s...")
                await asyncio.sleep(10)

    loop = asyncio.new_event_loop()
    loop.run_until_complete(_listen())


def _process_ais_message(msg):
    """Process an AIS message and update the ships dict."""
    msg_type = msg.get("MessageType", "")
    meta = msg.get("MetaData", {})
    message = msg.get("Message", {})

    if msg_type == "PositionReport":
        pos = message.get("PositionReport", {})
        mmsi = str(pos.get("UserID", ""))
        if not mmsi:
            return
        new_name = ""
        with _ships_lock:
            ship = _ships.setdefault(mmsi, {"mmsi": mmsi})
            ship["lat"] = pos.get("Latitude", 0)
            ship["lon"] = pos.get("Longitude", 0)
            ship["speed"] = round(pos.get("Sog", 0), 1)
            ship["heading"] = int(pos.get("Cog", 0))
            ship["last_seen"] = time.time()
            # MetaData often has ship name
            if meta.get("ShipName") and meta["ShipName"].strip():
                new_name = meta["ShipName"].strip()
                ship["name"] = new_name
        if new_name:
            _vessel_cache_upsert(mmsi, {"name": new_name})

    elif msg_type == "ShipStaticData":
        static = message.get("ShipStaticData", {})
        mmsi = str(static.get("UserID", ""))
        if not mmsi:
            return
        name = static.get("Name", "").strip()
        type_ = static.get("Type", 0)
        type_name = get_ship_type(type_)
        callsign = static.get("CallSign", "").strip()
        dim = static.get("Dimension", {})
        length = (dim.get("A", 0) or 0) + (dim.get("B", 0) or 0)
        dest = static.get("Destination", "").strip()
        with _ships_lock:
            ship = _ships.setdefault(mmsi, {"mmsi": mmsi})
            if name:
                ship["name"] = name
            ship["type"] = type_
            ship["type_name"] = type_name
            if dest:
                ship["destination"] = dest
            ship["callsign"] = callsign
            ship["length"] = length
            ship["last_seen"] = time.time()
        # Persist the static (non-voyage) fields. Destination is voyage data
        # and is intentionally NOT cached — it changes every trip.
        _vessel_cache_upsert(mmsi, {
            "name": name,
            "type": type_,
            "type_name": type_name,
            "callsign": callsign,
            "length": length,
        })


def _prune_stale_ships():
    """Remove ships not seen recently."""
    now = time.time()
    with _ships_lock:
        stale = [k for k, v in _ships.items()
                 if now - v.get("last_seen", 0) > SHIP_STALE_SECS]
        for k in stale:
            del _ships[k]


def handle_ships(params):
    """Return list of nearby ships — filtered by size and distance.
    Static fields (name/type/type_name/callsign/length) missing from the
    live AIS feed are filled in from the persistent vessel_static cache,
    so vessels we've seen before always carry full context even when
    today's WebSocket session hasn't received a fresh Type 5 message."""
    _prune_stale_ships()
    with _ships_lock:
        live_snapshot = [dict(s) for s in _ships.values()]
    # Merge cached static fields where the live data is missing them. Live
    # data always wins when present; cache only fills gaps.
    with _vessel_cache_lock:
        for s in live_snapshot:
            cached = _vessel_static_cache.get(s.get("mmsi", ""))
            if not cached:
                continue
            for field in ("name", "type", "type_name", "callsign", "length"):
                if not s.get(field) and cached.get(field):
                    s[field] = cached[field]
    ship_list = []
    for s in live_snapshot:
        if not s.get("name"):
            continue
        # Minimum length filter — only exclude if length was reported and is small
        length = s.get("length", 0)
        if length and length < SHIP_MIN_LENGTH:
            continue
        # Require a valid position fix before including
        lat = s.get("lat", 0)
        lon = s.get("lon", 0)
        if not lat or not lon:
            continue
        dist = _distance_miles(SHIP_CENTER_LAT, SHIP_CENTER_LON, lat, lon)
        if dist > SHIP_MAX_MILES:
            continue
        dist_mi = round(dist, 1)
        log_ship({**s, "distance_mi": dist_mi})
        ship_list.append({
            "name":        s.get("name", ""),
            "type":        s.get("type", 0),
            "type_name":   s.get("type_name", "Vessel"),
            "destination": s.get("destination", ""),
            "length":      s.get("length", 0),
            "heading":     s.get("heading", 0),
            "distance_mi": dist_mi,
        })
    ship_list.sort(key=lambda s: s.get("distance_mi", 999))
    return 200, json.dumps({"ships": ship_list}).encode()


def handle_health(params):
    """Health check endpoint. issues=[] means everything is healthy;
    a non-empty list means an upstream is degraded — the device uses
    this to show a small indicator on the display."""
    issues = []
    if _opensky_429_streak:
        issues.append("opensky_rate_limited")
    return 200, json.dumps({
        "status": "ok",
        "issues": issues,
        "cache_entries": len(_cache),
        "ships_tracked": len(_ships),
        "uptime_seconds": int(time.time() - _started_at),
    }).encode()


def handle_time(params):
    """Return current UTC seconds plus the proxy's local TZ offset.
    The device uses this as its sole time source — the Pi runs
    systemd-timesyncd, so it's NTP-authoritative, and HTTP over LAN
    is more reliable than UDP NTP from the device (some Wi-Fi networks
    block port 123) and more current than OWM's `dt` field (cached
    5–10 min on free-tier accounts)."""
    is_dst = time.localtime().tm_isdst > 0
    tz_offset = -time.altzone if is_dst else -time.timezone
    return 200, json.dumps({
        "utc": int(time.time()),
        "tz_offset_secs": tz_offset,
    }).encode()


def handle_ships_debug(params):
    """Return raw ship data without filtering, for diagnostics."""
    _prune_stale_ships()
    with _ships_lock:
        ships_raw = list(_ships.values())
    ships_raw.sort(key=lambda s: _distance_miles(
        SHIP_CENTER_LAT, SHIP_CENTER_LON,
        s.get("lat", 0), s.get("lon", 0)
    ))
    annotated = []
    for s in ships_raw[:20]:
        d = dict(s)
        d["distance_mi"] = round(_distance_miles(
            SHIP_CENTER_LAT, SHIP_CENTER_LON,
            s.get("lat", 0), s.get("lon", 0)
        ), 1)
        annotated.append(d)
    return 200, json.dumps({"ships": annotated, "total": len(ships_raw)}).encode()


# ---------------------------------------------------------------------------
# Device log — append-only flat file, one entry per line
# ---------------------------------------------------------------------------

def handle_devicelog_post(body):
    """Append device log messages to device.log.
    Expects JSON body: {"msgs": ["[HH:MM:SS] message", ...]}
    Each line written as: "YYYY-MM-DD HH:MM:SS | [HH:MM:SS] message"
    """
    try:
        data = json.loads(body.decode())
        msgs = data.get("msgs", [])
        if not msgs:
            return 400, json.dumps({"error": "no msgs"}).encode()
    except Exception as e:
        return 400, json.dumps({"error": str(e)}).encode()

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    lines = ["{} | {}\n".format(ts, m) for m in msgs]

    with _log_lock:
        try:
            with open(LOG_FILE, "a") as f:
                f.writelines(lines)
            with open(LOG_FILE, "r") as f:
                all_lines = f.readlines()
            if len(all_lines) > 10000:
                with open(LOG_FILE, "w") as f:
                    f.writelines(all_lines[-10000:])
        except Exception as e:
            return 500, json.dumps({"error": str(e)}).encode()

    return 200, json.dumps({"ok": True, "appended": len(lines)}).encode()


def handle_devicelog_get(params):
    """Return recent device log lines.
    ?lines=N  — how many tail lines to return (default 100, max 1000)
    """
    lines_n = min(int(params.get("lines", ["100"])[0]), 1000)

    with _log_lock:
        try:
            if not LOG_FILE.exists():
                return 200, json.dumps({"lines": [], "total": 0}).encode()
            with open(LOG_FILE, "r") as f:
                all_lines = f.readlines()
            recent = [l.rstrip("\n") for l in all_lines[-lines_n:]]
            total = len(all_lines)
        except Exception as e:
            return 500, json.dumps({"error": str(e)}).encode()

    return 200, json.dumps({"lines": recent, "total": total}).encode()


# ---------------------------------------------------------------------------
# Route registry — add new APIs here
# ---------------------------------------------------------------------------

def handle_sightings(params):
    """Query historical sightings log.
    ?type=ships|planes  (default: both)
    ?hours=N            (default: 24)
    ?limit=N            (default: 100)
    """
    kind   = params.get("type",  ["both"])[0]
    hours  = int(params.get("hours", ["24"])[0])
    limit  = int(params.get("limit", ["100"])[0])
    since  = int(time.time()) - hours * 3600
    result = {}
    with _db_lock:
        with sqlite3.connect(DB_PATH) as con:
            con.row_factory = sqlite3.Row
            if kind in ("ships", "both"):
                rows = con.execute(
                    "SELECT * FROM ships WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                    (since, limit)
                ).fetchall()
                result["ships"] = [dict(r) for r in rows]
            if kind in ("planes", "both"):
                rows = con.execute(
                    "SELECT * FROM planes WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                    (since, limit)
                ).fetchall()
                result["planes"] = [dict(r) for r in rows]
    return 200, json.dumps(result).encode()


ROUTES = {
    "/api/planes":      handle_planes,
    "/api/route":       handle_route,
    "/api/aircraft":    handle_aircraft,
    "/api/forecast":    handle_forecast,
    "/api/ships":       handle_ships,
    "/api/ships/debug": handle_ships_debug,
    "/api/sightings":   handle_sightings,
    "/api/devicelog":   handle_devicelog_get,
    "/api/health":      handle_health,
    "/api/time":        handle_time,
}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    def _check_auth(self):
        """Return True if request is authorized. When DEVICE_SECRET is empty,
        no auth is enforced (back-compat for LAN-only deployments)."""
        if not DEVICE_SECRET:
            return True
        return self.headers.get("X-Device-Secret", "") == DEVICE_SECRET

    def _send_json(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._check_auth():
            self._send_json(401, json.dumps({"error": "bad device secret"}).encode())
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        handler = ROUTES.get(path)
        if handler:
            status, body = handler(params)
        else:
            body = json.dumps({
                "error": "not found",
                "available_routes": list(ROUTES.keys()),
            }).encode()
            status = 404
        self._send_json(status, body)

    def do_POST(self):
        if not self._check_auth():
            self._send_json(401, json.dumps({"error": "bad device secret"}).encode())
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/devicelog":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            status, response = handle_devicelog_post(body)
        else:
            status, response = 404, json.dumps({"error": "not found"}).encode()
        self._send_json(status, response)

    def log_message(self, fmt, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] {args[0]}")


if __name__ == "__main__":
    _db_init()
    _vessel_cache_load()
    print(f"Matrix Portal Proxy — port {PORT}")
    print(f"Config: {CONFIG_FILE}")
    print(f"Routes: {', '.join(ROUTES.keys())}")
    print(f"Location: {LATITUDE}, {LONGITUDE} (bbox {BBOX})")
    print(f"Sightings DB: {DB_PATH}")

    # Start AIS WebSocket listener in background thread
    if AISSTREAM_KEY:
        ais_thread = threading.Thread(target=_ais_listener, daemon=True)
        ais_thread.start()
        print("AIS: WebSocket listener started")
    else:
        print("AIS: No aisstream_key configured, ship tracking disabled")

    server = HTTPServer(("", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
