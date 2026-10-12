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
