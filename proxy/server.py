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
import time
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
LATITUDE = float(_config.get("latitude", 42.36))
LONGITUDE = float(_config.get("longitude", -71.06))
BBOX = float(_config.get("bbox", 0.1))

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache = {}       # key -> {"data": bytes, "time": float}
_cache_lock = Lock()


def cache_get(key, max_age_sec):
    """Return cached bytes if fresh, else None."""
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["time"]) < max_age_sec:
            return entry["data"]
    return None


def cache_set(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "time": time.time()}


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
    """Proxy OpenSky states/all — returns aircraft in bounding box.
    Cached for 30 seconds to respect rate limits."""

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
    if status == 200:
        cache_set(cache_key, data)
    return status, data


def handle_route(params):
    """Proxy OpenSky route + aircraft type lookup.
    Pass callsign (required) and icao24 (optional) to get both route
    and aircraft type in one response. Cached for 1 hour."""

    callsign = params.get("callsign", [""])[0].strip()
    icao24 = params.get("icao24", [""])[0].strip()
    if not callsign:
        return 400, json.dumps({"error": "missing callsign"}).encode()

    cache_key = f"route:{callsign}:{icao24}" if icao24 else f"route:{callsign}"
    cached = cache_get(cache_key, max_age_sec=3600)
    if cached:
        return 200, cached

    result = {"callsign": callsign, "route": [], "typecode": "", "registration": ""}

    # Fetch route
    url = f"https://opensky-network.org/api/routes?callsign={callsign}"
    status, data = fetch(url, headers=opensky_headers())
    if status == 200 and data:
        try:
            route_data = json.loads(data)
            result["route"] = route_data.get("route", [])
            result["operatorIata"] = route_data.get("operatorIata", "")
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
    """Fetch tomorrow's weather from OpenWeatherMap 5-day forecast.
    Groups 3-hour intervals by date, returns tomorrow's hi/lo/condition.
    Cached for 1 hour."""

    cache_key = "forecast"
    cached = cache_get(cache_key, max_age_sec=3600)
    if cached:
        return 200, cached

    if not OWM_KEY:
        return 500, json.dumps({"error": "no openweather_key configured"}).encode()

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

        # Find tomorrow's date
        import datetime
        today = datetime.date.today()
        tomorrow = today + datetime.timedelta(days=1)
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")

        hi = -999
        lo = 999
        conditions = {}
        cond_id = 800  # default clear

        for item in items:
            dt_txt = item.get("dt_txt", "")
            if dt_txt.startswith(tomorrow_str):
                temp = item["main"]["temp"]
                if temp > hi:
                    hi = temp
                if temp < lo:
                    lo = temp
                # Track most common condition
                cid = item["weather"][0]["id"]
                cmain = item["weather"][0]["main"]
                conditions[cmain] = conditions.get(cmain, 0) + 1
                cond_id = cid  # use last one, or most severe

        if hi == -999:
            return 404, json.dumps({"error": "no forecast data for tomorrow"}).encode()

        # Pick most common condition
        most_common = max(conditions, key=conditions.get) if conditions else "Clear"

        result = {
            "hi": round(hi),
            "lo": round(lo),
            "cond": most_common,
            "cond_id": cond_id,
            "date": tomorrow_str,
        }
        body = json.dumps(result).encode()
        cache_set(cache_key, body)
        return 200, body
    except Exception as e:
        return 500, json.dumps({"error": str(e)}).encode()


def handle_health(params):
    """Health check endpoint."""
    return 200, json.dumps({
        "status": "ok",
        "cache_entries": len(_cache),
        "uptime_approx": "use /api/health to verify proxy is reachable",
    }).encode()


# ---------------------------------------------------------------------------
# Route registry — add new APIs here
# ---------------------------------------------------------------------------

ROUTES = {
    "/api/planes":    handle_planes,
    "/api/route":     handle_route,
    "/api/aircraft":  handle_aircraft,
    "/api/forecast":  handle_forecast,
    "/api/health":    handle_health,
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

    def log_message(self, fmt, *args):
        ts = time.strftime("%H:%M:%S")
        print(f"  [{ts}] {args[0]}")


if __name__ == "__main__":
    print(f"Matrix Portal Proxy — port {PORT}")
    print(f"Config: {CONFIG_FILE}")
    print(f"Routes: {', '.join(ROUTES.keys())}")
    print(f"Location: {LATITUDE}, {LONGITUDE} (bbox {BBOX})")
    server = HTTPServer(("", PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
