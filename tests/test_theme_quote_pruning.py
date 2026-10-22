"""Real collector/market wiring over fake HTTP; never uses a broker service."""
import pytest
from test_full_market import broker,response,quote
from test_theme_kiwoom import NOW,history_with_baseline
from test_kiwoom_daily import DAYS,row
from app.krx_history import HistoryStore
from app.theme_kiwoom import collect_theme


@pytest.mark.parametrize('case,expected',[
    ('known',['900001']),('unknown',['900001','900002']),('none',[])])
def test_prior_history_selects_quotes_before_http_without_losing_universe(broker,tmp_path,case,expected):
    broker.url='https://api.kiwoom.com'  # HTTP remains replaced by the fixture.
    history=HistoryStore(tmp_path/'history.db') if case=='unknown' else history_with_baseline(tmp_path)
    if case=='none':
        with history.connect() as db:db.execute('UPDATE daily_bars SET high=10400')
    calls=[];quoted=[];daily=[]
    def post(url,headers,json,timeout):
        api=headers['api-id'];calls.append(api)
        if api=='ka10099':
            codes={'0':['900001','900002'],'10':['999999'],'8':['999999']}[json['mrkt_tp']]
            return response({'list':[dict(code=c,name='합성'+c) for c in codes]})
        if api=='ka10095':
            codes=json['stk_cd'].split('|');quoted.extend(codes)
            return response({'atn_stk_infr':[dict(quote(c),dt='20261019') for c in codes]})
        assert api=='ka10081'
        daily.append(json['stk_cd'])
        return response({'stk_cd':json['stk_cd'],'stk_dt_pole_chart_qry':[row(d) for d in reversed(DAYS)]})
    broker.session.post.side_effect=post
    progress=[]
    result=collect_theme(history,broker,'REAL_QUOTES',now=NOW,progress=progress.append)
    assert quoted==expected and daily==expected
    assert result['search_scope']['universe_count']==2
    assert result['search_scope']['candidate_count']==len(expected)
    assert result['search_scope']['excluded_count']==2-len(expected)
    assert result['matched']==len(expected) and result['search_scope']['include_etf'] is False
    assert calls[:3]==['ka10099']*3
    assert all('999999'!=code for code in quoted)
    assert any('시세 조회 대상' in text for text in progress)
    if not expected:assert 'ka10095' not in calls and 'ka10081' not in calls


def test_invalid_quote_selector_is_rejected_before_quotes(broker):
    def post(url,headers,json,timeout):
        assert headers['api-id']=='ka10099'
        codes={'0':['900001'],'10':['900002'],'8':['999999']}[json['mrkt_tp']]
        return response({'list':[dict(code=c,name='합성') for c in codes]})
    broker.session.post.side_effect=post
    with pytest.raises(ValueError,match='전체 종목'):
        broker.scan_theme(lambda universe:{'missing'})

@pytest.mark.parametrize('damage',['missing','cancel'])
def test_selected_quote_failure_keeps_previous_detection(broker,tmp_path,damage):
    from app.theme_delivery import latest_snapshot
    from test_theme_kiwoom import Client
    history=history_with_baseline(tmp_path)
    prior=collect_theme(history,Client(),'REAL_QUOTES',now=NOW)
    broker.url='https://api.kiwoom.com';cancelled=[False];requests=[]
    def post(url,headers,json,timeout):
        requests.append(headers['api-id'])
        if headers['api-id']=='ka10099':
            market=json['mrkt_tp']
            codes={'0':['900001','900002'],'10':['999999'],'8':['999999']}[market]
            if market=='8' and damage=='cancel':cancelled[0]=True
            return response({'list':[dict(code=c,name='합성') for c in codes]})
        assert headers['api-id']=='ka10095' and damage=='missing'
        return response({'atn_stk_infr':[]})
    broker.session.post.side_effect=post
    with pytest.raises((ValueError,RuntimeError)):
        collect_theme(history,broker,'REAL_QUOTES',now=NOW,cancelled=lambda:cancelled[0])
    assert latest_snapshot(history,'REAL_QUOTES')['run_id']==prior['run_id']
    assert 'ka10081' not in requests
    if damage=='cancel':assert 'ka10095' not in requests
