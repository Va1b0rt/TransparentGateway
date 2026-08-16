import asyncio
import json
import sys
from pathlib import Path

CONNECTOR_DIR = str(Path(__file__).parents[1] / "validator")
sys.path.insert(0, CONNECTOR_DIR)
from connectors import JsonlInventoryConnector, collect_all


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
    assert candidates[0].credential_ref == "billing-a"
    assert candidates[0].source == "provider-a"
