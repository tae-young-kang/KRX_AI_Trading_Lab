"""Confirmed private proposal queue over temporary settings and ledgers."""
import json
import re
import pytest
from app.proposal_review_commands import ProposalReviewCommands
from app.condition_analysis_request import ConditionAnalysisRequest
from app.order_engine import connect
from test_proposal_application import rig as application_rig,status
from test_telegram_orders import sandbox,update


def rig(tmp_path):
    db,settings,pid=application_rig(tmp_path);clock=[1000.0]
    controller=ProposalReviewCommands(tmp_path/'commands.db',db,settings.values,clock=lambda:clock[0])
    return db,settings,pid,clock,controller


def send(controller,text,uid=1,scope='owner',session='s',now=1000):
    return controller.handle(text,scope=scope,session=session,uid=uid,now=now)


def token(reply):return re.search(r'/개선안확인 ([A-Za-z0-9_-]{16})',reply)[1]


def queue(controller,pid,action='승인'):
    preview=send(controller,f'/개선안{action} {pid}')
    assert f'개선안 {pid}' in preview and '/개선안확인 ' in preview
    send(controller,'/개선안확인 '+token(preview),uid=2)
    return preview


def test_confirmed_approval_waits_for_owner_then_saves_and_reports_once(tmp_path):
    db,settings,pid,clock,controller=rig(tmp_path);queue(controller,pid)
    assert status(db)=='PENDING' and settings.values['min_value_eok']==999
    assert not controller.sync(settings.values,settings.save,settings.strategy_snapshot,now=1001,busy=True)
    assert controller.sync(settings.values,settings.save,settings.strategy_snapshot,now=1002)
    assert status(db)=='APPROVED' and settings.strategy_snapshot()['min_value_eok']==100
    assert '적용 확인 완료' in send(controller,'/개선안처리상태',uid=3)
    assert not controller.sync(settings.values,lambda:pytest.fail('repeat save'),settings.strategy_snapshot,now=1003)


def test_rejection_uses_confirmation_without_changing_settings(tmp_path):
    db,settings,pid,clock,controller=rig(tmp_path);before=settings.path.read_bytes()
    queue(controller,pid,'거부')
    assert not controller.sync(settings.values,lambda:pytest.fail('save'),settings.strategy_snapshot,now=1002)
    assert status(db)=='REJECTED' and settings.path.read_bytes()==before
    assert '거부 완료' in send(controller,'/개선안처리상태',uid=3)


@pytest.mark.parametrize('damage',['base','patch','target','performance'])
def test_changed_basis_proposal_or_evidence_is_not_applied(tmp_path,damage):
    db,settings,pid,clock,controller=rig(tmp_path);queue(controller,pid)
    if damage=='base':settings.values['min_value_eok']=888;settings.save()
    elif damage=='patch':
        with db.connect() as conn:conn.execute('UPDATE proposals SET patch_json=?',(json.dumps({'min_value_eok':200}),))
    elif damage=='target':
        with connect(controller.path) as conn:
            value=json.loads(conn.execute('SELECT target_json FROM proposal_review_requests').fetchone()[0]);value['min_value_eok']=200
            conn.execute('UPDATE proposal_review_requests SET target_json=?',(json.dumps(value),))
    else:
        with db.connect() as conn:conn.execute('UPDATE paper_trades SET pnl=-100,pnl_pct=-1')
    before=settings.path.read_bytes()
    assert not controller.sync(settings.values,lambda:pytest.fail('save'),settings.strategy_snapshot,now=1002)
    assert settings.path.read_bytes()==before and status(db)=='PENDING'
    assert '처리 실패' in send(controller,'/개선안처리상태',uid=3)


@pytest.mark.parametrize('how',['expiry','withdraw','stop','restart','scope'])
def test_confirmation_scope_expiry_withdraw_stop_and_restart_never_apply(tmp_path,how):
    db,settings,pid,clock,controller=rig(tmp_path)
    preview=send(controller,f'/개선안승인 {pid}')
    if how=='scope':send(controller,'/개선안확인 '+token(preview),uid=2,scope='other')
    elif how=='withdraw':send(controller,'/개선안철회 '+token(preview),uid=2)
    elif how=='expiry':send(controller,'/개선안확인 '+token(preview),uid=2,now=1120)
    else:
        send(controller,'/개선안확인 '+token(preview),uid=2)
        if how=='stop':controller.cancel_session('owner','s')
        else:controller=ProposalReviewCommands(controller.path,db,settings.values,clock=lambda:clock[0])
    assert not controller.sync(settings.values,lambda:pytest.fail('save'),settings.strategy_snapshot,now=1002)
    assert status(db)=='PENDING' and settings.values['min_value_eok']==999


def test_deadline_rechecked_after_evidence_review_before_settings_write(tmp_path,monkeypatch):
    import app.proposal_application as module
    db,settings,pid,clock,controller=rig(tmp_path);queue(controller,pid)
    original=module.fresh_candidate
    def delayed(*args):
        result=original(*args);clock[0]=1121;return result
    monkeypatch.setattr(module,'fresh_candidate',delayed)
    assert not controller.sync(settings.values,lambda:pytest.fail('save'),settings.strategy_snapshot,now=1001)
    assert status(db)=='PENDING' and settings.values['min_value_eok']==999


@pytest.mark.parametrize('stop',[False,True])
def test_private_service_owner_applies_or_stop_cancels_and_status_stays_readable(tmp_path,sandbox,stop):
    db,settings,pid,clock,controller=rig(tmp_path);service,broker,_,service_clock=sandbox
    clock[0]=service_clock[0].timestamp()
    service.analysis_request=ConditionAnalysisRequest('DEMO',lambda page:'',review=controller)
    bad=update(sandbox,1,f'/개선안승인 {pid}');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    preview=service.handle(update(sandbox,2,f'/개선안승인 {pid}'))
    service.handle(update(sandbox,3,'/개선안확인 '+token(preview)))
    if stop:service.handle(update(sandbox,4,'/중지'))
    changed=controller.sync(settings.values,settings.save,settings.strategy_snapshot,
        now=clock[0]+2,active=service.accepts_search_requests)
    assert changed is (not stop)
    assert ('철회됨' if stop else '적용 확인 완료') in service.handle(update(sandbox,5,'/개선안처리상태'))
    assert broker.calls==[] and status(db)==('PENDING' if stop else 'APPROVED')


def test_interrupted_owner_is_unknown_after_restart_and_never_reapplies(tmp_path):
    from app.config import Settings
    from app.proposal_application import recover
    db,settings,pid,clock,controller=rig(tmp_path);queue(controller,pid)
    def interrupted():raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):controller.sync(settings.values,interrupted,settings.strategy_snapshot,now=1001)
    reopened=Settings(settings.root)
    assert recover(db,reopened.strategy_snapshot)['FAILED']==1
    controller=ProposalReviewCommands(controller.path,db,reopened.values,clock=lambda:clock[0])
    assert '결과 미확인' in send(controller,'/개선안처리상태',uid=3)
    assert not controller.sync(reopened.values,lambda:pytest.fail('retry'),reopened.strategy_snapshot,now=1002)
    assert status(db)=='PENDING'


def test_gui_owner_sync_publishes_approved_settings_to_condition_history_without_gui(tmp_path):
    from types import SimpleNamespace
    from app.telegram_conditions import ConditionCommands
    from test_recommendation_request import gui_method
    db,settings,pid,clock,controller=rig(tmp_path);queue(controller,pid)
    conditions=ConditionCommands(controller.path,settings.values)
    fake=SimpleNamespace(cfg=settings.values,settings=settings,scanning=False,work=None,fields={},
        condition_commands=conditions,proposal_review=controller,
        telegram_order_worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,
            service=SimpleNamespace(accepts_search_requests=lambda:True)),
        fill_stock_tables=lambda:None,refresh_strategy_fields=lambda:None,refresh_shadow=lambda:None,
        count_info=SimpleNamespace(setText=lambda text:None),
        shadow=SimpleNamespace(ensure_active_reference=lambda *a:None))
    gui_method('sync_telegram_conditions',datetime=SimpleNamespace(now=lambda:SimpleNamespace(timestamp=lambda:1002)))(fake)
    assert status(db)=='APPROVED' and fake.cfg['strategy_version']==2
    with connect(controller.path) as conn:
        current=json.loads(conn.execute('SELECT payload FROM condition_current WHERE id=?',(conditions.owner,)).fetchone()[0])
    assert current['conditions']['min_value_eok']==100 and current['version']==2


def test_expired_authority_does_not_claim_an_external_matching_write_as_approval(tmp_path,monkeypatch):
    import app.proposal_application as module
    db,settings,pid,clock,controller=rig(tmp_path);queue(controller,pid)
    original=module.fresh_candidate
    def external_change(*args):
        result=original(*args)
        settings.path.write_text(json.dumps(dict(settings.values,min_value_eok=100,strategy_version=2)),encoding='utf-8')
        clock[0]=1121
        return result
    monkeypatch.setattr(module,'fresh_candidate',external_change)
    assert not controller.sync(settings.values,lambda:pytest.fail('save'),settings.strategy_snapshot,now=1001)
    assert status(db)=='UNKNOWN'
    with db.connect() as conn:assert conn.execute('SELECT state FROM proposal_applications').fetchone()[0]=='ABORTED'
    assert module.recover(db,settings.strategy_snapshot)==dict(APPLIED=0,FAILED=0,UNKNOWN=0)
    assert status(db)=='UNKNOWN'
