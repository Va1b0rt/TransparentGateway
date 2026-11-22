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


import json
import pytest
import asn_updater as updater


def official_opener(body, *, checksum=None, change=None, api_error=None):
    digest = hashlib.sha256(body).hexdigest()
    asset = {'id': 7, 'name': 'origin-asn.mmdb', 'state': 'uploaded', 'size': len(body),
             'browser_download_url': updater.DEFAULT_DATABASE_URL, 'digest': 'sha256:' + digest}
    if change: asset.update(change)
    calls = []
    def opener(request, timeout):
        calls.append(request.full_url)
        if request.full_url == updater.GITHUB_RELEASE_API:
            if api_error: raise api_error
            return Response(json.dumps({'assets': [asset]}).encode())
        if request.full_url == updater.DEFAULT_CHECKSUM_URL:
            return Response(checksum if checksum is not None else b'0' * 64)
        assert request.full_url == updater.DEFAULT_DATABASE_URL
        return Response(body)
    return opener, calls


def test_staggered_release_uses_independent_asset_digest(tmp_path):
    opener, calls = official_opener(b'new database')
    destination = tmp_path / 'db'
    assert updater.update_once(destination, updater.DEFAULT_DATABASE_URL, updater.DEFAULT_CHECKSUM_URL, opener=opener)
    assert destination.read_bytes() == b'new database'
    assert updater.GITHUB_RELEASE_API in calls


@pytest.mark.parametrize('checksum', [b'', b'invalid', b'0' * 64])
def test_missing_or_stale_checksum_can_use_official_metadata(tmp_path, checksum):
    opener, _ = official_opener(b'new database', checksum=checksum)
    assert updater.update_once(tmp_path / 'db', updater.DEFAULT_DATABASE_URL, updater.DEFAULT_CHECKSUM_URL, opener=opener)


@pytest.mark.parametrize('change', [
    {'digest': None}, {'digest': 'sha256:' + '1' * 64}, {'digest': 'md5:abc'},
    {'browser_download_url': 'https://other.example/db'}, {'state': 'new'},
    {'size': 999}, {'id': True}, {'name': 'wrong.mmdb'},
])
def test_untrusted_or_mismatched_metadata_preserves_old_database(tmp_path, change):
    destination = tmp_path / 'db'; destination.write_bytes(b'old')
    os.utime(destination, (100, 100))
    opener, _ = official_opener(b'new database', change=change)
    with pytest.raises(ValueError):
        updater.update_once(destination, updater.DEFAULT_DATABASE_URL, updater.DEFAULT_CHECKSUM_URL, opener=opener)
    assert destination.read_bytes() == b'old' and destination.stat().st_mtime == 100
    assert list(tmp_path.iterdir()) == [destination]


def test_api_failure_does_not_refresh_stale_database(tmp_path):
    destination = tmp_path / 'db'; destination.write_bytes(b'old'); os.utime(destination, (100, 100))
    opener, _ = official_opener(b'new', api_error=OSError('private URL'))
    with pytest.raises(OSError):
        updater.update_once(destination, updater.DEFAULT_DATABASE_URL, updater.DEFAULT_CHECKSUM_URL, opener=opener)
    assert destination.stat().st_mtime == 100 and destination.read_bytes() == b'old'


def test_custom_checksum_is_not_overridden_by_github(tmp_path):
    calls = []
    def opener(request, timeout):
        calls.append(request.full_url)
        return Response(b'0' * 64 if request.full_url.endswith('.sha256') else b'new')
    with pytest.raises(ValueError):
        updater.update_once(tmp_path / 'db', updater.DEFAULT_DATABASE_URL, 'https://custom.example/db.sha256', opener=opener)
    assert updater.GITHUB_RELEASE_API not in calls
    assert not (tmp_path / 'db').exists()


def test_matching_checksum_needs_no_api(tmp_path):
    opener, calls = official_opener(b'new', checksum=hashlib.sha256(b'new').hexdigest().encode())
    updater.update_once(tmp_path / 'db', updater.DEFAULT_DATABASE_URL, updater.DEFAULT_CHECKSUM_URL, opener=opener)
    assert updater.GITHUB_RELEASE_API not in calls


def test_failure_retry_and_reset_hide_sensitive_errors(monkeypatch, capsys):
    import asyncio
    outcomes = [OSError('password=secret'), ValueError('token=secret'), True, OSError('secret')]
    sleeps = []
    def update(*args):
        result = outcomes.pop(0)
        if isinstance(result, Exception): raise result
        return result
    async def sleep(delay):
        sleeps.append(delay)
        if not outcomes: raise asyncio.CancelledError
    async def to_thread(function, *args):
        return function(*args)
    monkeypatch.setattr(updater.asyncio, 'to_thread', to_thread, raising=False)
    monkeypatch.setattr(updater, 'update_once', update)
    monkeypatch.setattr(updater.asyncio, 'sleep', sleep)
    monkeypatch.setenv('ASN_UPDATE_INTERVAL_SECONDS', '86400')
    with pytest.raises(asyncio.CancelledError): asyncio.run(updater.main())
    assert sleeps == [300, 600, 86400, 300]
    assert 'secret' not in capsys.readouterr().out
