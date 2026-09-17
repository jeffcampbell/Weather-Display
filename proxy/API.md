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
| `GET /api/tides` | Tide predictions (rolling 30-day window) with cache + offline fallback |
| `GET /api/ships` | Nearby vessels from the live AIS feed |
| `GET /api/ships/debug` | Unfiltered ship data for diagnostics |
| `GET /api/sightings` | Historical ship + plane log (SQLite) |
| `GET /api/devicelog` | Tail of device log entries |
| `POST /api/devicelog` | Append device log entries |
| `GET /api/health` | Liveness check |
| `GET /api/time` | Current UTC + the proxy's local TZ offset (the device's clock source) |
| `GET /api/status` | Normalized cloud/dev provider outage board (128x64 display) |

---

## OpenSky authentication

`/api/planes`, `/api/route` (OpenSky fallback), and `/api/aircraft` all hit OpenSky Network using **OAuth2 client credentials**. OpenSky deprecated Basic Auth in 2024; anonymous and legacy-Basic-Auth requests get the smallest credit budget and 429 quickly.

**Setup:** create an API client at https://opensky-network.org → **Account → API Client**. Put the resulting `clientId` and `clientSecret` in `proxy/config.json` as `opensky_client_id` and `opensky_client_secret`.

**How the proxy uses them:** on the first OpenSky-bound request after startup (or after the cached token is within 60 s of expiring), the proxy POSTs the credentials to OpenSky's Keycloak token endpoint and caches the returned bearer token in memory:

```
POST https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token
Content-Type: application/x-www-form-urlencoded
grant_type=client_credentials&client_id=...&client_secret=...
```

Tokens are good for ~30 minutes. The cache is process-local; restarting the proxy forces a fresh token. If the credentials are missing or the token exchange fails, OpenSky calls go out unauthenticated and you'll see `opensky_rate_limited` in `/api/health` quickly.

---

## `GET /api/planes`

Returns aircraft currently within the configured bounding box, proxied from OpenSky Network and reshaped into a compact device-friendly form.

**Query parameters:** none (uses `latitude`/`longitude`/`bbox` from `config.json`)

**Cache TTL:** 55 seconds. On a 429 rate-limit, an empty response is cached for 1 hour, then 2 hours on each successive 429 (logged via `_log_proxy_event`).

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

Looks up a flight's route (origin → destination airports) and aircraft type. Tries three upstreams, cheapest first: OpenSky route DB (free), adsbdb (free), then FlightAware AeroAPI (real-time, paid). The free DBs return a callsign's *scheduled* route, which goes stale when a callsign is reused for a different leg, so by default FlightAware is consulted to *override* a free answer — not merely as a last resort — giving the aircraft's actual current route (see `flightaware_override_free_routes`). FlightAware is still skipped entirely for bare N-number GA registrations (which it essentially never resolves) and once the monthly quota is exhausted. Aircraft type/registration come from hexdb.io (or FlightAware if it had them).

**Query parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `callsign` | yes | Flight callsign, e.g. `AAL1563` |
| `icao24` | no | Aircraft hex code — enables type/registration lookup |

**Cache TTL:** resolved routes are cached 1 hour per `callsign[+icao24]` pair; misses ("route not found") are cached 6 hours. Caching the miss matters most — the device re-polls every ~60 s, and an uncached miss meant a fresh billable FlightAware call on every poll for as long as the plane loitered in the bbox. Aircraft metadata cached 24 hours per ICAO24.

**Upstreams (in order):**
- `https://opensky-network.org/api/routes?callsign=...`
- `https://api.adsbdb.com/v0/callsign/...`
- `https://aeroapi.flightaware.com/aeroapi/flights/{callsign}` (consulted to override the free route when `flightaware_override_free_routes` is set — the default — or otherwise only when the free sources found nothing; requires `flightaware_key`, skipped for bare N-numbers, **and only while the monthly FlightAware quota is not exhausted**)
- `https://hexdb.io/api/v1/aircraft/{icao24}` (type + registration)

**Monthly spend cap:** FlightAware is the only paid upstream, so billable `/flights` calls are capped per calendar month (UTC) by `flightaware_monthly_limit` (default 450). The count is persisted to `flightaware_usage.json` next to `server.py`, so a proxy restart can't reset it mid-month; only genuinely billable calls count (a non-2xx response is refunded), and it rolls over automatically on the 1st. Once the cap is hit, this endpoint stops consulting FlightAware and serves whatever the free sources found — routes still resolve for most airline callsigns, you just lose the real-time paid fallback until the next month. Current usage is reported by [`GET /api/health`](#get-apihealth) (`flightaware_used` / `flightaware_limit`), which also raises a `flightaware_quota_exhausted` issue when the cap is reached.

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

## `GET /api/tides`

Returns a rolling 30-day window of high/low tide predictions for a NOAA CO-OPS
station, in the shape NOAA's own `datagetter` returns. The device fetches this
instead of calling NOAA directly, so a slow or hung NOAA connection only ties
up one Pi thread instead of tripping the device's watchdog.


Takes an optional `?loc=<name>`, whose entry may set its own `station` — tide stations are per-coastline, so a display on a different shore needs its own or it shows the wrong water. Precedence: explicit `?station=` > the location's `station` > the global `noaa_station`. The cache is keyed on the station itself, so two locations sharing one station share a cached month.

**Query parameters:**

| Param | Required | Description |
|-------|----------|-------------|
| `station` | no | NOAA station ID. Defaults to `noaa_station` in `config.json`. Must be a harmonic/reference station (subordinate/offset-only stations don't support the predictions product). |

**Cache TTL:** 1 day fresh; served up to ~25 days stale if NOAA is unreachable.
Tide predictions are deterministic astronomical data, so a day-old (or even a
weeks-old) fetch within its window is just as correct as a fresh one.

**Upstream:** `https://api.tidesandcurrents.noaa.gov/api/prod/datagetter` (predictions, `interval=hilo`, `datum=MLLW`, `time_zone=lst_ldt`)

**Success response (200):**

```json
{
  "predictions": [
    { "t": "2026-07-18 09:29", "v": "-0.5", "type": "L" },
    { "t": "2026-07-18 15:47", "v": "9.8",  "type": "H" }
  ]
}
```

| Field | Type | Description |
|-------|------|-------------|
| `t` | string | Local time of the extreme (`YYYY-MM-DD HH:MM`) |
| `v` | string | Predicted height at the extreme (ft, relative to MLLW) |
| `type` | string | `H` (high) or `L` (low) |

**Fallback chain.** NOAA's predictions engine goes down platform-wide for days
at a time (it returns HTTP 200 with a misleading `"No Predictions data was
found"` body). To keep the display from ever blanking to N/A, the endpoint
degrades through three tiers and **always returns valid JSON**:

1. **Live NOAA** — fetched and cached (1 day).
2. **Stale cache** — the last good fetch, served for up to ~25 days.
3. **Local harmonic prediction** — computed offline from the station's own
   published harmonic constituents (fetched once from NOAA's metadata API and
   cached to `harmonics_<station>.json`). See below.

If all three are unavailable it returns `{"predictions": [], "upstream_error": <status>}`.

### Optional: local offline fallback (`pytides`)

Tier 3 is optional and self-contained. If its venv isn't set up, the endpoint
simply skips it (tiers 1–2 still work). To enable it:

```sh
cd proxy
python3 -m venv tide_venv
./tide_venv/bin/pip install -r tide-requirements.txt
```

`proxy/tide_predict.py` runs in that venv as a **short-lived subprocess** (never
imported into the always-on proxy — numpy/scipy stay out of its resident
memory), computing hi/lo extrema from NOAA's published constituents with no
network at all. The constituents are bootstrapped automatically: after any
successful live fetch, and also on-demand from NOAA's metadata/`harcon`
endpoint (which usually stays up even when the predictions engine is down).

---

## Locations (`?loc=<name>`)

Every location-aware endpoint — `/api/planes`, `/api/forecast`, `/api/sky`, `/api/tides`, `/api/ships`, `/api/route` — takes an optional `?loc=<name>`, resolved against the `locations` block in `config.json`.

| `loc` | Behavior |
|-------|----------|
| omitted | The proxy's own `latitude`/`longitude`/`bbox`, reported as the location name `default`. This is what these endpoints have always done, so a caller that doesn't use locations needs no changes. |
| a configured name | That entry's `lat`/`lon`/`bbox`; any key it omits falls back to the global value. |
| an unknown name | **400** with the list of available names. Serving a different coastline silently would be worse than failing. |

`/api/planes` and `/api/forecast` also still accept raw `?lat=`/`?lon=`/`?bbox=` overrides, which win over `loc` and are cached under their coordinates so one caller's ad-hoc box is never served to another.

Responses are keyed and cached per location, and aircraft sightings are recorded with the location that saw them (see `flightaware_used_by_loc` on `/api/health`).

### Deprecated: `/api/v2/*`

`/api/v2/planes`, `/api/v2/forecast` and `/api/v2/sky` are aliases for the canonical paths above and will be removed. "v2" only ever meant "accepts `?loc=`", which every canonical path now does. Each hit is logged (at most once an hour per path) with the caller's address, so the aliases can be deleted once nothing is using them.

## `GET /api/ships`

Returns nearby vessels from the live AIS WebSocket feed (aisstream.io). Filters: must have a name, must have a valid position fix, length must be ≥30 m if reported, distance must be ≤10 mi from the configured location. Sorted nearest-first.

Static fields (`name`, `type`, `type_name`, `callsign`, `length`) missing from the live feed are filled in from the persistent `vessel_static` table in `sightings.db`. Each time we receive a Type 5 static report we UPSERT those fields for that MMSI, so vessels we've seen before always carry full context — even after a proxy restart, and even if today's WebSocket session hasn't received a fresh Type 5 yet. `destination` is intentionally **not** cached: it's voyage data and changes every trip, so we'd risk showing a stale port.


Takes an optional `?loc=<name>`: distances are measured from that location and filtered by its radius. Without one, the proxy's own coordinates are used.

**Ship tracking is opt-in per location.** A location tracks vessels only if its `locations` entry sets `ship_radius_mi` (its display radius in miles); the AIS subscription covers exactly those places, so an inland location costs no bandwidth and simply has nothing near it. If no location opts in, the proxy's own coordinates are used, which is the historical behavior. A location that never opted in still answers — the shared vessel pool just holds nothing within range, so the list comes back empty on its own.

The AIS bounding boxes are deliberately wider (±1°, ~69 miles) than any display radius: a vessel's name, type and length arrive in sporadic Type 5 messages rather than with every position report, so tracking it well before it comes into range is what lets the static cache identify it by the time it matters — and an unnamed vessel is dropped from the response entirely. Because the subscription is sent when the WebSocket connects, **changing which locations track ships requires a service restart**, not just a config edit.

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
  "flightaware_month": "2026-08",
  "flightaware_used": 37,
  "flightaware_used_by_loc": { "home": 12, "beach": 25 },
  "flightaware_limit": 450,
  "uptime_seconds": 8412
}
```

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Always `"ok"` if the proxy is running |
| `issues` | array | Tags for currently-degraded upstreams. Empty array = healthy. |
| `cache_entries` | int | Active entries in the in-memory cache |
| `ships_tracked` | int | Total ships in the AIS cache (before filtering) |
| `flightaware_month` | string | Current billing period (`YYYY-MM`, UTC) the quota is counting against |
| `flightaware_used` | int | Billable FlightAware `/flights` calls made this month |
| `flightaware_used_by_loc` | object | The same count split by the display that triggered it, keyed by `loc` (`default` = the v1 endpoint, which has no `loc`). The monthly cap is **shared across every display** using this proxy, so this is what tells you which one is consuming it. Tallies start at zero when a new billing period begins; they always sum to `flightaware_used` within a period. |
| `flightaware_limit` | int | Configured monthly cap (`flightaware_monthly_limit`) |
| `uptime_seconds` | int | Seconds since the proxy process started |

Known `issues` values: `opensky_rate_limited`, `flightaware_quota_exhausted`. (List grows as more upstream checks are added.)

---

## `GET /api/time`

Returns current UTC seconds plus the proxy's local TZ offset (DST-aware). The MatrixPortal calls this at boot and on every weather refresh — it's the device's only clock source.

**Response:**

```json
{
  "utc": 1778437800,
  "tz_offset_secs": -14400
}
```

| Field | Type | Description |
|-------|------|-------------|
| `utc` | int | Current UTC time as Unix seconds |
| `tz_offset_secs` | int | Seconds to add to UTC to get the proxy's local time. Includes DST. |

**Why this exists:** the device used to NTP-sync at boot and fall back to OWM's `dt` field if NTP failed. NTP over UDP gets blocked on some Wi-Fi networks; OWM's `dt` is the data calculation time, not the response time, and can be 5–10 min stale on free-tier accounts. The proxy is on the LAN and runs `systemd-timesyncd`, so it's the most reliable clock source available to the device.

**Cache TTL:** none.

---

## `GET /api/status`

Aggregates the public status feeds of major cloud/dev providers into one compact, pre-normalized payload for the 128x64 outage board. The proxy does all the HTTP and parsing (8+ heterogeneous feeds) so the memory-constrained device just reads levels. Providers, their adapters, and order come from the `status_providers` config block.

**Response:**

```json
{
  "providers": [
    { "name": "GitHub", "level": 0 },
    { "name": "Supabase", "level": 1, "title": "401 errors due to JWT rejections", "component": "API Gateway", "updated": 1786694012 },
    { "name": "AWS", "level": 2, "title": "Increased error rates", "component": "Multiple services - us-east-1", "updated": 1786871500 }
  ],
  "worst": 1,
  "ts": 1786871646
}
```

| Field | Type | Description |
|-------|------|-------------|
| `providers` | array | One entry per configured provider, in config order (stable, so the device grid layout is stable) |
| `providers[].name` | string | Display name (`GitHub`, `AWS`, …) |
| `providers[].level` | int | Normalized status: `0` = normal, `1` = degraded, `2` = outage |
| `providers[].title` | string | Short incident headline. **Present only when `level > 0`.** Truncated to ~48 chars. |
| `providers[].component` | string | Affected service/component (and region where the feed gives one). **Optional**, only when the feed provides it. Truncated to ~24 chars. |
| `providers[].updated` | int | Unix seconds of the incident's last update, when the feed carries one (Statuspage and AWS). **Optional.** The device renders it as a clock time, collapsing to `>24H` once it's over a day old. |
| `worst` | int | The maximum `level` across all providers — lets the device decide at a glance whether any incident cards are needed |
| `ts` | int | Unix seconds when this snapshot was built |

**Level mapping.** Each adapter reports the *current* worst state, not an incident's peak — a feed that stays flagged after the impact has cleared should not keep the board lit.

- **Atlassian Statuspage** (GitHub, Cloudflare, Supabase, HashiCorp, Anthropic, …): a green `status.indicator` is `0`. A non-green indicator is trusted only when `summary.json` also lists an active (unresolved) incident — otherwise it's routine component noise (e.g. Cloudflare's perpetual edge-PoP maintenance) and reports `0`. When there is an active incident, the level is the max of that incident's affected components' *current* statuses (`operational` = 0; `degraded_performance`/`under_maintenance`/`partial_outage` = 1; `major_outage` = 2), so it tracks recovery even before the provider marks the incident resolved. If `summary.json` can't be fetched, it falls back to the indicator (`minor` = 1, `major`/`critical` = 2).
- **GCP** (`incidents.json`): an open incident (no `end`) with a high severity or an `OUTAGE` impact = 2, else 1.
- **AWS** (`currentevents`, UTF-16 JSON): the worst *active* event, graded by the feed's numeric `status` (`2` = degraded/1, `3` = disruption/2; `0`/`1` normal/informational are ignored). Events with an `end_time` (resolved) or no activity in the last 7 days (abandoned-open feed artifacts) are dropped, regardless of severity.
- **Azure** (status RSS): an active item = 1, escalated to 2 on an outage/unavailable/down keyword; items whose text says "resolved" are skipped.

**Resilience:** each provider is fetched inside a `try`/`except`; a feed that errors or times out degrades to `level: 0` (and logs a `proxy:` line) rather than failing the whole board.

**Cache TTL:** 180s (`STATUS_CACHE_SEC`). The device polls at this cadence; the cache keeps upstream load to at most one round of feed fetches per window regardless of how many displays poll.

---

## `GET /api/calendar`

Merges every configured private `.ics` feed into two ready-to-render day lists — today and tomorrow — for the 128x64 agenda view. All of iCalendar's awkward parts (folded lines, `TZID` resolution, `RRULE` expansion, `EXDATE` holes, `RECURRENCE-ID` overrides) are handled here, so the device receives nothing but a name and a preformatted time per event. Events from all calendars are pooled; which feed an event came from is deliberately not reported.

Feeds come from the `calendar_ics_urls` config block. **Each URL is a secret** — anyone holding one can read that entire calendar — so they are never echoed in a response and a failing feed is logged by position (`calendar feed 2/3 failed`), never by URL.

**Response:**

```json
{
  "days": [
    {
      "label": "TODAY",
      "date": "WED SEP 16",
      "iso": "2026-09-16",
      "events": [
        { "time": "ALL DAY", "name": "Jeff PTO", "all_day": true, "start": 1789358400 },
        { "time": "8:30a", "name": "Daily standup", "all_day": false, "start": 1789561800 }
      ],
      "more": 0
    },
    { "label": "TOMORROW", "date": "THU SEP 17", "iso": "2026-09-17", "events": [], "more": 0 }
  ],
  "calendars": 2,
  "errors": 0,
  "ts": 1789567718
}
```

| Field | Type | Description |
|-------|------|-------------|
| `days` | array | Always two entries — today first, then tomorrow. Empty only when no feeds are configured. |
| `days[].label` | string | `TODAY` / `TOMORROW`, ready to render as the card heading |
| `days[].date` | string | Uppercase `DOW MON D` (e.g. `WED SEP 16`), ASCII-only |
| `days[].iso` | string | `YYYY-MM-DD` for the day, in the proxy's local timezone |
| `days[].events` | array | All-day events first, then chronological. Identical events are collapsed, so an invite that lands on two configured calendars is listed once. |
| `days[].events[].time` | string | `9:00a` / `12:30p`, or the literal `ALL DAY` |
| `days[].events[].name` | string | Event summary, ASCII-only, truncated to 24 chars at a word boundary (`Anniversary dinne..`) |
| `days[].events[].all_day` | bool | True for a date-valued (all-day) event; the device colors those differently |
| `days[].events[].start` | int | Unix seconds of the event's start — unused by the current view, handy for anything that wants to sort or compare against "now" |
| `days[].more` | int | Events beyond the 12-per-day cap (`_CAL_MAX_PER_DAY`). The device adds its own row-count overflow to this for the `+N more` row. |
| `calendars` | int | How many feeds are configured |
| `errors` | int | How many of them failed this refresh |
| `ts` | int | Unix seconds when this snapshot was built |

**Which events land on a day.** An all-day event covers every day it spans (`DTEND` is exclusive, per RFC 5545, so a Sep 16 all-day event does not leak into Sep 17). A timed event is listed on the day it *starts* — one running past midnight belongs to the day it began, not to both. Events with `STATUS:CANCELLED` are dropped.

**Recurrence support.** `FREQ=DAILY`/`WEEKLY`/`MONTHLY`/`YEARLY` with `INTERVAL`, `COUNT`, `UNTIL`, `BYDAY` (including ordinals like `2TU`, `-1FR`), `BYMONTHDAY` and `BYMONTH` — the subset Google Calendar emits. `EXDATE` deletions and `RECURRENCE-ID` overrides (a moved or cancelled single instance) are honored. An unrecognized `FREQ` degrades to the event's own `DTSTART` rather than vanishing or looping. Expansion skips whole periods straight to the two-day window, so a daily event created years ago costs a couple of steps rather than one per elapsed day; every rule is additionally capped at 2000 steps.

**Timezones.** `TZID` values are resolved through the host's tzdata (`zoneinfo`); `...Z` values are UTC; a floating value is taken as local. An unresolvable `TZID` (e.g. a Windows-style name) logs once and falls back to local. "Today" and "tomorrow" are measured in the `timezone` config key, else `/etc/timezone`, else the host's current UTC offset.

**Resilience:** each feed is fetched inside a `try`/`except`; one bad feed is logged and skipped so the remaining calendars still render. If *every* feed fails the endpoint returns **502** rather than empty days — the device treats that as a fetch error and keeps displaying its last good lists instead of blanking the card.

**Cache TTL:** 600s (`CALENDAR_CACHE_SEC`), keyed by the local date as well — past midnight the cached payload's "TODAY" is yesterday's list, so the rollover invalidates it immediately instead of serving a stale day for the rest of the window.

---

## Site-local hooks (`local_hooks.py`)

Some integrations only make sense on one machine — a dashboard that happens to run on the same Pi, a notifier, a metrics sink. Rather than carry that in this repo, the proxy offers a hook point: drop a `local_hooks.py` next to `server.py` and it will be imported at startup and called for each event.

```python
# proxy/local_hooks.py   (gitignored; see local_hooks.py.example)
def on_event(event, fields):
    if event == "flightaware_usage":
        ...   # forward fields["count"] / fields["limit"] wherever you like
```

| Event | Fields | Fired when |
|-------|--------|-----------|
| `flightaware_usage` | `count`, `limit` | A billable FlightAware call is reserved or refunded — i.e. whenever the month's running total changes |

**Best-effort by contract.** No module, no `on_event`, or an exception inside the hook is caught and logged; serving is never affected. Hooks are called outside the usage lock, but a hook that blocks still delays the request that triggered it, so keep them quick.

**You may not need one.** Budget-threshold crossings and geo-check enforcement are already written to the **system journal** via syslog under the ident `matrix-portal-proxy`, at `WARNING` (or `ERR` for the highest threshold). Any host tooling that watches the journal picks those up with no hook at all.

---

## Configuration (`config.json`)

```json
{
  "latitude":  42.36,
  "longitude": -71.06,
  "bbox":      0.1,

  "openweather_key":        "YOUR_OPENWEATHERMAP_API_KEY",
  "noaa_station":           "8443970",
  "opensky_client_id":      "YOUR_OPENSKY_CLIENT_ID",
  "opensky_client_secret":  "YOUR_OPENSKY_CLIENT_SECRET",
  "aisstream_key":          "YOUR_AISSTREAM_API_KEY",
  "flightaware_key":        ""
}
```

| Key | Used by | Description |
|-----|---------|-------------|
| `latitude` / `longitude` | All endpoints | Home location — center of the plane bounding box, the AIS subscription box, and ship distance calculations |
| `bbox` | `/api/planes` | Half-width of the plane search box in degrees (default `0.1` ≈ 7 mi) |
| `openweather_key` | `/api/forecast` | OpenWeatherMap API key |
| `noaa_station` | `/api/tides` | Default NOAA CO-OPS station when the device omits `?station=`. Must be a harmonic/reference station (not a subordinate/offset-only one). No API key needed. |
| `opensky_client_id` / `opensky_client_secret` | `/api/planes`, `/api/route`, `/api/aircraft` | OpenSky OAuth2 client credentials (generate at opensky-network.org → Account → API Client). The proxy exchanges them for short-lived bearer tokens automatically. |
| `aisstream_key` | `/api/ships` | AISStream.io WebSocket API key. If missing, ship tracking is disabled. |
| `flightaware_key` | `/api/route` | FlightAware AeroAPI key (paid). Overrides the free OpenSky / adsbdb route by default (see `flightaware_override_free_routes`); if missing, those are the only route sources. |
| `flightaware_alert_thresholds` | syslog | Billable-call counts at which a budget warning is written to the system journal, once each per month. Highest logs at `ERR`, earlier at `WARNING`. Informational only — they never gate a call. Default `[1000, 1800]`. |
| `flightaware_override_free_routes` | `/api/route` | When `true` (default), FlightAware overrides a route the free DBs already resolved, fixing stale "right tail, wrong route" answers from reused callsigns. `false` reverts to consulting FlightAware only when the free sources found nothing. |
| `device_secret` | every endpoint | Shared secret the device must send as `X-Device-Secret`. Leave blank to disable the check (recommended only when the proxy is LAN-only). |
| `calendar_ics_urls` | `/api/calendar` | Private `.ics` feed URLs (Google Calendar → Settings → *Integrate calendar* → **Secret address in iCal format**), as bare strings or `{"url": ...}` objects. Events from every feed are pooled. **Each URL is a password** — treat `config.json` accordingly. Empty list disables the endpoint. |
| `timezone` | `/api/calendar` | IANA timezone name that "today" and "tomorrow" are measured in. Omit to use the host's own timezone. |
| `status_providers` | `/api/status` | Providers to monitor, in display order. Each has `name` and `type`. `type: "statuspage"` needs a `host` (any Atlassian Statuspage site — add one with no code). `type: "aws"`/`"gcp"`/`"azure"` use built-in adapters (no `host`). Omit the whole key to use the built-in default set (GitHub, Cloudflare, Supabase, HashiCorp, Anthropic, AWS, GCP, Azure). |

The server's listening port is set via the `PORT` environment variable (default `6590`).
