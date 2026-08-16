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
import math
import os
import socket
import sqlite3
import subprocess
import time
import asyncio
import threading
import urllib.request
import urllib.error
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from pathlib import Path
from threading import Lock

# Floor for any socket read/write that doesn't pass an explicit timeout —
# without this a stuck TLS handshake on an upstream could pin a thread forever.
socket.setdefaulttimeout(20)

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
NOAA_STATION = _config.get("noaa_station", "8443970")
AISSTREAM_KEY = _config.get("aisstream_key", "")
FLIGHTAWARE_KEY = _config.get("flightaware_key", "")
DEVICE_SECRET = _config.get("device_secret", "")
LATITUDE = float(_config.get("latitude", 42.36))
LONGITUDE = float(_config.get("longitude", -71.06))
BBOX = float(_config.get("bbox", 0.1))

# Named locations for the v2 API. v1 endpoints ignore this and continue using
# the LATITUDE/LONGITUDE/BBOX globals above — leaving v1 behavior untouched.
LOCATIONS = _config.get("locations") or {}

# Cloud/dev service providers to monitor for /api/status. Statuspage-based
# providers are fully config-driven (name + host); AWS/GCP/Azure use built-in
# adapters keyed by "type". Defaults cover the launch set so the endpoint works
# even if config.json predates this feature. See handle_status.
_DEFAULT_STATUS_PROVIDERS = [
    {"name": "GitHub",     "type": "statuspage", "host": "www.githubstatus.com"},
    {"name": "Cloudflare", "type": "statuspage", "host": "www.cloudflarestatus.com"},
    {"name": "Supabase",   "type": "statuspage", "host": "status.supabase.com"},
    {"name": "HashiCorp",  "type": "statuspage", "host": "status.hashicorp.com"},
    {"name": "AWS",        "type": "aws"},
    {"name": "GCP",        "type": "gcp"},
    {"name": "Azure",      "type": "azure"},
]
STATUS_PROVIDERS = _config.get("status_providers") or _DEFAULT_STATUS_PROVIDERS


def resolve_location(params):
    """Resolve ?loc=<name> against the LOCATIONS config block. Returns
    (lat, lon, bbox, name) on success or (None, None, None, error_body) on
    failure, where error_body is a bytes JSON payload ready to return."""
    loc = params.get("loc", [""])[0].strip()
    if not loc:
        return None, None, None, json.dumps({
            "error": "missing loc",
            "available": sorted(LOCATIONS.keys()),
        }).encode()
    entry = LOCATIONS.get(loc)
    if not entry:
        return None, None, None, json.dumps({
            "error": "unknown location",
            "loc": loc,
            "available": sorted(LOCATIONS.keys()),
        }).encode()
    return (
        float(entry.get("lat", LATITUDE)),
        float(entry.get("lon", LONGITUDE)),
        float(entry.get("bbox", BBOX)),
        loc,
    )

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


ROUTE_CACHE_TTL_HIT = 3600      # a resolved route is good for an hour
ROUTE_CACHE_TTL_MISS = 21600    # a miss is sticky for 6h — see handle_route

# --- FlightAware AeroAPI monthly spend cap ---------------------------------
# AeroAPI bills per successful /flights/{ident} query (~1¢ each). Free sources
# are tried first and GA tails are skipped, but a runaway — a bug, or just a
# busy month of unresolvable airline callsigns loitering in the bbox — could
# still rack up charges. This is a hard monthly ceiling on *billable* calls,
# persisted to disk (flightaware_usage.json) so a proxy restart can't silently
# reset it mid-month, and rolled over automatically at the start of each UTC
# month. Once the cap is hit, handle_route stops consulting FlightAware and
# serves whatever the free sources found. Default keeps spend under the ~$5/mo
# free tier at ~1¢/query; override with "flightaware_monthly_limit" in config.
FLIGHTAWARE_MONTHLY_LIMIT = int(_config.get("flightaware_monthly_limit", 450))

# When True, FlightAware is consulted to *override* a route the free sources
# already resolved — not just as a last resort when they came up empty. This
# fixes the "right tail, wrong route" case: the free DBs return a callsign's
# *scheduled* route, which goes stale when a callsign/airframe is reused for a
# different leg, and a wrong-but-present free answer used to permanently block
# the accurate real-time source. Spend stays bounded by the small bbox query
# volume, the monthly cap, the GA-registration skip, and the per-(callsign,
# icao24) route cache — each airframe costs at most one FA call per cache
# window regardless of how often the device re-polls. Set false in config to
# fall back to the old free-first-wins behavior.
FLIGHTAWARE_OVERRIDE_FREE = bool(_config.get("flightaware_override_free_routes", True))
_FA_USAGE_PATH = Path(__file__).parent / "flightaware_usage.json"
_fa_usage_lock = Lock()
_fa_exhausted_logged_period = None


def _fa_period():
    return time.strftime("%Y-%m", time.gmtime())


def _fa_usage_read():
    """This month's usage dict {period, count}, rolling over at the start of a
    new UTC month. Caller must hold _fa_usage_lock."""
    try:
        d = json.loads(_FA_USAGE_PATH.read_text())
    except Exception:
        d = {}
    if d.get("period") != _fa_period():
        d = {"period": _fa_period(), "count": 0}
    return d


def _fa_usage_write(d):
    try:
        _FA_USAGE_PATH.write_text(json.dumps(d))
    except Exception as e:
        print(f"FlightAware usage write failed: {e}")


def flightaware_usage_status():
    """(used, limit) for the current month — surfaced on /api/health."""
    with _fa_usage_lock:
        return _fa_usage_read().get("count", 0), FLIGHTAWARE_MONTHLY_LIMIT


def _flightaware_reserve():
    """Atomically reserve one billable FlightAware call against the monthly
    cap. Returns True (and increments) if under the limit, else False. Reserving
    *before* the call makes the ceiling hard even under concurrent requests."""
    with _fa_usage_lock:
        d = _fa_usage_read()
        if d.get("count", 0) >= FLIGHTAWARE_MONTHLY_LIMIT:
            return False
        d["count"] = d.get("count", 0) + 1
        _fa_usage_write(d)
        return True


def _flightaware_refund():
    """Return a reserved slot to the pool when the request didn't actually bill
    (non-2xx response, or a network error before reaching FlightAware)."""
    with _fa_usage_lock:
        d = _fa_usage_read()
        if d.get("count", 0) > 0:
            d["count"] -= 1
            _fa_usage_write(d)


def _flightaware_note_exhausted():
    """Log the cap being hit, at most once per month, so the log isn't spammed
    on every ~60s poll for the rest of the billing period."""
    global _fa_exhausted_logged_period
    period = _fa_period()
    with _fa_usage_lock:
        if _fa_exhausted_logged_period == period:
            return
        _fa_exhausted_logged_period = period
    _log_proxy_event(
        f"FlightAware monthly cap of {FLIGHTAWARE_MONTHLY_LIMIT} reached for "
        f"{period} — serving routes from free sources only until next month"
    )


# A bare N-number is a tail registration, not a flight ident. FlightAware's
# /flights/{ident} essentially never resolves these to a scheduled route, and
# GA aircraft loiter in the bbox for hours, so querying them is pure spend.
def _is_ga_registration(callsign):
    return callsign[:1] == "N" and callsign[1:2].isdigit()


def handle_route(params):
    """Proxy route + aircraft type lookup. Falls through:
        OpenSky routes  ->  adsbdb  ->  FlightAware (real-time, paid)

    The free scheduled-route DBs are tried first. FlightAware is the paid,
    authoritative source: its data reflects what the aircraft is *actually*
    doing right now, whereas the DBs return scheduled-callsign data that can
    be stale or wrong (e.g. callsign reused later in the day for a different
    leg). Because a wrong-but-present free answer would otherwise be trusted
    forever, FlightAware is consulted to *override* the free route, not just
    when the free sources came up empty — see FLIGHTAWARE_OVERRIDE_FREE. At
    roughly a cent a query this is kept bounded by the monthly spend cap, the
    GA-registration skip, and the per-callsign+icao24 cache below.

    Both outcomes are cached per callsign+icao24 pair: hits for 1h, misses
    for 6h. Caching the misses matters more than caching the hits — the
    device re-polls every ~60s, and an uncached miss meant a fresh billable
    FlightAware call on every single poll for as long as the plane stayed in
    the bbox."""

    callsign = params.get("callsign", [""])[0].strip()
    icao24 = params.get("icao24", [""])[0].strip()
    if not callsign:
        return 400, json.dumps({"error": "missing callsign"}).encode()

    not_found = json.dumps({"error": "route not found", "callsign": callsign}).encode()

    cache_key = f"route:{callsign}:{icao24}" if icao24 else f"route:{callsign}"
    cached = cache_get(cache_key, max_age_sec=ROUTE_CACHE_TTL_HIT)
    if cached:
        # Misses are cached too, so a hit isn't automatically a 200.
        try:
            if json.loads(cached).get("route"):
                return 200, cached
        except Exception:
            pass
        return 404, not_found

    result = {"callsign": callsign, "route": [], "typecode": "", "registration": ""}

    # 1. OpenSky route DB (scheduled, free)
    url = f"https://opensky-network.org/api/routes?callsign={callsign}"
    status, data = fetch(url, headers=opensky_headers())
    if status == 200 and data:
        try:
            route_data = json.loads(data)
            result["route"] = route_data.get("route", [])
        except Exception:
            pass

    # 2. adsbdb (scheduled, free, alt source)
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

    # 3. FlightAware AeroAPI — paid, best accuracy, real-time. Consulted to
    #    *override* the free scheduled-route answer (which can be stale when a
    #    callsign is reused for a different leg), not merely as a last resort.
    #    Bounded by: FLIGHTAWARE_OVERRIDE_FREE, the monthly spend cap, the
    #    GA-registration skip, and the per-(callsign,icao24) route cache. With
    #    the override off, falls back to the old "only when free found nothing"
    #    behavior.
    fa_should_consult = FLIGHTAWARE_OVERRIDE_FREE or not result["route"]
    if fa_should_consult and FLIGHTAWARE_KEY and not _is_ga_registration(callsign):
        if not _flightaware_reserve():
            _flightaware_note_exhausted()   # cap hit — skip the billable call
        else:
            fa_url = f"https://aeroapi.flightaware.com/aeroapi/flights/{callsign}"
            fa_status, fa_data = fetch(fa_url, headers={"x-apikey": FLIGHTAWARE_KEY})
            if fa_status != 200:
                _flightaware_refund()   # non-2xx doesn't bill — reclaim the slot
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
    # Cache the miss as well, on a longer TTL, so a plane with no resolvable
    # route doesn't re-run this whole chain once a minute while it loiters.
    cache_set(cache_key, body, age_override=ROUTE_CACHE_TTL_MISS)
    return 404, not_found


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


TIDE_FETCH_DAYS = 30   # NOAA allows up to 1 year per request for hilo predictions
TIDE_CACHE_SEC = 86400        # re-fetch from NOAA at most once/day
TIDE_STALE_CACHE_SEC = 25 * 86400  # predictions don't decay — safe to serve
                                    # most of the fetched window if NOAA is down

# Local harmonic-prediction fallback (pytides-py3), used only when NOAA's
# live predictions API fails AND there's no usable cache left. Runs as a
# separate subprocess in its own venv rather than importing numpy/scipy
# into this always-on process — this Pi is a Zero 2 W with 512MB total, so
# that dependency weight is only worth paying for the few seconds a
# fallback prediction actually takes, not for the process's entire uptime.
TIDE_DIR = Path(__file__).parent
TIDE_VENV_PYTHON = TIDE_DIR / "tide_venv" / "bin" / "python3"
TIDE_PREDICT_SCRIPT = TIDE_DIR / "tide_predict.py"
TIDE_PREDICT_TIMEOUT_SEC = 45   # observed ~15s for a full 37-constituent,
                                 # 30-day run on this hardware; generous margin


def _harmonics_path(station):
    return TIDE_DIR / f"harmonics_{station}.json"


def _fetch_and_cache_harmonics(station):
    """One-time bootstrap: fetch a station's published harmonic constituents
    from NOAA's metadata API and cache them to disk indefinitely — this is
    what makes the local pytides fallback possible without any further
    NOAA dependency. Called opportunistically after a successful live tide
    fetch (proof NOAA is reachable); no-ops if already cached, so it's a
    true one-time cost, not a recurring one."""
    path = _harmonics_path(station)
    if path.exists():
        return True

    url = (
        "https://api.tidesandcurrents.noaa.gov/mdapi/prod/webapi/stations/"
        f"{station}/harcon.json?units=english"
    )
    status, data = fetch(url, timeout=10)
    if status != 200:
        _log_proxy_event(f"Harmonics fetch failed for station {station} (status={status})")
        return False

    try:
        raw = json.loads(data).get("HarmonicConstituents", [])
        consts = [
            {"name": c["name"], "amplitude": c["amplitude"], "phase_GMT": c["phase_GMT"]}
            for c in raw
        ]
        if not consts:
            raise ValueError("empty HarmonicConstituents")
        path.write_text(json.dumps(consts))
        _log_proxy_event(f"Cached {len(consts)} harmonic constituents for station {station}")
        return True
    except Exception as e:
        _log_proxy_event(f"Harmonics parse failed for station {station}: {e}")
        return False


def _local_harmonic_predict(station, begin_date, end_date):
    """Run the isolated pytides subprocess against cached harmonic
    constituents. Returns predictions JSON bytes, or None if unavailable
    (no venv/script/harmonics yet, or the subprocess failed)."""
    harmonics = _harmonics_path(station)
    if not (TIDE_VENV_PYTHON.exists() and TIDE_PREDICT_SCRIPT.exists() and harmonics.exists()):
        return None

    # No hard RLIMIT_AS here on purpose — numpy/scipy reserve a lot of
    # virtual address space (shared libs, BLAS/LAPACK, mmap'd allocator
    # arenas) far in excess of what they actually touch, so an address-space
    # cap kills the interpreter during import well before real memory
    # pressure — measured on this hardware: ~73MB peak RSS for a full
    # 37-constituent/30-day run, comfortably safe on its own. The timeout
    # below is the actual runaway-computation guard.
    try:
        result = subprocess.run(
            [str(TIDE_VENV_PYTHON), str(TIDE_PREDICT_SCRIPT),
             str(harmonics), f"{begin_date:%Y-%m-%d}", f"{end_date:%Y-%m-%d}"],
            capture_output=True, timeout=TIDE_PREDICT_TIMEOUT_SEC,
        )
    except Exception as e:
        _log_proxy_event(f"Local tide prediction subprocess failed to start: {e}")
        return None

    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[-300:]
        _log_proxy_event(f"Local tide prediction failed (rc={result.returncode}): {stderr}")
        return None

    try:
        parsed = json.loads(result.stdout)
        if not parsed.get("predictions"):
            return None
        return result.stdout
    except Exception as e:
        _log_proxy_event(f"Local tide prediction returned bad JSON: {e}")
        return None


def handle_tides(params):
    """Fetch a rolling 30-day window of tide predictions from NOAA CO-OPS
    and return them in the same shape the device already parses. Cached
    1 day — tide predictions are deterministic astronomical data, not
    live conditions, so a day-old (or even a couple-weeks-old) fetch is
    just as correct as a fresh one within its window.

    NOAA's API is fronted by AWS API Gateway and was intermittently hanging
    the ESP32-S3's constrained TLS stack for long enough to trip the
    device's 90s watchdog and reboot the whole thing — moving the fetch
    here means a bad NOAA connection only ties up one Pi thread, and on
    failure we serve the last good cache instead of an error so the
    device's display never has to fall back to N/A. Fetching a full month
    at once and caching it for a day also means an extended NOAA outage
    (like the one that prompted this) has to last nearly a month before
    the device runs out of valid cached predictions."""
    import datetime

    station = params.get("station", [NOAA_STATION])[0]
    cache_key = f"tides:{station}"
    cached = cache_get(cache_key, max_age_sec=TIDE_CACHE_SEC)
    if cached:
        return 200, cached

    today = datetime.date.today()
    end = today + datetime.timedelta(days=TIDE_FETCH_DAYS)
    url = (
        "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
        f"?begin_date={today:%Y%m%d}&end_date={end:%Y%m%d}"
        f"&station={station}&product=predictions&datum=MLLW"
        "&time_zone=lst_ldt&interval=hilo&units=english&format=json"
    )
    status, data = fetch(url, timeout=10)

    if status == 200:
        try:
            if "predictions" in json.loads(data):
                cache_set(cache_key, data)
                # Opportunistic one-time bootstrap for the local prediction
                # fallback below — no-ops instantly if already cached, so
                # this costs nothing on the 364 days a year NOAA is up.
                _fetch_and_cache_harmonics(station)
                return 200, data
        except Exception:
            pass

    # Upstream failed or returned something unexpected (e.g. NOAA's
    # {"error": ...} body) — serve a somewhat-stale cache rather than an
    # error, so the device keeps showing the last known-good tide instead
    # of falling back to N/A. Always return valid JSON either way — a
    # non-JSON/error body would make resp.json() raise on the device.
    stale = cache_get(cache_key, max_age_sec=TIDE_STALE_CACHE_SEC)
    if stale:
        _log_proxy_event(f"Tides upstream failed (status={status}), serving stale cache")
        return 200, stale

    # No live data and no cache left (e.g. a NOAA outage longer than
    # TIDE_STALE_CACHE_SEC, or this station's very first-ever request
    # happening during an outage) — fall back to predictions computed
    # locally from NOAA's own published harmonic constituents. Less
    # precise than NOAA's live engine, but needs no network at all.
    #
    # Ensure the constituents are cached first. Normally that happened after
    # a past successful live fetch, but on a fresh install whose very first
    # requests all land during an outage, it never did — and NOAA's separate
    # metadata/harcon endpoint tends to stay up even when the predictions
    # engine is down, so we can still bootstrap here and self-heal. No-ops if
    # already cached.
    _fetch_and_cache_harmonics(station)
    local = _local_harmonic_predict(station, today, end)
    if local:
        cache_set(cache_key, local, age_override=TIDE_CACHE_SEC)
        _log_proxy_event(f"Tides upstream failed (status={status}), serving local harmonic prediction")
        return 200, local

    _log_proxy_event(f"Tides upstream failed (status={status}), no cache or local fallback available")
    return 200, json.dumps({"predictions": [], "upstream_error": status}).encode()


# ---------------------------------------------------------------------------
# v2 API handlers
# ---------------------------------------------------------------------------
# v2 adds per-location support for planes + forecast, keyed by named entries
# in the "locations" config block. v1 handlers above are untouched so existing
# devices keep working unchanged. Ships and route/aircraft/time/health/devicelog
# are not duplicated in v2 — devices using v2 still hit the v1 versions for
# those (location-independent or single-bbox by design).

V2_PLANES_CACHE_TTL = 90  # vs. 55s in v1 — halves OpenSky burn per location


def handle_v2_planes(params):
    """Per-location aircraft fetch. Same response shape as /api/planes."""

    lat, lon, bbox, loc_or_err = resolve_location(params)
    if lat is None:
        return 400, loc_or_err

    cache_key = f"v2:planes:{loc_or_err}"
    cached = cache_get(cache_key, max_age_sec=V2_PLANES_CACHE_TTL)
    if cached:
        return 200, cached

    # Respect the v1 OpenSky backoff streak — if the home location is being
    # throttled, the second location is on the same OAuth2 quota and will hit
    # 429s too. Cache an empty rate_limited body per loc so we don't burn the
    # account's remaining credits hammering upstream.
    if _opensky_429_streak > 0:
        backoff_secs = 7200 if _opensky_429_streak >= 2 else 3600
        empty = json.dumps({"time": 0, "planes": [], "rate_limited": True}).encode()
        cache_set(cache_key, empty, age_override=backoff_secs)
        return 200, empty

    url = (
        f"https://opensky-network.org/api/states/all"
        f"?lamin={lat-bbox}&lomin={lon-bbox}"
        f"&lamax={lat+bbox}&lomax={lon+bbox}"
    )
    status, data = fetch(url, headers=opensky_headers())

    if status == 429:
        # Mirror v1's backoff: empty rate_limited body, 1h floor. We don't
        # mutate _opensky_429_streak from v2 — v1 owns that counter.
        empty = json.dumps({"time": 0, "planes": [], "rate_limited": True}).encode()
        cache_set(cache_key, empty, age_override=3600)
        _log_proxy_event(f"OpenSky 429 on v2 planes (loc={loc_or_err}) — backing off 1h")
        return 200, empty

    if status != 200:
        return 200, json.dumps({"time": 0, "planes": [], "upstream_error": status}).encode()

    try:
        raw = json.loads(data)
        states = raw.get("states") or []
        planes = []
        for s in states:
            try:
                if s[8]:
                    continue
                callsign = (s[1] or "").strip()
                if not callsign:
                    continue
                alt_m = s[7] or s[13] or 0
                entry = [
                    callsign[:8],
                    s[0] or "",
                    int(alt_m * 3.281),
                    int((s[9] or 0) * 1.944),
                    int(s[10] or 0),
                    int(s[11] or 0),
                ]
                planes.append(entry)
            except Exception:
                continue
        body = json.dumps({"time": raw.get("time", 0), "planes": planes}).encode()
        cache_set(cache_key, body, age_override=V2_PLANES_CACHE_TTL)
        return 200, body
    except Exception as e:
        return 200, json.dumps({"time": 0, "planes": [], "error": str(e)}).encode()


def handle_v2_forecast(params):
    """Per-location 3-day forecast. Same response shape as /api/forecast."""

    if not OWM_KEY:
        return 500, json.dumps({"error": "no openweather_key configured"}).encode()

    lat, lon, _bbox, loc_or_err = resolve_location(params)
    if lat is None:
        return 400, loc_or_err

    cache_key = f"v2:forecast:{loc_or_err}"
    cached = cache_get(cache_key, max_age_sec=3600)
    if cached:
        return 200, cached

    # Delegate to the v1 forecast handler by passing lat/lon overrides — it
    # already accepts those and keys its own cache on (lat,lon). We then
    # mirror the result into the v2 cache so v2 callers don't have to wait
    # on a v1 cache miss next time.
    status, body = handle_forecast({"lat": [str(lat)], "lon": [str(lon)]})
    if status == 200:
        cache_set(cache_key, body, age_override=3600)
    return status, body


# ---------------------------------------------------------------------------
# v2 Sky — naked-eye planet visibility for the upcoming evening, with cloud
# verdict from the location's forecast. Powered by JPL DE421 via skyfield.
# Skyfield is lazy-loaded so a missing install only affects /api/v2/sky;
# all other endpoints stay up.
# ---------------------------------------------------------------------------

_sky_loaded   = False
_sky_load_err = ""
_sky_ts       = None
_sky_eph      = None
_sky_lock     = Lock()

# Per-planet meta: human name, 4-char abbr, skyfield ephemeris key, typical
# magnitude (mid-cycle), brightness label. Mag is informational only — used
# to colour the "Bright/Dim" word the device shows beneath the planet glyph.
_PLANET_META = (
    ("Mercury", "Merc",  "mercury",            0.0),
    ("Venus",   "Venus", "venus",             -4.0),
    ("Mars",    "Mars",  "mars",               0.0),
    ("Jupiter", "Jup",   "jupiter barycenter", -2.2),
    ("Saturn",  "Sat",   "saturn barycenter",  0.5),
)


def _ensure_skyfield():
    """Lazy-load skyfield + de421.bsp once per process. Idempotent; returns
    True on success. On failure the error string is stored in _sky_load_err
    so the handler can include it in the 500 response (helpful for debugging
    a freshly-installed proxy)."""
    global _sky_loaded, _sky_load_err, _sky_ts, _sky_eph
    if _sky_loaded:
        return True
    with _sky_lock:
        if _sky_loaded:
            return True
        try:
            from skyfield.api import Loader
            loader = Loader(str(Path(__file__).parent / "skyfield_data"))
            _sky_ts  = loader.timescale()
            _sky_eph = loader("de421.bsp")
            _sky_loaded = True
            return True
        except Exception as e:
            _sky_load_err = str(e)
            _log_proxy_event(f"skyfield load failed: {e}")
            return False


_COMPASS_8 = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def _compass(az_deg):
    """8-point compass label for an azimuth in degrees (0=N, 90=E, …)."""
    return _COMPASS_8[int((az_deg + 22.5) % 360 // 45)]


def _cloud_verdict(cond_id):
    """OWM condition ID -> short viewing-condition label."""
    if cond_id == 800:               return "Clear"
    if 801 <= cond_id <= 802:        return "Hazy"
    if 803 <= cond_id <= 804:        return "Cloudy"
    return "Overcast"                # rain/snow/storm/fog/etc — no chance


def _brightness_label(mag):
    """Naked-eye brightness bucket. Roughly matches everyday descriptors."""
    if mag < -2: return "Brilliant"
    if mag <  0: return "Bright"
    if mag <  2: return "Visible"
    return "Faint"


def _local_tz_offset():
    """Same convention as handle_time: seconds to add to UTC for local."""
    is_dst = time.localtime().tm_isdst > 0
    return -time.altzone if is_dst else -time.timezone


def _evening_window_utc(tz_offset):
    """UTC (start, end) for tonight's viewing window. Evening starts 19:00
    local on the "current evening's" date and runs 7 hours to 02:00 local.
    Before 06:00 local we treat tonight as the evening that just began
    yesterday (so 03:00 callers still get tonight's data, not tomorrow's)."""
    import datetime
    now_utc = time.time()
    local_dt = datetime.datetime.utcfromtimestamp(now_utc + tz_offset)
    tonight_date = local_dt.date() if local_dt.hour >= 6 \
                   else local_dt.date() - datetime.timedelta(days=1)
    # Local 19:00 expressed as UTC: build datetime as if in UTC, then subtract offset
    start_local_dt = datetime.datetime.combine(tonight_date, datetime.time(19, 0))
    start_utc = int(start_local_dt.replace(tzinfo=datetime.timezone.utc).timestamp()) - tz_offset
    return tonight_date.isoformat(), start_utc, start_utc + 7 * 3600


def _hhmm_local(unix_t, tz_offset):
    """Unix seconds (UTC) -> 'HH:MM' string in the proxy's local TZ."""
    import datetime
    local_dt = datetime.datetime.utcfromtimestamp(unix_t + tz_offset)
    return local_dt.strftime("%H:%M")


def _tonight_clouds(lat, lon):
    """Pull tonight's cond_id from the existing forecast pipeline (cached
    there as well, so this adds no upstream traffic on a warm cache)."""
    status, body = handle_forecast({"lat": [str(lat)], "lon": [str(lon)]})
    if status != 200:
        return 800, "Clear"          # fail open — better to show planets than nothing
    try:
        fc = json.loads(body)
        days = fc.get("days") or []
        if not days:
            return 800, "Clear"
        today = days[0]
        return int(today.get("cond_id", 800)), today.get("cond", "Clear")
    except Exception:
        return 800, "Clear"


def handle_v2_sky(params):
    """Tonight's naked-eye planet visibility for a named location, bundled
    with a cloud verdict. Same response is good for the whole evening (6h
    cache) — planet altaz changes slowly and we never need sub-minute
    precision for 'is Jupiter up tonight'."""

    lat, lon, _bbox, loc_or_err = resolve_location(params)
    if lat is None:
        return 400, loc_or_err

    cache_key = f"v2:sky:{loc_or_err}"
    cached = cache_get(cache_key, max_age_sec=15 * 60)   # 15 min — sun/moon move
    if cached:
        return 200, cached

    if not _ensure_skyfield():
        return 500, json.dumps({
            "error": "skyfield unavailable",
            "detail": _sky_load_err,
        }).encode()

    from skyfield.api import wgs84
    tz_offset = _local_tz_offset()
    tonight_iso, start_utc, end_utc = _evening_window_utc(tz_offset)

    observer = _sky_eph["earth"] + wgs84.latlon(lat, lon)
    sun_target = _sky_eph["sun"]

    # 30-min sampling — ~15 samples covers the 7h window, more than enough
    # to localize peak altitude. Sun altitude evaluated once per step.
    times_unix = []
    sun_alts   = []
    t = start_utc
    while t <= end_utc:
        times_unix.append(t)
        gm = time.gmtime(t)
        skyt = _sky_ts.utc(gm.tm_year, gm.tm_mon, gm.tm_mday, gm.tm_hour, gm.tm_min)
        sun_alt, _, _ = observer.at(skyt).observe(sun_target).apparent().altaz()
        sun_alts.append(sun_alt.degrees)
        t += 1800

    planets_out = []
    for name, abbr, eph_key, typ_mag in _PLANET_META:
        target = _sky_eph[eph_key]
        best_alt = -90.0
        best_az  = 0.0
        best_t   = None
        rise_t = set_t = None
        prev_alt = None
        for i, t in enumerate(times_unix):
            # Only count samples in usable twilight or darker
            if sun_alts[i] > -6:
                prev_alt = None
                continue
            gm = time.gmtime(t)
            skyt = _sky_ts.utc(gm.tm_year, gm.tm_mon, gm.tm_mday, gm.tm_hour, gm.tm_min)
            alt, az, _ = observer.at(skyt).observe(target).apparent().altaz()
            a = alt.degrees
            if prev_alt is not None:
                if prev_alt < 0 <= a:
                    rise_t = t
                if prev_alt >= 0 > a:
                    set_t = t
            if a > best_alt:
                best_alt, best_az, best_t = a, az.degrees, t
            prev_alt = a

        if best_alt >= 10:           # "easily visible" threshold
            planets_out.append({
                "name":      name,
                "abbr":      abbr,
                "best_alt":  int(round(best_alt)),
                "best_az":   int(round(best_az)) % 360,
                "best_dir":  _compass(best_az),
                "best_time": _hhmm_local(best_t, tz_offset) if best_t else "",
                "rise":      _hhmm_local(rise_t, tz_offset) if rise_t else "",
                "set":       _hhmm_local(set_t,  tz_offset) if set_t  else "",
                "mag":       typ_mag,
                "bright":    _brightness_label(typ_mag),
            })

    cond_id, cond_str = _tonight_clouds(lat, lon)

    # Current sun + moon altaz so the device can show them on the chart and
    # decide whether to render a star field (sun below civil twilight).
    now_t = _sky_ts.now()
    sun_alt, sun_az, _ = observer.at(now_t).observe(_sky_eph["sun"]).apparent().altaz()
    moon_alt, moon_az, _ = observer.at(now_t).observe(_sky_eph["moon"]).apparent().altaz()

    # Moon illumination — angle between sun and moon as seen from earth.
    # cos(elongation) → illum = (1 - cos(e)) / 2.  Skyfield's almanac has
    # fraction_illuminated but we compute it here so we don't add a dep.
    sun_vec  = observer.at(now_t).observe(_sky_eph["sun"]).apparent()
    moon_vec = observer.at(now_t).observe(_sky_eph["moon"]).apparent()
    elong = sun_vec.separation_from(moon_vec).radians
    moon_illum = (1 - math.cos(elong)) / 2

    # Synodic phase (0=new, 0.25=1Q waxing, 0.5=full, 0.75=3Q waning) via
    # the almanac. Lets the device pick crescent direction without having
    # to do its own ecliptic-longitude math.
    from skyfield import almanac
    moon_phase = (almanac.moon_phase(_sky_eph, now_t).degrees / 360.0) % 1.0

    body = json.dumps({
        "tonight":     tonight_iso,
        "cond":        cond_str,
        "cond_id":     cond_id,
        "cloud_score": _cloud_verdict(cond_id),
        "sun": {
            "alt": int(round(sun_alt.degrees)),
            "az":  int(round(sun_az.degrees)) % 360,
        },
        "moon": {
            "alt":    int(round(moon_alt.degrees)),
            "az":     int(round(moon_az.degrees)) % 360,
            "illum":  round(float(moon_illum), 2),
            "phase":  round(float(moon_phase), 3),
            "waxing": bool(moon_phase < 0.5),   # numpy → python bool
        },
        "planets":     planets_out,
    }).encode()
    cache_set(cache_key, body, age_override=15 * 60)
    return 200, body


# ---------------------------------------------------------------------------
# AIS Ship Tracking — WebSocket listener + HTTP endpoint
# ---------------------------------------------------------------------------

_ships = {}         # MMSI -> ship info dict
_ships_lock = Lock()


def _normalize_length(length, ais_type):
    """AIS dimensions are specified in meters per spec, but some small-vessel
    operators misconfigure their transponders and broadcast dimensions in feet
    instead. This shows up as suspiciously large values for vessel types that
    are never that big in real life — e.g. a tug (type 52) reporting length=131
    is impossible at 131 m (≈ 430 ft, larger than a destroyer) but matches a
    real-world tug at 131 ft.

    For AIS types 30–59 (fishing, high-speed, towing, pilot, special craft),
    if the value exceeds 75 m we treat it as a feet reading and convert back
    to meters. Cargo/tanker/passenger (60+) never trigger — those vessels
    routinely exceed 75 m legitimately."""
    if not length:
        return length
    if 30 <= ais_type < 60 and length > 75:
        return round(length / 3.28084)
    return length


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
                "length": _normalize_length(length or 0, type_ or 0),
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

# Decades 4-9 each map to a single category, so bucketing by tens digit works.
AIS_TYPE_NAMES = {
    4: "HighSpeed", 5: "Special",
    6: "Passenger", 7: "Cargo", 8: "Tanker", 9: "Other",
}

# Decade 3 (30-39) is NOT one category — each code is a distinct vessel kind.
# Bucketing it by tens digit mislabels sailing/pleasure/etc. craft as "Fishing".
AIS_TYPE_NAMES_30S = {
    30: "Fishing", 31: "Towing", 32: "Towing", 33: "Dredging",
    34: "Diving", 35: "Military", 36: "Sailing", 37: "Pleasure",
    # 38, 39 are reserved — fall through to "Vessel".
}

def get_ship_type(ais_type):
    """Map AIS type integer (0-99) to category name."""
    if not ais_type:
        return "Vessel"
    if ais_type in AIS_TYPE_NAMES_30S:
        return AIS_TYPE_NAMES_30S[ais_type]
    decade = ais_type // 10
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
        length = _normalize_length(length, type_)
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
    fa_used, fa_limit = flightaware_usage_status()
    if fa_limit and fa_used >= fa_limit:
        issues.append("flightaware_quota_exhausted")
    return 200, json.dumps({
        "status": "ok",
        "issues": issues,
        "cache_entries": len(_cache),
        "ships_tracked": len(_ships),
        "flightaware_month": _fa_period(),
        "flightaware_used": fa_used,
        "flightaware_limit": fa_limit,
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


# ---------------------------------------------------------------------------
# Service status board (/api/status)
# ---------------------------------------------------------------------------
# Aggregates the public status feeds of major cloud/dev providers into one
# compact, normalized payload the 128x64 display polls. All the heavy HTTP +
# parsing lives here so the memory-constrained device just reads pre-digested
# levels. Normalized level scale everywhere: 0 = normal, 1 = degraded,
# 2 = outage. See STATUS_PROVIDERS config near the top of the file.

STATUS_CACHE_SEC = 180        # display polls this; keep upstream load light
_STATUS_TITLE_MAX = 48
_STATUS_COMPONENT_MAX = 24


def _status_trunc(s, limit):
    # Collapse whitespace and drop non-ASCII — the device's BDF fonts only have
    # ASCII glyphs, so curly quotes / em-dashes / etc. would render as gaps.
    s = " ".join(str(s or "").encode("ascii", "ignore").decode().split())
    return s if len(s) <= limit else s[: limit - 2].rstrip() + ".."


def _statuspage_indicator_level(indicator):
    """Map an Atlassian Statuspage status.indicator to our 0/1/2 scale."""
    return {"none": 0, "minor": 1, "major": 2, "critical": 2}.get(
        (indicator or "").lower(), 0)


def _status_statuspage(name, host):
    """Adapter for Atlassian Statuspage sites (GitHub, Cloudflare, Supabase,
    HashiCorp, ...). status.json gives the overall indicator, but that indicator
    is rolled up from every component's state — for Cloudflare that includes the
    handful of edge PoPs perpetually rerouting or in maintenance, which flips the
    indicator to "minor" even though the human status page (which suppresses that
    routine churn) reads "All Systems Operational". So we only trust a non-green
    indicator when summary.json also lists an active incident; otherwise it's
    background noise and we report normal, matching what a person sees."""
    st, body = fetch("https://{}/api/v2/status.json".format(host), timeout=8)
    if st != 200:
        raise RuntimeError("status.json HTTP {}".format(st))
    status_obj = json.loads(body).get("status", {})
    level = _statuspage_indicator_level(status_obj.get("indicator", "none"))
    entry = {"name": name, "level": level}
    if level == 0:
        return entry
    # Indicator is non-green. Confirm there's a real incident before escalating.
    # summary.json's `incidents` array holds only unresolved (active) incidents.
    try:
        st2, body2 = fetch("https://{}/api/v2/summary.json".format(host), timeout=8)
        if st2 != 200:
            raise RuntimeError("summary.json HTTP {}".format(st2))
        incidents = json.loads(body2).get("incidents") or []
    except Exception as e:
        # Can't confirm; don't silently hide a possible real problem. Keep the
        # indicator's level and fall back to the page description for a title.
        _log_proxy_event("status summary {} failed: {}".format(name, e))
        entry["title"] = _status_trunc(
            status_obj.get("description"), _STATUS_TITLE_MAX)
        return entry
    if not incidents:
        # Non-green indicator with no active incident == routine component noise
        # (edge maintenance / partial reroutes). Report normal, like the page.
        entry["level"] = 0
        return entry
    inc = incidents[0]
    entry["title"] = _status_trunc(inc.get("name"), _STATUS_TITLE_MAX)
    comps = inc.get("components") or []
    if comps:
        entry["component"] = _status_trunc(
            comps[0].get("name"), _STATUS_COMPONENT_MAX)
    if not entry.get("title"):
        entry["title"] = _status_trunc(
            status_obj.get("description"), _STATUS_TITLE_MAX)
    return entry


def _status_gcp(name):
    """Adapter for Google Cloud. incidents.json is an array; an incident with
    no `end` timestamp is still open. severity high (or an OUTAGE impact) => 2."""
    st, body = fetch("https://status.cloud.google.com/incidents.json", timeout=8)
    if st != 200:
        raise RuntimeError("incidents.json HTTP {}".format(st))
    incidents = json.loads(body) or []
    worst = None
    for inc in incidents:
        if inc.get("end"):
            continue    # resolved
        impact = (inc.get("status_impact") or "").upper()
        sev = (inc.get("severity") or "").lower()
        lvl = 2 if ("OUTAGE" in impact or sev == "high") else 1
        if worst is None or lvl > worst.get("level", 0):
            comps = inc.get("affected_products") or []
            worst = {
                "name": name,
                "level": lvl,
                "title": _status_trunc(inc.get("external_desc"), _STATUS_TITLE_MAX),
            }
            if comps:
                worst["component"] = _status_trunc(
                    comps[0].get("title"), _STATUS_COMPONENT_MAX)
    return worst or {"name": name, "level": 0}


def _status_aws(name):
    """Adapter for the AWS Health Dashboard public feed. Its shape is not a
    stable contract, so parse defensively: any current event => degraded, and
    an 'outage'/'unavailable' keyword escalates to 2."""
    st, body = fetch("https://health.aws.amazon.com/public/currentevents", timeout=8)
    if st != 200:
        raise RuntimeError("currentevents HTTP {}".format(st))
    data = json.loads(body)
    events = data if isinstance(data, list) else (data.get("events") or [])
    if not events:
        return {"name": name, "level": 0}
    ev = events[0]
    text = " ".join(str(ev.get(k, "")) for k in (
        "event_title", "summary", "service_name", "status_text", "description"))
    lvl = 2 if any(w in text.lower() for w in
                   ("outage", "unavailable", "not available", "down")) else 1
    entry = {"name": name, "level": lvl,
             "title": _status_trunc(text, _STATUS_TITLE_MAX)}
    svc = ev.get("service_name") or ev.get("service")
    region = ev.get("region") or ev.get("region_name")
    comp = " - ".join(x for x in (svc, region) if x)
    if comp:
        entry["component"] = _status_trunc(comp, _STATUS_COMPONENT_MAX)
    return entry


def _status_azure(name):
    """Adapter for Azure. No clean JSON — parse the status RSS feed. Items whose
    text says 'resolved' are skipped; any remaining active item => degraded."""
    import xml.etree.ElementTree as ET
    st, body = fetch("https://azure.status.microsoft/en-us/status/feed/", timeout=8)
    if st != 200:
        raise RuntimeError("azure feed HTTP {}".format(st))
    root = ET.fromstring(body)
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        desc = (item.findtext("description") or "").strip()
        if "resolved" in (title + " " + desc).lower():
            continue
        lvl = 2 if any(w in (title + " " + desc).lower() for w in
                       ("outage", "unavailable", "down")) else 1
        return {"name": name, "level": lvl,
                "title": _status_trunc(title, _STATUS_TITLE_MAX)}
    return {"name": name, "level": 0}


_STATUS_ADAPTERS = {
    "statuspage": lambda p: _status_statuspage(p["name"], p["host"]),
    "gcp":        lambda p: _status_gcp(p["name"]),
    "aws":        lambda p: _status_aws(p["name"]),
    "azure":      lambda p: _status_azure(p["name"]),
}


def handle_status(params):
    """Aggregate provider status feeds into one compact payload for the display.
    Providers at level 0 omit component/title. A feed that errors degrades to
    level 0 (logged) rather than failing the whole board. Cached for
    STATUS_CACHE_SEC so device polling never hammers the upstreams."""
    cache_key = "status"
    cached = cache_get(cache_key, max_age_sec=STATUS_CACHE_SEC)
    if cached:
        return 200, cached

    providers = []
    worst = 0
    for p in STATUS_PROVIDERS:
        name = p.get("name", "?")
        adapter = _STATUS_ADAPTERS.get(p.get("type", "statuspage"))
        if not adapter:
            _log_proxy_event("status: unknown provider type for {}".format(name))
            providers.append({"name": name, "level": 0})
            continue
        try:
            entry = adapter(p)
        except Exception as e:
            _log_proxy_event("status {} failed: {}".format(name, e))
            entry = {"name": name, "level": 0}
        worst = max(worst, entry.get("level", 0))
        providers.append(entry)

    body = json.dumps({
        "providers": providers,
        "worst": worst,
        "ts": int(time.time()),
    }).encode()
    cache_set(cache_key, body, age_override=STATUS_CACHE_SEC)
    return 200, body


ROUTES = {
    "/api/planes":      handle_planes,
    "/api/route":       handle_route,
    "/api/aircraft":    handle_aircraft,
    "/api/forecast":    handle_forecast,
    "/api/tides":       handle_tides,
    "/api/v2/planes":   handle_v2_planes,
    "/api/v2/forecast": handle_v2_forecast,
    "/api/v2/sky":      handle_v2_sky,
    "/api/ships":       handle_ships,
    "/api/ships/debug": handle_ships_debug,
    "/api/sightings":   handle_sightings,
    "/api/devicelog":   handle_devicelog_get,
    "/api/health":      handle_health,
    "/api/status":      handle_status,
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

    server = ThreadingHTTPServer(("", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
