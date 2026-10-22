"""Private fake Telegram -> persistent unexecuted request, with no broker IO."""
import hashlib
import json
import re
from datetime import datetime,timedelta

import pytest
from app.krx_history import HistoryStore
from app.order_engine import connect
from app.telegram_orders import TelegramOrderService
from app.theme_opening_requests import handle
from app.trading_clock import KST
from test_telegram_orders import sandbox,update


def source(tmp_path):
    history=HistoryStore(tmp_path/'source.db')
    with history.connect() as db:
        db.execute('CREATE TABLE theme_runs(id INTEGER PRIMARY KEY,mode TEXT,source TEXT,day TEXT,report_json TEXT,alert_ids_json TEXT)')
        db.execute('CREATE TABLE theme_alerts(id INTEGER PRIMARY KEY,fingerprint TEXT,mode TEXT,source TEXT,day TEXT,market TEXT,code TEXT,result_json TEXT,message TEXT,state TEXT)')
        for index in (1,2):
            payload=dict(theme=1,version=1,mode='MOCK',result=dict(status='COMPLETE',price_source='KIWOOM_MOCK_KRX',matched=True,detected_on='2026-10-19',code=f'90000{index}',name=f'합성{index}',market='KOSPI',reference_close=10000))
            raw=json.dumps(payload,sort_keys=True,ensure_ascii=False,allow_nan=False)
            db.execute('INSERT INTO theme_alerts VALUES(?,?,?,?,?,?,?,?,?,?)',(index,hashlib.sha256(raw.encode()).hexdigest(),'MOCK','KIWOOM_MOCK_KRX','2026-10-19','KOSPI',f'90000{index}',raw,'합성 알림','PREPARED'))
        db.execute('INSERT INTO theme_runs VALUES(?,?,?,?,?,?)',(1,'MOCK','KIWOOM_MOCK_KRX','2026-10-19',json.dumps({'search_scope':{'include_etf':False}}),'[1,2]'))
    return history


def wire(service,history,mode='MOCK'):
    service.theme_opening=lambda text,**kw:handle(history,mode,text,**kw)
    return service


def request(s,uid,text):return s[0].handle(update(s,uid,text))
def token(reply):return re.search('/예약확인 ([A-Za-z0-9_-]+)',reply)[1]
def rows(service):
    with connect(service.path) as db:return [dict(r) for r in db.execute('SELECT * FROM theme_opening_requests ORDER BY id')]


def test_private_flow_confirm_restart_read_cancel_and_duplicate(sandbox,tmp_path):
    service,broker,engine,clock=sandbox;clock[0]=datetime(2026,10,19,21,tzinfo=KST)
    history=source(tmp_path);wire(service,history)
    message=update(sandbox,1,'/테마1예약 전체 10만원 익절 4 손절 2')
    preview=service.handle(message)
    assert '확인 전' in preview and '2026-10-20 KRX' in preview and '예상 4주' in preview
    assert service.handle(message)==preview
    confirmed=request(sandbox,2,'/예약확인 '+token(preview))
    assert '미실행 요청 저장됨' in confirmed and '주문·예수금 확보·체결은 하지 않았어요' in confirmed
    assert request(sandbox,3,'/예약확인 '+token(preview))==confirmed
    assert request(sandbox,4,'/테마1예약 전체 10만원 익절 4 손절 2')==confirmed
    assert len(rows(service))==1 and broker.calls==[]
    restarted=wire(TelegramOrderService(service.path,engine,11,22,33,{},lambda:clock[0]),history)
    sandbox=(restarted,broker,engine,clock)
    assert '미실행' in request(sandbox,5,'/테마1예약목록')
    assert '익절 +4%' in request(sandbox,6,'/테마1예약상태 1')
    assert '철회됨' in request(sandbox,7,'/예약철회 1')
    assert '철회됨' in request(sandbox,8,'/예약철회 1')
    assert rows(restarted)[0]['state']=='CANCELED' and broker.calls==[]


@pytest.mark.parametrize('mode',['DEMO','REAL_QUOTES','BAD'])
def test_other_modes_write_no_reservation(sandbox,tmp_path,mode):
    service,broker,_,_=sandbox;wire(service,source(tmp_path),mode)
    assert '저장하지 않아요' in request(sandbox,1,'/테마1예약 전체 10만원')
    with connect(service.path) as db:assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='theme_opening_requests'").fetchone()
    assert broker.calls==[]


@pytest.mark.parametrize('mutation',['etf','source','price','runmode','missing','unknown_symbol','zero_budget'])
def test_invalid_source_or_target_never_creates_request(sandbox,tmp_path,mutation):
    service,broker,_,_=sandbox;history=source(tmp_path);wire(service,history)
    with history.connect() as db:
        if mutation=='etf':db.execute("UPDATE theme_runs SET report_json='{}'")
        if mutation=='source':db.execute("UPDATE theme_runs SET source='BAD'")
        if mutation=='price':db.execute("UPDATE theme_alerts SET result_json='{}'")
        if mutation=='runmode':db.execute("UPDATE theme_runs SET mode='REAL_QUOTES'")
        if mutation=='missing':db.execute('DELETE FROM theme_alerts WHERE id=1')
    command='/테마1예약 '+('없는종목 10만원' if mutation=='unknown_symbol' else '전체 1원' if mutation=='zero_budget' else '전체 10만원')
    assert '저장하지 않았어요' in request(sandbox,1,command)
    assert rows(service)==[] and broker.calls==[]


@pytest.mark.parametrize('change',['source','etf','price','expiry','open','restart','stop'])
def test_confirmation_rechecks_source_deadline_and_session(sandbox,tmp_path,change):
    service,broker,engine,clock=sandbox;clock[0]=datetime(2026,10,20,8,59,tzinfo=KST)
    history=source(tmp_path);wire(service,history)
    preview=request(sandbox,1,'/테마1예약 합성1 10만원')
    with history.connect() as db:
        if change=='source':db.execute("UPDATE theme_runs SET source='KRX'")
        if change=='etf':db.execute("UPDATE theme_runs SET report_json='{}'")
        if change=='price':db.execute("UPDATE theme_alerts SET result_json='{}' WHERE id=1")
    if change=='expiry':clock[0]+=timedelta(seconds=120)
    if change=='open':clock[0]=clock[0].replace(hour=9,minute=0)
    if change=='stop':service.stop()
    if change=='restart':
        service=wire(TelegramOrderService(service.path,engine,11,22,33,{},lambda:clock[0]),history)
        sandbox=(service,broker,engine,clock)
    answer=request(sandbox,2,'/예약확인 '+token(preview))
    assert '미실행 요청 저장됨' not in answer
    assert all(r['state']!='RECORDED_UNEXECUTED' for r in rows(service)) and broker.calls==[]


def test_owner_isolation_untrusted_and_saved_plan_immutable(sandbox,tmp_path):
    service,broker,_,clock=sandbox;history=source(tmp_path);wire(service,history)
    clock[0]=datetime(2026,10,19,21,tzinfo=KST)
    bad=update(sandbox,1,'/테마1예약 전체 10만원');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    preview=request(sandbox,2,'/테마1예약 전체 10만원')
    frozen=rows(service)[0]['payload']
    with connect(service.path) as db:
        assert '없어요' in handle(history,'MOCK','/테마1예약상태 1',database=db,scope='other',session='other',now=clock[0],active=True)
        assert '없어요' in handle(history,'MOCK','/예약철회 1',database=db,scope='other',session='other',now=clock[0],active=True)
    request(sandbox,3,'/예약확인 '+token(preview))
    assert rows(service)[0]['payload']==frozen and broker.calls==[]


def test_special_open_and_exact_expiry(sandbox,tmp_path,monkeypatch):
    import app.theme_opening_requests as module
    service,broker,_,clock=sandbox;clock[0]=datetime(2026,10,20,9,30,tzinfo=KST)
    wire(service,source(tmp_path))
    monkeypatch.setattr(module,'next_day',lambda now:'2026-10-20')
    monkeypatch.setattr(module,'krx_open',lambda day:datetime(2026,10,20,10,tzinfo=KST))
    preview=request(sandbox,1,'/테마1예약 전체 10만원')
    assert '확인 전' in preview
    clock[0]+=timedelta(seconds=120)
    assert '유효한 확인이 없어요' in request(sandbox,2,'/예약확인 '+token(preview))
    assert broker.calls==[]


def test_gui_callback_guards_current_mode_and_worker_forwards_without_gui(tmp_path):
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    tree=ast.parse(Path('app/gui.py').read_text(encoding='utf-8'))
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='TelegramOrderWorker']
    callback=next(k.value for call in calls for k in call.keywords if k.arg=='theme_opening')
    cfg={'mode':'MOCK'};fake=SimpleNamespace(history=source(tmp_path),cfg=cfg)
    invoked=[]
    fn=eval(compile(ast.Expression(callback),'<offline callback>','eval'),dict(self=fake,current_mode=lambda c:SimpleNamespace(key=c['mode']),theme_opening_handle=lambda *a,**k:invoked.append((a,k)) or 'called'))
    assert fn('/테마1예약목록',database=None,scope='test',session='test',now=None,active=True)=='called'
    cfg['mode']='REAL_QUOTES'
    assert '모드가 바뀌었어요' in fn('/테마1예약목록') and len(invoked)==1
    worker=ast.parse(Path('app/telegram_worker.py').read_text(encoding='utf-8'))
    forwarded=[k.value for n in ast.walk(worker) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='TelegramOrderService' for k in n.keywords if k.arg=='theme_opening']
    assert len(forwarded)==1 and isinstance(forwarded[0],ast.Attribute) and forwarded[0].attr=='theme_opening'


def test_replaced_legal_payload_cannot_reuse_original_confirmation_or_render(sandbox,tmp_path):
    from app.theme_opening_requests import _snapshot,_json
    service,broker,_,clock=sandbox;clock[0]=datetime(2026,10,19,21,tzinfo=KST)
    history=source(tmp_path);wire(service,history)
    preview=request(sandbox,1,'/테마1예약 전체 10만원')
    replacement=_snapshot(history,'MOCK',1,'전체',200000,200,300,clock[0])
    replacement['target_day']='2026-10-20'
    with connect(service.path) as db:
        db.execute('UPDATE theme_opening_requests SET payload=? WHERE id=1',(_json(replacement),))
    assert '확인하지 못했어요' in request(sandbox,2,'/테마1예약상태 1')
    assert '확인하지 못했어요' in request(sandbox,3,'/테마1예약목록')
    assert '확인하지 않았어요' in request(sandbox,4,'/예약확인 '+token(preview))
    assert rows(service)[0]['state']=='EXPIRED' and broker.calls==[]
