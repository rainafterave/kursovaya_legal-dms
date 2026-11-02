"""Read-only Docker resource metrics for the five application services."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import time
from urllib.parse import urlencode
from urllib.request import urlopen

API = os.getenv('DOCKER_API_URL', 'http://docker-metrics-proxy:2375')
SERVICES = {'nginx', 'app', 'db', 'redis', 'worker'}


def docker_get(path, parameters=None):
    query = '?' + urlencode(parameters) if parameters else ''
    with urlopen(API + path + query, timeout=6) as response:
        return json.load(response)


def containers():
    rows = docker_get('/containers/json', {'filters': json.dumps({'label': ['com.docker.compose.project=legal-dms']})})
    return [row for row in rows if row.get('Labels', {}).get('com.docker.compose.service') in SERVICES]


def escape(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('"', '\\"')


def container_metrics(row):
    stats = docker_get('/containers/' + row['Id'] + '/stats', {'stream': 'false', 'one-shot': 'true'})
    service = row['Labels']['com.docker.compose.service']
    labels = 'service="' + escape(service) + '",container="' + escape(row['Names'][0].lstrip('/')) + '"'
    memory = stats.get('memory_stats', {})
    cached = memory.get('stats', {}).get('inactive_file', memory.get('stats', {}).get('total_inactive_file', 0))
    networks = stats.get('networks', {}).values()
    values = {
        'legal_container_last_seen_seconds': time.time(),
        'legal_container_created_seconds': row['Created'],
        'legal_container_cpu_seconds_total': stats.get('cpu_stats', {}).get('cpu_usage', {}).get('total_usage', 0) / 1e9,
        'legal_container_memory_working_set_bytes': max(0, memory.get('usage', 0) - cached),
        'legal_container_network_receive_bytes_total': sum(net.get('rx_bytes', 0) for net in networks),
        'legal_container_network_transmit_bytes_total': sum(net.get('tx_bytes', 0) for net in stats.get('networks', {}).values()),
    }
    return [name + '{' + labels + '} ' + str(value) for name, value in values.items()]


def collect():
    lines = []
    for name in ('last_seen_seconds', 'created_seconds', 'cpu_seconds_total', 'memory_working_set_bytes',
                 'network_receive_bytes_total', 'network_transmit_bytes_total'):
        kind = 'counter' if name.endswith('_total') else 'gauge'
        lines.append('# TYPE legal_container_' + name + ' ' + kind)
    with ThreadPoolExecutor(max_workers=5) as pool:
        for values in pool.map(container_metrics, containers()):
            lines.extend(values)
    return '\n'.join(lines) + '\n'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == '/healthz':
                containers()
                body, content_type = b'ok\n', 'text/plain'
            elif self.path == '/metrics':
                body, content_type = collect().encode(), 'text/plain; version=0.0.4; charset=utf-8'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(body)
        except Exception as error:
            print('Metrics collection failed:', type(error).__name__, flush=True)
            self.send_error(503, 'Docker metrics are unavailable')


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 9100), Handler).serve_forever()
