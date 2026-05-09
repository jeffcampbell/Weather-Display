# Matrix Portal Proxy — API Reference

Base URL (Raspberry Pi): `http://YOUR_PI_IP:6590`

All responses are `application/json`. All endpoints are `GET` unless noted.

## Authentication

When `device_secret` is set in the proxy's `config.json`, **every endpoint** requires a matching `X-Device-Secret` header — auth is global, not per-route. When `device_secret` is empty (or omitted), no auth is enforced. Mismatched or missing header on a configured proxy returns `401 {"error": "bad device secret"}`.

| Endpoint | Purpose |
|----------|---------|
| `GET /api/planes` | Aircraft within bounding box (slim, device-friendly) |
| `GET /api/route` | Flight route + aircraft type/registration |
| `GET /api/aircraft` | Raw OpenSky aircraft metadata by ICAO24 |
| `GET /api/forecast` | 3-day weather forecast from OpenWeatherMap |
| `GET /api/ships` | Nearby vessels from the live AIS feed |
| `GET /api/ships/debug` | Unfiltered ship data for diagnostics |
| `GET /api/sightings` | Historical ship + plane log (SQLite) |
| `GET /api/devicelog` | Tail of device log entries |
| `POST /api/devicelog` | Append device log entries |
| `GET /api/health` | Liveness check |

---

## `GET /api/planes`

Returns aircraft currently within the configured bounding box, proxied from OpenSky Network and reshaped into a compact device-friendly form.

**Query parameters:** none (uses `latitude`/`longitude`/`bbox` from `config.json`)

**Cache TTL:** 55 seconds. On a 429 rate-limit, an empty response is cached for 1 hour.

**Upstream:** `https://opensky-network.org/api/states/all`

**Response:** Each plane is a 6-element positional array `[callsign, icao24, alt_ft, speed_kt, heading_deg, vrate_m_s]`. Positional arrays save ~180 bytes per plane on the device's heap vs. named-key dicts. Aircraft on the ground or without a callsign are filtered out.

```json
{
  "time": 1714500000,
  "planes": [
    ["AAL1563", "a1b2c3", 25000, 480, 285, 0],
    ["JBU42",   "a4d5e6", 18000, 380,  92, 256]
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `time` | int | Upstream OpenSky `time` field (Unix seconds) |
| `planes[i][0]` | string | Callsign (trimmed, max 8 chars) |
| `planes[i][1]` | string | ICAO24 hex address |
| `planes[i][2]` | int | Altitude (feet, baro or geometric) |
| `planes[i][3]` | int | Ground speed (knots) |
| `planes[i][4]` | int | True track (degrees, 0=N, 90=E) |
| `planes[i][5]` | int | Vertical rate (m/s) |

**Special responses (always 200):**

```json
{ "time": 0, "planes": [], "rate_limited": true }   // upstream 429
{ "time": 0, "planes": [], "upstream_error": 503 }  // upstream non-200
```

The endpoint always returns valid JSON with status 200 even on upstream failure, so the device's JSON parser doesn't trip.

---

## `GET /api/route`

Looks up a flight's route (origin → destination airports) and aircraft type. Tries three upstreams in order: FlightAware AeroAPI (real-time, paid), OpenSky route DB, then adsbdb. Aircraft type/registration come from hexdb.io (or FlightAware if it had them).

**Query parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `callsign` | yes | Flight callsign, e.g. `AAL1563` |
| `icao24` | no | Aircraft hex code — enables type/registration lookup |

**Cache TTL:** 1 hour per `callsign[+icao24]` pair. Aircraft metadata cached 24 hours per ICAO24.

**Upstreams (in order):**
- `https://aeroapi.flightaware.com/aeroapi/flights/{callsign}` (only if `flightaware_key` is configured)
- `https://opensky-network.org/api/routes?callsign=...`
- `https://api.adsbdb.com/v0/callsign/...`
- `https://hexdb.io/api/v1/aircraft/{icao24}` (type + registration)

**Success response (200):**

```json
{
  "callsign": "AAL1563",
  "route": ["KDFW", "KLGA"],
  "typecode": "B738",
  "registration": "N916NN"
}
```

- `route`: `[origin_icao, destination_icao]`. Empty array if no upstream had a match.
- `typecode`: ICAO aircraft type (e.g. `B738`); empty if unknown.
- `registration`: tail number; empty if unknown.

**Error responses:**

| Status | Body | Meaning |
|--------|------|---------|
| 400 | `{"error": "missing callsign"}` | No callsign provided |
| 404 | `{"error": "route not found", "callsign": "..."}` | No upstream has a route for this callsign |

---

## `GET /api/aircraft`

Returns raw aircraft metadata from OpenSky by ICAO24 hex code.

**Query parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `icao24` | yes | Aircraft ICAO24 hex, e.g. `a1b2c3` |

**Cache TTL:** 24 hours.

**Upstream:** `https://opensky-network.org/api/metadata/aircraft/icao24/{icao24}`

**Success response (200):** Raw OpenSky JSON, e.g.

```json
{
  "icao24": "a1b2c3",
  "registration": "N916NN",
  "manufacturername": "Boeing",
  "model": "737-823",
  "typecode": "B738",
  "operator": "American Airlines",
  "operatoricao": "AAL"
}
```

**Error responses:**

| Status | Body | Meaning |
|--------|------|---------|
| 400 | `{"error": "missing icao24"}` | No icao24 provided |
| 404 | `{"error": "aircraft not found", "icao24": "..."}` | OpenSky has no record for this hex |

---

## `GET /api/forecast`

Returns up to 3 days of weather (today + next 2) computed from OpenWeatherMap's 5-day/3-hour forecast, averaged per day.

**Query parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `lat` | no | Override config latitude |
| `lon` | no | Override config longitude |

**Cache TTL:** 1 hour (single global cache key — `lat`/`lon` overrides are not part of the cache key).

**Upstream:** `https://api.openweathermap.org/data/2.5/forecast`

**Success response (200):**

```json
{
  "days": [
    {
      "date": "2026-04-30",
      "hi": 57, "lo": 45,
      "cond": "Clouds", "cond_id": 803,
      "wind": 14, "wind_deg": 210
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `date` | string | ISO 8601 date (`YYYY-MM-DD`) |
| `hi` / `lo` | int | High / low temperature (°F) |
| `cond` | string | Most common OWM condition main group (`Clear`, `Rain`, `Clouds`, `Snow`, `Thunderstorm`) |
| `cond_id` | int | OWM condition ID (last forecast slot of the day) |
| `wind` | int | Average wind speed (mph) |
| `wind_deg` | int | Average wind direction (degrees, meteorological) |

**OWM condition ID groups (for `cond_id`):**

| Range | Condition |
|-------|-----------|
| 200–299 | Thunderstorm |
| 300–399 | Drizzle |
| 500–599 | Rain |
| 600–699 | Snow |
| 700–799 | Atmosphere (fog/mist/haze) |
| 800 | Clear |
| 801–804 | Clouds |

**Error responses:**

| Status | Body | Meaning |
|--------|------|---------|
| 500 | `{"error": "no openweather_key configured"}` | `openweather_key` missing from `config.json` |
| 500 | `{"error": "..."}` | Forecast parse error |

---

## `GET /api/ships`

Returns nearby vessels from the live AIS WebSocket feed (aisstream.io). Filters: must have a name, must have a valid position fix, length must be ≥30 m if reported, distance must be ≤10 mi from the configured location. Sorted nearest-first.

**Query parameters:** none

**Cache TTL:** none (live in-memory snapshot from the WebSocket listener)

**Upstream:** `wss://stream.aisstream.io/v0/stream` (persistent WebSocket, background thread, auto-reconnects)

**Success response (200):**

```json
{
  "ships": [
    {
      "name": "OCEAN VOYAGER",
      "type": 70,
      "type_name": "Cargo",
      "destination": "NEW YORK",
      "length": 185,
      "heading": 245,
      "distance_mi": 3.2
    }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Vessel name (from AIS static data or metadata) |
| `type` | int | AIS vessel type code (0–99) |
| `type_name` | string | Human-readable category (see below) |
| `destination` | string | Declared destination port; may be empty |
| `length` | int | Vessel length in meters (A+B dimensions); 0 if unreported |
| `heading` | int | Course over ground (degrees) |
| `distance_mi` | float | Distance from configured location (miles, 1 decimal) |

**AIS vessel type categories** (decade of `type` field):

| Range | `type_name` |
|-------|-------------|
| 30–39 | Fishing |
| 40–49 | HighSpeed |
| 50–59 | Special |
| 60–69 | Passenger |
| 70–79 | Cargo |
| 80–89 | Tanker |
| 90–99 | Other |
| other | Vessel |

**Notes:**
- Ships not seen for 10 minutes are pruned before each response.
- If `aisstream_key` is absent from `config.json`, the WebSocket listener does not start and `/api/ships` always returns `{"ships": []}`.
- Each call also writes any matching ship to the SQLite sightings log (deduped at 5 min per MMSI).

---

## `GET /api/ships/debug`

Returns up to 20 ships from the in-memory cache **without filtering** (no name/length/distance requirements), each annotated with computed `distance_mi`. Includes raw fields like `mmsi`, `lat`, `lon`, `speed`, `callsign`, `last_seen`. Useful for diagnosing why an expected ship isn't appearing in `/api/ships`.

**Response:**

```json
{
  "total": 47,
  "ships": [
    {
      "mmsi": "366123456",
      "name": "OCEAN VOYAGER",
      "type": 70,
      "type_name": "Cargo",
      "destination": "NEW YORK",
      "callsign": "WDF1234",
      "lat": 42.18,
      "lon": -70.72,
      "speed": 12.5,
      "heading": 245,
      "length": 185,
      "last_seen": 1714500000.0,
      "distance_mi": 3.2
    }
  ]
}
```

`total` is the count of ships in the in-memory cache before truncation to 20.

---

## `GET /api/sightings`

Queries the historical sightings log (SQLite — `sightings.db`).

**Query parameters:**

| Param | Default | Description |
|-------|---------|-------------|
| `type` | `both` | `ships`, `planes`, or `both` |
| `hours` | `24` | Look back this many hours |
| `limit` | `100` | Max rows per category |

**Response:**

```json
{
  "ships":  [ { "id": 1, "ts": 1714500000, "mmsi": "366123456", "name": "OCEAN VOYAGER", "type_name": "Cargo", "lat": 42.18, "lon": -70.72, "speed": 12.5, "heading": 245, "distance_mi": 3.2, "destination": "NEW YORK" } ],
  "planes": [ { "id": 1, "ts": 1714500000, "callsign": "AAL1563", "icao24": "a1b2c3", "alt_ft": 25000, "speed_kt": 480, "heading": 285, "lat": 42.21, "lon": -70.81, "distance_mi": 1.7 } ]
}
```

Either key is omitted when filtered out by `type`. Ships are deduped at 5 min per MMSI; planes at 2 min per callsign.

---

## `GET /api/devicelog`

Returns the tail of the device log file.

**Query parameters:**

| Param | Default | Max | Description |
|-------|---------|-----|-------------|
| `lines` | `100` | `1000` | Number of trailing lines to return |

**Response:**

```json
{
  "lines": [
    "2026-04-30 10:15:00 | [10:15:00] Boot OK",
    "2026-04-30 10:15:30 | [10:15:30] Wx:62F Clear 8mph SW"
  ],
  "total": 12345
}
```

`total` is the total number of lines in the log file (not just returned).

---

## `POST /api/devicelog`

Appends device-side log entries. Called by the MatrixPortal roughly every 5 minutes to flush its local buffer.

**Request body:**

```json
{ "msgs": ["[10:15:00] Boot OK", "[10:15:30] Wx:62F Clear 8mph SW"] }
```

**Response:**

```json
{ "ok": true, "appended": 2 }
```

The log file is rotated automatically: if it grows past 10 000 lines, the oldest are dropped on the next write.

**Errors:**

| Status | Body |
|--------|------|
| 400 | `{"error": "no msgs"}` or JSON parse error |
| 401 | `{"error": "bad device secret"}` — header missing or mismatched |
| 500 | `{"error": "..."}` on disk write failure |

---

## `GET /api/health`

Liveness check.

**Response:**

```json
{
  "status": "ok",
  "issues": ["opensky_rate_limited"],
  "cache_entries": 12,
  "ships_tracked": 3,
  "uptime_seconds": 8412
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"ok"` if the proxy is running |
| `issues` | array | Tags for currently-degraded upstreams. Empty array = healthy. |
| `cache_entries` | int | Active entries in the in-memory cache |
| `ships_tracked` | int | Total ships in the AIS cache (before filtering) |
| `uptime_seconds` | int | Seconds since the proxy process started |

Known `issues` values: `opensky_rate_limited`. (List grows as more upstream checks are added.)

---

## Configuration (`config.json`)

```json
{
  "latitude":  42.36,
  "longitude": -71.06,
  "bbox":      0.1,

  "openweather_key": "YOUR_OPENWEATHERMAP_API_KEY",
  "opensky_user":    "YOUR_OPENSKY_USERNAME",
  "opensky_pass":    "YOUR_OPENSKY_PASSWORD",
  "aisstream_key":   "YOUR_AISSTREAM_API_KEY",
  "flightaware_key": ""
}
```

| Key | Used by | Description |
|-----|---------|-------------|
| `latitude` / `longitude` | All endpoints | Home location — center of the plane bounding box, the AIS subscription box, and ship distance calculations |
| `bbox` | `/api/planes` | Half-width of the plane search box in degrees (default `0.1` ≈ 7 mi) |
| `openweather_key` | `/api/forecast` | OpenWeatherMap API key |
| `opensky_user` / `opensky_pass` | `/api/planes`, `/api/route`, `/api/aircraft` | OpenSky Basic Auth (anonymous requests are rate-limited harder) |
| `aisstream_key` | `/api/ships` | AISStream.io WebSocket API key. If missing, ship tracking is disabled. |
| `flightaware_key` | `/api/route` | FlightAware AeroAPI key (paid). If missing, falls back to OpenSky / adsbdb. |
| `device_secret` | every endpoint | Shared secret the device must send as `X-Device-Secret`. Leave blank to disable the check (recommended only when the proxy is LAN-only). |

The server's listening port is set via the `PORT` environment variable (default `6590`).
