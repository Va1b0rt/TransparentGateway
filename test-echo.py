#!/usr/bin/env python3
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class EchoHandler(BaseHTTPRequestHandler):
    def reply(self, include_body):
        body = json.dumps({
            'backend_peer': self.client_address[0],
            'method': self.command,
            'path': self.path,
            'headers': dict(self.headers),
        }, indent=2).encode() + b'\n'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)
    def do_GET(self):
        self.reply(True)
    def do_HEAD(self):
        self.reply(False)
    def log_message(self, format, *args):
        pass

ThreadingHTTPServer(('10.10.10.2', 8000), EchoHandler).serve_forever()
