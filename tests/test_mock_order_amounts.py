"""Amount input uses the existing limit queue and confirmation, fake IO only."""
import json
import re
import pytest
from app.order_engine import connect
from test_mock_telegram_orders import rig,handle,add_capture,mutations
from test_kiwoom_mock_dispatch import accepted


@pytest.mark.parametrize('side,amount,price,quantity',[
    ('매수','1만원','3000',3),('매수','10,000원','3,000',3),
    ('매도','2,500원','1000',2)])
def test_amount_is_floored_frozen_confirmed_and_sent_as_fixed_quantity(tmp_path,side,amount,price,quantity):
    cmd,session,http,_,_=rig(tmp_path)
    preview=handle(cmd,f'/{side} 900001 {amount} {price}')
    assert f'{quantity}주 (소수점 버림)' in preview and '수수료·세금 별도' in preview
    with connect(cmd.path) as db:
        payload=json.loads(db.execute('SELECT payload FROM intents').fetchone()[0])
    assert payload['quantity']==quantity and payload['requested_won'] in (10000,2500)
    assert not mutations(http)
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    assert '전송을 요청' in handle(cmd,'/모의확인 '+token,uid=2)
    add_capture(http);http.responses.append(accepted())
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert mutations(http)[0]['json']['ord_qty']==str(quantity)
    assert mutations(http)[0]['json']['trde_tp']=='0'
    assert cmd.process(session) is None and len(mutations(http))==1
    cmd.disarm();session.close()


@pytest.mark.parametrize('number,price,reason',[
    ('100원','1000','1주'),('1,00원','1000','쉼표'),
    ('11주','1000','한도'),('100만원','1000','한도'),
    ('1.5만원','1000','모의 주문 예'),('0원','1000','모의 주문 예')])
def test_invalid_or_over_limit_amount_does_not_create_intent(tmp_path,number,price,reason):
    cmd,session,http,_,_=rig(tmp_path)
    assert reason in handle(cmd,f'/매수 900001 {number} {price}')
    with connect(cmd.path) as db:assert db.execute('SELECT count(*) FROM intents').fetchone()[0]==0
    assert not mutations(http)
    cmd.disarm();session.close()


@pytest.mark.parametrize('quantity',['3','3주'])
def test_legacy_quantity_and_explicit_shares_keep_existing_confirmation(tmp_path,quantity):
    cmd,session,http,_,_=rig(tmp_path)
    reply=handle(cmd,f'/매수 900001 {quantity} 1000')
    assert '매수 3주' in reply and '/모의확인' in reply
    assert '요청 금액' not in reply
    assert not mutations(http)
    cmd.disarm();session.close()
