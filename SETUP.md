# Matrix Portal Tides + Weather + Plane Tracker

Displays weather and next tide on a 64x32 RGB LED matrix. Polls OpenSky every 60 seconds — when aircraft are overhead, the display switches to show plane callsign, altitude, and heading. Switches back when skies clear.

## Hardware

- Matrix Portal M4 (ATSAMD51J19 + ESP32 coprocessor)
- 64x32 RGB LED Matrix

## Display Layout

**Default screen** (yellow + cyan):
```
72F SUNNY      ← current weather
HI 2:30P      ← next high/low tide
```

**Plane screen** (green, cycles every 5s if multiple):
```
UAL1234       ← callsign
35Kft 270d    ← altitude + heading
```

## Setup Steps

### 1. Get Free API Keys

- **OpenWeatherMap** (weather): https://openweathermap.org/api
- **Adafruit IO** (time sync): https://io.adafruit.com
- **OpenSky Network** (optional, higher rate limits): https://opensky-network.org

### 2. Find Your NOAA Tide Station

Go to https://tidesandcurrents.noaa.gov/stations.html and find the station ID nearest to you (e.g., `8443970` for Boston).

### 3. Install CircuitPython Libraries

Copy these from the [Adafruit CircuitPython Bundle](https://circuitpython.org/libraries) to `CIRCUITPY/lib/`:

- `adafruit_matrixportal`
- `adafruit_portalbase`
- `adafruit_esp32spi`
- `adafruit_bus_device`
- `adafruit_requests`
- `adafruit_connection_manager`
- `adafruit_display_text`
- `adafruit_io`
- `neopixel`

### 4. Configure secrets.py

Edit `secrets.py` with your values:

- WiFi SSID and password
- OpenWeatherMap API key
- Adafruit IO username and key
- NOAA tide station ID
- Your latitude and longitude
- Timezone (Olson format, e.g., `America/New_York`)
- OpenSky credentials (optional but recommended)

### 5. Deploy to Board

Copy `secrets.py` and `code.py` to your CIRCUITPY drive.

## Tuning

| Setting | Default | Description |
|---------|---------|-------------|
| `BBOX` | `0.1` (~7 mi) | Bounding box radius in degrees for aircraft detection |
| `WEATHER_INTERVAL` | `600` | Seconds between weather/tide refreshes |
| `OPENSKY_INTERVAL` | `60` | Seconds between aircraft checks |
| `PLANE_CYCLE_SECS` | `5` | Seconds between cycling multiple planes |

## Potential Improvements

- [ ] Use a smaller BDF font (e.g., tom-thumb 3x5) to fit 4 lines instead of 2
- [ ] Add wind speed/direction to weather display
- [ ] Show tide height alongside time
- [ ] Add aircraft speed (knots) to plane display
- [ ] Color-code altitude (green = high, yellow = mid, red = low)
- [ ] Add button press to manually toggle between screens
