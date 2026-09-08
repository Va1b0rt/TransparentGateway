"""Pure readiness checks: freshness and validator lineage cannot be refreshed by ranking."""
import ipaddress


def assess(snapshots, last_success, now, limits):
    for name in ['raw','validated','reserve','active']:
        snapshot=snapshots[name]
        if not snapshot.get('entries') or not -5 <= now-snapshot['generated_at'] <= limits[name]:
            raise ValueError('empty or stale pool: '+name)
    if not last_success or last_success.get('healthy',0)<1 or not -5 <= now-last_success['completed_at']<=limits['validated']:
        raise ValueError('last successful validator cycle missing or stale')
    for name in ['validated','reserve','active']:
        snapshot=snapshots[name]
        cycle=snapshot.get('validator_cycle')
        if not cycle or not cycle.get('id') or cycle.get('healthy',0)<1 or not -5<=now-cycle['completed_at']<=limits['validated']:
            raise ValueError('validator cycle missing or stale: '+name)
        raw_time=snapshot['source_generated_at'] if name=='validated' else snapshot.get('raw_source_generated_at')
        if raw_time is None or not -5<=now-raw_time<=limits['raw']+limits['validated']:
            raise ValueError('raw lineage stale: '+name)
        if name!='validated' and (snapshot.get('source_generated_at') is None or not -5<=now-snapshot['source_generated_at']<=limits['validated']):
            raise ValueError('validated lineage stale: '+name)
    # Different adjacent cycles are allowed during pipeline publication; freshness and
    # usable identity membership still must hold. A recently reranked old cycle fails.
    def identities(name):
        return {(e['address'],e.get('credential_ref'),e.get('exit_ip')) for e in snapshots[name]['entries']}
    if not identities('active') <= identities('reserve') or not identities('active') <= identities('validated'):
        raise ValueError('active upstream not present in validated/reserve pools')
    for entry in snapshots['active']['entries']:
        ipaddress.ip_address(entry['exit_ip'])
        if entry.get('protocol') not in ['http','https','socks4','socks5'] or entry.get('success_streak',0)<1:
            raise ValueError('active upstream is not validated usable')
    return {'last_success_age_seconds':round(now-last_success['completed_at']),
            'active_cycle_age_seconds':round(now-snapshots['active']['validator_cycle']['completed_at'])}
