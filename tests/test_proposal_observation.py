"""Future observation integration with temporary synthetic ledgers only."""
from copy import deepcopy
import json
import pytest
from app.config import DEFAULTS
from app.storage import Database
from app.market import Stock
from app.proposal_observation import compare_and_observe
from app.proposal_report import report
from app.shadow import ShadowEngine
from app.strategy import evaluate
from test_condition_report import add


def rig(tmp_path):
    db=Database(tmp_path/'practice.db');cfg=deepcopy(DEFAULTS)
    add(db,cfg);cfg['min_value_eok']=999
    return db,cfg


def test_comparison_registers_fixed_context_once_and_never_applies(tmp_path):
    db,cfg=rig(tmp_path);before=dict(cfg)
    cfg['private_marker']='PRIVATE_MARKER'
    text=compare_and_observe(db,cfg)
    engine=ShadowEngine(db);rows=engine.strategies()
    assert len(rows)==1 and rows[0]['origin']=='PROPOSAL'
    fixed=json.loads(rows[0]['strategy_json'])
    assert fixed['min_value_eok']==100 and 'PRIVATE_MARKER' not in rows[0]['strategy_json']
    assert db.proposals()[0]['state']=='PENDING' and cfg['min_value_eok']==before['min_value_eok']
    compare_and_observe(db,cfg)
    assert len(engine.strategies())==1 and '1개' in text
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM shadow_trades').fetchone()[0]==0
    text=report(db,'DEMO')
    assert '새 조회 0회' in text and '검증 완료가 아니에요' in text


def test_unknown_stale_and_other_mode_candidates_are_not_mixed(tmp_path):
    db,cfg=rig(tmp_path)
    db.propose('legacy','legacy',{'min_value_eok':100})
    compare_and_observe(db,cfg)
    cfg['min_value_eok']=888
    compare_and_observe(db,cfg)
    rows=ShadowEngine(db).strategies()
    assert len(rows)==2 and {r['source_proposal_id'] for r in rows}=={2,3}
    assert all(json.loads(r['strategy_json'])['min_value_eok']==100 for r in rows)
    assert '새 조회' not in report(db,'MOCK')


def test_old_unprocessed_scan_cannot_become_new_observation(tmp_path):
    db,cfg=rig(tmp_path)
    stock=Stock('900001','합성 예제','KOSPI',10000,5,500,5000,200,source='DEMO')
    signal_cfg=dict(cfg,min_value_eok=100)
    old=db.save_scan([stock],evaluate([stock],signal_cfg),signal_cfg,'전체','DEMO')
    compare_and_observe(db,cfg);engine=ShadowEngine(db)
    assert engine.process_scan(old,[stock],today='2026-10-19')==[]
    with db.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM shadow_signals').fetchone()[0]==0
    new=db.save_scan([stock],evaluate([stock],signal_cfg),signal_cfg,'전체','DEMO')
    engine.process_scan(new,[stock],today='2026-10-20')
    assert '새 조회 1회' in report(db,'DEMO')
    compare_and_observe(db,cfg)
    with db.connect() as conn:
        assert conn.execute('SELECT after_scan_id FROM shadow_observation_start').fetchone()[0]==old
    assert len(engine.strategies())==1


def test_changed_existing_variant_is_not_silently_reused(tmp_path):
    db,cfg=rig(tmp_path);compare_and_observe(db,cfg)
    with db.connect() as conn:conn.execute("UPDATE shadow_strategies SET strategy_json='{}'")
    with pytest.raises(ValueError,match='고정 조건'):compare_and_observe(db,cfg)


def test_legacy_experiment_is_preserved_without_future_evidence_label(tmp_path):
    db,cfg=rig(tmp_path);compare_and_observe(db,cfg)
    with db.connect() as conn:
        conn.execute('DELETE FROM shadow_observation_start')
        conn.execute("UPDATE shadow_strategies SET strategy_json='{}'")
    assert '0개' in compare_and_observe(db,cfg)
    assert '별도 가상 관측 연결은 미확인' in report(db,'DEMO')
    assert ShadowEngine(db).strategies()[0]['strategy_json']=='{}'
