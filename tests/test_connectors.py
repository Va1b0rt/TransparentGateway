import asyncio
import json
import sys
from pathlib import Path

CONNECTOR_DIR = str(Path(__file__).parents[1] / "validator")
sys.path.insert(0, CONNECTOR_DIR)
from sources import CONNECTOR_REGISTRY, JsonlInventoryConnector, collect_all, load_connectors


def test_jsonl_connector_normalises_candidates_and_keeps_secret_reference(tmp_path):
    source = tmp_path / "upstreams.jsonl"
    source.write_text("\n".join([
        '{"endpoint":"proxy.example:3128","protocol":"http","credential_ref":"billing-a"}',
        '{"endpoint":"proxy.example:3128","protocol":"http","credential_ref":"billing-a"}',
        'not json',
    ]))
    candidates = asyncio.run(collect_all([JsonlInventoryConnector("provider-a", str(source))]))
    assert len(candidates) == 1
    assert candidates[0].address == "http://proxy.example:3128"
    assert candidates[0].host == "proxy.example"
    assert candidates[0].port == 3128
    assert candidates[0].credential_ref == "billing-a"
    assert candidates[0].source == "provider-a"


def test_loader_uses_allowed_types_and_active_connectors(tmp_path):
    inventory = tmp_path / "upstreams.jsonl"
    inventory.write_text('{"host":"2001:db8::1","port":1080,"protocol":"socks5"}\n')
    config = tmp_path / "connectors.json"
    config.write_text(json.dumps({
        "allowed_connector_types": ["jsonl_inventory"],
        "active_connectors": [{
            "type": "jsonl_inventory",
            "name": "socks-inventory",
            "path": str(inventory),
        }],
    }))

    connectors = load_connectors(str(config), CONNECTOR_REGISTRY)
    candidates = asyncio.run(collect_all(connectors))

    assert len(connectors) == 1
    assert candidates[0].endpoint == "[2001:db8::1]:1080"
    assert candidates[0].protocol == "socks5"


def test_loader_rejects_active_connector_that_is_not_allowed(tmp_path):
    config = tmp_path / "connectors.json"
    config.write_text(json.dumps({
        "allowed_connector_types": [],
        "active_connectors": [{
            "type": "jsonl_inventory",
            "name": "unexpected",
            "path": "/inventory/upstreams.jsonl",
        }],
    }))

    try:
        load_connectors(str(config), CONNECTOR_REGISTRY)
    except ValueError as error:
        assert "not allowed" in str(error)
    else:
        raise AssertionError("an active but disallowed connector was accepted")
