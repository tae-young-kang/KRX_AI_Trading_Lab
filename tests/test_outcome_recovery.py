from datetime import datetime
import json
import pytest
from app.outcome_recovery import collect_missing,missing_plan
from app.krx_history import HistoryStore,DailyBatch
from app.backtest import Bar
from app.trading_clock import KST
from test_recommendation_reports import store

NOW=datetime(2026,10,13,12,tzinfo=KST)


def setup(tmp_path):
    db,_=store(tmp_path);history=HistoryStore(tmp_path/'history.db')
    with db.connect() as conn:
        conn.execute("UPDATE recommendation_batches SET mode='REAL_QUOTES',created_at='2026-10-08T10:00:00+09:00'")
        symbols=[tuple(r) for r in conn.execute('SELECT DISTINCT market,code FROM recommendations')]
    class Client:
        def __init__(self):self.calls=[]
        def fetch_day(self,market,day):
            self.calls.append((market,day))
            bars=[Bar(day,code,'시험',market,100,110,90,105,1000,100,4000)
                  for m,code in symbols if m==market]
            return DailyBatch(bars,len(bars))
    return db,history,Client()


def test_missing_targets_only_are_fetched_and_reviews_are_corrected(tmp_path):
    db,history,client=setup(tmp_path);before=db.recommendation_history('REAL_QUOTES')
    result=collect_missing(db,history,client,'REAL_QUOTES',now=NOW)
    assert client.calls and all(day=='2026-10-12' for _,day in client.calls)
    assert len(client.calls)==len(set(client.calls))
    assert result['states']['OBSERVED']==3 and result['states']['NOT_DUE']==6
    assert result['collection']['requests']==len(client.calls)
    assert db.recommendation_history('REAL_QUOTES')==before
    repeat=collect_missing(db,history,client,'REAL_QUOTES',now=NOW)
    assert repeat['collection']['requests']==0 and repeat['added']==0
    assert missing_plan(db,'MOCK','2026-10-12',today=NOW.date())['targets']==[]


def test_empty_response_is_not_successful_price_evidence(tmp_path):
    db,history,client=setup(tmp_path)
    client.fetch_day=lambda market,day:DailyBatch([],0)
    result=collect_missing(db,history,client,'REAL_QUOTES',now=NOW)
    assert result['states']['PRICE_MISSING']==3
    assert result['collection']['rows']==0
    assert history.count('KRX')[0]==0


def test_cancel_keeps_collected_daily_data_and_does_not_claim_completion(tmp_path):
    db,history,client=setup(tmp_path)
    with pytest.raises(ValueError,match='중지'):
        collect_missing(db,history,client,'REAL_QUOTES',now=NOW,cancelled=lambda:bool(client.calls))
    assert history.count('KRX')[0]>0
    with pytest.raises(ValueError):
        collect_missing(db,history,client,'REAL_QUOTES',now=NOW,source='CSV_USER')


def test_future_missing_target_is_rejected_before_network(tmp_path):
    db,history,client=setup(tmp_path)
    from app.recommendation_cache import evaluate_requested
    evaluate_requested(db,history,'REAL_QUOTES',today=NOW.date())
    with db.connect() as conn:
        row=conn.execute('SELECT id,results_json FROM recommendation_reviews LIMIT 1').fetchone()
        data=json.loads(row['results_json']);data[0]['target_day']='2026-10-20';data[0]['state']='PRICE_MISSING'
        conn.execute('UPDATE recommendation_reviews SET results_json=? WHERE id=?',(json.dumps(data),row['id']))
    with pytest.raises(ValueError,match='누락 날짜'):
        missing_plan(db,'REAL_QUOTES','2026-10-12',today=NOW.date())
    assert client.calls==[]


def test_gui_recovery_uses_only_separate_krx_key_and_injected_daily_client(tmp_path):
    from types import SimpleNamespace
    from test_recommendation_request import gui_method
    from app.outcome_request import OutcomeRequest
    db,history,client=setup(tmp_path)
    request=OutcomeRequest('REAL_QUOTES',wall_clock=lambda:NOW)
    request.request(collect=True);keys=[]
    fake=SimpleNamespace(db=db,history=history,cfg={},work=None,remote_scan_active=False,
        remote_scan=SimpleNamespace(take=lambda *args,**kwargs:False),remote_theme=None,
        remote_outcome=request,remote_outcome_active=False,
        creds=SimpleNamespace(get=lambda name:keys.append(name) or 'fixture-krx'),
        telegram_order_worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,
            service=SimpleNamespace(accepts_search_requests=lambda:True)))
    fake.remote_outcome_done=lambda result:gui_method('remote_outcome_done')(fake,result)
    fake.run_task=lambda fn,done,text,with_progress=False:done(fn(lambda text:None,lambda:False) if with_progress else fn())
    def factory(key):
        assert key=='fixture-krx'
        return client
    gui_method('poll_remote_scan',current_mode=lambda cfg:SimpleNamespace(key='REAL_QUOTES'),KRXDailyClient=factory)(fake)
    assert request.state=='DONE' and client.calls and keys==['krx_api_key']
    assert '자료 보완 조회' in request.summary()
