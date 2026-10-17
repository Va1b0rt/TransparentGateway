import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest


PROJECT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("tg_quickstart", str(PROJECT / "scripts/tgctl.py"))
spec = importlib.util.spec_from_loader(loader.name, loader)
quickstart = importlib.util.module_from_spec(spec)
loader.exec_module(quickstart)


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    if os.geteuid() != 0:
        pytest.skip("ownership checks require root")
    monkeypatch.setattr(quickstart, "ROOT", tmp_path)
    monkeypatch.setattr(quickstart, "ENV_EXAMPLE", PROJECT / ".env.example")
    values, order, comments = quickstart.read_example_env()
    values.update({
        "CLIENT_CIDR": "192.168.91.0/24",
        "POOL_SIZE": "1",
        "RESERVE_SIZE": "10",
        "ANONYMITY_CHECK_URL": "https://echo.devdays.net.ua/",
    })
    documents = {
        ".env": quickstart.render_env(values, order, comments),
        "inventory/connectors.json": json.dumps({
            "allowed_connector_types": ["jsonl_inventory"],
            "active_connectors": [{
                "type": "jsonl_inventory",
                "name": "quickstart",
                "path": "/inventory/upstreams.jsonl",
            }],
        }),
        "inventory/upstreams.jsonl": json.dumps({
            "host": "proxy.example.net",
            "port": 3128,
            "protocol": "http",
            "credential_ref": "quickstart-proxy",
        }),
        "secrets/proxy-credentials.json": json.dumps({
            "quickstart-proxy": {"username": "user", "password": "secret"},
        }),
    }
    for name, content in documents.items():
        mode = quickstart.RUNTIME_FILES[name]
        quickstart.write_runtime(tmp_path / name, content + ("" if content.endswith("\n") else "\n"), mode)
    monkeypatch.setattr(quickstart, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", ""))
    return tmp_path


def test_valid_runtime_reports_candidate_without_exposing_secret(runtime):
    _values, candidates, credentials = quickstart.load_and_validate()
    assert len(candidates) == 1
    assert list(credentials) == ["quickstart-proxy"]


def test_legacy_endpoint_is_rejected(runtime):
    path = runtime / "inventory/upstreams.jsonl"
    quickstart.write_runtime(path, '{"endpoint":"proxy.example.net:3128","protocol":"http"}\n', 0o640)
    with pytest.raises(quickstart.UserError, match="legacy endpoint"):
        quickstart.load_and_validate()


def test_world_readable_credentials_are_rejected(runtime):
    (runtime / "secrets/proxy-credentials.json").chmod(0o644)
    with pytest.raises(quickstart.UserError, match="unsafe owner or permissions"):
        quickstart.load_and_validate()


def test_gateway_address_dns_is_sent_to_doh_before_generic_tproxy():
    entrypoint = (PROJECT / "gateway/entrypoint.sh").read_text()
    local_nat = "iptables -t nat -A TG_DNS_DOH -p udp -m addrtype --dst-type LOCAL -j REDIRECT --to-ports 5053"
    local_mangle = "iptables -t mangle -A TG_DNS_TPROXY -m addrtype --dst-type LOCAL -j RETURN"
    generic_tproxy = "iptables -t mangle -A TG_DNS_TPROXY -p udp -j TPROXY"

    assert local_nat in entrypoint
    assert local_mangle in entrypoint
    assert entrypoint.index(local_mangle) < entrypoint.index(generic_tproxy)


def test_systemd_unit_starts_existing_images_from_current_checkout(runtime):
    unit = quickstart.systemd_unit(300)

    assert f"WorkingDirectory={runtime}" in unit
    assert f"ExecStart=/usr/bin/python3 {runtime}/scripts/tgctl.py up --timeout 300 --no-build --skip-service" in unit
    assert "ExecStop=/usr/bin/docker compose stop --timeout 20" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "ExecStart=/usr/bin/docker" not in unit


def test_down_stops_installed_systemd_service_and_verifies_containers(runtime, monkeypatch, capsys):
    unit = runtime / "transparent-gateway.service"
    unit.write_text("test")
    monkeypatch.setattr(quickstart, "SYSTEMD_UNIT", unit)
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(quickstart.subprocess, "run", fake_run)

    quickstart.down(None)

    assert commands == [
        ["systemctl", "stop", "transparent-gateway.service"],
        ["docker", "compose", "ps", "--status", "running", "--quiet"],
    ]
    assert capsys.readouterr().out == "DOWN stopped=1\n"


def test_down_stops_compose_after_incomplete_first_start(runtime, monkeypatch):
    unit = runtime / "missing-transparent-gateway.service"
    monkeypatch.setattr(quickstart, "SYSTEMD_UNIT", unit)
    commands = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(quickstart.subprocess, "run", fake_run)

    quickstart.down(None)

    assert commands == [
        ["docker", "compose", "stop", "--timeout", "20"],
        ["docker", "compose", "ps", "--status", "running", "--quiet"],
    ]
