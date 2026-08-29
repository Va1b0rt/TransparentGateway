import json
import sys
from pathlib import Path

ROOT = str(Path(__file__).parents[1])
sys.path.insert(0, ROOT)

from common.credentials import CredentialStore


def test_credential_store_resolves_reference_and_reloads_rotated_file(tmp_path):
    path = tmp_path / "proxy-credentials.json"
    path.write_text(json.dumps({"provider-a": {"username": "first", "password": "one"}}))
    now = [0.0]
    store = CredentialStore(str(path), clock=lambda: now[0])

    assert store.get("provider-a") == ("first", "one")

    path.write_text(json.dumps({"provider-a": {"username": "second", "password": "two"}}))
    now[0] = 1.0
    assert store.get("provider-a") == ("second", "two")


def test_missing_credential_reference_fails_closed(tmp_path):
    path = tmp_path / "proxy-credentials.json"
    path.write_text("{}")
    store = CredentialStore(str(path))

    try:
        store.get("missing")
    except ValueError as error:
        assert "unavailable" in str(error)
    else:
        raise AssertionError("missing credential reference was accepted")
