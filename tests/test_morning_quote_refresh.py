"""Manual refresh over FakeQuery and temporary virtual records, no IO clients."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import ast
import copy
import json
from pathlib import Path
import pytest
from app.morning_quote_refresh import refresh,evidence_from_response,targets,summary
from app.morning_quote_request import MorningQuoteRequest
from app.modes import current_mode
from test_morning_quote_router import make,mark,bytes_of
from test_recommendation_request import gui_method
from test_telegram_orders import sandbox,update


class FakeQuery:
    def __init__(self,response,*,mode='REAL_QUOTES'):
        self.url='https://mockapi.kiwoom.com' if mode=='MOCK' else 'https://api.kiwoom.com'
        self.response=response;self.calls=[];self.closed=False
        self.session=SimpleNamespace(close=lambda:setattr(self,'closed',True))
    def query(self,api,path,body,**kwargs):
        self.calls.append((api,path,body))
        return copy.deepcopy(self.response)


def response(code='900001_NX',price='+001020',stamp='20261020080100'):
    return dict(return_code=0,stk_cd=code,stk_tic_chart_qry=[dict(cur_prc=price,cntr_tm=stamp)])


def test_existing_same_mode_holdings_are_queried_once_and_routed_to_today_and_carryover(tmp_path):
    old,_,_=make(tmp_path,mode='REAL_QUOTES');today,_,_=make(tmp_path,'2026-10-20','REAL_QUOTES')
    other,_,_=make(tmp_path,'2026-10-20','MOCK');before=bytes_of(other)
    tick=mark();client=FakeQuery(response())
    result=refresh(tmp_path,'REAL_QUOTES',lambda:client,clock=lambda:tick.at)
    assert result==dict(mode='REAL_QUOTES',targets=1,updated=2,closed=2,unconfirmed=0,failed=0,canceled=False)
    assert client.calls==[('ka10079','/api/dostk/chart',dict(stk_cd='900001_NX',tic_scope='1',upd_stkpc_tp='0'))]
    assert client.closed and bytes_of(other)==before
    assert old.db.trades()[0]['pnl']==today.db.trades()[0]['pnl']==200
    assert len(old.db.trades())==len(today.db.trades())==1


def test_no_holdings_demo_and_mock_nxt_never_construct_client(tmp_path):
    tick=mark();factory=lambda:pytest.fail('unexpected client')
    empty=tmp_path/'empty'
    assert refresh(empty,'REAL_QUOTES',factory,clock=lambda:tick.at)['targets']==0 and not empty.exists()
    unheld,_,_=make(tmp_path,mode='REAL_QUOTES',enter=False)
    assert refresh(tmp_path,'REAL_QUOTES',factory,clock=lambda:tick.at)['targets']==0
    demo,_,_=make(tmp_path/'demo');before=bytes_of(demo)
    assert refresh(tmp_path/'demo','DEMO',factory,clock=lambda:tick.at)['unconfirmed']==1
    assert bytes_of(demo)==before
    mock,_,_=make(tmp_path/'mock',mode='MOCK')
    assert refresh(tmp_path/'mock','MOCK',factory,clock=lambda:tick.at)['unconfirmed']==1
    assert not unheld.db.trades()


@pytest.mark.parametrize('change',[
    'wrong_code','wrong_venue','old_day','stale','future','zero','float','bad_price','conflict','error','empty'])
def test_invalid_or_ambiguous_response_never_changes_holdings_or_marks(tmp_path,change):
    tracker,_,_=make(tmp_path,mode='REAL_QUOTES');before=bytes_of(tracker);tick=mark();data=response()
    if change=='wrong_code':data['stk_cd']='900002_NX'
    elif change=='wrong_venue':data['stk_cd']='900001'
    elif change=='old_day':data['stk_tic_chart_qry'][0]['cntr_tm']='20261019080100'
    elif change=='stale':data['stk_tic_chart_qry'][0]['cntr_tm']='20261020080029'
    elif change=='future':data['stk_tic_chart_qry'][0]['cntr_tm']='20261020080101'
    elif change=='zero':data['stk_tic_chart_qry'][0]['cur_prc']='0'
    elif change=='float':data['stk_tic_chart_qry'][0]['cur_prc']=1020.0
    elif change=='bad_price':data['stk_tic_chart_qry'][0]['cur_prc']='PRIVATE_RESPONSE'
    elif change=='conflict':data['stk_tic_chart_qry'].append(dict(cur_prc='1021',cntr_tm='20261020080100'))
    elif change=='error':data['return_code']=1
    else:data['stk_tic_chart_qry']=[]
    result=refresh(tmp_path,'REAL_QUOTES',lambda:FakeQuery(data),clock=lambda:tick.at)
    assert result['updated']==0 and result['unconfirmed']==1 and bytes_of(tracker)==before
    assert 'PRIVATE' not in json.dumps(result)


def test_krx_mock_suffix_and_mode_guard_and_stopped_response(tmp_path):
    tracker,_,_=make(tmp_path,mode='MOCK',venue='KRX');before=bytes_of(tracker)
    at=mark().at.replace(hour=9);client=FakeQuery(response('900001',stamp='20261020090100'),mode='MOCK')
    result=refresh(tmp_path,'MOCK',lambda:client,clock=lambda:at)
    assert result['updated']==1 and client.calls[0][2]['stk_cd']=='900001'
    second,_,_=make(tmp_path/'second',mode='MOCK',venue='KRX');before=bytes_of(second)
    wrong=FakeQuery(response('900001',stamp='20261020090100'))
    result=refresh(tmp_path/'second','MOCK',lambda:wrong,clock=lambda:at)
    assert result['unconfirmed']==1 and wrong.calls==[] and bytes_of(second)==before
    active=[True]
    class StopQuery(FakeQuery):
        def query(self,*args,**kwargs):active[0]=False;return super().query(*args,**kwargs)
    result=refresh(tmp_path/'second','MOCK',lambda:StopQuery(response('900001',stamp='20261020090100'),mode='MOCK'),clock=lambda:at,active=lambda:active[0])
    assert result['canceled'] and result['updated']==0 and bytes_of(second)==before


@pytest.mark.parametrize('change',['mode','remaining','plan','unknown_env','unknown_data_mode','non_dict'])
def test_invalid_cohort_or_holding_identity_is_failed_without_query_or_write(tmp_path,change):
    tracker,_,_=make(tmp_path,mode='REAL_QUOTES');tick=mark()
    with tracker.db.connect() as db:
        if change in ('mode','unknown_env','unknown_data_mode','non_dict'):
            cfg=json.loads(db.execute('SELECT strat_json FROM paper_trades').fetchone()[0])
            if change=='mode':cfg['kiwoom_env']='MOCK'
            elif change=='unknown_env':cfg['kiwoom_env']='UNKNOWN'
            elif change=='unknown_data_mode':cfg['data_mode']='BAD'
            else:cfg=[]
            db.execute('UPDATE paper_trades SET strat_json=?',(json.dumps(cfg),))
        elif change=='remaining':db.execute('UPDATE paper_trades SET remaining_qty=0')
        else:db.execute("UPDATE morning_cohort SET payload='{}'")
    before=bytes_of(tracker)
    result=refresh(tmp_path,'REAL_QUOTES',lambda:pytest.fail('unexpected query'),clock=lambda:tick.at)
    assert result['failed']==1 and result['targets']==result['updated']==0 and bytes_of(tracker)==before
    assert '기록 확인 실패로 기존 보유 여부는 미확인' in summary(result)
    assert '조회할 기존 가상 보유가 없어요' not in summary(result)


def test_empty_records_are_distinct_from_unreadable_holdings_in_summary(tmp_path):
    tick=mark();result=refresh(tmp_path,'REAL_QUOTES',lambda:pytest.fail('unexpected client'),clock=lambda:tick.at)
    assert result['failed']==0 and '조회할 기존 가상 보유가 없어요' in summary(result)
    assert '기록 확인 실패' not in summary(result)


def test_request_single_owner_expiry_mode_stop_and_at_most_once_notice():
    clock=[0.];request=MorningQuoteRequest('REAL_QUOTES',clock=lambda:clock[0])
    request.request();assert not request.take('REAL_QUOTES',busy=True,active=True)
    with ThreadPoolExecutor(max_workers=3) as pool:
        assert sum(pool.map(lambda _:request.take('REAL_QUOTES',busy=False,active=True),range(3)))==1
    assert '이미' in request.request();request.cancel();assert not request.running()
    request.finish(success=True,result=dict(targets=1,updated=0,closed=0,unconfirmed=0,failed=0,canceled=True))
    sent=[];request.send_notice(lambda text:sent.append(text) or None,active=True)
    request.send_notice(lambda _:pytest.fail('resend'),active=True)
    assert len(sent)==1 and '미확인' in request.summary()
    request.request();clock[0]=120;assert not request.take('REAL_QUOTES',busy=False,active=True)
    assert '만료' in request.summary()
    request.request();assert not request.take('MOCK',busy=False,active=True)
    request.request();request.cancel();assert not request.take('REAL_QUOTES',busy=False,active=True)


def test_private_telegram_request_owner_poll_fake_query_and_result(tmp_path,sandbox):
    tracker,_,_=make(tmp_path,mode='REAL_QUOTES');tick=mark()
    service,broker,_,_=sandbox;request=MorningQuoteRequest('REAL_QUOTES');service.morning_quote_request=request
    bad=update(sandbox,1,'/가상시세갱신');bad['message']['from']['id']=99
    assert service.handle(bad) is None and request.state=='IDLE'
    event=update(sandbox,2,'/가상시세갱신')
    assert '조회를 요청' in service.handle(event) and service.handle(event)==service.handle(event)
    client=FakeQuery(response())
    worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,service=service)
    class Empty:
        def take(self,*a,**k):return False
    fake=SimpleNamespace(cfg=dict(data_mode='KIWOOM',kiwoom_env='REAL'),remote_scan=Empty(),remote_scan_active=False,
        remote_theme=None,remote_theme_active=False,remote_morning_quote=request,remote_morning_quote_active=False,
        telegram_order_worker=worker,work=None,broker_keys=lambda env:('synthetic-key','synthetic-secret'))
    fake.remote_morning_quote_done=lambda result:request.finish(success=True,result=result)
    fake.run_task=lambda fn,ok,label:ok(fn())
    refresh_with_clock=lambda root,mode,factory,active:refresh(root,mode,factory,clock=lambda:tick.at,active=active)
    gui_method('poll_remote_scan',current_mode=current_mode,KiwoomMarket=lambda *a:client,
        data_dir=lambda:tmp_path,refresh_morning_quotes=refresh_with_clock)(fake)
    assert request.state=='DONE' and '반영 1건' in service.handle(update(sandbox,3,'/가상시세상태'))
    assert tracker.db.trades()[0]['pnl']==200 and broker.calls==[]
    request.send_notice(lambda _:1,active=True)
    service.handle(update(sandbox,4,'/중지'))
    assert '중지' in service.handle(update(sandbox,5,'/가상시세갱신'))
    assert '반영 1건' in service.handle(update(sandbox,6,'/가상시세상태'))


def test_optional_request_is_forwarded_by_receiver_without_importing_or_starting_gui():
    source=Path(__file__).resolve().parents[1]/'app/telegram_worker.py'
    tree=ast.parse(source.read_text(encoding='utf-8'))
    calls=[node for node in ast.walk(tree) if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='TelegramOrderService']
    assert len(calls)==1 and any(k.arg=='morning_quote_request' and isinstance(k.value,ast.Attribute) and k.value.attr=='morning_quote_request' for k in calls[0].keywords)


@pytest.mark.parametrize('special',[False,True])
def test_closing_tick_private_command_updates_today_and_carryover(tmp_path,sandbox,monkeypatch,special):
    from datetime import datetime
    from app import morning_venue_policy as policy
    from app.morning_virtual import VenueTrade,MorningVirtual,record_path
    from app.storage import Database
    from app.config import DEFAULTS
    from app.trading_clock import KST,TradingCalendar
    from app.mock_telegram_orders import MockTelegramOrders
    hour=10 if special else 9
    close=datetime(2026,10,20,16 if special else 15,30,tzinfo=KST)
    original=policy.session_bounds
    if special:
        monkeypatch.setattr(policy,'session_bounds',lambda day:(close.replace(hour=10,minute=0),close) if day=='2026-10-20' else original(day))
    cfg=copy.deepcopy(DEFAULTS);cfg.update(data_mode='KIWOOM',kiwoom_env='REAL',paper_fee_pct=0,paper_sell_tax_pct=0,paper_slippage_pct=0)
    trackers=[]
    for day in ('2026-10-19','2026-10-20'):
        at=datetime.fromisoformat(day+'T09:00:00+09:00')
        if day=='2026-10-20':at=at.replace(hour=hour)
        tracker=MorningVirtual(Database(record_path(tmp_path,'REAL_QUOTES',day)))
        tracker.prepare(day=day,mode='REAL_QUOTES',origin='closing',targets=[dict(code='900001',name='합성',market='KOSPI',nxt_eligible=True)],settings=cfg,total_won=10000,now=at.replace(hour=7,minute=30),venue_policy=policy.KRX_OPEN)
        tracker.enter(VenueTrade('REAL_QUOTES','900001','KRX',1000,at,at,'FIRST_TRADE','a'*64),now=at)
        trackers.append(tracker)
    service,broker,_,_=sandbox;request=MorningQuoteRequest('REAL_QUOTES');service.morning_quote_request=request
    assert '조회를 요청' in service.handle(update(sandbox,1,'/가상시세갱신'))
    client=FakeQuery(response('900001',price='1001',stamp=close.strftime('%Y%m%d%H%M%S')))
    worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,service=service)
    class Empty:
        def take(self,*a,**k):return False
    fake=SimpleNamespace(cfg=cfg,remote_scan=Empty(),remote_scan_active=False,remote_theme=None,remote_theme_active=False,
        remote_morning_quote=request,remote_morning_quote_active=False,telegram_order_worker=worker,work=None,broker_keys=lambda env:('synthetic','synthetic'))
    fake.remote_morning_quote_done=lambda result:request.finish(success=True,result=result)
    fake.run_task=lambda fn,ok,label:ok(fn())
    refresh_clock=lambda root,mode,factory,active:refresh(root,mode,factory,clock=lambda:close,active=active)
    gui_method('poll_remote_scan',current_mode=current_mode,KiwoomMarket=lambda *a:client,data_dir=lambda:tmp_path,refresh_morning_quotes=refresh_clock)(fake)
    assert '반영 2건' in service.handle(update(sandbox,2,'/가상시세상태')) and broker.calls==[]
    for tracker in trackers:
        with tracker.db.connect() as db:
            assert db.execute('SELECT price,at FROM morning_marks WHERE day=?',('2026-10-20',)).fetchone()['price']==1001
        assert len(tracker.db.trades())==1
    assert not TradingCalendar(open_dates={close.date()}).is_open(close,'KRX')
    orders=MockTelegramOrders(tmp_path/'mock-gate',confirmed_day=close.date(),clock=lambda:close)
    assert not orders._open()


@pytest.mark.parametrize('case',['before','after','special-before','special-after','holiday','calendar-error'])
def test_quote_calendar_rejection_never_writes_or_creates(tmp_path,monkeypatch,case):
    from datetime import datetime
    from dataclasses import replace
    from app import morning_venue_policy as policy
    from app.morning_quote_router import route_trade
    from app.trading_clock import KST
    tracker,_,_=make(tmp_path,mode='REAL_QUOTES',venue='KRX')
    before=tracker.db.path.read_bytes()
    at=datetime(2026,10,20,8,59,59,tzinfo=KST)
    if case=='after':at=at.replace(hour=15,minute=30,second=1)
    if case.startswith('special-'):
        opening=at.replace(hour=10,minute=0,second=0)
        closing=at.replace(hour=16,minute=30,second=0)
        monkeypatch.setattr(policy,'session_bounds',lambda day:(opening,closing))
        at=opening-timedelta(seconds=1) if case=='special-before' else closing+timedelta(seconds=1)
    if case=='holiday':at=at.replace(day=18,hour=10)
    if case=='calendar-error':
        at=at.replace(hour=10)
        monkeypatch.setattr(policy,'session_bounds',lambda day:(_ for _ in ()).throw(ValueError('calendar unavailable')))
    evidence=replace(mark(),mode='REAL_QUOTES',venue='KRX',at=at,fetched_at=at)
    with pytest.raises(ValueError):route_trade(tmp_path,evidence,now=at)
    assert tracker.db.path.read_bytes()==before
    empty=tmp_path/'absent'
    with pytest.raises(ValueError):route_trade(empty,evidence,now=at)
    assert not empty.exists()
