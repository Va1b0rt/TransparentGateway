import importlib.util
from pathlib import Path
import sys
import urllib.request

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'validator'))
from sources.http_json_feed import HttpJsonFeedConnector, NoRedirect


def test_token_file_reloaded_and_used_only_in_header(tmp_path, monkeypatch):
    token = tmp_path/'token'
    seen = []
    class Response:
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, count): return b'[]'
    class Opener:
        def open(self, request, timeout):
            seen.append(request.get_header('Authorization'))
            assert request.full_url == 'https://example.test/feed'
            return Response()
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *args: Opener())
    connector = HttpJsonFeedConnector('test','https://example.test/feed',str(token))
    token.write_text('test-first-token\n')
    assert connector._fetch() == []
    temporary = tmp_path/'next'
    temporary.write_text('test-rotated-token\n')
    temporary.replace(token)
    assert connector._fetch() == []
    assert seen == ['Bearer test-first-token','Bearer test-rotated-token']
    token.write_text('invalid\nheader')
    with pytest.raises(ValueError, match='invalid'):
        connector._fetch()


def test_secret_urls_legacy_env_and_redirects_rejected():
    for url in ['https://user:pass@example.test/feed','https://example.test/feed?token=secret']:
        with pytest.raises(ValueError):
            HttpJsonFeedConnector('test', url)
    with pytest.raises(ValueError, match='migrate'):
        HttpJsonFeedConnector.from_config({'name':'test','url':'https://example.test/feed','token_env':'TOKEN'})
    with pytest.raises(ValueError, match='redirects'):
        NoRedirect().redirect_request(None,None,302,'Found',{},'https://elsewhere.test/')


def test_transfer_rejects_symlinks_and_insecure_files(tmp_path):
    import os
    if os.geteuid() != 0:
        pytest.skip('requires root ownership')
    sys.path.insert(0,str(ROOT/'deploy'))
    spec = importlib.util.spec_from_file_location('secret_transfer',ROOT/'deploy/secret-transfer.py')
    transfer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(transfer)
    tmp_path.chmod(0o700)
    for name in transfer.FILES:
        path=tmp_path/name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'{}')
        path.chmod(0o600)
    assert transfer.document(tmp_path)
    target=tmp_path/'provider-secrets/api-token'
    target.chmod(0o644)
    with pytest.raises(ValueError,match='permissions'):
        transfer.document(tmp_path)
    target.unlink()
    target.symlink_to(tmp_path/'.env')
    with pytest.raises(ValueError,match='regular'):
        transfer.document(tmp_path)
