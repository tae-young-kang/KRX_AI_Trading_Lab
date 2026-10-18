from concurrent.futures import ThreadPoolExecutor
import pytest
from app.backtest import Bar
from app.krx_history import HistoryStore
from app.recommendation_outcomes import SessionEvidence
from app.theme_store import prepare
from app.theme_delivery import deliver_one


def setup(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    days=('2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19')
    history.insert_bars([Bar(d,'900001','연습','KOSPI',100,110,99,100,1000,100,4000) for d in days],'DEMO')
    args=dict(sessions=SessionEvidence(days,days[0],days[-1],'DEMO'),through=days[-1],mode='DEMO',source='DEMO')
    prepare(history,**args)
    return history,args


def test_acknowledged_send_is_single_attempt_across_reopen_and_concurrency(tmp_path):
    history,_=setup(tmp_path)
    calls=[]
    def attempt(_):
        return deliver_one(HistoryStore(history.path),alert_id=1,mode='DEMO',source='DEMO',destination='a'*64,
                           send=lambda text:calls.append(text) or 123)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt,range(2)))
    assert len(calls)==1 and 'SENT' in results
    assert attempt(None)=='SENT' and len(calls)==1


@pytest.mark.parametrize('receipt',[None,False,0,-1])
def test_missing_receipt_never_claims_success_or_retries(tmp_path,receipt):
    history,_=setup(tmp_path);calls=[]
    kwargs=dict(alert_id=1,mode='DEMO',source='DEMO',destination='a'*64,
                send=lambda text:calls.append(text) or receipt)
    assert deliver_one(history,**kwargs)=='UNKNOWN'
    assert deliver_one(history,**kwargs)=='UNKNOWN' and len(calls)==1


def test_crash_leaves_unknown_and_stale_match_is_not_sent(tmp_path):
    history,args=setup(tmp_path)
    def crash(text): raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        deliver_one(history,alert_id=1,mode='DEMO',source='DEMO',destination='a'*64,send=crash)
    calls=[]
    assert deliver_one(history,alert_id=1,mode='DEMO',source='DEMO',destination='a'*64,send=lambda t:calls.append(t))=='UNKNOWN'
    with history.connect() as db:
        db.execute("UPDATE daily_bars SET trade_value_eok=1 WHERE source='DEMO'")
    prepare(history,**args)
    with pytest.raises(ValueError,match='최신'):
        deliver_one(history,alert_id=1,mode='DEMO',source='DEMO',destination='b'*64,send=lambda t:calls.append(t))
    assert calls==[]


def test_scope_and_status_hide_raw_recipient_and_separate_destinations(tmp_path):
    from app.theme_delivery import destination_scope,delivery_summary
    history,_=setup(tmp_path)
    scope=destination_scope(123,456)
    assert len(scope)==64 and scope!=destination_scope(123,457)
    kwargs=dict(mode='DEMO',source='DEMO',destination=scope)
    assert '미시도 1개' in delivery_summary(history,**kwargs)
    deliver_one(history,alert_id=1,send=lambda text:9,**kwargs)
    assert '발송 응답 확인 1개' in delivery_summary(history,**kwargs)
    other=delivery_summary(history,mode='DEMO',source='DEMO',destination=destination_scope(123,457))
    assert '미시도 1개' in other and '456' not in other


def test_explicit_batch_binds_run_and_recipient_and_never_resends(tmp_path):
    from app.theme_delivery import latest_snapshot,deliver_run
    history,args=setup(tmp_path)
    class Transport:
        def __init__(self): self.calls=[]
        def identity(self): return 123
        def send(self,chat,text): self.calls.append((chat,text));return 9
    transport=Transport()
    snapshot=latest_snapshot(history,'DEMO')
    result=deliver_run(history,mode='DEMO',run_id=snapshot['run_id'],transport=transport,chat_id=456)
    assert result['SENT']==1 and transport.calls[0][0]==456
    deliver_run(history,mode='DEMO',run_id=snapshot['run_id'],transport=transport,chat_id=456)
    assert len(transport.calls)==1
    prepare(history,**args)
    with pytest.raises(ValueError,match='바뀌었어요'):
        deliver_run(history,mode='DEMO',run_id=snapshot['run_id'],transport=transport,chat_id=456)
    assert len(transport.calls)==1


def test_multi_stock_interruption_and_explicit_continue_skip_unknown(tmp_path):
    from app.theme_delivery import latest_snapshot,deliver_run
    history,args=setup(tmp_path)
    with history.connect() as db:
        original=db.execute('SELECT * FROM daily_bars').fetchall()
    for code in ('900002','900003'):
        history.insert_bars([Bar(r['day'],code,'연습'+code,'KOSPI',100,110,99,100,1000,100,4000) for r in original],'DEMO')
    prepare(history,**args)
    run_id=latest_snapshot(history,'DEMO')['run_id']
    class Transport:
        def __init__(self): self.calls=[]
        def identity(self): return 123
        def send(self,chat,text):
            self.calls.append(text)
            if len(self.calls)==2: raise TimeoutError('fake response lost')
            return len(self.calls)
    transport=Transport()
    kwargs=dict(mode='DEMO',run_id=run_id,transport=transport,chat_id=456)
    first=deliver_run(history,**kwargs)
    assert first==dict(SENT=1,UNKNOWN=1,remaining=1) and len(transport.calls)==2
    reopened=HistoryStore(history.path)
    assert deliver_run(reopened,**kwargs)==first and len(transport.calls)==2
    final=deliver_run(reopened,continue_unattempted=True,**kwargs)
    assert final==dict(SENT=2,UNKNOWN=1,remaining=0) and len(transport.calls)==3
    assert '900003' in transport.calls[-1]
    assert deliver_run(reopened,continue_unattempted=True,**kwargs)==final and len(transport.calls)==3


def test_stop_before_next_alert_leaves_it_unattempted(tmp_path):
    from app.theme_delivery import latest_snapshot,deliver_run
    history,_=setup(tmp_path)
    class Transport:
        def identity(self): return 123
        def send(self,*args): raise AssertionError('must not send')
    result=deliver_run(history,mode='DEMO',run_id=latest_snapshot(history,'DEMO')['run_id'],
                       transport=Transport(),chat_id=456,stopped=lambda:True)
    assert result==dict(SENT=0,UNKNOWN=0,remaining=1)
