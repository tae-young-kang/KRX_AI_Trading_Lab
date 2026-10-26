from datetime import timedelta
import json
import re
import pytest
from app.order_engine import connect
from test_mock_telegram_orders import rig,handle,add_capture,mutations
from test_kiwoom_mock_dispatch import NOW,Response,accepted


def quote(http,upper=1300):
    http.responses.append(Response(dict(return_code=0,atn_stk_infr=[dict(stk_cd='900001',dt=NOW.strftime('%Y%m%d'),
        cntr_tm=NOW.strftime('%H%M%S'),cur_prc='1000',upl_pric=str(upper))]),headers={'cont-yn':'N','next-key':''}))


def market_rig(tmp_path):
    cmd,session,http,socket,clock=rig(tmp_path)
    cmd.symbol_lookup=lambda name,now:dict(code='900001',name='시장가 예제',market='KOSPI')
    return cmd,session,http,socket,clock


def prepare(cmd,session,http,text='/매수 900001 3주 시장가'):
    assert '아직 주문하지' in handle(cmd,text)
    assert mutations(http)==[]
    add_capture(http);quote(http)
    assert cmd.process(session)=='MARKET_PREPARED'
    preview=handle(cmd,'/주문상태',uid=2)
    assert '시장가 매수' in preview and '상한가' in preview,preview
    return re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]


def test_market_prepare_confirm_refresh_dispatch_has_no_limit_price_and_no_replay(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    token=prepare(cmd,session,http)
    assert mutations(http)==[]
    notice=cmd.notice_delivery('claim',scope='owner',session='receiver')
    assert '/모의확인 '+token in notice['text']
    cmd.notice_delivery('finish',scope='owner',session='receiver',key=notice['key'],delivered=True)
    assert cmd.notice_delivery('claim',scope='owner',session='receiver') is None
    assert '전송을 요청' in handle(cmd,'/모의확인 '+token,uid=3)
    add_capture(http);quote(http);http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert cmd.process(session) is None
    body=mutations(http)[0]['json']
    assert body==dict(dmst_stex_tp='KRX',stk_cd='900001',ord_qty='3',trde_tp='3')
    with connect(cmd.path) as db:
        payload=json.loads(db.execute('SELECT payload FROM intents').fetchone()[0])
    assert payload['limit_price']==1300 and payload['market_reserved_won']==3939
    assert '시장가 · 예산 상한가' in handle(cmd,'/주문목록',uid=4)
    cmd.disarm();session.close()


def test_budget_request_uses_upper_price_and_fee_floor_then_freezes_quantity(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    prepare(cmd,session,http,'/매수 시장가 예제 1만원 시장가')
    with connect(cmd.path) as db:
        payload=json.loads(db.execute('SELECT payload FROM intents').fetchone()[0])
    assert payload['quantity']==7 and payload['requested_won']==10000
    assert payload['market_reserved_won']==9191
    cmd.disarm();session.close()


@pytest.mark.parametrize('change',['upper','funds','expired','stopped'])
def test_changed_evidence_or_permission_blocks_market_mutation(tmp_path,change):
    cmd,session,http,_,clock=market_rig(tmp_path)
    token=prepare(cmd,session,http)
    handle(cmd,'/모의확인 '+token,uid=3)
    if change=='expired':clock[0]+=timedelta(seconds=121)
    elif change=='stopped':cmd.cancel_session('owner','receiver')
    else:
        if change=='funds':
            from test_kiwoom_mock_review import wire
            data=wire();data[0]['100stk_ord_alow_amt']='1';data[0]['ord_alow_amt']='1'
            add_capture(http,data)
        else:add_capture(http);quote(http,1400)
    cmd.process(session)
    assert mutations(http)==[]
    cmd.disarm();session.close()


def test_pending_preparation_is_canceled_on_stop_and_never_restored_as_order(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    handle(cmd,'/매수 900001 3주 시장가')
    assert '대기' in handle(cmd,'/매수 900001 1주 1000',uid=2)
    cmd.cancel_session('owner','receiver')
    assert cmd.process(session) is None
    assert mutations(http)==[]
    with connect(cmd.path) as db:
        assert db.execute('SELECT state FROM market_previews').fetchone()[0]=='CANCELED'
        assert db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]==0
    cmd.disarm();session.close()


def test_preparation_funds_failure_is_not_an_order_and_notice_is_private_once(tmp_path):
    from test_kiwoom_mock_review import wire
    cmd,session,http,_,_=market_rig(tmp_path)
    handle(cmd,'/매수 900001 3주 시장가')
    data=wire();data[0]['100stk_ord_alow_amt']='1';data[0]['ord_alow_amt']='1'
    add_capture(http,data);quote(http)
    assert cmd.process(session)=='MARKET_PREPARED'
    with connect(cmd.path) as db:assert db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]==0
    assert cmd.notice_delivery('claim',scope='other',session='receiver') is None
    notice=cmd.notice_delivery('claim',scope='owner',session='receiver')
    assert '주문은 보내지 않았어요' in notice['text']
    cmd.notice_delivery('finish',scope='owner',session='receiver',key=notice['key'],delivered=False)
    assert cmd.notice_delivery('claim',scope='owner',session='receiver') is None
    assert mutations(http)==[]
    cmd.disarm();session.close()


def test_market_fill_ledger_and_chain_do_not_invent_limit_price(tmp_path):
    from app.kiwoom_mock_chain import project_chain
    from test_mock_order_delivery import refresh_fills
    cmd,session,http,_,_=market_rig(tmp_path)
    token=prepare(cmd,session,http)
    handle(cmd,'/모의확인 '+token,uid=3)
    add_capture(http);quote(http);http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    refresh_fills(cmd,session,http,'filled')
    with connect(cmd.path) as db:key=db.execute('SELECT id FROM intents').fetchone()[0]
    chain=project_chain(cmd.journal,cmd.ledger,key)
    assert chain.filled_qty==3 and chain.filled_value==2980
    assert not chain.ready_for_orders
    cmd.disarm();session.close()


def test_emergency_cancels_market_preparation_without_waiting_for_owner_io(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    handle(cmd,'/매수 900001 3주 시장가')
    assert '긴급중지' in handle(cmd,'/긴급중지',uid=2)
    with connect(cmd.path) as db:assert db.execute('SELECT state FROM market_previews').fetchone()[0]=='CANCELED'
    assert cmd.process(session) is None and mutations(http)==[]
    cmd.disarm();session.close()
