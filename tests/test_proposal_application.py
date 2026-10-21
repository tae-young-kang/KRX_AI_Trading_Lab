"""Settings/proposal multi-resource outcomes over temporary data only."""
import json
import pytest
from app.config import Settings
from app.storage import Database
from app.strategy import create_proposals
from app.proposal_application import apply,recover
from test_condition_report import add


def rig(tmp_path):
    settings=Settings(tmp_path/'settings');db=Database(tmp_path/'practice.db')
    add(db,settings.values)
    settings.values['min_value_eok']=999
    settings.values['unexpected_setting']='DO_NOT_PERSIST'
    settings.save();create_proposals(db,settings.values)
    return db,settings,db.proposals()[0]['id']


def status(db):
    with db.connect() as conn:
        return conn.execute('SELECT state FROM proposals ORDER BY id DESC LIMIT 1').fetchone()[0]


def test_user_confirmed_application_checks_saved_result_and_does_not_persist_private_fields(tmp_path):
    db,settings,pid=rig(tmp_path);before=dict(settings.values)
    target=apply(db,settings.values,pid,settings.save,settings.strategy_snapshot)
    assert target['min_value_eok']==100 and settings.values['strategy_version']==2
    assert status(db)=='APPROVED' and settings.strategy_snapshot()==target
    assert settings.values['unexpected_setting']=='DO_NOT_PERSIST'
    with db.connect() as conn:
        row=conn.execute('SELECT * FROM proposal_applications').fetchone()
    assert row['state']=='APPLIED' and 'DO_NOT_PERSIST' not in row['base_json']+row['target_json']
    assert all(settings.values[key]==value for key,value in before.items() if key not in ('min_value_eok','strategy_version'))
    with pytest.raises(ValueError):apply(db,settings.values,pid,settings.save,settings.strategy_snapshot)


@pytest.mark.parametrize('when',['before_write','after_write'])
def test_save_exception_uses_actual_saved_readback_instead_of_false_approval(tmp_path,when):
    db,settings,pid=rig(tmp_path);before=dict(settings.values)
    def save():
        if when=='after_write':settings.save()
        raise OSError('PRIVATE_ERROR')
    if when=='before_write':
        with pytest.raises(ValueError,match='저장되지') as error:apply(db,settings.values,pid,save,settings.strategy_snapshot)
        assert 'PRIVATE_ERROR' not in str(error.value)
        assert settings.values==before and status(db)=='PENDING'
    else:
        apply(db,settings.values,pid,save,settings.strategy_snapshot)
        assert status(db)=='APPROVED' and settings.strategy_snapshot()['min_value_eok']==100


@pytest.mark.parametrize('when',['before_write','after_write'])
def test_process_interruption_reconciles_metadata_only_from_saved_settings(tmp_path,when):
    db,settings,pid=rig(tmp_path)
    def interrupted():
        if when=='after_write':settings.save()
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):apply(db,settings.values,pid,interrupted,settings.strategy_snapshot)
    assert status(db)=='APPLYING'
    reopened=Settings(settings.root);before=reopened.path.read_bytes()
    result=recover(db,reopened.strategy_snapshot)
    assert result['APPLIED' if when=='after_write' else 'FAILED']==1
    assert status(db)==('APPROVED' if when=='after_write' else 'PENDING')
    assert reopened.path.read_bytes()==before
    assert recover(db,reopened.strategy_snapshot)==dict(APPLIED=0,FAILED=0,UNKNOWN=0)


def test_stale_conditions_or_no_longer_positive_evidence_block_before_save(tmp_path):
    db,settings,pid=rig(tmp_path)
    settings.values['min_value_eok']=888;settings.save()
    with pytest.raises(ValueError,match='생성 당시'):apply(db,settings.values,pid,lambda:pytest.fail('save'),settings.strategy_snapshot)
    settings.values['min_value_eok']=999;settings.save()
    with db.connect() as conn:conn.execute('UPDATE paper_trades SET pnl=-100,pnl_pct=-1')
    with pytest.raises(ValueError,match='최신 기록'):apply(db,settings.values,pid,lambda:pytest.fail('save'),settings.strategy_snapshot)
    assert status(db)=='PENDING'


def test_neither_saved_base_nor_target_stays_unknown_and_blocks_reapplication(tmp_path):
    db,settings,pid=rig(tmp_path)
    def ambiguous():
        settings.values['min_value_eok']=777;settings.save()
    with pytest.raises(ValueError,match='미확인'):apply(db,settings.values,pid,ambiguous,settings.strategy_snapshot)
    assert status(db)=='UNKNOWN'
    reopened=Settings(settings.root)
    assert recover(db,reopened.strategy_snapshot)['UNKNOWN']==1
    with pytest.raises(ValueError,match='미확인'):apply(db,reopened.values,pid,reopened.save,reopened.strategy_snapshot)
    assert reopened.values['min_value_eok']==777


def test_unknown_patch_is_not_accepted(tmp_path):
    db,settings,pid=rig(tmp_path)
    with db.connect() as conn:conn.execute('UPDATE proposals SET patch_json=?',(json.dumps({'paper_fee_pct':99}),))
    with pytest.raises(ValueError,match='허용된'):apply(db,settings.values,pid,lambda:pytest.fail('save'),settings.strategy_snapshot)
    assert status(db)=='PENDING'


def test_tampered_recovery_target_is_not_promoted_even_when_saved_file_matches(tmp_path):
    db,settings,pid=rig(tmp_path)
    def interrupted():raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):apply(db,settings.values,pid,interrupted,settings.strategy_snapshot)
    settings.values['min_value_eok']=777;settings.save()
    with db.connect() as conn:
        target=json.loads(conn.execute('SELECT target_json FROM proposal_applications').fetchone()[0])
        target['min_value_eok']=777
        conn.execute('UPDATE proposal_applications SET target_json=?',(json.dumps(target),))
    assert recover(db,settings.strategy_snapshot)['UNKNOWN']==1
    assert status(db)=='APPLYING'


@pytest.mark.parametrize('fails',[False,True])
def test_pc_review_uses_verified_application_engine_without_gui(tmp_path,fails):
    from types import SimpleNamespace
    from test_recommendation_request import gui_method
    db,settings,pid=rig(tmp_path);warnings=[]
    if fails:
        def save():raise OSError('PRIVATE_ERROR')
        settings.save=save
    fake=SimpleNamespace(scanning=False,work=None,cfg=settings.values,db=db,settings=settings,
        proposal_table=SimpleNamespace(currentRow=lambda:0,item=lambda *a:SimpleNamespace(text=lambda:str(pid))),
        shadow=SimpleNamespace(ensure_active_reference=lambda *a:None),refresh_strategy_fields=lambda:None,
        refresh_shadow=lambda:None,refresh_proposals=lambda:None)
    messages=SimpleNamespace(StandardButton=SimpleNamespace(Yes=1),question=lambda *a:1,
        warning=lambda *a:warnings.append(a[-1]),information=lambda *a:None)
    gui_method('review_proposal',QMessageBox=messages,apply_proposal=apply)(fake,True)
    assert status(db)==('PENDING' if fails else 'APPROVED')
    assert bool(warnings)==fails and all('PRIVATE_ERROR' not in text for text in warnings)
    with db.connect() as conn:assert conn.execute('SELECT COUNT(*) FROM proposal_applications').fetchone()[0]==1
