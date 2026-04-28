#!/usr/bin/env python3
"""
Tiny dev server for the Matrix Portal Simulator.
Reads .env for API keys, serves simulator.html, and proxies API calls to
avoid CORS issues.

Usage:  python3 server.py
        Then open http://localhost:8000
"""

import http.server
import json
import os
import urllib.request
import urllib.error
import base64
from pathlib import Path

PORT = 8000
ENV_FILE = Path(__file__).parent / ".env"


def load_env():
    """Parse .env file into a dict."""
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


ENV = load_env()


def proxy_get(url, headers=None):
    """Fetch a URL server-side and return (status, body_bytes)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 502, json.dumps({"error": str(e)}).encode()


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        # --- /config.js  → inject env vars as JS ---
        if self.path == "/config.js":
            cfg = {
                "openweather_key": ENV.get("OPENWEATHER_KEY", ""),
                "noaa_station": ENV.get("NOAA_STATION", "8443970"),
                "latitude": float(ENV.get("LATITUDE", 42.36)),
                "longitude": float(ENV.get("LONGITUDE", -71.06)),
                "timezone": ENV.get("TIMEZONE", "America/New_York"),
                "opensky_user": ENV.get("OPENSKY_USER", ""),
                "opensky_pass": ENV.get("OPENSKY_PASS", ""),
            }
            body = f"const CONFIG = {json.dumps(cfg)};\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/weather → proxy OpenWeatherMap ---
        if self.path == "/api/weather":
            key = ENV.get("OPENWEATHER_KEY", "")
            lat = ENV.get("LATITUDE", 42.36)
            lon = ENV.get("LONGITUDE", -71.06)
            url = (
                f"https://api.openweathermap.org/data/2.5/weather"
                f"?lat={lat}&lon={lon}&appid={key}&units=imperial"
            )
            status, data = proxy_get(url)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/tides → proxy NOAA ---
        if self.path == "/api/tides":
            station = ENV.get("NOAA_STATION", "8443970")
            url = (
                f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
                f"?date=today&station={station}&product=predictions&datum=MLLW"
                f"&time_zone=lst_ldt&interval=hilo&units=english&format=json"
            )
            status, data = proxy_get(url)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/planes → proxy OpenSky ---
        if self.path == "/api/planes":
            lat = float(ENV.get("LATITUDE", 42.36))
            lon = float(ENV.get("LONGITUDE", -71.06))
            bbox = 0.1
            url = (
                f"https://opensky-network.org/api/states/all"
                f"?lamin={lat-bbox}&lomin={lon-bbox}"
                f"&lamax={lat+bbox}&lomax={lon+bbox}"
            )
            headers = {}
            user = ENV.get("OPENSKY_USER", "")
            pw = ENV.get("OPENSKY_PASS", "")
            if user and pw:
                cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
                headers["Authorization"] = f"Basic {cred}"
            status, data = proxy_get(url, headers)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/route?callsign=XXX → proxy OpenSky routes ---
        if self.path.startswith("/api/route?"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            callsign = qs.get("callsign", [""])[0].strip()
            if not callsign:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"missing callsign"}')
                return
            url = f"https://opensky-network.org/api/routes?callsign={callsign}"
            headers = {}
            user = ENV.get("OPENSKY_USER", "")
            pw = ENV.get("OPENSKY_PASS", "")
            if user and pw:
                cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
                headers["Authorization"] = f"Basic {cred}"
            status, data = proxy_get(url, headers)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/aircraft?icao24=XXX → proxy OpenSky metadata ---
        if self.path.startswith("/api/aircraft?"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            icao24 = qs.get("icao24", [""])[0].strip()
            if not icao24:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":"missing icao24"}')
                return
            url = f"https://opensky-network.org/api/metadata/aircraft/icao24/{icao24}"
            headers = {}
            user = ENV.get("OPENSKY_USER", "")
            pw = ENV.get("OPENSKY_PASS", "")
            if user and pw:
                cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
                headers["Authorization"] = f"Basic {cred}"
            status, data = proxy_get(url, headers)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- / → serve simulator.html ---
        if self.path == "/":
            self.path = "/simulator.html"

        return super().do_GET()

    def log_message(self, fmt, *args):
        # Quieter logging
        print(f"  {args[0]}")


if __name__ == "__main__":
    print(f"Matrix Portal Simulator → http://localhost:{PORT}")
    print(f"Keys loaded from: {ENV_FILE}")
    server = http.server.HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
