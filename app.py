#!/usr/bin/env python3
"""mqtt-bridge — Subscribe to Solar Assistant MQTT topics and expose them via HTTP API.

Optionally writes numeric topic values to InfluxDB for time-series history.
Set INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, and INFLUX_BUCKET in .env to enable.
Leave INFLUX_URL unset to run without InfluxDB (safe default).
"""
import os
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL.upper(), logging.INFO),
    format='%(asctime)s %(levelname)s %(message)s'
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MQTT config
# ---------------------------------------------------------------------------
MQTT_HOST      = os.environ.get('MQTT_HOST', '192.168.50.22')
MQTT_PORT      = int(os.environ.get('MQTT_PORT', '1883'))
MQTT_USER      = os.environ.get('MQTT_USER', '')
MQTT_PASS      = os.environ.get('MQTT_PASS', '')
MQTT_PREFIX    = os.environ.get('MQTT_PREFIX', 'solar_assistant')
MQTT_CLIENT_ID = os.environ.get('MQTT_CLIENT_ID', 'mqtt-bridge')
API_PORT       = int(os.environ.get('PORT', '5003'))

# ---------------------------------------------------------------------------
# InfluxDB config — all optional, writes disabled if INFLUX_URL is unset
# ---------------------------------------------------------------------------
INFLUX_URL    = os.environ.get('INFLUX_URL', '').strip()
INFLUX_TOKEN  = os.environ.get('INFLUX_TOKEN', '').strip()
INFLUX_ORG    = os.environ.get('INFLUX_ORG', 'home')
INFLUX_BUCKET = os.environ.get('INFLUX_BUCKET', 'solar')

# ---------------------------------------------------------------------------
# InfluxDB client setup
# influx_write_api and influx_query_api are None when InfluxDB is disabled.
# ---------------------------------------------------------------------------
influx_write_api = None
influx_query_api = None
_influx_client   = None

if INFLUX_URL and INFLUX_TOKEN:
    try:
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.write_api import SYNCHRONOUS
        _influx_client   = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
        influx_write_api = _influx_client.write_api(write_options=SYNCHRONOUS)
        influx_query_api = _influx_client.query_api()
        log.info('InfluxDB enabled → %s  bucket=%s  org=%s', INFLUX_URL, INFLUX_BUCKET, INFLUX_ORG)
    except Exception as e:
        log.error('Failed to initialise InfluxDB client: %s — writes disabled', e)
        influx_write_api = None
        influx_query_api = None
else:
    log.info('InfluxDB write disabled (INFLUX_URL or INFLUX_TOKEN not set)')

# ---------------------------------------------------------------------------
# Topic store — latest value per topic, in memory
# ---------------------------------------------------------------------------
store_lock  = threading.Lock()
topic_store = {}
# { "solar_assistant/inverter_1/temperature/state": { "value": "52.3", "ts": "14:23:01", "epoch": 1234567890 } }

mqtt_connected = False

# Global MQTT client reference — set in main() so the HTTP handler can publish
mqtt_client_ref = None


def set_topic(topic, value):
    now = datetime.now()
    with store_lock:
        topic_store[topic] = {
            'value': value,
            'ts':    now.strftime('%H:%M:%S'),
            'epoch': int(now.timestamp())
        }


def get_all_topics():
    with store_lock:
        return dict(topic_store)


def get_topic(path):
    """Look up a topic by full path or relative path (prefix auto-added if missing)."""
    with store_lock:
        if path in topic_store:
            return topic_store[path]
        full = f'{MQTT_PREFIX}/{path}'
        if full in topic_store:
            return topic_store[full]
    return None


def get_numeric_topics():
    """Return list of short topic names (prefix stripped) that have numeric values.
    Used by the graph config UI to populate the topic picker.
    """
    result = []
    with store_lock:
        for topic, data in topic_store.items():
            if not topic.endswith('/state'):
                continue
            try:
                float(data['value'])
            except (ValueError, TypeError):
                continue
            short = topic[len(MQTT_PREFIX) + 1:] if topic.startswith(MQTT_PREFIX + '/') else topic
            result.append(short)
    return sorted(result)


def build_tree():
    """Convert flat topic dict into a nested tree for easy consumption."""
    tree = {}
    with store_lock:
        items = list(topic_store.items())
    for topic, data in items:
        parts = topic.split('/')
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data
    return tree


# ---------------------------------------------------------------------------
# InfluxDB write helper
# ---------------------------------------------------------------------------
def write_to_influx(topic: str, value: str, ts: datetime):
    """Write a single numeric MQTT reading to InfluxDB. No-op if disabled."""
    if influx_write_api is None:
        return
    if not topic.endswith('/state'):
        return
    try:
        float_val = float(value)
    except (ValueError, TypeError):
        return
    short_topic = topic[len(MQTT_PREFIX) + 1:] if topic.startswith(MQTT_PREFIX + '/') else topic
    try:
        from influxdb_client import Point
        point = (
            Point('solar')
            .tag('topic', short_topic)
            .tag('prefix', MQTT_PREFIX)
            .field('value', float_val)
            .time(ts.replace(tzinfo=timezone.utc))
        )
        influx_write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)
        log.debug('InfluxDB write: %s = %s', short_topic, float_val)
    except Exception as e:
        log.warning('InfluxDB write failed for %s: %s', topic, e)


# ---------------------------------------------------------------------------
# InfluxDB history query
#
# GET /history?topics=a,b,c&range=24h&window=1m
#
# topics  — comma-separated short topic names (prefix already stripped)
# range   — Flux duration string: 24h (default), 6h, 7d, etc.
# window  — aggregation window: raw (default, no downsampling), 1m, 5m, 1h
#
# Returns:
# {
#   "success": true,
#   "range": "24h",
#   "window": "raw",
#   "series": {
#     "inverter_1/pv_power/state": [
#       { "time": 1234567890, "value": 1250.0 }, ...
#     ],
#     ...
#   }
# }
# ---------------------------------------------------------------------------
def query_history(topics: list, range_str: str = '24h', window: str = 'raw') -> dict:
    """Query InfluxDB for historical data for one or more topics."""
    if influx_query_api is None:
        return {'success': False, 'error': 'InfluxDB not enabled'}
    if not topics:
        return {'success': False, 'error': 'No topics specified'}

    # Build a Flux filter for the requested topics
    topic_filters = ' or '.join([f'r["topic"] == "{t}"' for t in topics])

    if window == 'raw' or not window:
        flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{range_str})
  |> filter(fn: (r) => r["_measurement"] == "solar" and r["_field"] == "value")
  |> filter(fn: (r) => {topic_filters})
  |> keep(columns: ["_time", "_value", "topic"])
  |> sort(columns: ["_time"])
'''
    else:
        flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{range_str})
  |> filter(fn: (r) => r["_measurement"] == "solar" and r["_field"] == "value")
  |> filter(fn: (r) => {topic_filters})
  |> aggregateWindow(every: {window}, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value", "topic"])
  |> sort(columns: ["_time"])
'''

    try:
        tables  = influx_query_api.query(flux, org=INFLUX_ORG)
        series  = {}
        for table in tables:
            for record in table.records:
                topic = record.values.get('topic', 'unknown')
                if topic not in series:
                    series[topic] = []
                series[topic].append({
                    'time':  int(record.get_time().timestamp()),
                    'value': record.get_value()
                })
        return {
            'success': True,
            'range':   range_str,
            'window':  window,
            'series':  series
        }
    except Exception as e:
        log.warning('InfluxDB history query failed: %s', e)
        return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# Energy history query — half-hourly grid import kWh for a single date
# ---------------------------------------------------------------------------
def query_energy_history(date_str: str) -> dict:
    """
    Query InfluxDB for half-hourly grid import totals for a given date.

    date_str: 'YYYY-MM-DD' in local time (Europe/London). If empty, defaults
              to today. Converts to UTC range before querying.

    Returns:
    {
      "success": true,
      "date": "2025-05-12",
      "slots": [
        { "interval_start": "2025-05-12T00:00:00+01:00",
          "interval_end":   "2025-05-12T00:30:00+01:00",
          "consumption_kwh": 0.142 },
        ...
      ]
    }

    grid_power_ct/state is in Watts, sampled roughly every second via MQTT.
    aggregateWindow(every:30m, fn:mean) gives mean watts for each window.
    kWh = mean_watts * 0.5 / 1000   (0.5 = half an hour)
    Negative values (grid export) are clamped to zero.
    """
    import zoneinfo
    from datetime import date as _date

    if influx_query_api is None:
        return {'success': False, 'error': 'InfluxDB not enabled'}

    LOCAL_TZ = zoneinfo.ZoneInfo('Europe/London')

    # Parse or default the date
    try:
        if date_str:
            local_date = datetime.strptime(date_str, '%Y-%m-%d').date()
        else:
            local_date = datetime.now(LOCAL_TZ).date()
    except ValueError:
        return {'success': False, 'error': f'Invalid date format: {date_str!r} — use YYYY-MM-DD'}

    # Build UTC range covering the full local day (handles BST/GMT correctly)
    day_start_local = datetime(local_date.year, local_date.month, local_date.day,
                               0, 0, 0, tzinfo=LOCAL_TZ)
    day_end_local   = datetime(local_date.year, local_date.month, local_date.day,
                               23, 59, 59, tzinfo=LOCAL_TZ)

    start_utc = day_start_local.astimezone(timezone.utc)
    end_utc   = day_end_local.astimezone(timezone.utc)

    start_str = start_utc.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str   = end_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

    short_topic = f'inverter_1/grid_power_ct/state'

    flux = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: {start_str}, stop: {end_str})
  |> filter(fn: (r) => r["_measurement"] == "solar" and r["_field"] == "value")
  |> filter(fn: (r) => r["topic"] == "{short_topic}")
  |> aggregateWindow(every: 30m, fn: mean, createEmpty: false)
  |> keep(columns: ["_time", "_value"])
  |> sort(columns: ["_time"])
'''

    try:
        tables = influx_query_api.query(flux, org=INFLUX_ORG)
        import_slots = []
        export_slots = []
        for table in tables:
            for record in table.records:
                t       = record.get_time()                          # UTC datetime — this is window END
                t_local = t.astimezone(LOCAL_TZ)
                start_t = t_local - __import__('datetime').timedelta(minutes=30)
                watts   = record.get_value() or 0.0
                # Import: clamp export (negative) to zero
                import_kwh = max(0.0, watts * 0.5 / 1000.0)
                # Export: clamp import (positive) to zero, return as positive kWh value
                export_kwh = max(0.0, -watts * 0.5 / 1000.0)
                slot = {
                    'interval_start': start_t.isoformat(),
                    'interval_end':   t_local.isoformat(),
                }
                import_slots.append({**slot, 'consumption_kwh': round(import_kwh, 4)})
                export_slots.append({**slot, 'consumption_kwh': round(export_kwh, 4)})
        return {
            'success':       True,
            'date':          local_date.isoformat(),
            'slots':         import_slots,   # backward-compatible — import only
            'export_slots':  export_slots,
        }
    except Exception as e:
        log.warning('energy-history query failed: %s', e)
        return {'success': False, 'error': str(e)}


# ---------------------------------------------------------------------------
# MQTT callbacks
# ---------------------------------------------------------------------------
def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        log.info('Connected to MQTT broker at %s:%d', MQTT_HOST, MQTT_PORT)
        subscribe_topic = f'{MQTT_PREFIX}/#'
        client.subscribe(subscribe_topic)
        log.info('Subscribed to %s', subscribe_topic)
        mqtt_connected = True
    else:
        log.error('MQTT connection failed with code %d', rc)
        mqtt_connected = False


def on_disconnect(client, userdata, rc):
    global mqtt_connected
    log.warning('MQTT disconnected (rc=%d)', rc)
    mqtt_connected = False


def on_message(client, userdata, msg):
    try:
        value = msg.payload.decode('utf-8').strip()
        now   = datetime.now()
        set_topic(msg.topic, value)
        write_to_influx(msg.topic, value, now)
        log.debug('Topic update: %s = %s', msg.topic, value)
    except Exception as e:
        log.warning('Failed to process message on %s: %s', msg.topic, e)


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------
class BridgeHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.debug('API %s - ' + fmt, self.address_string(), *args)

    def send_json(self, code, data):
        body = json.dumps(data, indent=2).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', len(body))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed  = urlparse(self.path)
        path    = parsed.path.rstrip('/')
        parts   = [p for p in path.split('/') if p]
        qs      = parse_qs(parsed.query)

        # GET /health
        if path == '/health':
            return self.send_json(200, {
                'status':          'ok',
                'mqtt_connected':  mqtt_connected,
                'topic_count':     len(topic_store),
                'broker':          f'{MQTT_HOST}:{MQTT_PORT}',
                'prefix':          MQTT_PREFIX,
                'influx_enabled':  influx_write_api is not None,
            })

        # GET /topics — full flat dict of all topics and their latest values
        if path == '/topics':
            return self.send_json(200, {
                'success':        True,
                'mqtt_connected': mqtt_connected,
                'topic_count':    len(topic_store),
                'topics':         get_all_topics()
            })

        # GET /topics/tree — nested tree structure
        if path == '/topics/tree':
            return self.send_json(200, {
                'success':        True,
                'mqtt_connected': mqtt_connected,
                'tree':           build_tree()
            })

        # GET /topics/numeric — short names of all topics with numeric values
        # Used by the graph config modal to populate the topic picker.
        if path == '/topics/numeric':
            return self.send_json(200, {
                'success': True,
                'topics':  get_numeric_topics()
            })

        # GET /topics/{path...} — single topic value
        if parts and parts[0] == 'topics' and len(parts) > 1:
            topic_path = '/'.join(parts[1:])
            result = get_topic(topic_path)
            if result:
                return self.send_json(200, {
                    'success': True,
                    'topic':   topic_path,
                    **result
                })
            return self.send_json(404, {
                'success': False,
                'error':   f'Topic not found: {topic_path}'
            })

        # GET /summary — curated key solar assistant values by friendly label
        if path == '/summary':
            keys = [
                ('temperature',   f'{MQTT_PREFIX}/inverter_1/temperature/state'),
                ('pv_power',      f'{MQTT_PREFIX}/total/pv_power/state'),
                ('load_power',    f'{MQTT_PREFIX}/total/load_power/state'),
                ('battery_power', f'{MQTT_PREFIX}/total/battery_power/state'),
                ('battery_soc',   f'{MQTT_PREFIX}/total/battery_state_of_charge/state'),
                ('grid_power',    f'{MQTT_PREFIX}/total/grid_power/state'),
                ('inverter_power',f'{MQTT_PREFIX}/inverter_1/power/state'),
                ('ac_output',     f'{MQTT_PREFIX}/inverter_1/ac_output_voltage/state'),
            ]
            summary = {}
            with store_lock:
                for label, topic in keys:
                    if topic in topic_store:
                        summary[label] = topic_store[topic]
            return self.send_json(200, {
                'success':        True,
                'mqtt_connected': mqtt_connected,
                'summary':        summary
            })

        # GET /history?topics=a,b&range=24h&window=raw
        if path == '/history':
            if influx_query_api is None:
                return self.send_json(503, {'success': False, 'error': 'InfluxDB not enabled'})
            topics_raw = qs.get('topics', [''])[0]
            topics     = [t.strip() for t in topics_raw.split(',') if t.strip()]
            range_str  = qs.get('range',  ['24h'])[0]
            window     = qs.get('window', ['raw'])[0]
            result     = query_history(topics, range_str, window)
            code       = 200 if result.get('success') else 500
            return self.send_json(code, result)

        # GET /energy-history?date=YYYY-MM-DD
        # Returns 48 half-hourly grid import kWh slots for the given date.
        # Negative values (export) are clamped to zero.
        if path == '/energy-history':
            if influx_query_api is None:
                return self.send_json(503, {'success': False, 'error': 'InfluxDB not enabled'})
            date_str = qs.get('date', [''])[0].strip()
            result   = query_energy_history(date_str)
            code     = 200 if result.get('success') else (400 if 'invalid' in result.get('error','').lower() else 500)
            return self.send_json(code, result)

        self.send_json(404, {'error': 'Unknown endpoint'})

    def do_POST(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')

        # POST /publish  — publish a value to an MQTT topic
        # Body: { "topic": "solar_assistant/inverter_1/time_point_1/set", "value": "06:00" }
        if path == '/publish':
            if not mqtt_connected or mqtt_client_ref is None:
                return self.send_json(503, {'success': False, 'error': 'MQTT not connected'})
            try:
                length = int(self.headers.get('Content-Length', 0))
                body   = json.loads(self.rfile.read(length))
                topic  = body.get('topic', '').strip()
                value  = str(body.get('value', '')).strip()
                if not topic or value == '':
                    return self.send_json(400, {'success': False, 'error': 'topic and value are required'})
                result = mqtt_client_ref.publish(topic, value, qos=1, retain=False)
                result.wait_for_publish(timeout=5)
                log.info('Published: %s = %s', topic, value)
                return self.send_json(200, {'success': True, 'topic': topic, 'value': value})
            except Exception as e:
                log.warning('Publish failed: %s', e)
                return self.send_json(500, {'success': False, 'error': str(e)})

        self.send_json(404, {'error': 'Unknown endpoint'})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_api():
    server = HTTPServer(('0.0.0.0', API_PORT), BridgeHandler)
    log.info('MQTT Bridge API listening on port %d', API_PORT)
    server.serve_forever()


def main():
    log.info('MQTT Bridge starting')
    log.info('Broker: %s:%d  Prefix: %s', MQTT_HOST, MQTT_PORT, MQTT_PREFIX)

    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()

    global mqtt_client_ref
    client = mqtt.Client(client_id=MQTT_CLIENT_ID)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.reconnect_delay_set(min_delay=5, max_delay=60)
    mqtt_client_ref = client

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        client.disconnect()


if __name__ == '__main__':
    main()
