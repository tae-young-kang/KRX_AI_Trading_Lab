"""Local analysis handoff/record provenance/private report over synthetic DBs."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from types import SimpleNamespace
import pytest
from app.condition_analysis_request import ConditionAnalysisRequest
from app.proposal_report import report
from app.storage import Database
from app.strategy import create_proposals
from app.config import DEFAULTS
from test_condition_report import add
from test_telegram_orders import sandbox,update
from test_recommendation_request import gui_method
from app.modes import current_mode


def rig(tmp_path):
    db=Database(tmp_path/'practice.db');cfg=deepcopy(DEFAULTS);add(db,cfg)
    cfg['min_value_eok']=999
    clock=[0.0]
    request=ConditionAnalysisRequest('DEMO',lambda page:report(db,'DEMO',page=page),clock=lambda:clock[0])
    return db,cfg,clock,request


def test_claim_compare_preserves_settings_records_context_and_sends_once(tmp_path):
    db,cfg,clock,request=rig(tmp_path);before=dict(cfg)
    request.request()
    assert not request.take('DEMO',busy=True,active=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        claimed=list(pool.map(lambda _:request.take('DEMO',busy=False,active=True),range(3)))
    assert sum(claimed)==1
    result=create_proposals(db,dict(cfg));request.finish(success=True,result=result)
    assert cfg==before and len(db.proposals())==1 and db.proposals()[0]['state']=='PENDING'
    with db.connect() as conn:
        row=conn.execute('SELECT * FROM proposal_context').fetchone()
    assert row['mode']=='DEMO' and json.loads(row['base_json'])['min_value_eok']==999
    text=request.report(1)
    assert '거래대금 → 100' in text and '검토 대기' in text and '자동 적용' in text
    create_proposals(db,cfg)
    assert len(db.proposals())==1
    sent=[];request.send_notice(lambda text:sent.append(text) or None,active=True)
    request.send_notice(lambda text:pytest.fail('resend'),active=True)
    assert len(sent)==1 and '미확인' in request.summary()


def test_expiry_mode_and_stopped_owner_prevent_analysis(tmp_path):
    _,_,clock,request=rig(tmp_path);request.request();clock[0]=120
    assert not request.take('DEMO',busy=False,active=True) and '만료' in request.summary()
    request.request()
    assert not request.take('MOCK',busy=False,active=True)
    request.request()
    assert not request.take('DEMO',busy=False,active=False)


def test_legacy_proposals_and_private_fields_are_not_shown_or_promoted(tmp_path):
    db,cfg,_,_=rig(tmp_path)
    db.propose('PRIVATE_TITLE','PRIVATE_RATIONALE',{'min_value_eok':100})
    assert 'PRIVATE' not in report(db,'DEMO') and '확인된 개선안이 없어요' in report(db,'DEMO')
    cfg['unexpected_secret']='PRIVATE_MARKER'
    create_proposals(db,cfg)
    with db.connect() as conn:body=conn.execute('SELECT base_json FROM proposal_context').fetchone()[0]
    assert 'PRIVATE_MARKER' not in body
    before=db.path.read_bytes();text=report(db,'DEMO')
    assert '거래대금 → 100' in text and 'PRIVATE' not in text and db.path.read_bytes()==before
    assert '확인된 개선안이 없어요' in report(db,'MOCK')
    # Same patch with a different captured base is a different review proposal.
    cfg['min_value_eok']=888;create_proposals(db,cfg)
    assert len(db.proposals())==3


def test_report_rejects_unknown_patch_and_wrong_context_without_echoing_values(tmp_path):
    db,cfg,_,_=rig(tmp_path);create_proposals(db,cfg)
    with db.connect() as conn:conn.execute('UPDATE proposals SET patch_json=?',(json.dumps({'unknown':'PRIVATE_MARKER'}),))
    with pytest.raises(ValueError,match='근거'):report(db,'DEMO')


def test_private_commands_stopped_reading_and_no_orders(tmp_path,sandbox):
    db,cfg,_,request=rig(tmp_path);service,broker,_,_=sandbox;service.analysis_request=request
    bad=update(sandbox,1,'/조건비교');bad['message']['from']['id']=99
    assert service.handle(bad) is None and request.state=='IDLE'
    assert '요청했어요' in service.handle(update(sandbox,2,'/조건비교'))
    request.take('DEMO',busy=False,active=True);request.finish(success=True,result=create_proposals(db,cfg))
    service.handle(update(sandbox,3,'/중지'))
    assert '중지' in service.handle(update(sandbox,4,'/조건비교'))
    assert '거래대금 → 100' in service.handle(update(sandbox,5,'/개선안'))
    assert '비교를 마쳤어요' in service.handle(update(sandbox,6,'/조건비교상태'))
    assert broker.calls==[]


def test_gui_poll_uses_owner_task_and_captured_config_without_gui(tmp_path):
    db,cfg,_,request=rig(tmp_path);request.request();calls=[]
    fake=SimpleNamespace(cfg=cfg,db=db,remote_analysis=request,remote_analysis_active=False,
        remote_scan=None,remote_theme=None,remote_scan_active=False,remote_theme_active=False,work=None,
        telegram_order_worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,
            service=SimpleNamespace(accepts_search_requests=lambda:True)))
    class Empty:
        def take(self,*args,**kwargs):return False
    fake.remote_scan=Empty()
    fake.remote_analysis_done=lambda result:request.finish(success=True,result=result)
    fake.run_task=lambda fn,ok,text:calls.append(text) or ok(fn())
    gui_method('poll_remote_scan',current_mode=current_mode,create_proposals=create_proposals)(fake)
    assert calls==['저장 조건 비교 완료'] and request.state=='DONE' and len(db.proposals())==1
