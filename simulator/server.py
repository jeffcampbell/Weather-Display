#!/usr/bin/env python3
"""
Tiny dev server for the Matrix Portal Simulator.
Reads .env for API keys, serves simulator.html, and proxies API calls to
avoid CORS issues.

Usage:  python3 server.py [port]
        Then open http://localhost:8000  (or whichever port)
"""

import http.server
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
BASE_DIR = Path(__file__).parent
DEVICE_DIR = BASE_DIR.parent / "device"
ENV_FILE = BASE_DIR / ".env"
DEVICE_LOG_FILE = BASE_DIR / "device.log"


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
PI_PROXY = ENV.get("PI_PROXY", "http://localhost:6590")  # set PI_PROXY=http://YOUR_PI_IP:6590 in .env


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

        # --- /api/tides → proxy NOAA (supports ?date=today|tomorrow) ---
        if self.path == "/api/tides" or self.path.startswith("/api/tides?"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            date_param = qs.get("date", ["today"])[0]
            if date_param not in ("today", "tomorrow"):
                date_param = "today"
            station = ENV.get("NOAA_STATION", "8443970")
            url = (
                f"https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
                f"?date={date_param}&station={station}&product=predictions&datum=MLLW"
                f"&time_zone=lst_ldt&interval=hilo&units=english&format=json"
            )
            status, data = proxy_get(url)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/planes → proxy to Pi ---
        if self.path == "/api/planes" or self.path.startswith("/api/planes?"):
            status, data = proxy_get(f"{PI_PROXY}{self.path}")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/route?callsign=XXX&icao24=XXX → proxy to Pi ---
        if self.path.startswith("/api/route?"):
            status, data = proxy_get(f"{PI_PROXY}{self.path}")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/aircraft?icao24=XXX → proxy to Pi ---
        if self.path.startswith("/api/aircraft?"):
            status, data = proxy_get(f"{PI_PROXY}{self.path}")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/airline?icao=XXX → lookup from airlines.csv ---
        if self.path.startswith("/api/airline?"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            icao = qs.get("icao", [""])[0].strip().upper()
            result = {"icao": icao, "iata": icao[:2], "name": icao, "color": "0x00AA44"}
            csv_path = DEVICE_DIR / "airlines.csv"
            if csv_path.exists():
                for line in csv_path.read_text().splitlines()[1:]:
                    parts = line.strip().split(",")
                    if len(parts) >= 4 and parts[0] == icao:
                        result = {"icao": parts[0], "iata": parts[1],
                                  "name": parts[2], "color": parts[3]}
                        break
            body = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return

        # --- /api/ships → proxy to Pi ---
        if self.path == "/api/ships":
            status, data = proxy_get(f"{PI_PROXY}/api/ships")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)
            return

        # --- /api/devicelog → read local device.log ---
        if self.path == "/api/devicelog" or self.path.startswith("/api/devicelog?"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            lines_n = min(int(qs.get("lines", ["100"])[0]), 1000)
            try:
                if DEVICE_LOG_FILE.exists():
                    with open(DEVICE_LOG_FILE) as f:
                        all_lines = f.readlines()
                    recent = [l.rstrip("\n") for l in all_lines[-lines_n:]]
                    total = len(all_lines)
                else:
                    recent, total = [], 0
                body = json.dumps({"lines": recent, "total": total}).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
            return

        # --- / → serve simulator.html ---
        if self.path == "/":
            self.path = "/simulator.html"

        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/devicelog":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body.decode())
                msgs = data.get("msgs", [])
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                lines = ["{} | {}\n".format(ts, m) for m in msgs]
                with open(DEVICE_LOG_FILE, "a") as f:
                    f.writelines(lines)
                response = json.dumps({"ok": True, "appended": len(lines)}).encode()
                status = 200
            except Exception as e:
                response = json.dumps({"error": str(e)}).encode()
                status = 500
        else:
            response = json.dumps({"error": "not found"}).encode()
            status = 404
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(response))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, fmt, *args):
        # Quieter logging
        print(f"  {args[0]}")


if __name__ == "__main__":
    print(f"Matrix Portal Simulator → http://localhost:{PORT}")
    print(f"Keys loaded from: {ENV_FILE}")
    print(f"Tip: python3 server.py <port> to use a different port")
    server = http.server.HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
