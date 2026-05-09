# Device Setup

## Hardware

- [Adafruit MatrixPortal S3](https://www.adafruit.com/product/5778) or [MatrixPortal M4](https://www.adafruit.com/product/4745)
- [64×32 RGB LED Matrix Panel](https://www.adafruit.com/product/2278) (3 mm or 4 mm pitch)
- USB-C power supply, 5 V, at least 2 A

## 1. Install CircuitPython

Download CircuitPython 10.x for your board from [circuitpython.org](https://circuitpython.org/downloads) and follow the flashing instructions.

## 2. Install Libraries

Download the [Adafruit CircuitPython Bundle](https://circuitpython.org/libraries) matching your CircuitPython version. Copy these folders/files into `CIRCUITPY/lib/`:

```
adafruit_matrixportal/
adafruit_portalbase/
adafruit_display_text/
adafruit_bitmap_font/
adafruit_bus_device/
adafruit_connection_manager/
adafruit_esp32spi/        # M4 only (not needed on S3)
adafruit_requests/
adafruit_ntp.mpy
neopixel.mpy
```

## 3. Copy Font Files

The display uses two BDF bitmap fonts. Copy these onto `CIRCUITPY/` (root, not lib/):

- `4x6.bdf` — small font (conditions, wind, tide time)
- `5x8.bdf` — medium font (temperature, clock, route)

These ship with CircuitPython releases and the Adafruit bundle.

## 4. Copy Data Files

Copy these from `device/` to `CIRCUITPY/` (root):

| File | Purpose |
|------|---------|
| `airlines.csv` | Airline ICAO → display name + color |
| `airports.csv` | ICAO airport code → 3-letter display code |
| `conditions.csv` | OWM weather condition ID → short text |

## 5. Configure secrets.py

Copy `device/secrets.py.template` to `device/secrets.py` and fill in your values, then copy that file to `CIRCUITPY/secrets.py`.

`secrets.py` is gitignored and must never be committed.

| Key | Where to get it |
|-----|----------------|
| `ssid` / `password` | Your Wi-Fi credentials |
| `openweather_key` | [openweathermap.org/api](https://openweathermap.org/api) — free |
| `noaa_station` | [tidesandcurrents.noaa.gov/stations.html](https://tidesandcurrents.noaa.gov/stations.html) — find the station nearest you |
| `latitude` / `longitude` | Your location in decimal degrees |
| `tz_offset_hours` | Static UTC offset used at boot. The device auto-re-syncs to the OWM-reported (DST-aware) offset on every weather fetch, so this just needs to be approximately right. |
| `proxy_host` | `http://YOUR_PI_IP:6590` — see proxy setup in root README |

## 6. Deploy code.py

Copy `device/code.py` to `CIRCUITPY/code.py`. CircuitPython restarts automatically.

## 7. (Optional, S3 only) Enable the web workflow

Once the device boots correctly from USB, you can switch to managing it over Wi-Fi via [CircuitPython's web workflow](https://learn.adafruit.com/getting-started-with-web-workflow-using-the-code-editor/device-setup). After this step the CIRCUITPY drive no longer mounts as USB storage, so do it last — when you're confident the device is working.

1. Copy `device/settings.toml.template` to `device/settings.toml` and fill in:

   | Key | What to put |
   |-----|-------------|
   | `CIRCUITPY_WIFI_SSID` / `CIRCUITPY_WIFI_PASSWORD` | Same Wi-Fi as in `secrets.py` |
   | `CIRCUITPY_WEB_API_PASSWORD` | A password the web editor will ask for; not your Wi-Fi password |
   | `CIRCUITPY_WEB_API_PORT` | `80` |

   `settings.toml` is gitignored.

2. Copy `device/settings.toml` to `CIRCUITPY/settings.toml`.
3. Copy `device/boot.py` to `CIRCUITPY/boot.py`. On boards with native Wi-Fi (S3) it calls `storage.disable_usb_drive()` so the web workflow gets read-write access; on the M4 it detects the missing `wifi` module and skips the call, so the file is safe to ship either way. (On the S3, USB mass storage and the web workflow can't both have write access — Adafruit's recommendation is to disable USB.)
4. Hard-reset the board (power-cycle or press the reset button — a soft reset isn't enough).
5. Visit [code.circuitpython.org](https://code.circuitpython.org/) and click **Connect to Device**. Pick `cpy-XXXXXX.local` (or use the device's IP) and enter the `CIRCUITPY_WEB_API_PASSWORD`.

To get USB drive access back, edit `boot.py` (via the web editor) to comment out `storage.disable_usb_drive()` and hard-reset.

> **Note:** Wi-Fi credentials live in two places — `secrets.py` (read by the MatrixPortal library at runtime) and `settings.toml` (read by CircuitPython at boot). Keep them in sync if you change networks.

## Configuration

Key settings at the top of `code.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `WEATHER_INTERVAL` | `600` | Seconds between weather/tide refreshes |
| `OPENSKY_INTERVAL` | `60` | Seconds between aircraft checks |
| `PLANE_CYCLE_SECS` | `5` | Seconds per plane when multiple are overhead |
| `PLANE_MAX_SECS` | `600` | Max time on plane screen before forcing a weather break |
| `SHIP_INTERVAL` | `60` | Seconds between ship list refreshes |
| `PLANES_ENABLED` | `True` | Disable to turn off plane tracking entirely |
| `SHIPS_ENABLED` | `True` | Disable to turn off ship tracking entirely |
| `DEMO_MODE` | `False` | Cycle test fixtures without network (development only) |

The plane bounding-box size lives on the proxy (`bbox` in `config.json`), not the device.

## Troubleshooting

**Stuck on "LOADING..."** — Wi-Fi failed. Check `ssid`/`password` in `secrets.py`.

**Weather shows "N/A"** — Check `openweather_key` and internet connectivity.

**No planes shown** — Verify `proxy_host` is reachable and the proxy is running. Try increasing `bbox` in the proxy's `config.json`.

**No ships shown** — Ships require `aisstream_key` in the proxy's `config.json` and a location near a shipping lane.

**Frequent reboots** — Normal. The device auto-reboots at 03:30 daily and after repeated fetch failures to clear memory fragmentation.
