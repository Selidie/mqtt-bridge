# mqtt-bridge

A lightweight MQTT subscriber that connects to the Solar Assistant broker, caches all published topic values in memory, exposes them via a simple HTTP API, and optionally writes numeric readings to InfluxDB for time-series history.

Part of a home automation stack alongside [tuya-bridge](https://github.com/Selidie/tuya-bridge), [fan-controller](https://github.com/Selidie/fan-controller) and [home-dashboard](https://github.com/Selidie/home-dashboard).

---

## What it does

- Subscribes to `solar_assistant/#` (configurable prefix) on startup
- Maintains an in-memory cache of every topic value with timestamp
- Exposes an HTTP API so any service can query any MQTT value without needing an MQTT client
- New topics appear automatically as Solar Assistant publishes them — no code changes needed
- **InfluxDB integration** — optionally writes every numeric `/state` topic to InfluxDB 2.x for time-series history and graphing. Disabled by default; set `INFLUX_URL` and `INFLUX_TOKEN` to enable.
- **History endpoint** — queries InfluxDB for historical data with configurable time range and aggregation window, consumed by the home-dashboard chart UI
- **Energy history endpoint** — queries InfluxDB for half-hourly grid import totals for a specific date, used by the home-dashboard Electricity Usage page. Export (negative CT values) is clamped to zero. Returns kWh per 30-minute slot aligned to `Europe/London` local time.

---

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check — MQTT connection status, topic count, InfluxDB enabled flag |
| `GET /topics` | Full flat dict of all cached topics and their latest values |
| `GET /topics/tree` | Same data as a nested JSON tree |
| `GET /topics/numeric` | Short names of all topics with numeric values (used by chart config UI) |
| `GET /topics/{path}` | Single topic value (prefix optional) |
| `GET /summary` | Curated key solar values by friendly label |
| `GET /history` | Time-series history from InfluxDB (requires InfluxDB enabled) |
| `GET /energy-history` | Half-hourly grid import kWh for a given date (requires InfluxDB enabled) |

### Example responses

**`GET /topics/inverter_1/temperature/state`**
```json
{
  "success": true,
  "topic": "inverter_1/temperature/state",
  "value": "52.3",
  "ts": "14:23:01",
  "epoch": 1710508981
}
```

**`GET /summary`**
```json
{
  "success": true,
  "mqtt_connected": true,
  "summary": {
    "temperature":   { "value": "52.3",  "ts": "14:23:01", "epoch": 1710508981 },
    "pv_power":      { "value": "1840",  "ts": "14:23:01", "epoch": 1710508981 },
    "load_power":    { "value": "620",   "ts": "14:23:01", "epoch": 1710508981 },
    "battery_soc":   { "value": "87",    "ts": "14:23:01", "epoch": 1710508981 },
    "battery_power": { "value": "1180",  "ts": "14:23:01", "epoch": 1710508981 },
    "grid_power":    { "value": "0",     "ts": "14:23:01", "epoch": 1710508981 }
  }
}
```

**`GET /history?topics=inverter_1/pv_power/state,total/battery_power/state&range=24h&window=5m`**
```json
{
  "success": true,
  "range": "24h",
  "window": "5m",
  "series": {
    "inverter_1/pv_power/state": [
      { "time": 1710508981, "value": 1840.0 }
    ]
  }
}
```

### History query parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `topics` | Comma-separated short topic names (prefix already stripped) | required |
| `range` | Flux duration string: `1h`, `6h`, `24h`, `7d`, etc. | `24h` |
| `window` | Aggregation window: `raw` (no downsampling), `1m`, `5m`, `1h`, etc. | `raw` |

### Energy history query parameters

| Parameter | Description | Default |
|-----------|-------------|---------|
| `date` | Date to query in `YYYY-MM-DD` format (Europe/London local time) | today |

**Example response:**

**`GET /energy-history?date=2026-05-10`**
```json
{
  "success": true,
  "date": "2026-05-10",
  "slots": [
    {
      "interval_start": "2026-05-10T00:00:00+01:00",
      "interval_end":   "2026-05-10T00:30:00+01:00",
      "consumption_kwh": 0.1423
    },
    ...
  ]
}
```

- Uses `inverter_1/grid_power_ct/state` (CT clamp reading, Watts)
- Aggregates via `aggregateWindow(every: 30m, fn: mean)`, converts mean watts to kWh (`watts × 0.5 / 1000`)
- Negative values (grid export) are clamped to zero — import only
- InfluxDB timestamps are window-end; response returns corrected `interval_start` (window-end minus 30 minutes) to align with Octopus API conventions

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in your values.

### MQTT settings

| Variable | Description | Default |
|----------|-------------|---------|
| `MQTT_HOST` | IP or hostname of MQTT broker | `192.168.xx.xx` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `MQTT_USER` | MQTT username (leave blank if none) | `` |
| `MQTT_PASS` | MQTT password (leave blank if none) | `` |
| `MQTT_PREFIX` | Topic prefix to subscribe to | `solar_assistant` |
| `MQTT_CLIENT_ID` | MQTT client identifier | `mqtt-bridge` |
| `PORT` | HTTP API port | `5003` |
| `LOG_LEVEL` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) | `INFO` |

### InfluxDB settings (optional)

Leave `INFLUX_URL` empty to run without InfluxDB — this is the safe default and requires no InfluxDB instance.

| Variable | Description | Default |
|----------|-------------|---------|
| `INFLUX_URL` | InfluxDB 2.x base URL — set to enable writes | `` |
| `INFLUX_TOKEN` | InfluxDB API token with write/read access to the bucket | `` |
| `INFLUX_ORG` | InfluxDB organisation name | `home` |
| `INFLUX_BUCKET` | InfluxDB bucket to write to | `solar` |

> **Note:** Never commit your `.env` file. It is included in `.gitignore`.

---

## InfluxDB setup

InfluxDB 2.x is required for history features. The simplest way to run it is via the provided `docker-compose.yml` in the workspace root, which starts InfluxDB as part of the full stack.

To set up manually:

1. Run InfluxDB: `docker run -d -p 8086:8086 influxdb:2.7`
2. Open `http://localhost:8086` and complete initial setup (org: `home`, bucket: `solar`)
3. Generate an API token with read/write access to the `solar` bucket
4. Add `INFLUX_URL`, `INFLUX_TOKEN`, `INFLUX_ORG`, and `INFLUX_BUCKET` to your `.env`

Once configured, mqtt-bridge automatically writes all numeric `/state` topics to InfluxDB on every MQTT message.

---

## Running with Docker

### Docker Compose (recommended)

See the root [`docker-compose.yml`](../docker-compose.yml) for the full stack including InfluxDB.

Single-service snippet (without InfluxDB):

```yaml
services:
  mqtt-bridge:
    image: ghcr.io/selidie/mqtt-bridge:latest
    container_name: mqtt-bridge
    restart: unless-stopped
    env_file: .env
    networks:
      - home-stack

networks:
  home-stack:
    external: true
```

### Build and run manually

```bash
docker build -t mqtt-bridge .
docker run -d \
  --name mqtt-bridge \
  --env-file .env \
  --network home-stack \
  mqtt-bridge
```

---

## Development

```bash
pip install -r requirements.txt
python app.py
```

---

## Architecture

```
Solar Assistant MQTT Broker
        │
        │  subscribe to solar_assistant/#
        ▼
  ┌──────────────────┐
  │ mqtt-bridge      │  in-memory topic cache
  │                  │
  │  /health         │
  │  /topics         │
  │  /topics/tree    │
  │  /topics/numeric │
  │  /topics/{path}  │
  │  /summary        │
  │  /history        │  ◄── queries InfluxDB
  │  /energy-history │  ◄── queries InfluxDB (half-hourly kWh, date-scoped)
  └──────┬───────────┘
         │               │
         │ HTTP          │ write numeric /state topics
         ▼               ▼
  ┌──────────────┐   ┌──────────┐
  │home-dashboard│   │ InfluxDB │
  └──────────────┘   └──────────┘
```

---

## Related projects

- [tuya-bridge](https://github.com/Selidie/tuya-bridge) — HTTP API gateway for Tuya smart devices
- [fan-controller](https://github.com/Selidie/fan-controller) — MQTT temperature-driven fan automation
- [home-dashboard](https://github.com/Selidie/home-dashboard) — Web UI for monitoring, control, solar history charts, and electricity usage comparison
