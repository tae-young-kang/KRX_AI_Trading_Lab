from datetime import timedelta
import json
import pytest
from app.mock_auto_exit import exit_key
from app.order_engine import connect
from test_mock_telegram_orders import rig,handle,add_capture,mutations
from test_mock_protection_flow import buy,evidence
from test_kiwoom_mock_dispatch import accepted
from test_mock_market_intents import market_rig,prepare,quote


def auto_buy(cmd,session,http):
    preview=buy(cmd,session,http,' 익절 2 손절 3 자동매도')
    assert '추가 확인 없이 KRX 모의 시장가 매도' in preview
    with connect(cmd.path) as db:
        parent=db.execute("SELECT * FROM intents WHERE state='ACKNOWLEDGED'").fetchone()
    payload=json.loads(parent['payload'])
    assert payload['auto_exit'] is True and payload['auto_exit_generation']==cmd.generation
    return dict(parent)


@pytest.mark.parametrize('price,reason',[(1020,'TAKE_PROFIT'),(960,'STOP_LOSS')])
def test_explicit_parent_consent_triggers_one_rechecked_market_sale(tmp_path,price,reason):
    cmd,session,http,_,clock=rig(tmp_path)
    parent=auto_buy(cmd,session,http)
    evidence(http,price=price)
    result=cmd.monitor_protection(session)
    assert result['decision']['state']==reason and result['auto_exit_state']=='QUEUED'
    with connect(cmd.path) as db:
        child=db.execute('SELECT * FROM intents WHERE id=?',(exit_key(parent['id']),)).fetchone()
        payload=json.loads(child['payload'])
    assert child['state']=='QUEUED' and child['uid']<0
    assert payload['protection_parent']==parent['id'] and payload['quantity']==3
    assert len(mutations(http))==1
    evidence(http,price=price);http.responses.append(accepted('0000014'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert mutations(http)[-1]['json']==dict(dmst_stex_tp='KRX',stk_cd='900001',ord_qty='3',trde_tp='3')
    assert mutations(http)[-1]['headers']['api-id']=='kt10001'
    clock[0]+=timedelta(seconds=31)
    assert cmd.monitor_protection(session) is None and cmd.process(session) is None
    assert len(mutations(http))==2
    assert '사전 허용한 자동 보호 매도' in handle(cmd,'/주문목록',uid=3)
    notices=[]
    while True:
        notice=cmd.notice_delivery('claim',scope='owner',session='receiver')
        if notice is None:break
        notices.append(notice['text'])
        cmd.notice_delivery('finish',scope='owner',session='receiver',key=notice['key'],delivered=True)
    assert any('자동매도 허용 요청: 시장가 매도 접수 응답 확인' in text for text in notices)
    cmd.disarm();session.close()


def test_observation_only_parent_never_gains_auto_permission(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    buy(cmd,session,http)
    evidence(http,price=1020)
    assert 'auto_exit_state' not in cmd.monitor_protection(session)
    with connect(cmd.path) as db:assert db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]==1
    assert len(mutations(http))==1
    cmd.disarm();session.close()


@pytest.mark.parametrize('change',['emergency','expired','holdings','consent'])
def test_auto_sale_still_needs_permission_fresh_position_and_live_deadline(tmp_path,change):
    cmd,session,http,_,clock=rig(tmp_path)
    parent=auto_buy(cmd,session,http)
    evidence(http,price=1020);cmd.monitor_protection(session)
    if change=='emergency':handle(cmd,'/긴급중지',uid=3)
    elif change=='expired':clock[0]+=timedelta(seconds=31)
    else:
        if change=='consent':
            payload=json.loads(parent['payload']);payload['auto_exit']=False
            with connect(cmd.path) as db:db.execute('UPDATE intents SET payload=? WHERE id=?',(json.dumps(payload),parent['id']))
        from test_kiwoom_mock_review import wire
        data=wire()
        for record in data[2]['acnt_ord_cntr_prst_array']:record['ord_no']='0000013'
        if change=='holdings':data[1]['acnt_evlt_remn_indv_tot'][0]['trde_able_qty']='2'
        add_capture(http,data)
    cmd.process(session)
    assert len(mutations(http))==1
    cmd.disarm();session.close()


def test_new_connection_does_not_reuse_old_auto_consent(tmp_path):
    cmd,session,http,_,_=rig(tmp_path)
    auto_buy(cmd,session,http)
    cmd.generation='f'*32
    before=len(http.calls)
    assert cmd.monitor_protection(session) is None and len(http.calls)==before
    cmd.disarm();session.close()


def test_market_buy_preview_includes_auto_permission_and_frozen_policy(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    prepare(cmd,session,http,'/매수 900001 3주 시장가 익절 2 손절 3 자동매도')
    with connect(cmd.path) as db:payload=json.loads(db.execute('SELECT payload FROM intents').fetchone()[0])
    assert payload['auto_exit'] is True and payload['take_bps']==200 and payload['stop_bps']==300
    assert '추가 확인 없이' in handle(cmd,'/주문상태',uid=4)
    assert len(mutations(http))==0
    cmd.disarm();session.close()


def test_daily_limit_records_failed_auto_request_without_silent_retry(tmp_path):
    cmd,session,http,_,clock=rig(tmp_path)
    parent=auto_buy(cmd,session,http)
    with connect(cmd.path) as db:
        for index in range(2):
            db.execute('INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?)',
                (f'example-{index}','owner','receiver',100+index,'',json.dumps(dict(code='900002',side='BUY',quantity=1,limit_price=1000)),
                 clock[0].timestamp()+120,'ACKNOWLEDGED',cmd.day.isoformat()))
    evidence(http,price=1020)
    assert cmd.monitor_protection(session)['auto_exit_state']=='FAILED'
    with connect(cmd.path) as db:
        row=db.execute('SELECT * FROM intents WHERE id=?',(exit_key(parent['id']),)).fetchone()
    assert '3회' in json.loads(row['payload'])['protection_block_reason']
    assert cmd.process(session) is None and len(mutations(http))==1
    cmd.disarm();session.close()


@pytest.mark.parametrize('text',[
    '/매수 900001 3주 1000 자동매도',
    '/매수 900001 3주 시장가 자동매도',
    '/매도 900001 1주 1000 익절 2 손절 3 자동매도'])
def test_auto_permission_cannot_be_implicit_or_attached_to_a_sale(tmp_path,text):
    cmd,session,http,_,_=rig(tmp_path)
    assert '/모의확인' not in handle(cmd,text)
    with connect(cmd.path) as db:assert db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]==0
    assert mutations(http)==[]
    cmd.disarm();session.close()
