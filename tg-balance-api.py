#!/usr/bin/env python3
import hmac
import json
import os
import subprocess
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODE_PATH = Path('/etc/haproxy/tg-balance-mode')
TOKEN_PATH = Path('/etc/haproxy/tg-balance-api.token')
CONFIG_PATH = Path('/etc/haproxy/haproxy.cfg')
VALID_MODES = {'round-robin', 'stick-table'}


def current_mode():
    value = MODE_PATH.read_text().strip()
    return value if value in VALID_MODES else 'round-robin'


def render(mode):
    sticky = '    stick on src\n' if mode == 'stick-table' else ''
    return f'''global
    log /dev/log local0
    maxconn 1000
    stats socket /run/haproxy/admin.sock mode 660 level admin

defaults
    mode tcp
    timeout connect 5s
    timeout client 60s
    timeout server 60s

frontend test_forward_proxy
    bind 10.10.10.2:3129
    default_backend proxy_pool

backend proxy_pool
    balance roundrobin
    stick-table type ip size 200k expire 30m store conn_cur
{sticky}    option tcp-check
    server test1 10.10.10.11:3128 check
    server test2 10.10.10.12:3128 check
    server test3 10.10.10.13:3128 check
    server test4 10.10.10.14:3128 check
    server test5 10.10.10.15:3128 check
    server test6 10.10.10.16:3128 check
    server test7 10.10.10.17:3128 check
    server test8 10.10.10.18:3128 check
'''


def set_mode(mode):
    if mode not in VALID_MODES:
        raise ValueError('unsupported mode')
    data = render(mode)
    fd, temp_name = tempfile.mkstemp(prefix='haproxy.cfg.', dir='/etc/haproxy')
    try:
        with os.fdopen(fd, 'w') as handle:
            handle.write(data)
        subprocess.run(['/usr/sbin/haproxy', '-c', '-f', temp_name], check=True, capture_output=True, text=True)
        os.replace(temp_name, CONFIG_PATH)
        subprocess.run(['/usr/bin/systemctl', 'reload', 'haproxy'], check=True, capture_output=True, text=True)
        MODE_PATH.write_text(mode + '\n')
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class Handler(BaseHTTPRequestHandler):
    def send_json(self, code, payload):
        raw = json.dumps(payload).encode() + b'\n'
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        expected = 'Bearer ' + TOKEN_PATH.read_text().strip()
        return hmac.compare_digest(self.headers.get('Authorization', ''), expected)

    def do_GET(self):
        if self.path != '/v1/balancing':
            self.send_json(404, {'error': 'not found'})
            return
        mode = current_mode()
        self.send_json(200, {
            'mode': mode,
            'backend': 'proxy_pool',
            'selection': 'roundrobin' if mode == 'round-robin' else 'stick-table (stick on src)',
        })

    def do_POST(self):
        modes = {
            '/v1/balancing/round-robin': 'round-robin',
            '/v1/balancing/stick-table': 'stick-table',
        }
        mode = modes.get(self.path)
        if mode is None:
            self.send_json(404, {'error': 'not found'})
            return
        if not self.authorized():
            self.send_json(401, {'error': 'bearer token required'})
            return
        try:
            set_mode(mode)
            self.send_json(200, {'mode': mode, 'result': 'applied'})
        except subprocess.CalledProcessError as error:
            self.send_json(500, {'error': 'haproxy reload failed', 'detail': error.stderr[-500:]})
        except Exception as error:
            self.send_json(500, {'error': type(error).__name__})

    def log_message(self, format, *args):
        pass

ThreadingHTTPServer(('192.168.88.104', 8081), Handler).serve_forever()
