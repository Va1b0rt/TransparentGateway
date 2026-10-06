#!/usr/bin/python3
"""Check a client's only IPv4 route, DNS, and HTTPS path without proxy settings."""

import argparse
import hashlib
import ipaddress
import json
import socket
import subprocess
import sys
import urllib.request
from urllib.parse import urlsplit
import uuid


def exit_id(address):
    ipaddress.ip_address(address)
    return hashlib.sha256(address.encode()).hexdigest()[:12]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", help="expected IPv4 gateway")
    parser.add_argument("--url", default="https://echo.devdays.net.ua/")
    args = parser.parse_args()

    routes = json.loads(subprocess.check_output(["ip", "-j", "-4", "route", "show", "default"]))
    if len(routes) != 1 or not routes[0].get("gateway") or not routes[0].get("dev"):
        raise ValueError("the client must have exactly one IPv4 default route")
    route = routes[0]
    if args.gateway and route["gateway"] != args.gateway:
        raise ValueError("the IPv4 default route uses an unexpected gateway")
    print(f"CLIENT_ROUTE PASS gateway={route['gateway']} dev={route['dev']}")

    hostname = urlsplit(args.url).hostname
    if not hostname:
        raise ValueError("echo URL has no hostname")
    addresses = sorted({row[4][0] for row in socket.getaddrinfo(hostname, 443, socket.AF_INET)})
    if not addresses:
        raise ValueError("DNS returned no IPv4 addresses")
    print(f"CLIENT_DNS PASS addresses={len(addresses)}")

    marker = uuid.uuid4().hex
    request = urllib.request.Request(args.url, headers={
        "User-Agent": "TG-Quickstart-Client/1.0",
        "X-Request-ID": marker,
    })
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError("public echo returned an unexpected HTTP status")
        data = json.load(response)
    headers = {name.lower(): value for name, value in data.get("headers", {}).items()}
    if headers.get("x-request-id") != marker or not data.get("backend_peer"):
        raise ValueError("public echo response is incomplete or belongs to another request")
    print(f"CLIENT_HTTPS PASS request_id={marker}")
    print(f"PUBLIC_EXIT id={exit_id(data['backend_peer'])}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"CLIENT_CHECK FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
