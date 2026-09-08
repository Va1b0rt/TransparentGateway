import copy
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'deploy'))
from pool_health import assess


def state():
    cycle={'id':'successful-test-cycle','completed_at':990,'healthy':1,'checked':1}
    entry={'address':'192.0.2.1:3128','protocol':'http','exit_ip':'198.51.100.1','success_streak':1}
    snapshots={name:{'generated_at':995,'entries':[entry.copy()],'validator_cycle':cycle.copy(),
                     'source_generated_at':990,'raw_source_generated_at':985} for name in ['raw','validated','reserve','active']}
    return snapshots,cycle,{name:60 for name in snapshots}


def test_fresh_success_is_ready():
    pools,cycle,limits=state()
    assert assess(pools,cycle,1000,limits)['last_success_age_seconds']==10


@pytest.mark.parametrize('kind',['raw','validated','reserve','active'])
def test_stale_or_empty_pool_fails(kind):
    pools,cycle,limits=state()
    pools[kind]['generated_at']=800
    with pytest.raises(ValueError): assess(pools,cycle,1000,limits)
    pools[kind]['generated_at']=995
    pools[kind]['entries']=[]
    with pytest.raises(ValueError): assess(pools,cycle,1000,limits)


def test_reranking_old_validator_does_not_make_it_ready():
    pools,cycle,limits=state()
    pools['active']['validator_cycle']['completed_at']=800
    with pytest.raises(ValueError,match='cycle'): assess(pools,cycle,1000,limits)
    pools,cycle,limits=state()
    pools['reserve']['source_generated_at']=800
    with pytest.raises(ValueError,match='lineage'): assess(pools,cycle,1000,limits)


def test_failed_success_marker_or_unvalidated_active_fails():
    pools,cycle,limits=state()
    with pytest.raises(ValueError,match='last successful'): assess(pools,None,1000,limits)
    pools['active']['entries'][0]['address']='different:3128'
    with pytest.raises(ValueError,match='not present'): assess(pools,cycle,1000,limits)
