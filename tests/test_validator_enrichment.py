import asyncio
import json
import sys
from pathlib import Path

VALIDATOR_DIR = str(Path(__file__).parents[1] / "validator")
ROOT = str(Path(__file__).parents[1])
sys.path[:0] = [VALIDATOR_DIR, ROOT]

import validator
from proxy_probe import CheckTarget, EchoObservation
from sources import ProxyCandidate


def test_validator_enriches_actual_exit_ip_and_does_not_serialize_credentials(monkeypatch):
    seen = {}

    async def observation(candidate, target, timeout, auth):
        seen["auth"] = auth
        return EchoObservation("203.0.113.8", {}), 25

    class Credentials:
        def get(self, reference):
            assert reference == "provider-a"
            return "secret-user", "secret-password"

    class Resolver:
        def resolve(self, address):
            seen["exit_ip"] = address
            return {
                "asn_status": "verified",
                "asn": "AS64500",
                "organization": "Example Network",
                "prefix": "203.0.113.0/24",
            }

    monkeypatch.setattr(validator, "proxy_observation", observation)
    candidate = ProxyCandidate(
        "proxy.example",
        1080,
        "socks5",
        "test",
        "provider-a",
        {"asn": "AS-SPOOFED", "country": "UA"},
    )
    result = asyncio.run(validator.probe(
        candidate,
        CheckTarget.parse("https://echo.example/"),
        {"192.0.2.10"},
        2,
        Credentials(),
        Resolver(),
    ))

    assert result is not None
    assert seen == {
        "auth": ("secret-user", "secret-password"),
        "exit_ip": "203.0.113.8",
    }
    assert result["exit_ip"] == "203.0.113.8"
    assert result["network"]["asn"] == "AS64500"
    assert result["metadata"]["source_asn"] == "AS-SPOOFED"
    encoded = json.dumps(result)
    assert "secret-user" not in encoded
    assert "secret-password" not in encoded
