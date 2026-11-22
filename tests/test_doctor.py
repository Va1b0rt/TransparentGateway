import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('tg_doctor', ROOT / 'scripts/doctor.py')
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


class UserError(Exception):
    pass


@pytest.fixture
def lab(monkeypatch):
    monkeypatch.setattr(doctor.time, 'time', lambda: 1000)
    cycle = {'id': 'test', 'completed_at': 990, 'healthy': 1}
    entry = {'address': '192.0.2.1:3128', 'protocol': 'http', 'exit_ip': '198.51.100.1', 'success_streak': 1}
    pools = {kind: {'generated_at': 995, 'source_generated_at': 990, 'raw_source_generated_at': 985,
                   'validator_cycle': cycle.copy(), 'entries': [entry.copy()]}
             for kind in ('raw', 'validated', 'reserve', 'active')}
    data = {'pools': pools, 'cycle': cycle, 'stopped': set(), 'calls': [], 'probe': 'MATCH', 'docker': True}
    values = {'FETCH_INTERVAL_SECONDS': '20', 'VALIDATION_INTERVAL_SECONDS': '20',
              'RANK_INTERVAL_SECONDS': '20', 'ANONYMITY_CHECK_URL': 'https://echo.example/'}

    def run(command, **kwargs):
        data['calls'].append(command)
        if command[:2] == ['docker', 'info']:
            if not data['docker']:
                raise RuntimeError('SECRET http://user:password@private.example')
            output = '28'
        elif command[:3] == ['docker', 'compose', 'config']:
            output = ''
        elif command[:3] == ['docker', 'compose', 'ps']:
            output = json.dumps([{'Service': name, 'State': 'exited' if name in data['stopped'] else 'running'}
                                 for name in ('redis', 'fetcher', 'validator', 'ranker', 'asn-updater', 'gateway')])
        elif command[:3] == ['docker', 'compose', 'exec']:
            key = command[-1]
            if key.startswith('proxy_pool:'):
                output = json.dumps(pools[key.split(':')[1]])
            elif key == 'validator:last_success:v1':
                output = json.dumps(data['cycle'])
            else:
                output = json.dumps(pools['active'])
        else:
            assert command[0] == sys.executable
            if isinstance(data['probe'], Exception):
                raise data['probe']
            output = data['probe']
        return subprocess.CompletedProcess(command, 0, output, '')

    ctl = SimpleNamespace(ROOT=ROOT, UserError=UserError, run=run,
                          load_and_validate=lambda **kwargs: (values, [], {}))
    return ctl, data


def rows(lab):
    return {r[0]: r[1:] for r in doctor.diagnose(lab[0])}


def test_healthy_host_is_not_client_proof(lab):
    result = rows(lab)
    assert result['relay.echo'][0] == 'PASS'
    assert result['client'][0] == 'SKIP'
    assert not any(row[0] == 'FAIL' for row in result.values())
    rendered = str(result)
    assert '198.51.100.1' not in rendered and '192.0.2.1' not in rendered
    assert all(not any(token in c for token in ('up', 'down', 'restart', 'SET', 'DEL')) for c in lab[1]['calls'])


def test_stopped_fetcher_does_not_hide_readable_pools(lab):
    lab[1]['stopped'].add('fetcher')
    result = rows(lab)
    assert result['service.fetcher'][0] == 'FAIL'
    assert 'state=exited' in result['service.fetcher'][1]
    assert result['pool.raw'][0] == 'PASS'
    assert 'logs --tail 50 fetcher' in result['service.fetcher'][2]


@pytest.mark.parametrize('kind', ['raw', 'validated', 'reserve', 'active'])
@pytest.mark.parametrize('fault', ['empty', 'stale', 'malformed'])
def test_pool_fault_is_localized_and_blocks_probe(lab, kind, fault):
    pool = lab[1]['pools'][kind]
    if fault == 'empty': pool['entries'] = []
    elif fault == 'stale': pool['generated_at'] = 800
    else: pool['generated_at'] = 'SECRET'
    result = rows(lab)
    assert result['pool.' + kind][0] == 'FAIL'
    assert result['relay.echo'][0] == 'SKIP'
    assert 'SECRET' not in str(result)


def test_stale_lineage_cannot_be_hidden_by_fresh_ranking(lab):
    lab[1]['pools']['active']['validator_cycle']['completed_at'] = 800
    result = rows(lab)
    assert result['pool.active'][0] == 'PASS'
    assert result['pool.lineage'][0] == 'FAIL'


def test_docker_failure_is_redacted_and_dependencies_skipped(lab):
    lab[1]['docker'] = False
    result = rows(lab)
    assert result['docker'][0] == 'FAIL'
    assert result['pool.active'][0] == 'SKIP'
    assert 'SECRET' not in str(result) and 'password' not in str(result)


@pytest.mark.parametrize('verdict', ['TIMEOUT', 'MISMATCH', 'TLS_FAILED', 'RELAY_REFUSED', 'REQUEST_FAILED', 'SECRET'])
def test_relay_failure_and_untrusted_output(lab, verdict):
    lab[1]['probe'] = verdict
    result = rows(lab)
    assert result['relay.echo'][0] == 'FAIL'
    assert 'SECRET' not in str(result)


def test_bad_config_does_not_prevent_docker_check(lab):
    def broken(**kwargs): raise ValueError('password=SECRET')
    lab[0].load_and_validate = broken
    result = rows(lab)
    assert result['config'][0] == 'FAIL'
    assert result['docker'][0] == 'PASS'
    assert 'SECRET' not in str(result)


def test_total_deadline_stops_external_calls(lab, monkeypatch):
    ticks = iter([0, 61])
    monkeypatch.setattr(doctor.time, 'monotonic', lambda: next(ticks, 61))
    result = rows(lab)
    assert result['docker'][0] == 'FAIL'
    assert lab[1]['calls'] == []


def test_probe_process_timeout_is_a_failure(lab):
    lab[1]['probe'] = subprocess.TimeoutExpired('private-command', 1, output='SECRET')
    result = rows(lab)
    assert result['relay.echo'][0] == 'FAIL'
    assert 'SECRET' not in str(result)


def test_cli_failure_exit_and_no_raw_exception(lab, capsys):
    lab[0].require_root = lambda: None
    lab[1]['docker'] = False
    with pytest.raises(SystemExit) as error:
        doctor.main(lab[0], SimpleNamespace(timeout=60))
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert 'DOCTOR FAIL scope=host client=NOT_TESTED' in output
    assert 'SECRET' not in output
