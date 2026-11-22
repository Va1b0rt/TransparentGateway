"""Read-only host diagnostics. Never render subprocess output or exception values."""
import json
import math
import subprocess
import sys
import time


def diagnose(ctl, timeout=60):
    sys.path.insert(0, str(ctl.ROOT / 'deploy'))
    from readiness import SERVICES
    from pool_health import assess
    deadline = time.monotonic() + timeout
    results = []

    def add(name, state, detail, action=''):
        results.append((name, state, detail, action))

    def query(command, input_text=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        result = ctl.run(command, timeout=min(15, remaining), input_text=input_text)
        if result.returncode:
            raise RuntimeError
        return result.stdout

    def logs(service):
        return 'sudo docker compose logs --tail 50 ' + service

    values = None
    try:
        # Configuration checks are local; Docker is diagnosed independently below.
        values, _, _ = ctl.load_and_validate(check_compose=False)
        limits = {kind: max(60, 3 * int(values[key])) for kind, key in (
            ('raw', 'FETCH_INTERVAL_SECONDS'), ('validated', 'VALIDATION_INTERVAL_SECONDS'),
            ('reserve', 'RANK_INTERVAL_SECONDS'), ('active', 'RANK_INTERVAL_SECONDS'))}
        if any(int(values[key]) <= 0 for key in ('FETCH_INTERVAL_SECONDS', 'VALIDATION_INTERVAL_SECONDS', 'RANK_INTERVAL_SECONDS')):
            raise ValueError
        add('config', 'PASS', 'runtime files and permissions valid')
    except ctl.UserError as error:
        values = None
        # UserError in the local validator contains only authored diagnostics,
        # fixed file/key names, and line numbers; never subprocess stderr.
        add("config", "FAIL", str(error), "review the named runtime file locally")
    except Exception:
        values = None
        add('config', 'FAIL', 'runtime configuration invalid; values hidden',
            'sudo python3 scripts/tgctl.py check (inspect locally; do not share raw errors)')

    docker = False
    try:
        query(['docker', 'info', '--format', '{{.ServerVersion}}'])
        docker = True
        add('docker', 'PASS', 'daemon responds')
    except Exception:
        add('docker', 'FAIL', 'daemon unavailable or diagnostic deadline reached', 'sudo systemctl status docker --no-pager')

    compose = False
    if docker and values is not None:
        try:
            query(['docker', 'compose', 'config', '--quiet'])
            compose = True
            add('compose', 'PASS', 'configuration accepted')
        except Exception:
            add('compose', 'FAIL', 'Compose unavailable or configuration rejected', 'sudo docker compose config --quiet')
    else:
        add('compose', 'SKIP', 'requires valid config and Docker')

    running = set()
    if compose:
        try:
            rows = [json.loads(line) for line in query(['docker', 'compose', 'ps', '--all', '--format', 'json']).splitlines() if line]
            if len(rows) == 1 and isinstance(rows[0], list):
                rows = rows[0]
            for service in sorted(SERVICES):
                matches = [row for row in rows if row.get('Service') == service]
                healthy = len(matches) == 1 and matches[0].get('State') == 'running' and matches[0].get('Health', '') in ('', 'healthy')
                if healthy:
                    running.add(service)
                add('service.' + service, 'PASS' if healthy else 'FAIL',
                    'running' if healthy else 'missing, stopped, or unhealthy', '' if healthy else logs(service))
        except Exception:
            add('services', 'FAIL', 'could not read service status', 'sudo docker compose ps --all')
    else:
        add('services', 'SKIP', 'requires accepted Compose configuration')

    snapshots = {}
    now = time.time()
    owners = {'raw': 'fetcher', 'validated': 'validator', 'reserve': 'ranker', 'active': 'ranker'}

    def redis(key):
        return json.loads(query(['docker', 'compose', 'exec', '-T', 'redis', 'redis-cli', '--raw', 'GET', key]))

    good_pools = True
    for kind in owners:
        if 'redis' not in running:
            add('pool.' + kind, 'SKIP', 'Redis is not available')
            good_pools = False
            continue
        try:
            snapshot = redis('proxy_pool:' + kind + ':v1')
            if not isinstance(snapshot, dict) or not isinstance(snapshot.get('entries'), list):
                raise ValueError
            stamp = snapshot['generated_at']
            if isinstance(stamp, bool) or not isinstance(stamp, (float, int)) or not math.isfinite(stamp):
                raise ValueError
            snapshots[kind] = snapshot
            count = len(snapshot['entries'])
            age = now - stamp
            valid = count > 0 and -5 <= age <= limits[kind]
            detail = f'entries={count} age={round(age)}s limit={limits[kind]}s'
            if count == 0:
                detail += '; empty pool'
            if not -5 <= age <= limits[kind]:
                detail += '; stale or future timestamp'
            add('pool.' + kind, 'PASS' if valid else 'FAIL', detail, '' if valid else logs(owners[kind]))
            good_pools &= valid
        except Exception:
            good_pools = False
            add('pool.' + kind, 'FAIL', 'snapshot missing, malformed, or unreadable', logs(owners[kind]))

    last = None
    validator_ok = False
    if 'redis' in running:
        try:
            last = redis('validator:last_success:v1')
            age = now - last['completed_at']
            validator_ok = last['healthy'] > 0 and -5 <= age <= limits['validated']
            add('validator.cycle', 'PASS' if validator_ok else 'FAIL',
                'recent successful cycle' if validator_ok else 'successful cycle stale or has no healthy upstream',
                '' if validator_ok else logs('validator'))
        except Exception:
            add('validator.cycle', 'FAIL', 'successful cycle missing or unreadable', logs('validator'))
    else:
        add('validator.cycle', 'SKIP', 'Redis is not available')

    lineage = False
    if good_pools and validator_ok:
        try:
            assess(snapshots, last, now, limits)
            compatibility = redis('transparent-gateway:validated:v1')
            if [e['address'] for e in compatibility['entries']] != [e['address'] for e in snapshots['active']['entries']]:
                raise ValueError
            lineage = True
            add('pool.lineage', 'PASS', 'fresh validator lineage and active membership agree')
        except Exception:
            add('pool.lineage', 'FAIL', 'lineage or active membership invalid; publication may be in progress', logs('ranker') + '; repeat doctor')
    else:
        add('pool.lineage', 'SKIP', 'requires fresh nonempty pools and successful validator cycle')

    if lineage and 'gateway' in running:
        # Child deadline bounds all socket reads, including slow/dripping responses.
        probe = '''import json,sys,socket,ssl
sys.path.insert(0,sys.argv[1])
from readiness import relay_echo
v=json.load(sys.stdin)
try:
 e=relay_echo(v['url'])
 print('MATCH' if e.get('backend_peer') in v['exits'] else 'MISMATCH')
except ssl.SSLError: print('TLS_FAILED')
except (socket.timeout,TimeoutError): print('TIMEOUT')
except ConnectionRefusedError: print('RELAY_REFUSED')
except Exception: print('REQUEST_FAILED')
'''
        explanations = {'MATCH': 'fresh echo request matches active exit; addresses hidden',
                        'MISMATCH': 'echo exit does not match active pool', 'TLS_FAILED': 'TLS verification or handshake failed',
                        'TIMEOUT': 'relay/upstream request timed out', 'RELAY_REFUSED': 'local relay refused connection',
                        'REQUEST_FAILED': 'CONNECT or echo response failed validation'}
        try:
            verdict = query([sys.executable, '-c', probe, str(ctl.ROOT / 'deploy')], json.dumps({
                'url': values['ANONYMITY_CHECK_URL'], 'exits': [e['exit_ip'] for e in snapshots['active']['entries']]})).strip()
            detail = explanations.get(verdict, 'unexpected probe result; output hidden')
            if verdict == 'MATCH' and values['ANONYMITY_CHECK_URL'].startswith('http:'):
                add('relay.echo', 'WARN', detail + '; HTTP echo has no TLS verification')
            else:
                add('relay.echo', 'PASS' if verdict == 'MATCH' else 'FAIL', detail, '' if verdict == 'MATCH' else logs('gateway'))
        except (subprocess.TimeoutExpired, TimeoutError):
            add('relay.echo', 'FAIL', 'request timed out or diagnostic deadline reached', logs('gateway'))
        except Exception:
            add('relay.echo', 'FAIL', 'could not execute relay probe', logs('gateway'))
    else:
        add('relay.echo', 'SKIP', 'requires usable active pool and running gateway')
    add('client', 'SKIP', 'client route, DNS and HTTPS not tested from this host',
        'on the client: python3 scripts/client-check.py --help')
    return results


def main(ctl, args):
    ctl.require_root()
    if not 1 <= args.timeout <= 300:
        raise ctl.UserError('doctor timeout must be between 1 and 300 seconds')
    results = diagnose(ctl, args.timeout)
    for name, state, detail, action in results:
        print(f'{state:4} {name}: {detail}')
        if action:
            print('     next: ' + action)
    failed = any(row[1] == 'FAIL' for row in results)
    warned = any(row[1] == 'WARN' for row in results)
    print('DOCTOR ' + ('FAIL' if failed else 'WARN' if warned else 'PASS') + ' scope=host client=NOT_TESTED')
    if failed:
        raise SystemExit(1)
