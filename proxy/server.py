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

import calendar
import csv
import datetime
import json
import syslog
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

# Named locations, selected per request with ?loc=<name>. Omitting loc yields
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
    {"name": "Anthropic",  "type": "statuspage", "host": "status.claude.com"},
    {"name": "AWS",        "type": "aws"},
    {"name": "GCP",        "type": "gcp"},
    {"name": "Azure",      "type": "azure"},
]
STATUS_PROVIDERS = _config.get("status_providers") or _DEFAULT_STATUS_PROVIDERS


# Name reported for the proxy's own latitude/longitude/bbox — the location a
# request gets when it doesn't name one.
DEFAULT_LOC_NAME = "default"


def resolve_location(params):
    """Resolve ?loc=<name> against the LOCATIONS config block. Returns
    (lat, lon, bbox, name) on success or (None, None, None, error_body) on
    failure, where error_body is a bytes JSON payload ready to return.
    Omitting loc is valid and yields the proxy's default location."""
    loc = params.get("loc", [""])[0].strip()
    if not loc:
        # No loc given: the proxy's own latitude/longitude/bbox, under the name
        # "default". This is what the endpoints have always done when asked
        # without a location, so a caller that doesn't care about locations
        # never has to learn they exist. An *unknown* name is still an error —
        # quietly serving a different coastline would be worse than a 400.
        return LATITUDE, LONGITUDE, BBOX, DEFAULT_LOC_NAME
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
# Site-local hooks
# ---------------------------------------------------------------------------
# A deployment can drop a `local_hooks.py` next to this file to receive proxy
# events — budget usage, and whatever else gets added — and forward them to
# whatever that particular machine cares about: a dashboard, a notifier, a
# metrics sink. Such integrations are specific to one host, so they are
# deliberately NOT part of this repository (local_hooks.py is gitignored); see
# local_hooks.py.example for the interface.
#
# Everything here is best-effort by design: no module, no `on_event`, a slow
# hook or an exception inside one must never affect serving. The proxy states
# facts; it holds no opinion about alerting policy.
try:
    import local_hooks as _local_hooks
except ImportError:
    _local_hooks = None


def notify_local(event, **fields):
    """Hand one event to the site-local hook module, if this host has one."""
    if _local_hooks is None:
        return
    handler = getattr(_local_hooks, "on_event", None)
    if handler is None:
        return
    try:
        handler(event, fields)
    except Exception as e:
        print(f"local_hooks {event} failed: {e}")


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache = {}       # key -> {"data": bytes, "time": float}
_cache_lock = Lock()
ROUTE_CACHE_PREFIX = "route:"   # cache keys under this prefix persist to disk
ROUTE_CACHE_TTL_HIT = 3600      # a resolved route is good for an hour
ROUTE_CACHE_TTL_MISS = 21600    # a miss is sticky for 6h — see handle_route

# Airport-code aliases applied to resolved routes just before they're cached
# and returned. An exact code from any upstream source (OpenSky/adsbdb/
# FlightAware) is rewritten to its alias, so the substitution is uniform
# regardless of which source resolved the leg. Keyed by the code as the source
# reports it. Default rewrites DJT -> PBI; override with "route_code_aliases".
ROUTE_CODE_ALIASES = _config.get("route_code_aliases", {"DJT": "PBI"})
_started_at = time.time()

# Consecutive OpenSky 429s; reset on the next successful upstream fetch.
# Used by handle_planes to escalate the back-off window from 1h → 2h.
_opensky_429_streak = 0


# One cache window for every location. At 90s a display polling each minute
# sees a cached answer every other poll, which is the trade that keeps two
# locations on one OpenSky account from drawing 429s.
PLANES_CACHE_TTL = 90


def _opensky_backoff_secs():
    """How long to sit out after a 429: 1h for the first in a streak, 2h once
    the next attempt is throttled too."""
    return 7200 if _opensky_429_streak >= 2 else 3600


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
    entry = {"data": data, "time": time.time(), "age_override": age_override}
    with _cache_lock:
        _cache[key] = entry
    # Route lookups are the only expensive entries — each one can cost a
    # billable FlightAware call — so they're mirrored to SQLite and restored at
    # startup (see _route_cache_load). Everything else (planes/weather/sky) is
    # free to re-fetch and short-lived enough that persisting it is pointless.
    # Deliberately outside _cache_lock: disk I/O must not block cache readers.
    if key.startswith(ROUTE_CACHE_PREFIX):
        _route_cache_persist(key, entry)


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
        # Persistent route cache — survives proxy restarts so a restart no
        # longer discards routes we already paid FlightAware to resolve.
        con.execute("""
            CREATE TABLE IF NOT EXISTS route_cache (
                key          TEXT PRIMARY KEY,
                data         BLOB NOT NULL,
                time         REAL NOT NULL,
                age_override REAL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS ships_ts  ON ships(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS planes_ts ON planes(ts)")
        # planes.loc names which display's bounding box saw the aircraft, so
        # traffic — and the FlightAware spend it drives — can be attributed per
        # display. Added after the table shipped, so migrate in place rather
        # than requiring a rebuild; PRAGMA check keeps it idempotent.
        if "loc" not in {r[1] for r in con.execute("PRAGMA table_info(planes)")}:
            con.execute("ALTER TABLE planes ADD COLUMN loc TEXT")
        # Same for vessels: which display's radius the sighting was inside.
        if "loc" not in {r[1] for r in con.execute("PRAGMA table_info(ships)")}:
            con.execute("ALTER TABLE ships ADD COLUMN loc TEXT")


def _route_cache_persist(key, entry):
    """Mirror one route cache entry to SQLite. Best-effort — a failure to
    persist must never break the request that produced the entry."""
    try:
        with _db_lock:
            with sqlite3.connect(DB_PATH) as con:
                con.execute(
                    "INSERT OR REPLACE INTO route_cache (key,data,time,age_override) "
                    "VALUES (?,?,?,?)",
                    (key, entry["data"], entry["time"], entry["age_override"])
                )
    except Exception as e:
        print("route_cache persist failed:", e)


def _route_cache_load():
    """Restore still-fresh route entries into _cache at startup and drop the
    expired rows. Original timestamps are preserved, so entries keep expiring
    on their pre-restart schedule — a restart resumes the cache rather than
    silently granting every entry a fresh TTL. A NULL age_override means the
    entry was stored by the default-TTL path, so it expires at TTL_HIT."""
    now = time.time()
    loaded = 0
    try:
        with _db_lock:
            with sqlite3.connect(DB_PATH) as con:
                con.execute(
                    "DELETE FROM route_cache "
                    "WHERE ? - time >= COALESCE(age_override, ?)",
                    (now, ROUTE_CACHE_TTL_HIT)
                )
                cur = con.execute(
                    "SELECT key,data,time,age_override FROM route_cache")
                for key, data, ts, age in cur:
                    _cache[key] = {"data": bytes(data), "time": ts,
                                   "age_override": age}
                    loaded += 1
    except Exception as e:
        print("route_cache load failed:", e)
    print("Route cache: {} entries restored".format(loaded))

# Deduplicate: don't log the same vessel again within this window
_SHIP_LOG_INTERVAL  = 300   # 5 minutes
_PLANE_LOG_INTERVAL = 120   # 2 minutes
_last_ship_log  = {}  # mmsi  -> last logged ts
_last_plane_log = {}  # callsign -> last logged ts

def log_ship(s, loc=DEFAULT_LOC_NAME):
    """Record one vessel sighting. `loc` is the display whose radius it was
    inside — distances are measured from there, so the same vessel can be a
    different distance away in two rows."""
    mmsi = s.get("mmsi", "")
    now = int(time.time())
    # Throttle per (loc, mmsi): a vessel can sit inside two coastal locations'
    # radii at once, and a shared key would drop the second display's row.
    throttle_key = (loc, mmsi)
    if now - _last_ship_log.get(throttle_key, 0) < _SHIP_LOG_INTERVAL:
        return
    _last_ship_log[throttle_key] = now
    with _db_lock:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO ships (ts,mmsi,name,type_name,lat,lon,speed,heading,distance_mi,destination,loc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (now, mmsi, s.get("name",""), s.get("type_name",""),
                 s.get("lat"), s.get("lon"), s.get("speed"), s.get("heading"),
                 s.get("distance_mi"), s.get("destination",""), loc)
            )

def log_plane(callsign, icao24, alt_ft, speed_kt, heading, lat, lon,
              loc="default", obs_lat=None, obs_lon=None):
    """Record one aircraft sighting. `loc` names the display whose bounding box
    saw it — "default" for the v1 endpoint, which uses the proxy's own
    lat/lon/bbox rather than a named location. obs_lat/obs_lon are the observer
    the distance is measured from, defaulting to the proxy's globals."""
    now = int(time.time())
    # Throttle per (loc, callsign), not per callsign: one aircraft can sit
    # inside both displays' boxes at once, and a shared key would silently drop
    # the second display's sighting — exactly the under-count this logging is
    # meant to fix.
    throttle_key = (loc, callsign)
    if now - _last_plane_log.get(throttle_key, 0) < _PLANE_LOG_INTERVAL:
        return
    _last_plane_log[throttle_key] = now
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
    distance_mi = _dist(LATITUDE if obs_lat is None else obs_lat,
                        LONGITUDE if obs_lon is None else obs_lon, lat, lon)
    with _db_lock:
        with sqlite3.connect(DB_PATH) as con:
            con.execute(
                "INSERT INTO planes (ts,callsign,icao24,alt_ft,speed_kt,heading,lat,lon,distance_mi,loc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now, callsign, icao24, alt_ft, speed_kt, heading, lat, lon,
                 distance_mi, loc)
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
    """Fetch aircraft in a bounding box from OpenSky and return a slim
    device-friendly response — only the fields the device actually uses, with
    on-ground / no-callsign rows already filtered out.

    Cuts the JSON payload roughly in half vs. raw OpenSky, which matters a lot
    to the SAMD51 device: smaller parse = less heap fragmentation.

    The box comes from ?loc=<name>, or the proxy's own location when none is
    given. Raw ?lat=/?lon=/?bbox= still override both, and are cached under
    their coordinates so one caller's ad-hoc box can't be served to another.
    This is the single implementation behind both /api/planes and the
    deprecated /api/v2/planes."""
    global _opensky_429_streak

    lat, lon, bbox, loc = resolve_location(params)
    if lat is None:
        return 400, loc

    if any(k in params for k in ("lat", "lon", "bbox")):
        lat = float(params.get("lat", [lat])[0])
        lon = float(params.get("lon", [lon])[0])
        bbox = float(params.get("bbox", [bbox])[0])
        cache_key = f"planes:{lat},{lon},{bbox}"
    else:
        cache_key = f"planes:{loc}"

    cached = cache_get(cache_key, max_age_sec=PLANES_CACHE_TTL)
    if cached:
        return 200, cached

    # A 429 throttles the whole OpenSky account, not one bounding box, so once
    # a streak is running don't let a second location go ask again — it would
    # only burn the remaining credits and deepen the backoff.
    if _opensky_429_streak > 0:
        empty = json.dumps({"time": 0, "planes": [], "rate_limited": True}).encode()
        cache_set(cache_key, empty, age_override=_opensky_backoff_secs())
        return 200, empty

    url = (
        f"https://opensky-network.org/api/states/all"
        f"?lamin={lat-bbox}&lomin={lon-bbox}"
        f"&lamax={lat+bbox}&lomax={lon+bbox}"
    )
    status, data = fetch(url, headers=opensky_headers())

    if status == 429:
        _opensky_429_streak += 1
        # First 429 in a streak backs off 1h; successive ones escalate to 2h,
        # to be a better citizen to the OpenSky API.
        backoff_secs = _opensky_backoff_secs()
        empty = json.dumps({"time": 0, "planes": [], "rate_limited": True}).encode()
        cache_set(cache_key, empty, age_override=backoff_secs)
        _log_proxy_event("OpenSky 429 #{} (loc={}) — backing off {}h".format(
            _opensky_429_streak, loc, backoff_secs // 3600))
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
                log_plane(callsign[:8], s[0] or "", entry[2], entry[3], entry[4],
                          p_lat, p_lon, loc=loc, obs_lat=lat, obs_lon=lon)
            except Exception:
                continue                                 # skip malformed rows, keep the rest
        body = json.dumps({"time": raw.get("time", 0), "planes": planes}).encode()
        cache_set(cache_key, body, age_override=PLANES_CACHE_TTL)
        return 200, body
    except Exception as e:
        return 200, json.dumps({"time": 0, "planes": [], "error": str(e)}).encode()


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

# Geo-plausibility check on the free-tier route: a narrower, self-limiting
# alternative to FLIGHTAWARE_OVERRIDE_FREE for deciding when a paid lookup is
# worth it. See route_geo_implausible() below. "observe" computes and logs a
# verdict without gating spend; "enforce" lets a flagged verdict trigger the
# paid call; "off" skips the check entirely. Defaults to "observe" — new code
# that can spend money should never ship pre-armed.
ROUTE_GEO_CHECK_MODE = str(_config.get("route_geo_check_mode", "observe")).strip().lower()
if ROUTE_GEO_CHECK_MODE not in ("off", "observe", "enforce"):
    ROUTE_GEO_CHECK_MODE = "observe"   # unrecognized value fails safe, not open
ROUTE_GEO_CHECK_DEADBAND_DEG = max(0.0, float(_config.get("route_geo_check_deadband_deg", 0.5)))

_AIRPORT_COORDS_PATH = Path(__file__).parent / "airport_coords.csv"


def _load_airport_coords():
    """icao -> longitude (float), for the route geo-plausibility check.
    Missing/unreadable/malformed file -> empty dict, which fails the whole
    check open (see route_geo_implausible) rather than crashing startup."""
    d = {}
    try:
        with open(_AIRPORT_COORDS_PATH, newline="") as f:
            for row in csv.DictReader(f):
                icao = (row.get("icao") or "").strip().upper()
                lon = row.get("lon")
                if not icao or lon in (None, ""):
                    continue
                try:
                    d[icao] = float(lon)
                except ValueError:
                    continue
    except Exception as e:
        print(f"airport_coords.csv load failed: {e}")
    return d


_AIRPORT_LON = _load_airport_coords()
_geo_check_lock = Lock()
_geo_check_stats = {"total": 0, "flagged": 0, "unknown": 0, "enforced": 0}

# Notable events also go to the system journal, not just this proxy's own
# device.log — the journal is where a host's existing log tooling already
# looks, so the proxy can state a fact and stay out of alerting policy.
syslog.openlog(ident="matrix-portal-proxy")

# Budget-approaching warnings: the first time this month's usage crosses each
# configured threshold, say so on the system journal. The highest configured
# threshold logs at ERR, earlier ones at WARNING. State is in-memory only, so
# a restart right after a crossing can re-log it once — harmless, since
# journal-watching tools group by message text and apply their own cooldown.
FLIGHTAWARE_ALERT_THRESHOLDS = sorted(
    int(t) for t in _config.get("flightaware_alert_thresholds", [1000, 1800]))
_fa_alert_logged = set()   # {(period, threshold), ...} logged this process

# Optional schedule gate. When the applicable flag file exists and reads as
# "off" (or 0/false/no/free), FlightAware is skipped entirely for that scope —
# free routes only, zero billable calls — regardless of the override and the
# monthly cap. A missing or unreadable flag means enabled, so the
# default/normal behavior is unchanged and the box fails safe toward its usual
# operation. Files are (re-)read cheaply on each route lookup, so cron can flip
# them with no service restart.
#
# Per-location file (flightaware_enabled_<loc>) takes precedence over the
# master file (flightaware_enabled) when a request carries a ?loc=<name>, so
# one shared proxy can run paid enrichment on a schedule for a single location
# (e.g. weekends-only for a shared multi-location box) without affecting
# others. Requests with no loc (single-location v1 callers) only ever consult
# the master file.
_FA_ENABLED_FLAG_PATH = Path(__file__).parent / "flightaware_enabled"


def _fa_flag_dir():
    return _FA_ENABLED_FLAG_PATH.parent


def flightaware_enabled_now(loc=""):
    """False only when the applicable schedule flag file explicitly says off;
    True when it's absent/unreadable (fail-safe to normal FlightAware
    behavior). Checks the per-location file first, then falls back to the
    master file."""
    candidates = []
    if loc:
        candidates.append(_fa_flag_dir() / f"flightaware_enabled_{loc}")
    candidates.append(_FA_ENABLED_FLAG_PATH)
    for path in candidates:
        try:
            val = path.read_text().strip().lower()
        except Exception:
            continue
        return val not in ("off", "0", "false", "no", "free")
    return True
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
        d = {"period": _fa_period(), "count": 0, "by_loc": {}}
    d.setdefault("by_loc", {})      # usage files written before per-loc split
    return d


def _flightaware_check_thresholds(count, limit):
    """Warn on the system journal the first time usage crosses each configured
    threshold this month. Purely informational — it never gates a call."""
    period = _fa_period()
    for i, threshold in enumerate(FLIGHTAWARE_ALERT_THRESHOLDS):
        if count < threshold:
            continue
        key = (period, threshold)
        if key in _fa_alert_logged:
            continue
        _fa_alert_logged.add(key)
        pct = round(100 * threshold / limit) if limit else 0
        priority = (syslog.LOG_ERR
                    if i == len(FLIGHTAWARE_ALERT_THRESHOLDS) - 1
                    else syslog.LOG_WARNING)
        syslog.syslog(
            priority,
            f"FlightAware AeroAPI usage crossed {threshold} of {limit} calls "
            f"this month ({pct}%) — {count} used so far",
        )


def _fa_usage_write(d):
    try:
        _FA_USAGE_PATH.write_text(json.dumps(d))
    except Exception as e:
        print(f"FlightAware usage write failed: {e}")


def flightaware_usage_by_loc():
    """This month's billable-call count per display, e.g. {"home": 412,
    "beach": 1588}. "default" covers the v1 endpoint (no loc param)."""
    with _fa_usage_lock:
        return dict(_fa_usage_read().get("by_loc", {}))


def flightaware_usage_status():
    """(used, limit) for the current month — surfaced on /api/health."""
    with _fa_usage_lock:
        return _fa_usage_read().get("count", 0), FLIGHTAWARE_MONTHLY_LIMIT


def _flightaware_reserve(loc=""):
    """Atomically reserve one billable FlightAware call against the monthly
    cap. Returns True (and increments) if under the limit, else False. Reserving
    *before* the call makes the ceiling hard even under concurrent requests.
    `loc` attributes the spend to the display that triggered it — the cap is
    shared between displays, so without this there's no way to see which one is
    consuming it (surfaced as flightaware_used_by_loc on /api/health)."""
    key = loc or "default"
    with _fa_usage_lock:
        d = _fa_usage_read()
        if d.get("count", 0) >= FLIGHTAWARE_MONTHLY_LIMIT:
            return False
        d["count"] = d.get("count", 0) + 1
        d["by_loc"][key] = d["by_loc"].get(key, 0) + 1
        _fa_usage_write(d)
        count = d["count"]
    # Outside the lock: neither the journal nor a site-local hook should be
    # able to stall a request that's holding the usage file.
    _flightaware_check_thresholds(count, FLIGHTAWARE_MONTHLY_LIMIT)
    notify_local("flightaware_usage", count=count, limit=FLIGHTAWARE_MONTHLY_LIMIT)
    return True


def _flightaware_refund(loc=""):
    """Return a reserved slot to the pool when the request didn't actually bill
    (non-2xx response, or a network error before reaching FlightAware). Must be
    given the same `loc` the reservation used, or the per-display tallies drift
    from the total."""
    key = loc or "default"
    with _fa_usage_lock:
        d = _fa_usage_read()
        if d.get("count", 0) > 0:
            d["count"] -= 1
            if d["by_loc"].get(key, 0) > 0:
                d["by_loc"][key] -= 1
            _fa_usage_write(d)
        count = d.get("count", 0)
    notify_local("flightaware_usage", count=count, limit=FLIGHTAWARE_MONTHLY_LIMIT)


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


def _observer_lon_for_route_check(loc):
    """Best-effort observer longitude for the geo-plausibility check. Unlike
    resolve_location(), this never errors: an unknown/blank loc or a
    malformed locations{} entry falls back to the global LONGITUDE default.
    The geo check is a heuristic nudge toward spending money, not a
    correctness requirement — it must never block a route lookup."""
    entry = LOCATIONS.get(loc) if loc else None
    if entry:
        try:
            return float(entry.get("lon", LONGITUDE))
        except (TypeError, ValueError):
            return LONGITUDE
    return LONGITUDE


def _route_lon_plausible(origin_icao, dest_icao, observer_lon, deadband_deg):
    """True/False if we can evaluate whether observer_lon plausibly lies
    between origin/dest longitude (with deadband_deg slack past either
    endpoint); None ("unknown") if either ICAO isn't in the coordinate
    table. Callers must treat None as "don't gate on this", not as either
    verdict — that's the fail-open contract for unrecognized airports."""
    o_lon = _AIRPORT_LON.get((origin_icao or "").strip().upper())
    d_lon = _AIRPORT_LON.get((dest_icao or "").strip().upper())
    if o_lon is None or d_lon is None:
        return None
    lo, hi = (o_lon, d_lon) if o_lon <= d_lon else (d_lon, o_lon)
    return (lo - deadband_deg) <= observer_lon <= (hi + deadband_deg)


def _geo_check_note(verdict):
    with _geo_check_lock:
        _geo_check_stats["total"] += 1
        if verdict is None:
            _geo_check_stats["unknown"] += 1
        elif verdict is False:
            _geo_check_stats["flagged"] += 1


def route_geo_implausible(route, loc):
    """True only when the free-tier route looks confidently wrong for this
    observer — a positive signal to spend on FlightAware. Every failure mode
    (check off, route not a [origin, dest] pair, unrecognized airport)
    returns False: this function is only ever allowed to add spend, never to
    add certainty it doesn't have."""
    if ROUTE_GEO_CHECK_MODE == "off" or not route or len(route) != 2:
        return False
    observer_lon = _observer_lon_for_route_check(loc)
    verdict = _route_lon_plausible(route[0], route[1], observer_lon, ROUTE_GEO_CHECK_DEADBAND_DEG)
    _geo_check_note(verdict)
    return verdict is False


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
    loc = params.get("loc", [""])[0].strip()
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

    # 2.5. Geo-plausibility check on whatever free-tier route was found above.
    #      If the observer isn't plausibly between the origin/dest longitudes
    #      (see route_geo_implausible), the free answer is likely stale for a
    #      reused callsign — worth a paid lookup in "enforce" mode. Always
    #      evaluated (even in "observe" mode) so its counters/log stay useful
    #      for tuning before it's allowed to gate spend.
    geo_flag = route_geo_implausible(result["route"], loc)
    if geo_flag:
        _log_proxy_event(
            f"route geo-check: {callsign} route={result['route']} "
            f"loc={loc or 'default'} deadband={ROUTE_GEO_CHECK_DEADBAND_DEG}deg "
            f"mode={ROUTE_GEO_CHECK_MODE}"
            + ("" if ROUTE_GEO_CHECK_MODE == "enforce" else " [observe-only, not gating]")
        )

    # 3. FlightAware AeroAPI — paid, best accuracy, real-time. Consulted to
    #    *override* the free scheduled-route answer (which can be stale when a
    #    callsign is reused for a different leg), not merely as a last resort.
    #    Bounded by: FLIGHTAWARE_OVERRIDE_FREE, the monthly spend cap, the
    #    GA-registration skip, and the per-(callsign,icao24) route cache. With
    #    the override off, falls back to the old "only when free found nothing"
    #    behavior — except now the geo-plausibility check (above) can also
    #    promote a "free tier found something, but it looks geographically
    #    wrong" case into a paid lookup, once ROUTE_GEO_CHECK_MODE is
    #    "enforce".
    # True only when the geo check is the *reason* fa_should_consult fires —
    # i.e. override is off and the free tier did find a route, so without the
    # geo check this callsign would NOT have been consulted. Used to log/count
    # actual paid pickups attributable to this check, not just flagged verdicts.
    geo_is_deciding_reason = (
        ROUTE_GEO_CHECK_MODE == "enforce" and geo_flag
        and not FLIGHTAWARE_OVERRIDE_FREE and result["route"])

    fa_should_consult = flightaware_enabled_now(loc) and (
        FLIGHTAWARE_OVERRIDE_FREE
        or not result["route"]
        or (ROUTE_GEO_CHECK_MODE == "enforce" and geo_flag))
    if fa_should_consult and FLIGHTAWARE_KEY and not _is_ga_registration(callsign):
        if not _flightaware_reserve(loc):
            _flightaware_note_exhausted()   # cap hit — skip the billable call
        else:
            if geo_is_deciding_reason:
                with _geo_check_lock:
                    _geo_check_stats["enforced"] += 1
                geo_msg = (
                    f"route geo-check ENFORCED: {callsign} route={result['route']} "
                    f"loc={loc or 'default'} — paid FlightAware call triggered by the geo check"
                )
                _log_proxy_event(geo_msg)
                # Also to the system journal, so spend is visible to a host's
                # log tooling and not just this proxy's device.log. Logged at
                # INFO, not WARNING: the geo check doing its job and buying a
                # correction is the expected outcome, not a fault, and it fires
                # often enough (~120/day here) to drown a channel that escalates
                # warn-and-above. The budget-threshold alerts stay at WARNING/ERR
                # because those do want a human. Counted either way in
                # route_geo_checks_enforced on /api/health.
                syslog.syslog(syslog.LOG_INFO, geo_msg)
            fa_url = f"https://aeroapi.flightaware.com/aeroapi/flights/{callsign}"
            fa_status, fa_data = fetch(fa_url, headers={"x-apikey": FLIGHTAWARE_KEY})
            if fa_status != 200:
                _flightaware_refund(loc)   # non-2xx doesn't bill — reclaim the slot
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

    if result["route"] and ROUTE_CODE_ALIASES:
        result["route"] = [ROUTE_CODE_ALIASES.get(code, code) for code in result["route"]]

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
    Returns today, tomorrow, and day-after with hi/lo/condition/wind. Cached
    for an hour per location. Takes ?loc=<name>, the proxy's own location when
    none is given, or raw ?lat=/?lon= overrides. Single implementation behind
    both /api/forecast and the deprecated /api/v2/forecast."""

    if not OWM_KEY:
        return 500, json.dumps({"error": "no openweather_key configured"}).encode()

    lat, lon, _bbox, loc = resolve_location(params)
    if lat is None:
        return 400, loc

    if any(k in params for k in ("lat", "lon")):
        lat = float(params.get("lat", [lat])[0])
        lon = float(params.get("lon", [lon])[0])
        cache_key = f"forecast:{lat},{lon}"
    else:
        cache_key = f"forecast:{loc}"
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
    the device runs out of valid cached predictions.

    The station comes from an explicit ?station=, else the station configured
    on ?loc=<name>, else the global noaa_station. Caching is keyed on the
    station itself rather than the location, so two locations sharing a station
    share one cached month."""
    import datetime

    station = params.get("station", [""])[0].strip()
    if not station:
        _lat, _lon, _bbox, loc = resolve_location(params)
        if _lat is None:
            return 400, loc
        station = station_for(loc)
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
# Location-aware handlers
# ---------------------------------------------------------------------------
# Every endpoint below takes an optional ?loc=<name> resolved against the
# `locations` config block; without one it serves the proxy's own coordinates.
# The /api/v2/* paths are deprecated aliases for the same handlers, kept only
# until the displays stop calling them — see DEPRECATED_ROUTES.
# ---------------------------------------------------------------------------
# Sky — naked-eye planet visibility for the upcoming evening, with cloud
# verdict from the location's forecast. Powered by JPL DE421 via skyfield.
# Skyfield is lazy-loaded so a missing install only affects /api/sky;
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

# Tropical zodiac: 12 equal 30° signs measured from the vernal equinox along
# the ecliptic of date. Index = floor(ecliptic_longitude / 30).
_ZODIAC_SIGNS = (
    "Aries", "Taurus", "Gemini", "Cancer", "Leo", "Virgo",
    "Libra", "Scorpio", "Sagittarius", "Capricorn", "Aquarius", "Pisces",
)


def _zodiac_sign(ecl_lon_deg):
    """Ecliptic longitude (degrees) -> tropical zodiac sign name."""
    return _ZODIAC_SIGNS[int(ecl_lon_deg // 30) % 12]


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


def handle_sky(params):
    """Tonight's naked-eye planet visibility for a named location, bundled
    with a cloud verdict. Same response is good for the whole evening (6h
    cache) — planet altaz changes slowly and we never need sub-minute
    precision for 'is Jupiter up tonight'."""

    lat, lon, _bbox, loc_or_err = resolve_location(params)
    if lat is None:
        return 400, loc_or_err

    cache_key = f"sky:{loc_or_err}"
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

    # Tropical zodiac sign each classical body currently occupies. Geocentric
    # apparent ecliptic longitude in the ecliptic/equinox of date (epoch=now_t)
    # — this is what "the Sun is in Leo" means. Independent of tonight's
    # visibility, so we cover the Sun, Moon, and all five naked-eye planets.
    geo = _sky_eph["earth"]

    def _sign_of(target):
        _lat, ecl_lon, _dist = (
            geo.at(now_t).observe(target).apparent().ecliptic_latlon(epoch=now_t)
        )
        return _zodiac_sign(ecl_lon.degrees)

    zodiac = {
        "Sun":  _sign_of(_sky_eph["sun"]),
        "Moon": _sign_of(_sky_eph["moon"]),
    }
    for _name, _abbr, _eph_key, _mag in _PLANET_META:
        zodiac[_name] = _sign_of(_sky_eph[_eph_key])

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
        "zodiac":      zodiac,
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
SHIP_MAX_MILES = 10    # display radius when a location doesn't set its own

# A location opts in to ship tracking by giving its `locations` entry a
# `ship_radius_mi`. Only those places are subscribed to on AISStream, so an
# inland location costs no bandwidth and simply has no vessels near it. With
# nothing opted in, the proxy's own coordinates are used — what this endpoint
# has always done.
_AIS_BOX_DEGREES = 1.0   # ~69 miles; see _ais_boxes() for why it stays wide


def ship_locations():
    """[(name, lat, lon, radius_mi), ...] for every location tracking ships."""
    out = []
    for name, entry in sorted(LOCATIONS.items()):
        radius = entry.get("ship_radius_mi")
        if radius is None:
            continue
        try:
            radius = float(radius)
        except (TypeError, ValueError):
            _log_proxy_event(f"ships: bad ship_radius_mi for {name}, ignoring")
            continue
        out.append((name, float(entry.get("lat", LATITUDE)),
                    float(entry.get("lon", LONGITUDE)), radius))
    if not out:
        out.append((DEFAULT_LOC_NAME, LATITUDE, LONGITUDE, float(SHIP_MAX_MILES)))
    return out


def station_for(loc):
    """NOAA station for a location — its own `station`, else the global
    noaa_station. Tide stations are per-coastline, so a second display on a
    different shore needs its own or it shows the wrong water."""
    entry = LOCATIONS.get(loc) or {}
    return str(entry.get("station") or NOAA_STATION)


def ship_radius_for(loc):
    """Display radius in miles for one location. A location that never opted in
    still gets an answer — the shared vessel pool just won't hold anything near
    it, so the list comes back empty on its own rather than by special case."""
    entry = LOCATIONS.get(loc) or {}
    try:
        return float(entry.get("ship_radius_mi", SHIP_MAX_MILES))
    except (TypeError, ValueError):
        return float(SHIP_MAX_MILES)


def _ais_boxes():
    """AISStream bounding boxes — one per ship-tracking location.

    Deliberately generous (±1°, ~69 miles) rather than sized to the display
    radius. A vessel's name, type and length arrive in sporadic Type 5 messages
    rather than with every position report, so tracking it long before it comes
    into range is what lets _vessel_static_cache know what it is by the time it
    matters — and handle_ships drops any vessel it can't name. Overlapping boxes
    are harmless: _process_ais_message upserts by MMSI, so a duplicate delivery
    costs a little CPU and changes nothing."""
    d = _AIS_BOX_DEGREES
    return [[[lat - d, lon - d], [lat + d, lon + d]]
            for _name, lat, lon, _radius in ship_locations()]

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
                # Boxes are fixed at connect time, so a change to which
                # locations track ships needs a service restart, not just a
                # config edit.
                subscribe = {
                    "APIKey": AISSTREAM_KEY,
                    "BoundingBoxes": _ais_boxes(),
                    "FilterMessageTypes": ["PositionReport", "ShipStaticData"],
                }
                print("AIS: connecting to {} for {}".format(
                    url, ", ".join(n for n, _la, _lo, _r in ship_locations())))
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
    """Return list of nearby ships — filtered by size and distance from
    ?loc=<name>, or the proxy's own location when none is given.

    Static fields (name/type/type_name/callsign/length) missing from the
    live AIS feed are filled in from the persistent vessel_static cache,
    so vessels we've seen before always carry full context even when
    today's WebSocket session hasn't received a fresh Type 5 message."""
    lat0, lon0, _bbox, loc = resolve_location(params)
    if lat0 is None:
        return 400, loc
    max_miles = ship_radius_for(loc)
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
        dist = _distance_miles(lat0, lon0, lat, lon)
        if dist > max_miles:
            continue
        dist_mi = round(dist, 1)
        log_ship({**s, "distance_mi": dist_mi}, loc=loc)
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
        "flightaware_used_by_loc": flightaware_usage_by_loc(),
        "flightaware_limit": fa_limit,
        "flightaware_enabled": flightaware_enabled_now(),
        "route_geo_check_mode": ROUTE_GEO_CHECK_MODE,
        "route_geo_checks_total": _geo_check_stats["total"],
        "route_geo_checks_flagged": _geo_check_stats["flagged"],
        "route_geo_checks_unknown": _geo_check_stats["unknown"],
        "route_geo_checks_enforced": _geo_check_stats["enforced"],
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
    """Return raw ship data without filtering, for diagnostics. Distances are
    measured from ?loc=<name>, like /api/ships."""
    lat0, lon0, _bbox, loc = resolve_location(params)
    if lat0 is None:
        return 400, loc
    _prune_stale_ships()
    with _ships_lock:
        ships_raw = list(_ships.values())
    ships_raw.sort(key=lambda s: _distance_miles(
        lat0, lon0, s.get("lat", 0), s.get("lon", 0)
    ))
    annotated = []
    for s in ships_raw[:20]:
        d = dict(s)
        d["distance_mi"] = round(_distance_miles(
            lat0, lon0, s.get("lat", 0), s.get("lon", 0)
        ), 1)
        annotated.append(d)
    return 200, json.dumps({"ships": annotated, "total": len(ships_raw),
                            "loc": loc}).encode()


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
    if len(s) <= limit:
        return s
    # Truncate at a word boundary so the label doesn't end mid-word
    # ("...a limited numb.."). Reserve 2 chars for the ".." marker. Fall back to
    # a hard character cut when the first word alone overflows (no space to break
    # on), or when honoring the word boundary would throw away most of the budget
    # — a long word early in the string is better shown clipped than as almost
    # nothing. ".." stays ASCII on purpose (BDF fonts have no ellipsis glyph).
    head = s[: limit - 2]
    cut = head.rfind(" ")
    if cut >= (limit - 2) * 0.6:
        head = head[:cut]
    return head.rstrip(" ,.;:-") + ".."


def _statuspage_indicator_level(indicator):
    """Map an Atlassian Statuspage status.indicator to our 0/1/2 scale."""
    return {"none": 0, "minor": 1, "major": 2, "critical": 2}.get(
        (indicator or "").lower(), 0)


def _statuspage_component_level(status):
    """Map an Atlassian Statuspage *component* status to our 0/1/2 scale.
    This is the current, live state of one service — unlike the page indicator
    or an incident's impact, which stay pinned at the peak until resolution."""
    return {
        "operational": 0,
        "degraded_performance": 1,
        "under_maintenance": 1,
        "partial_outage": 1,
        "major_outage": 2,
    }.get((status or "").lower(), 0)


def _statuspage_updated_epoch(iso):
    """Parse a Statuspage ISO8601-UTC timestamp (e.g. 2026-08-17T20:45:28.860Z)
    into a Unix epoch. Returns None if absent/unparseable."""
    if not iso:
        return None
    s = str(iso).split(".")[0].rstrip("Z")
    try:
        return int(calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return None


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
    ind_level = _statuspage_indicator_level(status_obj.get("indicator", "none"))
    entry = {"name": name, "level": ind_level}
    if ind_level == 0:
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
    # Grade severity off the incident's CURRENT affected-component statuses, not
    # the page indicator / incident impact — those stay pinned at the peak
    # ("major"/"critical") until the incident is resolved, so they keep screaming
    # OUTAGE for hours after the services actually recovered. The worst live
    # component is what a person reading the page sees right now.
    comps = inc.get("components") or []
    worst_comp = None
    level = 0
    for c in comps:
        cl = _statuspage_component_level(c.get("status"))
        if worst_comp is None or cl > level:
            level, worst_comp = cl, c
    if not comps:
        level = ind_level    # no per-component detail; trust the indicator
    entry["level"] = level
    updated = _statuspage_updated_epoch(inc.get("updated_at"))
    if updated:
        entry["updated"] = updated
    if level == 0:
        # Incident still open but every affected component is back to
        # operational — GitHub just hasn't clicked "resolve". Report normal.
        return entry
    entry["title"] = _status_trunc(inc.get("name"), _STATUS_TITLE_MAX)
    if worst_comp is not None:
        entry["component"] = _status_trunc(
            worst_comp.get("name"), _STATUS_COMPONENT_MAX)
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


# AWS keeps recently-resolved events in currentevents, and sometimes leaves a
# real event open (no end_time) yet stops updating it for months. So we can't
# just take events[0]: we filter to genuinely-active events and grade by the
# feed's numeric status (0 normal/resolved, 1 informational, 2 degradation,
# 3 disruption). Any event we haven't seen an update on in a week is treated as
# stale and dropped — a real ongoing AWS event gets updated far more often than
# that, so a week-quiet entry is an abandoned-open feed artifact, not a signal.
_AWS_STATUS_LEVEL = {"2": 1, "3": 2}          # 0/1 => not shown
_AWS_STALE_SEC = 7 * 86400


def _aws_event_updated(ev):
    """Epoch of an AWS event's most recent activity: newest event_log timestamp,
    falling back to the event's `date`. None if neither parses."""
    best = None
    for logentry in (ev.get("event_log") or []):
        try:
            ts = int(logentry.get("timestamp"))
        except (TypeError, ValueError):
            continue
        if best is None or ts > best:
            best = ts
    if best is None:
        try:
            best = int(ev.get("date"))
        except (TypeError, ValueError):
            best = None
    return best


def _status_aws(name):
    """Adapter for the AWS Health Dashboard public feed (UTF-16 JSON; json.loads
    handles the BOM). Report the worst genuinely-active event: skip anything
    with an end_time (resolved) or not updated in the last 7 days (stale). See
    the module comment above for the rationale."""
    st, body = fetch("https://health.aws.amazon.com/public/currentevents", timeout=8)
    if st != 200:
        raise RuntimeError("currentevents HTTP {}".format(st))
    data = json.loads(body)
    events = data if isinstance(data, list) else (data.get("events") or [])
    now = int(time.time())
    worst = None          # (level, updated_epoch, event)
    for ev in events:
        if ev.get("end_time"):
            continue      # resolved
        lvl = _AWS_STATUS_LEVEL.get(str(ev.get("status")).strip())
        if not lvl:
            continue      # normal / informational — don't light the board
        updated = _aws_event_updated(ev)
        if updated is None or now - updated > _AWS_STALE_SEC:
            continue      # stale (or undatable) — drop it
        key = (lvl, updated)
        if worst is None or key > worst[0:2]:
            worst = (lvl, updated, ev)
    if worst is None:
        return {"name": name, "level": 0}
    lvl, updated, ev = worst
    entry = {"name": name, "level": lvl,
             "title": _status_trunc(ev.get("summary"), _STATUS_TITLE_MAX)}
    if updated:
        entry["updated"] = updated
    comp = " - ".join(x for x in (
        ev.get("service_name") or ev.get("service"),
        ev.get("region_name") or ev.get("region")) if x)
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


# ---------------------------------------------------------------------------
# Calendar  (/api/calendar)
# ---------------------------------------------------------------------------
# Reduces one or more private .ics feeds (Google Calendar's "Secret address in
# iCal format") to the only thing the display needs: what is happening today
# and tomorrow, as a name plus a start time. Everything awkward about
# iCalendar — folded lines, TZID resolution, RRULE expansion, EXDATE holes,
# RECURRENCE-ID overrides — is handled here so the memory-constrained device
# just renders two short lists. Events from every configured calendar are
# pooled; which feed one came from is deliberately not reported.
#
# The feed URLs are secrets (anyone holding one can read the whole calendar),
# so they live in config.json and are never echoed back or logged.

CALENDAR_CACHE_SEC = 600      # device polls at this cadence; feeds change slowly
_CAL_NAME_MAX = 24            # chars — exactly what the device's 4x6 row fits
_CAL_MAX_PER_DAY = 12         # events per day sent to the device
_CAL_ITER_CAP = 2000          # hard bound on RRULE expansion steps per event
_CAL_NO_TITLE = "(no title)"
_CAL_DOW = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_CAL_MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
            "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

# Accepts either bare URL strings or {"url": ...} objects, so a calendar can be
# given a config-side label without the endpoint caring.
CALENDAR_FEEDS = []
for _entry in (_config.get("calendar_ics_urls") or []):
    _url = str((_entry.get("url", "") if isinstance(_entry, dict) else _entry) or "").strip()
    if _url.startswith("webcal://"):
        _url = "https://" + _url[len("webcal://"):]
    if _url:
        CALENDAR_FEEDS.append(_url)


# --- Timezones -------------------------------------------------------------

_cal_zone_cache = {}


def _cal_zone(tzid):
    """ZoneInfo for an IANA TZID, or None when it can't be resolved (unknown
    name, or no tzdata installed). Cached — ZoneInfo construction hits disk."""
    if not tzid:
        return None
    if tzid in _cal_zone_cache:
        return _cal_zone_cache[tzid]
    zone = None
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tzid)
    except Exception as e:
        _log_proxy_event("calendar: unresolved TZID {} ({})".format(tzid, e))
    _cal_zone_cache[tzid] = zone
    return zone


def _cal_local_zone():
    """The timezone "today" and "tomorrow" are measured in. Prefers the
    `timezone` config key, then the host's /etc/timezone, and finally the fixed
    UTC offset the C library reports for right now — which is correct across
    the two days this endpoint spans."""
    zone = _cal_zone(str(_config.get("timezone", "")).strip())
    if zone:
        return zone
    try:
        with open("/etc/timezone") as f:
            zone = _cal_zone(f.read().strip())
    except OSError:
        zone = None
    return zone or datetime.datetime.now().astimezone().tzinfo


# --- iCalendar parsing -----------------------------------------------------

def _ics_unfold(text):
    """Logical iCalendar lines. Long content lines are folded at 75 octets with
    a leading space or tab on each continuation (RFC 5545 3.1), so they have to
    be rejoined before anything else can be parsed."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _ics_unescape(value):
    r"""Undo TEXT escaping (RFC 5545 3.3.11): \n and \N are newlines, and a
    backslash before any other character (comma, semicolon, backslash) escapes
    it. Scanned left to right so an escaped backslash can't swallow the
    character after it."""
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in ("n", "N") else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _ics_parse_line(line):
    """'DTSTART;TZID=America/New_York:20260916T090000' ->
    ('DTSTART', {'TZID': 'America/New_York'}, '20260916T090000'). The value is
    separated by the first colon that isn't inside a quoted parameter."""
    quoted = False
    head = value = None
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            break
    if head is None:
        return "", {}, ""
    parts = head.split(";")
    params = {}
    for part in parts[1:]:
        key, sep, val = part.partition("=")
        if sep:
            params[key.strip().upper()] = val.strip().strip('"')
    return parts[0].strip().upper(), params, value


def _ics_events(text):
    """Yield one dict per VEVENT: {PROP: [(params, value), ...]}. Properties of
    nested components are skipped — a VALARM carries its own SUMMARY /
    DESCRIPTION that would otherwise overwrite the event's."""
    event = None
    depth = 0
    for line in _ics_unfold(text):
        name, _params, value = _ics_parse_line(line)
        if not name:
            continue
        kind = value.strip().upper()
        if name == "BEGIN":
            if event is None:
                if kind == "VEVENT":
                    event = {}
            else:
                depth += 1
        elif name == "END":
            if event is None:
                continue
            if depth:
                depth -= 1
            elif kind == "VEVENT":
                yield event
                event = None
        elif event is not None and not depth:
            event.setdefault(name, []).append((_params, value))


def _cal_prop(event, name):
    """First raw value of a property, or "" when absent."""
    entries = event.get(name)
    return entries[0][1].strip() if entries else ""


def _ics_datetime(params, value, local_zone):
    """Parse a DTSTART / DTEND / RECURRENCE-ID / EXDATE value into
    (naive local datetime, is_all_day). Date-only values mark an all-day event
    and come back as local midnight. UTC ("...Z") and TZID values are converted
    into local_zone; a floating value is already local. (None, False) when the
    value is missing or malformed."""
    v = (value or "").strip()
    if not v:
        return None, False
    if params.get("VALUE", "").upper() == "DATE" or len(v) == 8:
        try:
            return datetime.datetime.strptime(v[:8], "%Y%m%d"), True
        except ValueError:
            return None, False
    try:
        naive = datetime.datetime.strptime(v[:15], "%Y%m%dT%H%M%S")
    except ValueError:
        return None, False
    source = datetime.timezone.utc if v.endswith("Z") else \
        (_cal_zone(params.get("TZID", "")) or local_zone)
    return naive.replace(tzinfo=source).astimezone(local_zone).replace(tzinfo=None), False


def _cal_duration(value):
    """iCalendar DURATION ('PT1H30M', 'P2D') -> timedelta; 0 if unparseable.
    'M' is minutes — months aren't legal in an iCalendar duration."""
    v = (value or "").strip().upper().lstrip("+")
    sign = -1 if v.startswith("-") else 1
    v = v.lstrip("-")
    if not v.startswith("P"):
        return datetime.timedelta(0)
    units = {"W": 604800, "D": 86400, "H": 3600, "M": 60, "S": 1}
    secs = 0
    digits = ""
    for ch in v[1:]:
        if ch.isdigit():
            digits += ch
        else:
            if digits and ch in units:
                secs += int(digits) * units[ch]
            digits = ""      # 'T' separator, or anything unexpected
    return datetime.timedelta(seconds=sign * secs)


# --- RRULE expansion -------------------------------------------------------

_ICS_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _rrule_parse(value):
    """'FREQ=WEEKLY;BYDAY=MO,WE' -> {'FREQ': 'WEEKLY', 'BYDAY': 'MO,WE'}."""
    rule = {}
    for part in (value or "").split(";"):
        key, sep, val = part.partition("=")
        if key.strip() and sep:
            rule[key.strip().upper()] = val.strip()
    return rule


def _rrule_int(rule, key, default=None):
    try:
        return int(rule[key])
    except (KeyError, ValueError):
        return default


def _rrule_ints(rule, key):
    out = []
    for piece in rule.get(key, "").split(","):
        piece = piece.strip()
        if piece:
            try:
                out.append(int(piece))
            except ValueError:
                pass
    return out


def _rrule_weekdays(rule):
    """BYDAY tokens ('MO', '2TU', '-1FR') as given, minus anything malformed."""
    return [t.strip().upper() for t in rule.get("BYDAY", "").split(",")
            if t.strip()[-2:].upper() in _ICS_WEEKDAYS]


def _cal_month_days(year, month, byday, monthdays, default_day):
    """Concrete dates in one month selected by a MONTHLY/YEARLY rule's BYDAY
    ('2TU', '-1FR', bare 'MO') and BYMONTHDAY (negative counts from the end),
    defaulting to DTSTART's day-of-month. Days the month doesn't have (Feb 30,
    a 5th Friday in a month with four) are skipped, per RFC 5545."""
    last = calendar.monthrange(year, month)[1]
    first_weekday = datetime.date(year, month, 1).weekday()
    days = set()
    for token in byday:
        weekday = _ICS_WEEKDAYS.index(token[-2:])
        matches = list(range(1 + (weekday - first_weekday) % 7, last + 1, 7))
        ordinal = token[:-2]
        if not ordinal:
            days.update(matches)
            continue
        try:
            nth = int(ordinal)
        except ValueError:
            continue
        if 0 < nth <= len(matches):
            days.add(matches[nth - 1])
        elif 0 > nth >= -len(matches):
            days.add(matches[nth])
    for day in monthdays:
        if day < 0:
            day = last + 1 + day
        if 1 <= day <= last:
            days.add(day)
    if not days and default_day <= last:
        days.add(default_day)
    return [datetime.date(year, month, d) for d in sorted(days)]


def _rrule_dates(start, rule, win_start, win_end, local_zone):
    """Dates a recurring event starts on, restricted to [win_start, win_end].

    Covers the subset Google Calendar emits: FREQ DAILY/WEEKLY/MONTHLY/YEARLY
    with INTERVAL, COUNT, UNTIL, BYDAY, BYMONTHDAY and BYMONTH. An unrecognized
    FREQ degrades to DTSTART's own date, so an exotic rule shows one event
    rather than vanishing or spinning. Without COUNT the generator skips whole
    periods straight to the window — a daily event set up years ago costs a
    couple of steps, not one per elapsed day. With COUNT every occurrence has
    to be walked (the limit counts from DTSTART), but COUNT itself bounds it."""
    freq = rule.get("FREQ", "").upper()
    interval = max(1, _rrule_int(rule, "INTERVAL", 1) or 1)
    count = _rrule_int(rule, "COUNT")
    until_dt, _ = _ics_datetime({}, rule.get("UNTIL", ""), local_zone)
    until = until_dt.date() if until_dt else None
    start_date = start.date()
    skip_to = win_start if count is None else None

    if freq == "DAILY":
        def candidates():
            day = start_date
            if skip_to and day < skip_to:
                day += datetime.timedelta(
                    days=((skip_to - day).days // interval) * interval)
            for _ in range(_CAL_ITER_CAP):
                yield day
                day += datetime.timedelta(days=interval)
    elif freq == "WEEKLY":
        wanted = sorted({_ICS_WEEKDAYS.index(t[-2:]) for t in _rrule_weekdays(rule)}) \
            or [start_date.weekday()]

        def candidates():
            # Weeks are anchored on Monday (WKST defaults to MO, and Google
            # never sends anything else).
            week = start_date - datetime.timedelta(days=start_date.weekday())
            if skip_to and week < skip_to:
                week += datetime.timedelta(
                    weeks=(((skip_to - week).days // 7) // interval) * interval)
            for _ in range(_CAL_ITER_CAP):
                for weekday in wanted:
                    yield week + datetime.timedelta(days=weekday)
                week += datetime.timedelta(weeks=interval)
    elif freq in ("MONTHLY", "YEARLY"):
        byday = _rrule_weekdays(rule)
        monthdays = _rrule_ints(rule, "BYMONTHDAY")
        months = sorted(_rrule_ints(rule, "BYMONTH")) or [start_date.month]

        def candidates():
            step = 0
            if skip_to:
                if freq == "MONTHLY":
                    elapsed = ((skip_to.year - start_date.year) * 12
                               + skip_to.month - start_date.month)
                else:
                    elapsed = skip_to.year - start_date.year
                step = max(0, elapsed // interval)
            while step < _CAL_ITER_CAP:
                if freq == "MONTHLY":
                    total = (start_date.year * 12 + start_date.month - 1) + step * interval
                    spans = [(total // 12, total % 12 + 1)]
                else:
                    spans = [(start_date.year + step * interval, m) for m in months]
                for year, month in spans:
                    for day in _cal_month_days(year, month, byday, monthdays,
                                               start_date.day):
                        yield day
                step += 1
    else:
        return [start_date] if win_start <= start_date <= win_end else []

    out = []
    occurrences = 0
    for day in candidates():
        if day < start_date:
            continue                       # BYDAY can back-fill before DTSTART
        if until and day > until:
            break
        occurrences += 1
        if count is not None and occurrences > count:
            break
        if day > win_end:
            break
        if day >= win_start:
            out.append(day)
    return out


# --- Feed -> events --------------------------------------------------------

def _cal_overlaps(start, end, all_day, win_start, win_end):
    """Whether a single (non-recurring) occurrence touches the day window.
    All-day events carry an exclusive DTEND date — a Sep 16 all-day event ends
    at Sep 17 00:00 — so a second is trimmed before taking its last day."""
    if start.date() > win_end:
        return False
    last = end or start
    if all_day and end:
        last = last - datetime.timedelta(seconds=1)
    return last.date() >= win_start


def _cal_feed_events(text, win_start, win_end, local_zone):
    """Every occurrence in one .ics feed touching [win_start, win_end], as
    {"start", "end", "all_day", "name"} dicts with naive local datetimes."""
    events = list(_ics_events(text))
    # A VEVENT carrying RECURRENCE-ID is one modified instance of its parent
    # series; the parent's expansion has to skip that slot, since the instance
    # may have been moved to another day (or cancelled outright).
    overrides = set()
    for event in events:
        rid = event.get("RECURRENCE-ID")
        uid = _cal_prop(event, "UID")
        if rid and uid:
            slot, _ = _ics_datetime(rid[0][0], rid[0][1], local_zone)
            if slot:
                overrides.add((uid, slot))

    out = []
    for event in events:
        if _cal_prop(event, "STATUS").upper() == "CANCELLED":
            continue
        dtstart = event.get("DTSTART")
        if not dtstart:
            continue
        start, all_day = _ics_datetime(dtstart[0][0], dtstart[0][1], local_zone)
        if not start:
            continue
        end = None
        if event.get("DTEND"):
            end, _ = _ics_datetime(event["DTEND"][0][0], event["DTEND"][0][1], local_zone)
        elif event.get("DURATION"):
            end = start + _cal_duration(event["DURATION"][0][1])
        name = _ics_unescape(_cal_prop(event, "SUMMARY")).strip() or _CAL_NO_TITLE
        rrule = event.get("RRULE")

        if not rrule or event.get("RECURRENCE-ID"):
            if _cal_overlaps(start, end, all_day, win_start, win_end):
                out.append({"start": start, "end": end,
                            "all_day": all_day, "name": name})
            continue

        uid = _cal_prop(event, "UID")
        exdates = set()
        for params, value in event.get("EXDATE", []):
            for piece in value.split(","):
                hole, _ = _ics_datetime(params, piece, local_zone)
                if hole:
                    exdates.add(hole)
        span = (end - start) if end else datetime.timedelta(0)
        for day in _rrule_dates(start, _rrule_parse(rrule[0][1]),
                                win_start, win_end, local_zone):
            occurrence = datetime.datetime.combine(day, start.time())
            if occurrence in exdates or (uid, occurrence) in overrides:
                continue
            out.append({"start": occurrence, "end": occurrence + span,
                        "all_day": all_day, "name": name})
    return out


def _cal_fmt_time(dt):
    """'9:00a' / '12:30p' — the compact form the display's 4x6 font fits."""
    return "{}:{:02d}{}".format(dt.hour % 12 or 12, dt.minute,
                                "p" if dt.hour >= 12 else "a")


def _cal_day_entry(label, day, events, local_zone):
    """One day's payload: all-day events first, then chronological. Identical
    events are collapsed, so an invite that lands on two of the configured
    calendars is listed once."""
    picked = []
    for event in events:
        if event["all_day"]:
            end = event["end"]
            last = (end - datetime.timedelta(seconds=1)).date() if end \
                else event["start"].date()
            if not (event["start"].date() <= day <= last):
                continue
        elif event["start"].date() != day:
            # Timed events are listed on the day they start. One running past
            # midnight belongs to the day it began, not to both.
            continue
        picked.append(event)
    picked.sort(key=lambda e: (not e["all_day"], e["start"], e["name"]))

    listed = []
    seen = set()
    for event in picked:
        key = (event["all_day"], event["start"], event["name"])
        if key in seen:
            continue
        seen.add(key)
        listed.append({
            "time": "ALL DAY" if event["all_day"] else _cal_fmt_time(event["start"]),
            "name": _status_trunc(event["name"], _CAL_NAME_MAX) or _CAL_NO_TITLE,
            "all_day": bool(event["all_day"]),
            "start": int(event["start"].replace(tzinfo=local_zone).timestamp()),
        })
    return {
        "label": label,
        "date": "{} {} {}".format(_CAL_DOW[day.weekday()],
                                  _CAL_MON[day.month - 1], day.day),
        "iso": day.isoformat(),
        "events": listed[:_CAL_MAX_PER_DAY],
        "more": max(0, len(listed) - _CAL_MAX_PER_DAY),
    }


def handle_calendar(params):
    """Today's and tomorrow's events, pooled across every configured .ics feed.
    One bad feed is logged and skipped so the rest of the board still renders;
    only an all-feeds failure is an error, and that returns 502 rather than an
    empty day so the device keeps displaying its last good lists."""
    if not CALENDAR_FEEDS:
        return 200, json.dumps({
            "days": [], "calendars": 0, "errors": 0, "ts": int(time.time()),
        }).encode()

    local_zone = _cal_local_zone()
    today = datetime.datetime.now(local_zone).date()
    tomorrow = today + datetime.timedelta(days=1)
    # Keyed by date as well as TTL: past midnight the cached payload's "TODAY"
    # is yesterday's list, and must not be served for the rest of the window.
    cache_key = "calendar:{}".format(today.isoformat())
    cached = cache_get(cache_key, max_age_sec=CALENDAR_CACHE_SEC)
    if cached:
        return 200, cached

    events = []
    errors = 0
    for i, url in enumerate(CALENDAR_FEEDS):
        try:
            status, body = fetch(url, timeout=20)
            if status != 200:
                raise RuntimeError("HTTP {}".format(status))
            events.extend(_cal_feed_events(
                body.decode("utf-8", "replace"), today, tomorrow, local_zone))
        except Exception as e:
            errors += 1
            # Identify the feed by position only — the URL is a secret.
            _log_proxy_event("calendar feed {}/{} failed: {}".format(
                i + 1, len(CALENDAR_FEEDS), e))
    if errors == len(CALENDAR_FEEDS):
        return 502, json.dumps({
            "error": "all calendar feeds failed",
            "calendars": len(CALENDAR_FEEDS),
        }).encode()

    body = json.dumps({
        "days": [
            _cal_day_entry("TODAY", today, events, local_zone),
            _cal_day_entry("TOMORROW", tomorrow, events, local_zone),
        ],
        "calendars": len(CALENDAR_FEEDS),
        "errors": errors,
        "ts": int(time.time()),
    }).encode()
    cache_set(cache_key, body, age_override=CALENDAR_CACHE_SEC)
    return 200, body


# Canonical routes. Location-aware ones take an optional ?loc=<name>.
ROUTES = {
    "/api/planes":      handle_planes,
    "/api/route":       handle_route,
    "/api/aircraft":    handle_aircraft,
    "/api/forecast":    handle_forecast,
    "/api/tides":       handle_tides,
    "/api/sky":         handle_sky,
    "/api/ships":       handle_ships,
    "/api/ships/debug": handle_ships_debug,
    "/api/sightings":   handle_sightings,
    "/api/devicelog":   handle_devicelog_get,
    "/api/health":      handle_health,
    "/api/status":      handle_status,
    "/api/calendar":    handle_calendar,
    "/api/time":        handle_time,
}


# Deprecated path aliases. /api/v2/* meant nothing more than "accepts ?loc=",
# which every canonical path above now does, so these exist only until the
# displays stop calling them. Each hit is logged (throttled) so we can tell
# when that has actually happened rather than guessing.
DEPRECATED_ROUTES = {
    "/api/v2/planes":   ("/api/planes",   handle_planes),
    "/api/v2/forecast": ("/api/forecast", handle_forecast),
    "/api/v2/sky":      ("/api/sky",      handle_sky),
}
_deprecated_logged = {}        # (path, client) -> last time we logged a hit
_DEPRECATED_LOG_INTERVAL = 3600


def note_deprecated_path(path, replacement, client):
    """Log a deprecated-path hit at most once an hour per (path, caller), so a
    display polling every minute leaves one line instead of sixty. Keyed by
    caller as well as path on purpose: the point of this log is to find out
    *who* still needs migrating, and a single throttle per path would let one
    chatty client hide every other one for the hour."""
    now = time.time()
    key = (path, client)
    if now - _deprecated_logged.get(key, 0) < _DEPRECATED_LOG_INTERVAL:
        return
    _deprecated_logged[key] = now
    _log_proxy_event("deprecated path {} used by {} — use {}".format(
        path, client, replacement))


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
        if not handler:
            alias = DEPRECATED_ROUTES.get(path)
            if alias:
                replacement, handler = alias
                note_deprecated_path(path, replacement, self.client_address[0])
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
    _route_cache_load()
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
