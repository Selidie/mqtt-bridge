# mqtt-bridge

A lightweight MQTT subscriber that connects to the Solar Assistant broker, caches all published topic values in memory, and exposes them via a simple HTTP API. Designed to run as a Docker container alongside other home automation services.

Part of a home automation stack alongside [tuya-bridge](https://github.com/Selidie/tuya-bridge), [fan-controller](https://github.com/Selidie/fan-controller) and [tuya-dashboard](https://github.com/Selidie/tuya-dashboard).

---

## What it does

- Subscribes to `solar_assistant/#` (configurable prefix) on startup
- Maintains an in-memory cache of every topic value with timestamp
- Exposes an HTTP API so any service on the network can query any MQTT value without needing an MQTT client itself
- New topics appear automatically as Solar Assistant publishes them — no code changes needed

---

## Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Health check — connection status, topic count |
| `GET /topics` | Full flat dict of all cached topics and values |
| `GET /topics/tree` | Same data as a nested JSON tree |
| `GET /topics/{path}` | Single topic value (prefix optional) |
| `GET /summary` | Curated key solar values by friendly label |

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

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and fill in your values.

| Variable | Description | Default |
|----------|-------------|---------|
| `MQTT_HOST` | IP or hostname of MQTT broker | `192.168.xx.xx` |
| `MQTT_PORT` | MQTT broker port | `1883` |
| `MQTT_USER` | MQTT username (leave blank if none) | `` |
| `MQTT_PASS` | MQTT password (leave blank if none) | `` |
| `MQTT_PREFIX` | Topic prefix to subscribe to | `<your_broker>` |
| `MQTT_CLIENT_ID` | MQTT client identifier | `mqtt-bridge` |
| `PORT` | HTTP API port | `5003` |
| `LOG_LEVEL` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`) | `INFO` |

> **Note:** Never commit your `.env` file. It is included in `.gitignore`.

---

## Running with Docker

### Docker Compose (recommended)

```yaml
services:
  mqtt-bridge:
    image: ghcr.io/selidie/mqtt-bridge:latest
    container_name: mqtt-bridge
    restart: unless-stopped
    env_file: .env
    ports:
      - "5003:5003"
    networks:
      - frontend

networks:
  frontend:
    external: true
```

### Build and run manually

```bash
docker build -t mqtt-bridge .
docker run -d \
  --name mqtt-bridge \
  --env-file .env \
  --network frontend \
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
  ┌─────────────┐
  │ mqtt-bridge │  caches all topic values in memory
  │             │
  │  /health    │
  │  /topics    │
  │  /topics/{path}
  │  /summary   │
  └──────┬──────┘
         │ HTTP
         ▼
  ┌──────────────────┐
  │  tuya-dashboard  │  polls /topics or /summary, renders widgets
  └──────────────────┘
```

---

## Related projects

- [tuya-bridge](https://github.com/Selidie/tuya-bridge) — HTTP API gateway for Tuya smart devices
- [fan-controller](https://github.com/Selidie/fan-controller) — MQTT temperature-driven fan automation
- [tuya-dashboard](https://github.com/Selidie/tuya-dashboard) — Web UI for monitoring and control
