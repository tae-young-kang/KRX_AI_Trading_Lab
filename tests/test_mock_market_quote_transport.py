"""Fake HTTP only. No GUI, credential store, broker login or order endpoint."""
import pytest
from app.kiwoom_mock_contract import ContractError, MockPlan, market_quote_plan
from app.kiwoom_mock_transport import validated_plan
from app.mock_market_budget import preview_buy
from test_kiwoom_mock_dispatch import transport, Response
from test_mock_market_quote import fixture
from test_mock_market_budget import inputs


def test_authenticated_owner_reads_quote_and_feeds_budget_without_orders():
    client,http,_=transport()
    payload,headers=fixture()
    http.responses.append(Response(payload,headers=headers))
    quote,upper=client.read_market_quote('900001')
    url,options=http.calls[-1]
    assert url=='https://mockapi.kiwoom.com/api/dostk/stkinfo'
    assert options['json']=={'stk_cd':'900001'}
    assert options['headers']['api-id']=='ka10095'
    capture,_,_,args=inputs()
    args['auth_generation']=client.auth_generation
    from dataclasses import replace
    capture=replace(capture,auth_generation=client.auth_generation)
    result=preview_buy(capture,quote,upper,quantity=10,**args)
    assert result['reserved_won']==13010 and not result['executable']
    with pytest.raises(ContractError):client.exchange(result['contract_draft'])
    assert len(http.calls)==2 and all('/ordr' not in url for url,_ in http.calls)


@pytest.mark.parametrize('change',['auth','profile','stop','expiry','mode'])
def test_connection_changes_during_read_discard_response(change):
    client,http,_=transport()
    payload,headers=fixture()
    http.responses.append(Response(payload,headers=headers))
    original=http.post
    def post(url,**options):
        response=original(url,**options)
        if change=='auth':client._auth_generation='c'*32
        elif change=='profile':client.profile_id='c'*32
        elif change=='stop':client.cancelled=lambda:True
        elif change=='expiry':client._expires=client._clock()
        elif change=='mode':client.environment='REAL'
        return response
    http.post=post
    with pytest.raises(ContractError):client.read_market_quote('900001')
    assert len(http.calls)==2


@pytest.mark.parametrize('fields',[(('stk_cd','900001_NX'),),
    (('stk_cd','900001'),('extra','1')),
    (('stk_cd','900001'),('stk_cd','900001'))])
def test_handcrafted_quote_fields_are_not_sent(fields):
    client,http,_=transport()
    with pytest.raises(ContractError):client.exchange(MockPlan('ka10095',fields))
    assert len(http.calls)==1


def test_quote_cursor_and_missing_auth_are_rejected_before_network():
    client,http,_=transport()
    plan=market_quote_plan('900001')
    assert validated_plan(plan)==plan
    with pytest.raises(ContractError):client.exchange(plan,cursor='synthetic')
    client.close()
    with pytest.raises(ContractError):client.read_market_quote('900001')
    assert len(http.calls)==1


def test_verified_session_refreshes_account_then_reads_quote_for_preview():
    from test_kiwoom_mock_watch import setup
    from test_kiwoom_mock_review import wire, ACCOUNT
    session,client,http,socket,_=setup()
    with pytest.raises(ContractError):
        session.preview_market_buy(code='900001',name='합성',market='KOSDAQ',fee_bps=2,quantity=10)
    session.verify(ACCOUNT)
    identity=dict(return_code=0,acctNo=ACCOUNT)
    http.responses.append(Response(identity))
    session.start()
    socket.frames.extend([dict(trnm='LOGIN',return_code=0),dict(trnm='REG',return_code=0)])
    session.poll(); session.poll()
    rows=[identity]+wire()+wire()+[identity]
    http.responses.extend(Response(row,headers={'cont-yn':'N'}) for row in rows)
    payload,headers=fixture()
    http.responses.append(Response(payload,headers=headers))
    result=session.preview_market_buy(code='900001',name='합성',market='KOSDAQ',fee_bps=2,quantity=10)
    assert result['reserved_won']==13010 and result['available_won']==97010
    assert result['targets'][0]['quantity']==10
    assert not result['executable'] and not session.ready_for_orders
    assert all('/ordr' not in url for url,_ in http.calls)
    session.close()
