"""Existing queue -> owner processing -> receiver feedback, all fake IO."""
import re
import pytest
from app.order_engine import connect
from app.mock_watch_status import MockWatchStatus
from app.mock_order_delivery import initialize
from test_mock_telegram_orders import rig,add_capture,mutations
from test_kiwoom_mock_dispatch import accepted,Response
from test_kiwoom_mock_review import wire
from test_telegram_orders import sandbox,update


def processed(tmp_path,sandbox,result='ACKNOWLEDGED'):
    cmd,session,http,_,_=rig(tmp_path)
    service,_,_,_=sandbox
    status=MockWatchStatus()
    status.bind_orders(cmd)
    service.orders_enabled=False
    service.watch_orders=status.handle_orders
    service.watch_delivery=status.order_delivery
    preview=service.handle(update(sandbox,1,'/매수 900001 3 1000'))
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    service.handle(update(sandbox,2,'/모의확인 '+token))
    data=wire()
    if result=='FAILED':data[0]['ord_alow_amt']='1'
    add_capture(http,first=data)
    if result=='ACKNOWLEDGED':http.responses.append(accepted('0000013'))
    elif result=='REJECTED':http.responses.append(Response(dict(return_code=1)))
    elif result=='UNKNOWN':http.responses.append(TimeoutError('synthetic-private-diagnostic'))
    assert cmd.process(session)==result
    return service,cmd,session,http


@pytest.mark.parametrize('receipt,expected',[(7,'SENT'),(None,'UNKNOWN'),(True,'UNKNOWN'),(0,'UNKNOWN'),('raise','UNKNOWN')])
def test_owner_result_is_sent_once_and_missing_receipt_never_retries(tmp_path,sandbox,receipt,expected):
    service,cmd,session,http=processed(tmp_path,sandbox)
    sent=[]
    class Sender:
        def send(self,chat,text):
            sent.append((chat,text))
            if receipt=='raise':raise TimeoutError('synthetic-private-diagnostic')
            return receipt
    service.send_order_notice(Sender())
    service.send_order_notice(Sender())
    assert len(sent)==1 and sent[0][0]==service.chat_id
    assert '체결 완료는 아니에요' in sent[0][1] and '실제 돈 사용 안 함' in sent[0][1]
    assert len(mutations(http))==1
    with connect(cmd.path) as db:
        assert db.execute('SELECT state FROM result_delivery').fetchone()[0]==expected
    report=service.handle(update(sandbox,3,'/주문상태'))
    assert ('텔레그램 발송 응답 확인' if expected=='SENT' else '발송 결과 미확인') in report
    cmd.disarm();session.close()


@pytest.mark.parametrize('result,phrase',[('REJECTED','거절'),('FAILED','전송 전 확인에 실패'),('UNKNOWN','중복 주문하지 마세요')])
def test_non_success_feedback_matches_actual_processing_result(tmp_path,sandbox,result,phrase):
    service,cmd,session,http=processed(tmp_path,sandbox,result)
    sent=[]
    class Sender:
        def send(self,chat,text):sent.append(text);return 7
    service.send_order_notice(Sender())
    assert len(sent)==1 and phrase in sent[0]
    assert 'synthetic-private-diagnostic' not in sent[0]
    assert len(mutations(http))==(0 if result=='FAILED' else 1)
    cmd.disarm();session.close()


@pytest.mark.parametrize('change',['stopped','takeover','practice'])
def test_inactive_or_wrong_receiver_cannot_claim_feedback(tmp_path,sandbox,change):
    service,cmd,session,_=processed(tmp_path,sandbox)
    if change=='stopped':service.stop()
    elif change=='takeover':
        with connect(service.path) as db:db.execute("UPDATE tg_sessions SET session='another'")
    else:service.orders_enabled=True
    class Sender:
        def send(self,*args):pytest.fail('must not send')
    service.send_order_notice(Sender())
    with connect(cmd.path) as db:assert db.execute('SELECT count(*) FROM result_delivery').fetchone()[0]==0
    cmd.disarm();session.close()


def test_delivery_is_scope_bound_and_crashed_claim_is_not_retried(tmp_path,sandbox):
    service,cmd,session,_=processed(tmp_path,sandbox)
    assert cmd.notice_delivery('claim',scope='another',session=service.session) is None
    assert cmd.notice_delivery('claim',scope=service.scope,session='another') is None
    context=dict(scope=service.scope,session=service.session)
    notice=cmd.notice_delivery('claim',**context)
    cmd.notice_delivery('finish',key=notice['key'],delivered=True,scope='another',session=service.session)
    with connect(cmd.path) as db:
        assert db.execute('SELECT state FROM result_delivery').fetchone()[0]=='SENDING'
        initialize(db)
    assert cmd.notice_delivery('claim',**context) is None
    with connect(cmd.path) as db:assert db.execute('SELECT state FROM result_delivery').fetchone()[0]=='UNKNOWN'
    cmd.disarm();session.close()
