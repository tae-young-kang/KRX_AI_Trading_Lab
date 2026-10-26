from dataclasses import replace
import json
import re
import pytest
from app.mock_market_budget import preview_sell
from app.kiwoom_mock_contract import ContractError
from app.order_engine import connect
from test_mock_market_budget import inputs
from test_mock_market_intents import market_rig,quote
from test_mock_telegram_orders import handle,add_capture,mutations
from test_kiwoom_mock_dispatch import Response,accepted
from test_kiwoom_mock_review import wire


def sell_prepare(cmd,session,http,text='/매도 900001 2주 시장가',data=None):
    assert '아직 주문하지' in handle(cmd,text)
    add_capture(http,data);quote(http)
    assert cmd.process(session)=='MARKET_PREPARED'
    preview=handle(cmd,'/주문상태',uid=2)
    assert '시장가 매도' in preview and '매도 가능 2주' in preview,preview
    return re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]


def test_sell_reads_sellable_shares_even_with_zero_buying_power_then_confirms_and_dispatches(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    data=wire();data[0]['ord_alow_amt']='0';data[0]['100stk_ord_alow_amt']='0'
    token=sell_prepare(cmd,session,http,data=data)
    assert mutations(http)==[]
    handle(cmd,'/모의확인 '+token,uid=3)
    add_capture(http,data);quote(http);http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert mutations(http)[0]['headers']['api-id']=='kt10001'
    assert mutations(http)[0]['json']==dict(dmst_stex_tp='KRX',stk_cd='900001',ord_qty='2',trde_tp='3')
    assert cmd.process(session) is None and len(mutations(http))==1
    cmd.disarm();session.close()


def test_sell_amount_floors_at_quote_and_never_recalculates_after_confirmation(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    token=sell_prepare(cmd,session,http,'/매도 시장가 예제 2500원 시장가')
    with connect(cmd.path) as db:payload=json.loads(db.execute('SELECT payload FROM intents').fetchone()[0])
    assert payload['quantity']==2 and payload['requested_won']==2500 and payload['market_reserved_won']==0
    assert 'market_fee_bps' not in payload
    handle(cmd,'/모의확인 '+token,uid=3)
    add_capture(http);quote(http)
    http.responses[-1].payload['atn_stk_infr'][0]['cur_prc']='800'
    http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert mutations(http)[0]['json']['ord_qty']=='2'
    cmd.disarm();session.close()


def test_reduced_sellable_shares_after_confirmation_blocks_sale(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    token=sell_prepare(cmd,session,http)
    handle(cmd,'/모의확인 '+token,uid=3)
    data=wire();data[1]['acnt_evlt_remn_indv_tot'][0]['trde_able_qty']='1'
    add_capture(http,data)
    assert cmd.process(session)=='FAILED' and mutations(http)==[]
    cmd.disarm();session.close()


def test_held_shares_cannot_substitute_for_sellable_quantity(tmp_path):
    cmd,session,http,_,_=market_rig(tmp_path)
    handle(cmd,'/매도 900001 3주 시장가')
    add_capture(http);quote(http)
    assert cmd.process(session)=='MARKET_PREPARED'
    with connect(cmd.path) as db:assert db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]==0
    notice=cmd.notice_delivery('claim',scope='owner',session='receiver')
    assert '매도 가능 수량' in notice['text'] and mutations(http)==[]
    cmd.disarm();session.close()


def test_sell_preview_evidence_guards_and_subshare_amount():
    capture,quote,upper,args=inputs();args.pop('fee_bps')
    capture=replace(capture,observations=replace(capture.observations,holdings=(('900001',3,2),)))
    result=preview_sell(capture,quote,upper,quantity=2,**args)
    assert result['state']=='PREVIEW_ONLY' and result['estimated_gross_won']==2000
    assert result['contract_draft'].api_id=='kt10001' and not result['executable']
    assert preview_sell(capture,quote,upper,quantity=3,**args)['contract_draft'] is None
    assert preview_sell(capture,quote,upper,budget_won=999,**args)['state']=='BELOW_ONE_SHARE'
    with pytest.raises(ContractError):preview_sell(capture,replace(quote,mode='REAL_QUOTES'),upper,quantity=1,**args)
    with pytest.raises(ContractError):preview_sell(replace(capture,profile_id='c'*32),quote,upper,quantity=1,**args)
    with pytest.raises(ContractError):preview_sell(capture,quote,upper,quantity=True,**args)
