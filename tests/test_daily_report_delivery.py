from datetime import datetime,timedelta
import pytest
from app.daily_report_delivery import DailyReportDelivery,pages
from app.order_engine import connect
from app.trading_clock import KST
from test_telegram_orders import sandbox,update


NOW=datetime(2026,10,20,20,30,tzinfo=KST)


def tick(delivery,now=NOW,active=lambda:True,build=lambda d:['실제 입력','가상 기록'],send=lambda t:1):
    return delivery.tick(now=now,active=active,build=build,send=send)


def test_daily_pages_are_frozen_spaced_and_not_repeated_after_restart(tmp_path):
    path=tmp_path/'delivery.db';delivery=DailyReportDelivery(path,'owner','REAL_QUOTES');sent=[]
    send=lambda text:sent.append(text) or len(sent)
    assert tick(delivery,send=send)=='READY'
    assert tick(delivery,send=send)=='WAIT'
    delivery=DailyReportDelivery(path,'owner','REAL_QUOTES')
    assert tick(delivery,now=NOW+timedelta(seconds=2),build=lambda d:pytest.fail('rebuilt'),send=send)=='SENT'
    assert sent==['실제 입력','가상 기록']
    assert tick(delivery,now=NOW+timedelta(seconds=4),send=send)=='SENT'
    assert '모든 페이지 발송 응답 확인' in delivery.summary(NOW)


@pytest.mark.parametrize('receipt',[None,False,0,-1,'1'])
def test_uncertain_receipt_never_replays_or_claims_complete(tmp_path,receipt):
    path=tmp_path/'delivery.db';delivery=DailyReportDelivery(path,'owner','DEMO');sent=[]
    def send(text):sent.append(text);return receipt
    assert tick(delivery,send=send)=='UNKNOWN'
    delivery=DailyReportDelivery(path,'owner','DEMO')
    assert tick(delivery,now=NOW+timedelta(seconds=3),send=send)=='UNKNOWN'
    assert len(sent)==1 and '미확인' in delivery.summary(NOW)


def test_build_failure_stop_before_due_closed_day_and_scopes(tmp_path):
    path=tmp_path/'delivery.db';delivery=DailyReportDelivery(path,'owner','DEMO')
    assert tick(delivery,active=lambda:False)=='INACTIVE'
    assert tick(delivery,now=NOW-timedelta(seconds=1))=='NOT_DUE'
    assert tick(delivery,now=NOW.replace(day=24))=='CLOSED_DAY'
    assert tick(delivery,build=lambda day:[])=='FAILED'
    assert tick(delivery,build=lambda day:pytest.fail('retry'))=='FAILED'
    assert tick(DailyReportDelivery(path,'other','DEMO'))=='READY'
    assert tick(DailyReportDelivery(path,'owner','MOCK'))=='READY'


def test_crash_sending_is_unknown_and_stop_during_build_preserves_pending(tmp_path):
    path=tmp_path/'delivery.db';delivery=DailyReportDelivery(path,'owner','DEMO');active=[True]
    def build(day):active[0]=False;return ['한 페이지']
    assert tick(delivery,active=lambda:active[0],build=build)=='INACTIVE'
    assert '발송 대기' in delivery.summary(NOW)
    with connect(path) as db:db.execute("UPDATE daily_report_delivery SET state='SENDING'")
    delivery=DailyReportDelivery(path,'owner','DEMO')
    assert tick(delivery,send=lambda t:pytest.fail('crash replay'))=='UNKNOWN'


def test_pagination_does_not_drop_details_or_accept_nonsequential_or_long_text():
    assert pages(lambda d,p:f'내용{p}'+(f'\n다음: /가상하루 {d} 2' if p==1 else ''),'2026-10-20','/가상하루')==[
        '내용1\n다음: /가상하루 2026-10-20 2','내용2']
    with pytest.raises(ValueError):pages(lambda d,p:f'다음: /가상하루 {d} 3','2026-10-20','/가상하루')
    with pytest.raises(ValueError):pages(lambda d,p:'🙂'*2000,'2026-10-20','/가상하루')


def test_existing_private_service_builds_separate_reports_without_broker_calls(tmp_path,sandbox):
    service,broker,_,clock=sandbox
    clock[0]=NOW
    service.daily_delivery=DailyReportDelivery(service.path,service.scope,'DEMO')
    service.actual_daily=lambda day,page=1:f'[실제 체결 입력] {day}'
    service.morning_setup=lambda text:'[가상 하루 기록] 별도 예산'
    class Transport:
        def __init__(self):self.messages=[]
        def send(self,chat,text):self.messages.append(text);return len(self.messages)
    transport=Transport()
    for index in range(3):
        clock[0]=NOW+timedelta(seconds=index*2)
        service.send_daily_notice(transport)
    assert len(transport.messages)==3
    assert '모의계좌 하루 결산은 아직' in transport.messages[0]
    assert '[실제 체결 입력]' in transport.messages[1] and '[가상 하루 기록]' in transport.messages[2]
    bad=update(sandbox,1,'/결산알림상태');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    assert '모든 페이지' in service.handle(update(sandbox,2,'/결산알림상태'))
    service.handle(update(sandbox,3,'/중지'))
    clock[0]+=timedelta(days=1)
    assert service.send_daily_notice(transport)=='INACTIVE'
    assert len(transport.messages)==3 and broker.calls==[]



def configured(sandbox,mode='REAL_QUOTES',callback=None):
    service,broker,_,clock=sandbox;clock[0]=NOW
    service.daily_delivery=DailyReportDelivery(service.path,service.scope,mode)
    service.actual_daily=lambda day,page=1:'[실제 체결 입력]'
    service.morning_setup=lambda text:'[가상 하루 기록]'
    service.classified_performance=callback
    class Transport:
        def __init__(self):self.messages=[]
        def send(self,chat,text):self.messages.append(text);return len(self.messages)
    return service,broker,clock,Transport()


def test_real_nightly_full_classified_pages_use_existing_readonly_reports(tmp_path,sandbox):
    from test_classified_performance import rig,before
    from app.classified_performance import report
    from app.morning_day import report as virtual_report
    from app.actual_trade_records import daily_text
    rig(tmp_path,'2026-10-19');rig(tmp_path,'2026-10-20')
    service,broker,clock,transport=configured(sandbox,callback=lambda day,now,page:report(tmp_path,'REAL_QUOTES',day,now=now,page=page))
    service.actual_daily=lambda day,page=1:daily_text(tmp_path/'actual_fill_records.db',day,page=page)
    service.morning_setup=lambda text:virtual_report(tmp_path,'REAL_QUOTES',text.split()[1],now=clock[0],page=int(text.split()[2]))
    saved=before(tmp_path)
    clock[0]=NOW-timedelta(seconds=1);assert service.send_daily_notice(transport)=='NOT_DUE'
    for index in range(6):
        clock[0]=NOW+timedelta(seconds=index*2);state=service.send_daily_notice(transport)
    assert state=='SENT' and len(transport.messages)==6
    assert sum(t.startswith('[추천 성적 분리]') for t in transport.messages)==2
    assert '시장초과수익: 미산출' in transport.messages[-1]
    assert '실제 시세 모드의 새 결산' in service.daily_delivery.summary(clock[0],classified_available=True)
    assert before(tmp_path)==saved and broker.calls==[]


@pytest.mark.parametrize('mode',['DEMO','MOCK'])
def test_other_modes_never_invoke_real_classified_report(sandbox,mode):
    service,broker,clock,transport=configured(sandbox,mode,lambda *a,**k:pytest.fail('REAL classification mixed'))
    for index in range(3):clock[0]=NOW+timedelta(seconds=index*2);state=service.send_daily_notice(transport)
    assert state=='SENT' and len(transport.messages)==3 and broker.calls==[]


@pytest.mark.parametrize('kind',['missing','exception','unsupported','order','length','stopped','date-changed'])
def test_failed_classified_build_is_not_silently_omitted_or_sent(sandbox,kind):
    service,broker,clock,transport=configured(sandbox)
    def callback(day,now,page):
        if kind=='exception':raise ValueError('PRIVATE_ERROR')
        if kind=='unsupported':return '추천 성적 분리는 실제 시세 보기 모드에서 확인해 주세요.'
        if kind=='order':return '[추천 성적 분리]\n다음: /추천성적분리 '+day+' 3'
        if kind=='length':return '[추천 성적 분리]'+'🙂'*2000
        if kind=='stopped':service.stop()
        if kind=='date-changed':clock[0]=NOW+timedelta(days=1)
        return '[추천 성적 분리]'
    service.classified_performance=None if kind=='missing' else callback
    assert service.send_daily_notice(transport) in ('FAILED','INACTIVE')
    with connect(service.path) as db:
        row=db.execute('SELECT state,payload FROM daily_report_delivery').fetchone()
    assert row['state']=='FAILED' and row['payload'] is None and transport.messages==[] and broker.calls==[]


@pytest.mark.parametrize('state',['READY','SENT','UNKNOWN'])
def test_existing_payload_is_not_rebuilt_after_update_even_without_callback(sandbox,state):
    import json
    service,broker,clock,transport=configured(sandbox)
    payload=json.dumps(['이전 저장 보고'],ensure_ascii=False)
    with connect(service.path) as db:
        db.execute('INSERT INTO daily_report_delivery VALUES(?,?,?,?,?,?,?)',(service.scope,'REAL_QUOTES','2026-10-20',state,payload,0,None))
    outcome=service.send_daily_notice(transport)
    with connect(service.path) as db:row=db.execute('SELECT payload FROM daily_report_delivery').fetchone()
    assert row['payload']==payload
    assert transport.messages==(['이전 저장 보고'] if state=='READY' else []) and broker.calls==[]
    assert outcome==('SENT' if state=='READY' else state)


def test_nightly_mode_switch_in_real_gui_callback_fails_before_delivery(tmp_path,sandbox):
    import ast
    from pathlib import Path
    from types import SimpleNamespace
    from app.classified_performance import report
    service,broker,clock,transport=configured(sandbox)
    tree=ast.parse((Path(__file__).resolve().parents[1]/'app/gui.py').read_text(encoding='utf-8'))
    callback=next(k.value for n in ast.walk(tree) if isinstance(n,ast.Call) for k in n.keywords if k.arg=='classified_performance')
    fake=SimpleNamespace(cfg=dict(data_mode='KIWOOM',kiwoom_env='MOCK'))
    service.classified_performance=eval(compile(ast.Expression(callback),'fake-gui','eval'),{'self':fake,'classified_performance_report':report,'data_dir':lambda:tmp_path})
    assert service.send_daily_notice(transport)=='FAILED' and transport.messages==[] and broker.calls==[]



def test_new_classified_payload_delivery_failure_keeps_unknown_without_replay(sandbox):
    service,broker,clock,transport=configured(sandbox,callback=lambda day,now,page:'[추천 성적 분리] 미산출')
    def failed(chat,text):transport.messages.append(text);raise OSError('PRIVATE_FAILURE')
    transport.send=failed
    assert service.send_daily_notice(transport)=='UNKNOWN'
    with connect(service.path) as db:before=tuple(db.execute('SELECT payload,cursor FROM daily_report_delivery').fetchone())
    service.daily_delivery=DailyReportDelivery(service.path,service.scope,'REAL_QUOTES')
    service.classified_performance=lambda *a,**k:pytest.fail('rebuild after restart')
    clock[0]+=timedelta(seconds=2)
    assert service.send_daily_notice(transport)=='UNKNOWN' and len(transport.messages)==1
    with connect(service.path) as db:assert tuple(db.execute('SELECT payload,cursor FROM daily_report_delivery').fetchone())==before
    assert 'PRIVATE_FAILURE' not in before[0] and broker.calls==[]


def test_stale_receiver_scope_cannot_prepare_new_classified_payload(sandbox):
    service,broker,clock,transport=configured(sandbox,callback=lambda *a,**k:pytest.fail('old receiver built'))
    with connect(service.path) as db:db.execute("UPDATE tg_sessions SET session='new-receiver' WHERE scope=?",(service.scope,))
    assert service.send_daily_notice(transport)=='INACTIVE' and transport.messages==[] and broker.calls==[]
    with connect(service.path) as db:assert db.execute('SELECT COUNT(*) FROM daily_report_delivery').fetchone()[0]==0
