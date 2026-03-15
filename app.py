#!/usr/bin/env python3
"""mqtt-bridge — Subscribe to Solar Assistant MQTT topics and expose them via HTTP API."""
import os
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from datetime import datetime
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
# Config
# ---------------------------------------------------------------------------
MQTT_HOST      = os.environ.get('MQTT_HOST', '192.168.50.22')
MQTT_PORT      = int(os.environ.get('MQTT_PORT', '1883'))
MQTT_USER      = os.environ.get('MQTT_USER', '')
MQTT_PASS      = os.environ.get('MQTT_PASS', '')
MQTT_PREFIX    = os.environ.get('MQTT_PREFIX', 'solar_assistant')
MQTT_CLIENT_ID = os.environ.get('MQTT_CLIENT_ID', 'mqtt-bridge')
API_PORT       = int(os.environ.get('PORT', '5003'))

# ---------------------------------------------------------------------------
# Topic store
# ---------------------------------------------------------------------------
store_lock  = threading.Lock()
topic_store = {}
# { "solar_assistant/inverter_1/temperature/state": { "value": "52.3", "ts": "14:23:01", "epoch": 1234567890 } }

mqtt_connected = False


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
        set_topic(msg.topic, value)
        log.debug('Topic update: %s = %s', msg.topic, value)
    except Exception as e:
        log.warning('Failed to decode message on %s: %s', msg.topic, e)


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
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip('/')
        parts  = [p for p in path.split('/') if p]

        # GET /health
        if path == '/health':
            return self.send_json(200, {
                'status':         'ok',
                'mqtt_connected': mqtt_connected,
                'topic_count':    len(topic_store),
                'broker':         f'{MQTT_HOST}:{MQTT_PORT}',
                'prefix':         MQTT_PREFIX
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

        # GET /topics/{path...} — single topic value
        # e.g. /topics/solar_assistant/inverter_1/temperature/state
        #   or /topics/inverter_1/temperature/state  (prefix auto-added)
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

    # Start HTTP API in background thread
    api_thread = threading.Thread(target=run_api, daemon=True)
    api_thread.start()

    # Connect to MQTT and loop forever
    client = mqtt.Client(client_id=MQTT_CLIENT_ID)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message
    client.reconnect_delay_set(min_delay=5, max_delay=60)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        client.disconnect()


if __name__ == '__main__':
    main()
