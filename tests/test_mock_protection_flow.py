"""Opt-in BUY policy -> owner observation loop -> stored trigger, fake IO."""
import re
from datetime import timedelta
import pytest
from app.order_engine import connect
from test_mock_telegram_orders import rig,handle,add_capture,mutations
from test_kiwoom_mock_dispatch import accepted,Response
from test_kiwoom_mock_review import wire
from test_mock_market_quote import fixture


def buy(cmd,session,http,policy=' 익절 2 손절 3'):
    preview=handle(cmd,'/매수 900001 3주 1000'+policy)
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    handle(cmd,'/모의확인 '+token,uid=2)
    add_capture(http);http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    return preview


def evidence(http,*,price,second='100000',sellable='3'):
    data=wire()
    for row in data[2]['acnt_ord_cntr_prst_array']:row['ord_no']='0000013'
    data[1]['acnt_evlt_remn_indv_tot'][0]['trde_able_qty']=sellable
    add_capture(http,data)
    payload,headers=fixture()
    payload['atn_stk_infr'][0].update(cur_prc=str(price),cntr_tm=second)
    http.responses.append(Response(payload,headers=headers))


@pytest.mark.parametrize('price,state',[(1020,'TAKE_PROFIT'),(960,'STOP_LOSS')])
def test_confirmed_policy_and_fills_feed_trigger_without_sale(tmp_path,price,state):
    cmd,session,http,_,clock=rig(tmp_path)
    assert '자동 매도 미연결' in buy(cmd,session,http)
    evidence(http,price=price)
    result=cmd.monitor_protection(session)
    assert result['new_trigger'] and result['decision']['state']==state
    assert result['decision']['quantity']==3 and not result['executable']
    assert '조건 도달 기록' in handle(cmd,'/보호상태')
    assert cmd.notice_delivery('claim',scope='another',session='receiver') is None
    notices=[]
    for index in range(3):
        notice=cmd.notice_delivery('claim',scope='owner',session='receiver')
        notices.append(notice['text'])
        cmd.notice_delivery('finish',scope='owner',session='receiver',key=notice['key'],delivered=index<2)
    assert '조건 도달' in notices[-1] and '매도 주문은 아직 보내지 않았어요' in notices[-1]
    assert cmd.notice_delivery('claim',scope='owner',session='receiver') is None
    count=len(http.calls)
    clock[0]+=timedelta(seconds=31)
    assert cmd.monitor_protection(session) is None and len(http.calls)==count
    assert len(mutations(http))==1
    cmd.disarm();session.close()


def test_hold_is_throttled_and_rechecked_with_new_evidence(tmp_path):
    cmd,session,http,_,clock=rig(tmp_path)
    buy(cmd,session,http)
    evidence(http,price=1000)
    result=cmd.monitor_protection(session)
    assert result['state']=='OBSERVING' and result['decision']['state']=='HOLD'
    count=len(http.calls)
    assert cmd.monitor_protection(session) is None and len(http.calls)==count
    clock[0]+=timedelta(seconds=30)
    evidence(http,price=1020,second='100030')
    assert cmd.monitor_protection(session)['decision']['state']=='TAKE_PROFIT'
    cmd.disarm();session.close()


@pytest.mark.parametrize('case',['no_policy','stopped','sellable'])
def test_monitor_needs_opt_in_live_session_and_reconciled_sellable_quantity(tmp_path,case):
    cmd,session,http,_,_=rig(tmp_path)
    buy(cmd,session,http,policy='' if case=='no_policy' else ' 익절 2 손절 3')
    before=len(http.calls)
    if case=='stopped':cmd.cancel_session('owner','receiver')
    if case=='sellable':
        evidence(http,price=1020,sellable='2')
        assert cmd.monitor_protection(session)['state']=='BLOCKED'
    else:
        assert cmd.monitor_protection(session) is None and len(http.calls)==before
    with connect(cmd.protection.path) as db:
        assert not db.execute("SELECT 1 FROM protection_plans WHERE state='TRIGGERED'").fetchone()
    assert len(mutations(http))==1
    cmd.disarm();session.close()


@pytest.mark.parametrize('text',['/매도 900001 1 1000 익절 2 손절 3',
    '/매수 900001 1 1000 익절 0 손절 3'])
def test_invalid_policy_never_creates_order(tmp_path,text):
    cmd,session,http,_,_=rig(tmp_path)
    reply=handle(cmd,text)
    assert '/모의확인' not in reply
    with connect(cmd.path) as db:assert db.execute('SELECT count(*) FROM intents').fetchone()[0]==0
    assert not mutations(http)
    cmd.disarm();session.close()
