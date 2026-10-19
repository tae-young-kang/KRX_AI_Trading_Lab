"""Full Theme1 query-plan -> read-only daily -> saved alerts -> fake Telegram."""
from datetime import datetime
from app.krx_history import HistoryStore
from app.market import Stock
from app.backtest import Bar
from app.theme_kiwoom import collect_theme
from app.theme_request import ThemeRequest
from app.theme_sources import KIWOOM_SOURCES
from app.trading_clock import KST
from test_kiwoom_daily import DAYS,row,page

NOW=datetime(2026,10,19,21,tzinfo=KST)


class Client:
    url='https://api.kiwoom.com'
    def __init__(self):self.calls=[]
    def scan(self,market,**kwargs):
        assert market=='전체' and kwargs['include_etf'] is False
        return [Stock(code,'합성'+code,'KOSPI',10000,1,200,None,4000,source='KIWOOM',
                      quote_stamp='2026-10-19T15:30:00') for code in ('900001','900002')]
    def query(self,api,path,body,*args):
        assert api=='ka10081' and path=='/api/dostk/chart'
        self.calls.append(body['stk_cd'])
        data,headers=page([row(d) for d in reversed(DAYS)])
        data['stk_cd']=body['stk_cd']
        return data,headers


def history_with_baseline(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    bars=[]
    for code in ('900001','900002'):
        for day in DAYS[:-1]:
            bars.append(Bar(day,code,'합성','KOSPI',10000,10500 if code=='900001' else 10400,9900,10000,1000,100,4000))
    history.insert_bars(bars,'KRX')
    return history


def test_entire_pipeline_prunes_impossible_symbols_and_delivers_once(tmp_path):
    history=history_with_baseline(tmp_path);client=Client()
    before=history.count('KRX')
    result=collect_theme(history,client,'REAL_QUOTES',now=NOW)
    assert client.calls==['900001'] and result['matched']==1
    scope=result['search_scope']
    assert scope['universe_count']==2 and scope['excluded_count']==1 and scope['missing_symbols']==0
    assert result['missing_market_days']==[] and history.count('KRX')==before
    assert result['results'][0]['cap_eok']==4000
    assert history.count(KIWOOM_SOURCES['REAL_QUOTES'])[0]==7
    request=ThemeRequest(history,'REAL_QUOTES',wall_clock=lambda:NOW)
    request.request();data=request.take('REAL_QUOTES',busy=False,active=True)
    assert data['source']==KIWOOM_SOURCES['REAL_QUOTES']
    request.finish(success=True,result=result)
    class Transport:
        def __init__(self):self.messages=[]
        def identity(self):return 33
        def send(self,chat,text):self.messages.append(text);return len(self.messages)
    transport=Transport()
    request.send_notice(transport,22,active=True)
    request.send_notice(transport,22,active=True)
    assert len(transport.messages)==2 and '[테마1 · 종목 감지]' in transport.messages[1]
    assert '900001' in transport.messages[1] and '응답 확인 1개' in request.summary()


def test_unknown_history_is_queried_and_bad_server_is_blocked(tmp_path):
    import pytest
    history=HistoryStore(tmp_path/'history.db');client=Client()
    result=collect_theme(history,client,'REAL_QUOTES',now=NOW)
    assert client.calls==['900001','900002'] and result['matched']==2
    assert result['search_scope']['unknown_count']==2
    with pytest.raises(ValueError):collect_theme(history,client,'MOCK',now=NOW)


def test_no_possible_candidates_still_saves_clear_empty_result(tmp_path):
    history=history_with_baseline(tmp_path)
    with history.connect() as db:db.execute('UPDATE daily_bars SET high=10400')
    client=Client()
    result=collect_theme(history,client,'REAL_QUOTES',now=NOW)
    assert client.calls==[] and result['matched']==0 and result['missing_market_days']==[]
    assert result['search_scope']['excluded_count']==2


def test_gui_request_path_uses_collector_without_constructing_gui(tmp_path):
    from types import SimpleNamespace
    from test_recommendation_request import gui_method
    from app.recommendation_request import RecommendationRequest
    history=history_with_baseline(tmp_path);client=Client();closed=[]
    client.session=SimpleNamespace(close=lambda:closed.append(1))
    request=ThemeRequest(history,'REAL_QUOTES',wall_clock=lambda:NOW)
    request.request()
    fake=SimpleNamespace(remote_scan=RecommendationRequest('REAL_QUOTES'),remote_scan_active=False,
        remote_theme=request,remote_theme_active=False,work=None,cfg={},history=history,quote_batch_profile=None,
        broker_keys=lambda env:('fixture-key','fixture-secret'),
        telegram_order_worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,
            service=SimpleNamespace(accepts_search_requests=lambda:True)),
        scan=lambda **kwargs:None)
    fake.remote_theme_done=lambda result:gui_method('remote_theme_done')(fake,result)
    fake.run_task=lambda fn,ok,text,with_progress=False:ok(fn(lambda message:None,lambda:False))
    gui_method('poll_remote_scan',current_mode=lambda cfg:SimpleNamespace(key='REAL_QUOTES'),
        KIWOOM_SOURCES=KIWOOM_SOURCES,KiwoomMarket=lambda *args,**kwargs:client,
        collect_kiwoom_theme=lambda *args,**kwargs:collect_theme(*args,now=NOW,**kwargs))(fake)
    assert request.state=='DONE' and closed==[1] and client.calls==['900001']


def test_cancel_or_bad_response_does_not_replace_previous_data(tmp_path):
    import pytest
    history=history_with_baseline(tmp_path);client=Client()
    first=collect_theme(history,client,'REAL_QUOTES',now=NOW)
    count=history.count(KIWOOM_SOURCES['REAL_QUOTES'])
    with pytest.raises(ValueError):
        collect_theme(history,Client(),'REAL_QUOTES',now=NOW,cancelled=lambda:True)
    assert history.count(KIWOOM_SOURCES['REAL_QUOTES'])==count
    from app.theme_delivery import latest_snapshot
    assert latest_snapshot(history,'REAL_QUOTES')['run_id']==first['run_id']


def test_new_request_upgrades_old_krx_selection_to_current_kiwoom_path(tmp_path):
    from app.theme_calendar import request_selection
    from app.theme_input import prepare_confirmed
    history=HistoryStore(tmp_path/'history.db')
    prepare_confirmed(history,request_selection('REAL_QUOTES','KRX',now=NOW),'REAL_QUOTES')
    request=ThemeRequest(history,'REAL_QUOTES',wall_clock=lambda:NOW)
    request.request()
    assert request.take('REAL_QUOTES',busy=False,active=True)['source']==KIWOOM_SOURCES['REAL_QUOTES']


def test_unknown_send_outcome_is_not_automatically_repeated(tmp_path):
    history=history_with_baseline(tmp_path)
    result=collect_theme(history,Client(),'REAL_QUOTES',now=NOW)
    request=ThemeRequest(history,'REAL_QUOTES',wall_clock=lambda:NOW)
    request.request();request.take('REAL_QUOTES',busy=False,active=True);request.finish(success=True,result=result)
    class Transport:
        def __init__(self):self.calls=0
        def send(self,chat,text):self.calls+=1;raise RuntimeError('synthetic uncertain send')
    transport=Transport()
    request.send_notice(transport,22,active=True)
    request.send_notice(transport,22,active=True)
    assert transport.calls==1 and '응답 미확인' in request.summary()


def test_etf_switch_reaches_full_universe_query(tmp_path):
    class EtfClient(Client):
        def scan(self,market,**kwargs):
            assert kwargs['include_etf'] is True
            kwargs['include_etf']=False
            return super().scan(market,**kwargs)
    result=collect_theme(HistoryStore(tmp_path/'history.db'),EtfClient(),'REAL_QUOTES',now=NOW,include_etf=True)
    assert result['search_scope']['include_etf'] is True and result['matched']==2


def test_pc_today_search_routes_to_same_pipeline_without_gui(tmp_path):
    from types import SimpleNamespace
    from test_recommendation_request import gui_method
    history=history_with_baseline(tmp_path);client=Client();results=[]
    client.session=SimpleNamespace(close=lambda:None)
    fake=SimpleNamespace(cfg={},history=history,quote_batch_profile=None,
        broker_keys=lambda env:('fixture-key','fixture-secret'),theme_prepared=results.append)
    fake.run_task=lambda fn,ok,text,with_progress=False:ok(fn(lambda message:None,lambda:False))
    gui_method('prepare_theme_guided',current_mode=lambda cfg:SimpleNamespace(key='REAL_QUOTES'),
        QInputDialog=SimpleNamespace(getItem=lambda *args,**kwargs:('키움 시세로 찾기 · 당일 조회',True)),
        QMessageBox=SimpleNamespace(question=lambda *args:1,StandardButton=SimpleNamespace(Yes=1)),
        datetime=SimpleNamespace(now=lambda tz:NOW),KST=KST,
        KiwoomMarket=lambda *args,**kwargs:client,collect_kiwoom_theme=collect_theme)(fake)
    assert len(results)==1 and results[0]['matched']==1
    assert client.calls==['900001']


def test_query_failure_and_mid_query_cancel_keep_previous_run(tmp_path):
    import pytest
    from app.theme_delivery import latest_snapshot
    history=history_with_baseline(tmp_path)
    previous=collect_theme(history,Client(),'REAL_QUOTES',now=NOW)
    before=history.count(KIWOOM_SOURCES['REAL_QUOTES'])
    class Broken(Client):
        def query(self,*args):raise RuntimeError('synthetic query failure')
    with pytest.raises(RuntimeError):collect_theme(history,Broken(),'REAL_QUOTES',now=NOW)
    client=Client()
    with pytest.raises(ValueError):
        collect_theme(history,client,'REAL_QUOTES',now=NOW,cancelled=lambda:len(client.calls)>0)
    assert history.count(KIWOOM_SOURCES['REAL_QUOTES'])==before
    assert latest_snapshot(history,'REAL_QUOTES')['run_id']==previous['run_id']


def test_mock_and_real_daily_caches_remain_separate(tmp_path):
    history=history_with_baseline(tmp_path)
    collect_theme(history,Client(),'REAL_QUOTES',now=NOW)
    mock=Client();mock.url='https://mockapi.kiwoom.com'
    collect_theme(history,mock,'MOCK',now=NOW)
    assert history.count(KIWOOM_SOURCES['REAL_QUOTES'])[0]==7
    assert history.count(KIWOOM_SOURCES['MOCK'])[0]==7
    from app.theme_delivery import latest_snapshot
    assert latest_snapshot(history,'REAL_QUOTES')['source']!=latest_snapshot(history,'MOCK')['source']


def test_invalid_symbol_does_not_block_valid_alert(tmp_path):
    class Partial(Client):
        def query(self,*args):
            data,headers=super().query(*args)
            if data['stk_cd']=='900002':
                data['stk_dt_pole_chart_qry'][0]['high_pric']='invalid'
            return data,headers
    history=HistoryStore(tmp_path/'history.db')
    result=collect_theme(history,Partial(),'REAL_QUOTES',now=NOW)
    assert result['matched']==1
    assert result['search_scope']['invalid_symbols']==1
    assert history.count(KIWOOM_SOURCES['REAL_QUOTES'])[0]==7
    request=ThemeRequest(history,'REAL_QUOTES',wall_clock=lambda:NOW)
    request.request();request.take('REAL_QUOTES',busy=False,active=True)
    request.finish(success=True,result=result)
    assert request.state=='PARTIAL'
    class Transport:
        def __init__(self):self.messages=[]
        def identity(self):return 33
        def send(self,chat,text):self.messages.append(text);return len(self.messages)
    transport=Transport()
    request.send_notice(transport,22,active=True)
    assert len(transport.messages)==2 and '900001' in transport.messages[-1]


def test_nxt_observation_after_request_is_checked_at_completion(tmp_path,monkeypatch):
    from dataclasses import replace
    from datetime import timedelta
    import app.theme_kiwoom as module
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW+timedelta(seconds=20)
    monkeypatch.setattr(module,'datetime',Clock)
    # Keep the request a datetime of the patched class for input validation.
    requested=Clock(2026,10,19,21,tzinfo=KST)
    class Observed(Client):
        def scan(self,*args,**kwargs):
            return [replace(s,nxt_source='KIWOOM_KA10099',nxt_eligible=True,
                nxt_observed_at=(requested+timedelta(seconds=5)).isoformat())
                for s in super().scan(*args,**kwargs)]
    result=collect_theme(HistoryStore(tmp_path/'history.db'),Observed(),'REAL_QUOTES',now=requested)
    assert result['matched']==2
