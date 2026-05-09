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
import base64
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

OPENSKY_USER = _config.get("opensky_user", "")
OPENSKY_PASS = _config.get("opensky_pass", "")
OWM_KEY = _config.get("openweather_key", "")
AISSTREAM_KEY = _config.get("aisstream_key", "")
FLIGHTAWARE_KEY = _config.get("flightaware_key", "")
LATITUDE = float(_config.get("latitude", 42.36))
LONGITUDE = float(_config.get("longitude", -71.06))
BBOX = float(_config.get("bbox", 0.1))

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache = {}       # key -> {"data": bytes, "time": float}
_cache_lock = Lock()


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


def opensky_headers():
    """Build auth headers for OpenSky if credentials are configured."""
    if OPENSKY_USER and OPENSKY_PASS:
        cred = base64.b64encode(f"{OPENSKY_USER}:{OPENSKY_PASS}".encode()).decode()
        return {"Authorization": f"Basic {cred}"}
    return {}


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
    cached = cache_get(cache_key, max_age_sec=30)
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

    if status == 429:
        empty = json.dumps({"time": 0, "planes": [], "rate_limited": True}).encode()
        cache_set(cache_key, empty, age_override=3600)  # back off for 1 hour
        print("OpenSky rate-limited (429) — caching empty response for 1 hour")
        return 200, empty

    if status != 200:
        return status, data

    try:
        raw = json.loads(data)
        states = raw.get("states") or []
        # Return a slim positional-array per plane: [call, icao24, alt, spd, hdg, vrate]
        # Positional arrays avoid the ~30-byte string-key interning per field
        # that named-key dicts cost on the device's JSON parser. With ~6 fields
        # per plane, that's ~180 bytes saved per plane in device heap.
        planes = []
        for s in states:
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
        body = json.dumps({"time": raw.get("time", 0), "planes": planes}).encode()
        cache_set(cache_key, body)
        return 200, body
    except Exception as e:
        return 500, json.dumps({"error": str(e)}).encode()


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
    Cached for 1 hour."""

    cache_key = "forecast"
    cached = cache_get(cache_key, max_age_sec=3600)
    if cached:
        return 200, cached

    if not OWM_KEY:
        return 500, json.dumps({"error": "no openweather_key configured"}).encode()

    import datetime
    import math

    lat = float(params.get("lat", [LATITUDE])[0])
    lon = float(params.get("lon", [LONGITUDE])[0])
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
            temp = item["main"]["temp"]
            entry["hi"] = max(entry["hi"], temp)
            entry["lo"] = min(entry["lo"], temp)
            cid = item["weather"][0]["id"]
            cmain = item["weather"][0]["main"]
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
        with _ships_lock:
            ship = _ships.setdefault(mmsi, {"mmsi": mmsi})
            ship["lat"] = pos.get("Latitude", 0)
            ship["lon"] = pos.get("Longitude", 0)
            ship["speed"] = round(pos.get("Sog", 0), 1)
            ship["heading"] = int(pos.get("Cog", 0))
            ship["last_seen"] = time.time()
            # MetaData often has ship name
            if meta.get("ShipName") and meta["ShipName"].strip():
                ship["name"] = meta["ShipName"].strip()

    elif msg_type == "ShipStaticData":
        static = message.get("ShipStaticData", {})
        mmsi = str(static.get("UserID", ""))
        if not mmsi:
            return
        with _ships_lock:
            ship = _ships.setdefault(mmsi, {"mmsi": mmsi})
            name = static.get("Name", "").strip()
            if name:
                ship["name"] = name
            ship["type"] = static.get("Type", 0)
            ship["type_name"] = get_ship_type(static.get("Type", 0))
            dest = static.get("Destination", "").strip()
            if dest:
                ship["destination"] = dest
            ship["callsign"] = static.get("CallSign", "").strip()
            dim = static.get("Dimension", {})
            ship["length"] = (dim.get("A", 0) or 0) + (dim.get("B", 0) or 0)
            ship["last_seen"] = time.time()


def _prune_stale_ships():
    """Remove ships not seen recently."""
    now = time.time()
    with _ships_lock:
        stale = [k for k, v in _ships.items()
                 if now - v.get("last_seen", 0) > SHIP_STALE_SECS]
        for k in stale:
            del _ships[k]


def handle_ships(params):
    """Return list of nearby ships — filtered by size and distance."""
    _prune_stale_ships()
    with _ships_lock:
        ship_list = []
        for s in _ships.values():
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
            s["distance_mi"] = round(dist, 1)
            ship_list.append(s)
            log_ship(s)
        # Sort by distance
        ship_list.sort(key=lambda s: s.get("distance_mi", 999))
    body = json.dumps({"ships": ship_list}).encode()
    return 200, body


def handle_health(params):
    """Health check endpoint."""
    return 200, json.dumps({
        "status": "ok",
        "cache_entries": len(_cache),
        "ships_tracked": len(_ships),
        "uptime_approx": "use /api/health to verify proxy is reachable",
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
}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ProxyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        handler = ROUTES.get(path)
        if handler:
            status, body = handler(params)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            # List available routes
            body = json.dumps({
                "error": "not found",
                "available_routes": list(ROUTES.keys()),
            }).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/devicelog":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            status, response = handle_devicelog_post(body)
        else:
            status, response = 404, json.dumps({"error": "not found"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(response))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, fmt, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] {args[0]}")


if __name__ == "__main__":
    _db_init()
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
