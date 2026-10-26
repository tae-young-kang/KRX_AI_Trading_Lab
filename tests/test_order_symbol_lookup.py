from datetime import timedelta
import json
import re
import pytest
from app.order_symbol_lookup import saved_symbol
from app.krx_history import HistoryStore
from app.backtest import Bar
from app.theme_sources import KIWOOM_SOURCES
from app.order_engine import connect
from app.mock_watch_status import MockWatchStatus
from test_mock_telegram_orders import rig,handle,add_capture,mutations
from test_kiwoom_mock_dispatch import NOW,accepted


def history_with_names(tmp_path,mode='MOCK',stamp=None):
    history=HistoryStore(tmp_path/'history.db')
    history.insert_bars([Bar(NOW.date().isoformat(),'900001','예제 종목','KOSPI',1000,1100,990,1000,100,100,4000)],KIWOOM_SOURCES[mode])
    with history.connect() as db:db.execute('UPDATE daily_bars SET fetched_at=?',((stamp or NOW).isoformat(),))
    return history


def test_name_amount_policy_confirmation_freezes_code_then_fake_dispatch(tmp_path):
    history=history_with_names(tmp_path)
    cmd,session,http,_,_=rig(tmp_path)
    cmd.symbol_lookup=lambda name,now:saved_symbol(history,'MOCK',name,now)
    preview=handle(cmd,'/매수 예제 종목 3000원 1000 익절 2 손절 3')
    assert '예제 종목 (900001)' in preview and '매수 3주' in preview
    with connect(cmd.path) as db:payload=json.loads(db.execute('SELECT payload FROM intents').fetchone()[0])
    assert payload['requested_symbol']==dict(code='900001',name='예제 종목',market='KOSPI')
    assert payload['code']=='900001' and payload['requested_won']==3000
    assert payload['take_bps']==200 and payload['stop_bps']==300
    cmd.symbol_lookup=lambda *args:pytest.fail('confirmation re-resolved name')
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    handle(cmd,'/모의확인 '+token,uid=2)
    add_capture(http);http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert len(mutations(http))==1
    assert mutations(http)[0]['json']['stk_cd']=='900001'
    cmd.disarm();session.close()


@pytest.mark.parametrize('case',['old','future','wrong_mode','missing','ambiguous'])
def test_uncertain_names_do_not_create_order_intents(tmp_path,case):
    history=history_with_names(tmp_path,mode='REAL_QUOTES' if case=='wrong_mode' else 'MOCK',
        stamp=NOW-timedelta(days=1) if case=='old' else NOW+timedelta(seconds=1) if case=='future' else NOW)
    if case=='ambiguous':
        with history.connect() as db:
            db.execute("INSERT INTO daily_bars SELECT source,day,market,'900002',name,open,high,low,close,volume,trade_value_eok,cap_eok,fetched_at FROM daily_bars")
    cmd,session,http,_,_=rig(tmp_path)
    cmd.symbol_lookup=lambda name,now:saved_symbol(history,'MOCK',name,now)
    text=handle(cmd,'/매수 '+('다른 종목' if case=='missing' else '예제 종목')+' 3주 1000')
    assert '종목코드' in text and '/모의확인' not in text
    with connect(cmd.path) as db:assert db.execute('SELECT COUNT(*) FROM intents').fetchone()[0]==0
    assert mutations(http)==[]
    cmd.disarm();session.close()


def test_status_binding_enables_name_resolution_and_disarmed_gate_stays_closed(tmp_path):
    history=history_with_names(tmp_path)
    cmd,session,http,_,_=rig(tmp_path)
    status=MockWatchStatus(clock=lambda:NOW,symbol_lookup=lambda name,now:saved_symbol(history,'MOCK',name,now))
    status.bind_orders(cmd)
    preview=status.handle_orders('/매수 예제 종목 3주 1000',scope='owner',session='receiver',uid=1,now=NOW.timestamp())
    assert '예제 종목 (900001)' in preview
    cmd.disarm()
    assert '잠겨 있거나' in status.handle_orders('/매수 예제 종목 3주 1000',scope='owner',session='receiver',uid=2,now=NOW.timestamp())
    assert mutations(http)==[]
    session.close()


def test_name_sell_preview_keeps_quantity_and_requires_confirmation(tmp_path):
    history=history_with_names(tmp_path)
    cmd,session,http,_,_=rig(tmp_path)
    cmd.symbol_lookup=lambda name,now:saved_symbol(history,'MOCK',name,now)
    preview=handle(cmd,'/매도 예제 종목 2주 1000')
    assert '예제 종목 (900001) 매도 2주' in preview and '/모의확인' in preview
    with connect(cmd.path) as db:
        row=db.execute('SELECT state,payload FROM intents').fetchone()
        assert row['state']=='WAITING' and json.loads(row['payload'])['side']=='SELL'
    assert mutations(http)==[]
    cmd.disarm();session.close()
