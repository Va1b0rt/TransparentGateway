#!/usr/bin/python3
"""Configure, validate, start, and inspect a first Transparent Gateway instance."""

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
RUNTIME_FILES = {
    ".env": 0o600,
    "inventory/connectors.json": 0o640,
    "inventory/upstreams.jsonl": 0o640,
    "secrets/proxy-credentials.json": 0o640,
}
PROTOCOLS = {"http", "https", "socks4", "socks5"}


class UserError(Exception):
    pass


def run(command, *, timeout=120, input_text=None):
    return subprocess.run(
        [str(item) for item in command],
        cwd=ROOT,
        text=True,
        input=input_text,
        capture_output=True,
        timeout=timeout,
    )


def require_root():
    if os.geteuid() != 0:
        raise UserError("run this command with sudo")


def prompt(label, default=None):
    suffix = f" [{default}]" if default is not None else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def yes_no(label, default=False):
    suffix = " [y/N]" if not default else " [Y/n]"
    value = input(label + suffix + ": ").strip().lower()
    if not value:
        return default
    if value not in {"y", "yes", "n", "no"}:
        raise UserError("answer yes or no")
    return value in {"y", "yes"}


def read_example_env():
    values = {}
    order = []
    comments = []
    for line in ENV_EXAMPLE.read_text().splitlines():
        if not line or line.startswith("#"):
            comments.append(line)
            order.append(("comment", len(comments) - 1))
            continue
        if "=" not in line:
            raise UserError("invalid .env.example")
        key, value = line.split("=", 1)
        values[key] = value
        order.append(("value", key))
    return values, order, comments


def render_env(values, order, comments):
    lines = []
    for kind, item in order:
        lines.append(comments[item] if kind == "comment" else f"{item}={values[item]}")
    return "\n".join(lines) + "\n"


def write_runtime(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    os.chown(path, 0, 0 if path.name == ".env" else 65532)


def configure(_args):
    require_root()
    existing = [name for name in RUNTIME_FILES if (ROOT / name).exists()]
    if existing:
        raise UserError("runtime files already exist; remove them explicitly before reconfiguring")

    client_cidr = prompt("Client IPv4 subnet routed through this gateway", "192.168.91.0/24")
    try:
        network = ipaddress.IPv4Network(client_cidr, strict=True)
    except ValueError as error:
        raise UserError("client subnet must be an IPv4 network such as 192.168.91.0/24") from error
    if network.prefixlen == 0:
        raise UserError("CLIENT_CIDR cannot be 0.0.0.0/0")

    protocol = prompt("Protocol specified by your proxy provider (http, https, socks4, socks5)", "http").lower()
    if protocol not in PROTOCOLS:
        raise UserError("unsupported proxy protocol")
    host = prompt("Authorised upstream proxy host or IP")
    if not host or any(char.isspace() for char in host):
        raise UserError("proxy host is empty or invalid")
    try:
        port = int(prompt("Proxy port", "3128"))
    except ValueError as error:
        raise UserError("proxy port must be a number") from error
    if not 1 <= port <= 65535:
        raise UserError("proxy port must be between 1 and 65535")

    credentials = {}
    candidate = {"host": host, "port": port, "protocol": protocol, "source": "quickstart"}
    if yes_no("Does the proxy require a username and password?"):
        reference = "quickstart-proxy"
        username = prompt("Proxy username")
        password = getpass.getpass("Proxy password: ")
        if not username or not password:
            raise UserError("proxy username and password cannot be empty")
        candidate["credential_ref"] = reference
        credentials[reference] = {"username": username, "password": password}

    echo_url = prompt("Validation echo URL (press Enter to use the public first-run service)", "https://echo.devdays.net.ua/")
    validate_echo_url(echo_url)
    values, order, comments = read_example_env()
    values.update({
        "CLIENT_CIDR": str(network),
        "POOL_SIZE": "1",
        "RESERVE_SIZE": "10",
        "VALIDATION_CONCURRENCY": "4",
        "VALIDATION_INTERVAL_SECONDS": "60",
        "ANONYMITY_CHECK_URL": echo_url,
        "FETCH_INTERVAL_SECONDS": "60",
        "RANK_INTERVAL_SECONDS": "15",
    })
    connectors = {
        "allowed_connector_types": ["jsonl_inventory"],
        "active_connectors": [{
            "type": "jsonl_inventory",
            "name": "quickstart",
            "path": "/inventory/upstreams.jsonl",
        }],
    }
    write_runtime(ROOT / ".env", render_env(values, order, comments), 0o600)
    write_runtime(ROOT / "inventory/connectors.json", json.dumps(connectors, indent=2) + "\n", 0o640)
    write_runtime(ROOT / "inventory/upstreams.jsonl", json.dumps(candidate, separators=(",", ":")) + "\n", 0o640)
    write_runtime(ROOT / "secrets/proxy-credentials.json", json.dumps(credentials, indent=2) + "\n", 0o640)
    (ROOT / "provider-secrets").mkdir(mode=0o750, exist_ok=True)
    print("CONFIGURE PASS")
    print("Run: sudo python3 scripts/tgctl.py check")


def parse_env(path):
    values = {}
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise UserError(f"invalid .env line {number}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise UserError(f"invalid or duplicate .env key on line {number}")
        if any(char in value for char in ["$", "\\", '"', "'", "`", "\r"]):
            raise UserError(f".env values must be literal and unquoted (line {number})")
        values[key] = value
    return values


def validate_echo_url(value):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UserError("echo URL must use HTTP or HTTPS")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise UserError("echo URL cannot contain credentials, query, or fragment")


def validate_permissions():
    for name, mode in RUNTIME_FILES.items():
        path = ROOT / name
        if not path.exists():
            raise UserError(f"missing runtime file: {name}; run sudo python3 scripts/tgctl.py configure")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise UserError(f"runtime path is not a regular file: {name}")
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != mode:
            raise UserError(f"unsafe owner or permissions for {name}; expected root and {mode:o}")
        if mode == 0o640 and info.st_gid != 65532:
            raise UserError(f"wrong group for {name}; expected GID 65532")


def load_and_validate():
    validate_permissions()
    values = parse_env(ROOT / ".env")
    required = {
        "CLIENT_CIDR", "VALIDATOR_REDIS_URL", "GATEWAY_REDIS_URL", "POOL_SIZE",
        "RESERVE_SIZE", "ANONYMITY_CHECK_URL", "DNS_DEFAULT_RESOLVER_IPS",
        "DNS_DOH_IP", "DNS_DOH_SERVER_NAME",
    }
    missing = sorted(required - values.keys())
    if missing:
        raise UserError("missing .env values: " + ", ".join(missing))
    try:
        client = ipaddress.IPv4Network(values["CLIENT_CIDR"], strict=True)
    except ValueError as error:
        raise UserError("CLIENT_CIDR must be a canonical IPv4 network") from error
    if client.prefixlen == 0:
        raise UserError("CLIENT_CIDR cannot be 0.0.0.0/0")
    validate_echo_url(values["ANONYMITY_CHECK_URL"])
    if values["VALIDATOR_REDIS_URL"] != "redis://redis:6379/0" or values["GATEWAY_REDIS_URL"] != "redis://127.0.0.1:6379/0":
        raise UserError("quickstart requires the included local Redis")
    try:
        if int(values["POOL_SIZE"]) < 1 or int(values["RESERVE_SIZE"]) < int(values["POOL_SIZE"]):
            raise ValueError
    except ValueError as error:
        raise UserError("POOL_SIZE and RESERVE_SIZE are invalid") from error

    credentials = json.loads((ROOT / "secrets/proxy-credentials.json").read_text())
    if not isinstance(credentials, dict):
        raise UserError("credential store must be a JSON object")
    for reference, secret in credentials.items():
        if not reference or not isinstance(secret, dict) or not isinstance(secret.get("username"), str) or not isinstance(secret.get("password"), str):
            raise UserError("credential store contains an invalid entry")
        if secret["username"] == "replace-me" or secret["password"] == "replace-me":
            raise UserError("replace example proxy credentials before continuing")

    connectors = json.loads((ROOT / "inventory/connectors.json").read_text())
    active = connectors.get("active_connectors") if isinstance(connectors, dict) else None
    if connectors.get("allowed_connector_types") != ["jsonl_inventory"] or not isinstance(active, list) or len(active) != 1:
        raise UserError("quickstart requires one active jsonl_inventory connector")
    connector = active[0]
    if connector.get("type") != "jsonl_inventory" or connector.get("path") != "/inventory/upstreams.jsonl":
        raise UserError("inventory connector path or type is invalid")

    candidates = []
    for number, line in enumerate((ROOT / "inventory/upstreams.jsonl").read_text().splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        candidate = json.loads(line)
        if "endpoint" in candidate:
            raise UserError(f"inventory line {number} uses legacy endpoint; use separate host and port fields")
        if not candidate.get("host") or ".internal.example" in candidate["host"]:
            raise UserError(f"inventory line {number} still contains an example proxy host")
        if not isinstance(candidate.get("port"), int) or not 1 <= candidate["port"] <= 65535:
            raise UserError(f"inventory line {number} has an invalid port")
        if candidate.get("protocol") not in PROTOCOLS:
            raise UserError(f"inventory line {number} has an unsupported protocol")
        reference = candidate.get("credential_ref")
        if reference and reference not in credentials:
            raise UserError(f"inventory line {number} refers to missing credentials")
        candidates.append(candidate)
    if not candidates:
        raise UserError("proxy inventory is empty")

    compose = run(["docker", "compose", "config", "--quiet"], timeout=60)
    if compose.returncode:
        raise UserError("docker compose rejected the configuration")
    return values, candidates, credentials


def check(_args):
    require_root()
    _values, candidates, credentials = load_and_validate()
    print("CONFIG_CHECK PASS")
    print("connectors=1")
    print(f"candidates={len(candidates)}")
    print(f"credential_sets={len(credentials)}")
    print("compose=valid")


def up(args):
    require_root()
    load_and_validate()
    command = ["docker", "compose", "up", "--build", "--detach"]
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode:
        raise UserError("docker compose up failed")
    wait(argparse.Namespace(timeout=args.timeout))


def active_snapshot():
    result = run([
        "docker", "compose", "exec", "-T", "redis", "redis-cli", "--raw",
        "GET", "proxy_pool:active:v1",
    ], timeout=30)
    if result.returncode or not result.stdout.strip():
        return None
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return None
    return data


def wait(args):
    require_root()
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        snapshot = active_snapshot()
        if snapshot and snapshot["entries"]:
            print(f"ACTIVE_POOL ready={len(snapshot['entries'])}")
            print(f"generated_at={snapshot['generated_at']}")
            return
        time.sleep(3)
    raise UserError("active proxy pool did not become ready before the timeout")


def status(_args):
    require_root()
    result = subprocess.run(["docker", "compose", "ps"], cwd=ROOT)
    if result.returncode:
        raise UserError("cannot read service status")
    snapshot = active_snapshot()
    if snapshot and snapshot["entries"]:
        print(f"ACTIVE_POOL ready={len(snapshot['entries'])}")
        print(f"generated_at={snapshot['generated_at']}")
        identifiers = sorted({
            hashlib.sha256(entry["exit_ip"].encode()).hexdigest()[:12]
            for entry in snapshot["entries"] if entry.get("exit_ip")
        })
        print("active_exit_ids=" + ",".join(identifiers))
    else:
        print("ACTIVE_POOL ready=0")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure", help="create runtime files without printing secrets").set_defaults(handler=configure)
    commands.add_parser("check", help="validate runtime files without changing network rules").set_defaults(handler=check)
    start = commands.add_parser("up", help="build, start, and wait for the first active proxy")
    start.add_argument("--timeout", type=int, default=300)
    start.set_defaults(handler=up)
    waiting = commands.add_parser("wait", help="wait for a non-empty active proxy pool")
    waiting.add_argument("--timeout", type=int, default=300)
    waiting.set_defaults(handler=wait)
    commands.add_parser("status", help="show services and a redacted active-pool summary").set_defaults(handler=status)
    args = parser.parse_args()
    try:
        args.handler(args)
    except (UserError, json.JSONDecodeError, OSError, subprocess.TimeoutExpired) as error:
        print(f"{args.command.upper()} FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
