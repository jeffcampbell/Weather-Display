# Matrix Portal Tracker

A live weather, tide, aircraft, and ship tracker running on an Adafruit MatrixPortal and a 64×32 RGB LED matrix.

The display cycles between three screens depending on what's happening nearby:

**Weather + Tides** (default)
- Animated tide basin with pixel-art weather sky (sun/moon/clouds/rain/snow/lightning/fog)
- Current temperature, conditions, and wind
- Next high or low tide time
- Live clock

**Aircraft** (when planes are overhead)
- Airline color scheme, IATA code, flight route (e.g. `BOS→SFO`)
- Airline name, altitude, compass heading
- Aircraft type code and tail number

**Ships** (when vessels are nearby)
- Vessel name, type, and destination
- Distance and heading
- Animated ship silhouette on an ocean background

---

## Hardware

| Part | Link |
|------|------|
| Adafruit MatrixPortal S3 | https://www.adafruit.com/product/5778 |
| Adafruit MatrixPortal M4 (older, also works) | https://www.adafruit.com/product/4745 |
| 64×32 RGB LED Matrix (3 mm pitch) | https://www.adafruit.com/product/2278 |
| 5 V / 2 A+ USB-C power supply | any |

---

## Architecture

```
[MatrixPortal device]  ── Wi-Fi ──►  OpenWeatherMap  (weather, every 10 min)
                                 ──►  NOAA Tides API  (tides, every 10 min)
                                 ──►  [Raspberry Pi proxy :6590]
                                           │
                                           ├──► OpenSky Network      (aircraft positions)
                                           ├──► FlightAware AeroAPI  (routes, optional paid)
                                           └──► AISStream WebSocket  (live AIS ship feed)
```

A Raspberry Pi (or any always-on Linux box) runs `proxy/server.py`. It handles:
- **HTTPS proxying** — the MatrixPortal M4's ESP32 co-processor can't negotiate TLS with all APIs
- **AIS WebSocket** — persistent connection to AISStream.io for live ship positions
- **Response caching** — reduces upstream API calls
- **Device logging** — receives periodic log POSTs from the device for remote monitoring

---

## Quick Start

### 1. Get API keys

| Service | Required | Free tier | Link |
|---------|----------|-----------|------|
| OpenWeatherMap | Yes | Yes | https://openweathermap.org/api |
| NOAA Tides | No key needed | — | https://tidesandcurrents.noaa.gov/stations.html |
| OpenSky Network | For planes | Yes | https://opensky-network.org |
| AISStream.io | For ships | Yes | https://aisstream.io |
| FlightAware AeroAPI | Better routes | Paid | https://flightaware.com/aeroapi |

### 2. Set up the Raspberry Pi proxy

```bash
# On the Pi:
git clone <this repo>
cd matrix-portal-tracker/proxy
cp config.json.template config.json
# Edit config.json with your API keys and location
python3 server.py
```

To run as a systemd service so it survives reboots:

```ini
# /etc/systemd/system/matrix-portal-proxy.service
[Unit]
Description=Matrix Portal HTTP Proxy
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/matrix-portal-tracker/proxy/server.py
WorkingDirectory=/home/pi/matrix-portal-tracker/proxy
Restart=on-failure
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now matrix-portal-proxy.service
```

### 3. Set up the MatrixPortal device

See [device/SETUP.md](device/SETUP.md) for the full walkthrough. Quick version:

1. Flash CircuitPython 10.x onto your MatrixPortal
2. Copy libraries from the Adafruit bundle to `CIRCUITPY/lib/`
3. Copy font files (`4x6.bdf`, `5x8.bdf`) to `CIRCUITPY/`
4. Copy data files (`airlines.csv`, `airports.csv`, `conditions.csv`) to `CIRCUITPY/`
5. Copy `device/secrets.py.template` to `device/secrets.py`, fill in credentials, copy to `CIRCUITPY/secrets.py`
6. Copy `device/code.py` to `CIRCUITPY/code.py`

### 4. (Optional) Run the browser simulator

The simulator renders the display in a browser canvas at 10× scale, useful for development without hardware.

```bash
cd simulator
cp .env.template .env
# Edit .env with your API keys
pip install -r requirements.txt   # if first time
python3 server.py
# Open http://localhost:8000
```

---

## Project Structure

```
device/
  code.py                 Main CircuitPython application
  secrets.py.template     Configuration template (copy to secrets.py)
  airlines.csv            Airline ICAO code → display name + color
  airports.csv            ICAO airport → 3-letter display code
  conditions.csv          OWM condition ID → short text label
  SETUP.md                Detailed device setup guide

proxy/
  server.py               HTTP proxy + AIS WebSocket listener
  config.json.template    Configuration template (copy to config.json)
  API.md                  Full proxy API reference

simulator/
  simulator.html          Browser-based pixel-exact display replica
  server.py               Dev server (proxies API calls, serves HTML)
  screenshot.py           Playwright regression screenshot tester
  extract_fonts.py        BDF font extractor (run with device mounted)
  .env.template           Simulator configuration template
```

---

## Device Logging

The device POSTs a buffer of timestamped log entries to `POST /api/devicelog` on the proxy approximately every 5 minutes. You can tail the log remotely:

```bash
curl http://YOUR_PI_IP:6590/api/devicelog?lines=50
```

Logs are stored in `proxy/device.log`.

---

## License

MIT
