import hashlib
import os
import sys
from pathlib import Path

VALIDATOR_DIR = str(Path(__file__).parents[1] / "validator")
sys.path.insert(0, VALIDATOR_DIR)

from asn_resolver import AsnDatabaseError, AsnResolver
from asn_updater import update_once


class FakeReader:
    def __init__(self, values):
        self.values = values
        self.closed = False

    def get_with_prefix_len(self, address):
        return self.values.get(address, (None, 24))

    def close(self):
        self.closed = True


def test_asn_resolver_enriches_observed_exit_ip(tmp_path):
    database = tmp_path / "origin-asn.mmdb"
    database.write_bytes(b"fixture")
    reader = FakeReader({
        "203.0.113.8": ({
            "autonomous_system_number": 64500,
            "autonomous_system_organization": "Example Network",
        }, 24),
    })

    with AsnResolver(
        str(database),
        60,
        reader_factory=lambda _path: reader,
    ) as resolver:
        result = resolver.resolve("203.0.113.8")

    assert result == {
        "asn_status": "verified",
        "asn": "AS64500",
        "organization": "Example Network",
        "prefix": "203.0.113.0/24",
    }
    assert reader.closed


def test_asn_resolver_marks_missing_address_unknown(tmp_path):
    database = tmp_path / "origin-asn.mmdb"
    database.write_bytes(b"fixture")
    with AsnResolver(
        str(database),
        60,
        reader_factory=lambda _path: FakeReader({}),
    ) as resolver:
        result = resolver.resolve("192.0.2.1")

    assert result["asn_status"] == "unknown"
    assert result["asn"] is None


def test_asn_resolver_rejects_stale_database(tmp_path):
    database = tmp_path / "origin-asn.mmdb"
    database.write_bytes(b"fixture")
    os.utime(database, (1, 1))

    try:
        with AsnResolver(str(database), 60, reader_factory=lambda _path: FakeReader({})):
            pass
    except AsnDatabaseError as error:
        assert "stale" in str(error)
    else:
        raise AssertionError("stale ASN database was accepted")


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.body[:limit]


def test_asn_updater_verifies_checksum_and_replaces_atomically(tmp_path):
    body = b"valid-mmdb-content"
    checksum = hashlib.sha256(body).hexdigest().encode() + b"  origin-asn.mmdb\n"

    def opener(request, timeout):
        assert timeout == 60
        return Response(checksum if request.full_url.endswith(".sha256") else body)

    destination = tmp_path / "asn" / "origin-asn.mmdb"
    changed = update_once(
        destination,
        "https://example.test/origin-asn.mmdb",
        "https://example.test/origin-asn.mmdb.sha256",
        opener=opener,
    )

    assert changed
    assert destination.read_bytes() == body
    assert not list(destination.parent.glob("origin-asn.mmdb.*"))
