# Matrix Portal Proxy — API Reference

Base URL (Raspberry Pi): `http://YOUR_PI_IP:6590`

All endpoints are `GET`, all responses are `application/json`.

---

## `GET /api/planes`

Returns aircraft currently within the configured bounding box, proxied from OpenSky Network.

**Query parameters:** none (uses config lat/lon/bbox)

**Cache TTL:** 30 seconds

**Upstream:** `https://opensky-network.org/api/states/all`

**Response:** Raw OpenSky `states/all` JSON. The `states` array contains one entry per aircraft; each state is a positional array with fields in this order:

| Index | Field | Type | Description |
|-------|-------|------|-------------|
| 0 | `icao24` | string | ICAO 24-bit hex address |
| 1 | `callsign` | string | Flight callsign (may have trailing spaces) |
| 2 | `origin_country` | string | Country of registration |
| 5 | `longitude` | float | Current longitude (decimal degrees) |
| 6 | `latitude` | float | Current latitude (decimal degrees) |
| 7 | `baro_altitude` | float | Barometric altitude (meters) |
| 9 | `velocity` | float | Ground speed (m/s) |
| 10 | `true_track` | float | True track angle (degrees, clockwise from north) |
| 13 | `geo_altitude` | float | Geometric altitude (meters) |

```json
{
  "time": 1714500000,
  "states": [
    ["a1b2c3", "AAL1563 ", "United States", 1714499990, 1714499990,
     -70.82, 42.17, 7620.0, false, 247.5, 285.3, null, null, 7772.0,
     "2000", false, 0]
  ]
}
```

**Error responses:**

| Status | Meaning |
|--------|---------|
| 429 | OpenSky rate limit exceeded |
| 502 | Upstream unreachable |

---

## `GET /api/route`

Looks up a flight's route (origin → destination airports) and aircraft type. Tries OpenSky first, falls back to adsbdb.com. Aircraft metadata comes from hexdb.io.

**Query parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `callsign` | yes | Flight callsign, e.g. `AAL1563` |
| `icao24` | no | Aircraft hex code — enables type/registration lookup |

**Cache TTL:** 1 hour (per callsign+icao24 pair)

**Upstreams:**
- `https://opensky-network.org/api/routes?callsign=...`
- `https://api.adsbdb.com/v0/callsign/...` (fallback when OpenSky has no route)
- `https://hexdb.io/api/v1/aircraft/...` (aircraft type/registration)

**Success response (200):**

```json
{
  "callsign": "AAL1563",
  "route": ["KDFW", "KLGA"],
  "typecode": "B738",
  "registration": "N916NN"
}
```

- `route`: array of ICAO airport codes `[origin, destination]`; empty array if not found
- `typecode`: ICAO aircraft type code (e.g. `B738`, `A320`); empty string if unknown
- `registration`: tail number; empty string if unknown

**Error responses:**

| Status | Body | Meaning |
|--------|------|---------|
| 400 | `{"error": "missing callsign"}` | No callsign provided |
| 404 | `{"error": "route not found", "callsign": "..."}` | Neither upstream has a route for this callsign |

---

## `GET /api/aircraft`

Returns raw aircraft metadata from OpenSky by ICAO24 hex code.

**Query parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `icao24` | yes | Aircraft ICAO24 hex, e.g. `a1b2c3` |

**Cache TTL:** 24 hours (aircraft type never changes)

**Upstream:** `https://opensky-network.org/api/metadata/aircraft/icao24/{icao24}`

**Success response (200):**

```json
{
  "icao24": "a1b2c3",
  "registration": "N916NN",
  "manufacturericao": "BOEING",
  "manufacturername": "Boeing",
  "model": "737-823",
  "typecode": "B738",
  "serialnumber": "30085",
  "linenumber": "812",
  "icaoaircrafttype": "L2J",
  "operator": "American Airlines",
  "operatorcallsign": "AMERICAN",
  "operatoricao": "AAL",
  "operatoriata": "AA",
  "owner": "American Airlines",
  "categoryDescription": "No ADS-B Emitter Category Information",
  "built": "2000-09-01",
  "engines": "CFM56-7B27"
}
```

**Error responses:**

| Status | Body | Meaning |
|--------|------|---------|
| 400 | `{"error": "missing icao24"}` | No icao24 provided |
| 404 | `{"error": "aircraft not found", "icao24": "..."}` | OpenSky has no record for this hex |

---

## `GET /api/forecast`

Returns a 3-day weather forecast (today, tomorrow, day after) built from OpenWeatherMap's 5-day/3-hour forecast, averaged per day.

**Query parameters:** none (uses config lat/lon)

**Cache TTL:** 1 hour

**Upstream:** `https://api.openweathermap.org/data/2.5/forecast`

**Success response (200):**

```json
{
  "days": [
    {
      "date": "2026-04-30",
      "hi": 57,
      "lo": 45,
      "cond": "Clouds",
      "cond_id": 803,
      "wind": 14,
      "wind_deg": 210
    },
    {
      "date": "2026-05-01",
      "hi": 63,
      "lo": 48,
      "cond": "Clear",
      "cond_id": 800,
      "wind": 8,
      "wind_deg": 180
    },
    {
      "date": "2026-05-02",
      "hi": 55,
      "lo": 41,
      "cond": "Rain",
      "cond_id": 500,
      "wind": 19,
      "wind_deg": 260
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `date` | string | ISO 8601 date (`YYYY-MM-DD`) |
| `hi` | int | High temperature (°F) |
| `lo` | int | Low temperature (°F) |
| `cond` | string | Most common OWM condition main group for the day (e.g. `Clear`, `Rain`, `Clouds`, `Snow`, `Thunderstorm`) |
| `cond_id` | int | OWM condition ID (last slot of the day wins; use for icon selection) |
| `wind` | int | Average wind speed (mph) |
| `wind_deg` | int | Average wind direction (degrees, meteorological — 0/360=N, 90=E, 180=S, 270=W) |

**OWM condition IDs (relevant groups):**

| Range | Condition |
|-------|-----------|
| 200–299 | Thunderstorm |
| 300–399 | Drizzle |
| 500–599 | Rain |
| 600–699 | Snow |
| 700–799 | Atmosphere (fog, mist, haze) |
| 800 | Clear |
| 801–804 | Clouds |

**Error responses:**

| Status | Body | Meaning |
|--------|------|---------|
| 500 | `{"error": "no openweather_key configured"}` | `openweather_key` missing from `config.json` |
| 502 | `{"error": "forecast fetch failed"}` | OWM unreachable |

---

## `GET /api/ships`

Returns nearby vessels from the live AIS WebSocket feed (aisstream.io). Filtered by position fix, minimum length (≥30 m), and maximum distance (≤10 miles from home location). Sorted nearest-first.

**Query parameters:** none

**Cache TTL:** none (live from in-memory WebSocket cache)

**Upstream:** `wss://stream.aisstream.io/v0/stream` (persistent WebSocket, background thread)

**Success response (200):**

```json
{
  "ships": [
    {
      "mmsi": "366123456",
      "name": "OCEAN VOYAGER",
      "type": 70,
      "type_name": "Cargo",
      "destination": "NEW YORK",
      "callsign": "WDF1234",
      "speed": 12.5,
      "heading": 245,
      "lat": 42.18,
      "lon": -70.72,
      "length": 185,
      "distance_mi": 3.2,
      "last_seen": 1714500000.0
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `mmsi` | string | Maritime Mobile Service Identity (9-digit vessel ID) |
| `name` | string | Vessel name (from AIS static data or metadata) |
| `type` | int | AIS vessel type code (0–99) |
| `type_name` | string | Human-readable type category (see table below) |
| `destination` | string | Declared destination port (may be absent or blank) |
| `callsign` | string | Radio callsign |
| `speed` | float | Speed over ground (knots) |
| `heading` | int | Course over ground (degrees) |
| `lat` | float | Current latitude |
| `lon` | float | Current longitude |
| `length` | int | Vessel length in meters (A+B dimensions from AIS) |
| `distance_mi` | float | Distance from home location (miles) |
| `last_seen` | float | Unix timestamp of last AIS message |

**AIS vessel type categories:**

| Type codes | `type_name` | Display color |
|------------|-------------|---------------|
| 30–39 | Fishing | `0x44AA44` (green) |
| 40–49 | HighSpeed | `0xFF8800` (orange) |
| 50–59 | Special | `0xAAAA00` (olive) |
| 60–69 | Passenger | `0x44AAFF` (blue) |
| 70–79 | Cargo | `0xCC8844` (rust) |
| 80–89 | Tanker | `0xFF4444` (red) |
| 90–99 | Other | `0x888888` (gray) |
| other | Vessel | `0x666688` (dim) |

**Notes:**
- Ships without a name, without a valid position fix, or with length < 30 m are excluded.
- Ships not seen in the last 10 minutes are pruned before each response.
- The WebSocket listener reconnects automatically on disconnect.
- If `aisstream_key` is absent from `config.json`, ship tracking is disabled and `ships` will always be empty.

**Error responses:** always returns `200` with `{"ships": []}` even when no ships are present.

---

## `GET /api/health`

Liveness check — confirms the proxy is reachable and reports basic internal state.

**Query parameters:** none

**Cache TTL:** none

**Success response (200):**

```json
{
  "status": "ok",
  "cache_entries": 12,
  "ships_tracked": 3,
  "uptime_approx": "use /api/health to verify proxy is reachable"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"ok"` if the proxy is running |
| `cache_entries` | int | Number of active entries in the in-memory cache |
| `ships_tracked` | int | Total ships in the AIS cache (before filtering; includes ships outside range) |

---

## Configuration (`config.json`)

```json
{
  "opensky_user": "your-opensky-username",
  "opensky_pass": "your-opensky-password",
  "openweather_key": "your-owm-api-key",
  "aisstream_key": "your-aisstream-api-key",
  "latitude": 42.142039,
  "longitude": -70.693353,
  "bbox": 0.1
}
```

| Key | Used by | Description |
|-----|---------|-------------|
| `opensky_user` / `opensky_pass` | `/api/planes`, `/api/route`, `/api/aircraft` | OpenSky Basic Auth (anonymous requests are rate-limited more aggressively) |
| `openweather_key` | `/api/forecast` | OpenWeatherMap API key |
| `aisstream_key` | `/api/ships` | AISStream.io WebSocket API key |
| `latitude` / `longitude` | All endpoints | Home location — center of bounding boxes and distance calculations |
| `bbox` | `/api/planes` | Half-width of the plane search bounding box (degrees); default `0.1` ≈ 7 miles |
