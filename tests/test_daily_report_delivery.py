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
