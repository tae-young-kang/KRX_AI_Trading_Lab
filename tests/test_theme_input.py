import json
from datetime import date
import pytest
from app.theme_input import load_selection,prepare_selection
from app.krx_history import HistoryStore


def selection():
    days=['2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19']
    return dict(version=1,mode='REAL_QUOTES',source='KRX',through=days[-1],
                calendar=dict(days=days,begin=days[0],end=days[-1],source='USER_CONFIRMED'))


def test_explicit_selection_empty_cache_is_not_full_market_result(tmp_path):
    path=tmp_path/'selection.json';path.write_text(json.dumps(selection()))
    result=prepare_selection(HistoryStore(tmp_path/'hist.db'),path,'REAL_QUOTES',today=date(2026,10,19))
    assert result['symbols']==0 and len(result['missing_market_days'])==14
    assert result['coverage']=='CACHED_SYMBOLS_ONLY'


@pytest.mark.parametrize('change',[{'mode':'MOCK'},{'source':'DEMO'},{'through':'2026-10-18'},{'version':True}])
def test_invalid_selection_rejected_before_preparing(tmp_path,change):
    data=selection();data.update(change)
    path=tmp_path/'selection.json';path.write_text(json.dumps(data))
    with pytest.raises(ValueError): load_selection(path,'REAL_QUOTES',today=date(2026,10,19))


def test_user_file_cannot_claim_official_calendar_or_future_completion(tmp_path):
    data=selection();path=tmp_path/'selection.json';path.write_text(json.dumps(data))
    with pytest.raises(ValueError): load_selection(path,'REAL_QUOTES',today=date(2026,10,18))
    data['calendar']['source']='OFFICIAL';path.write_text(json.dumps(data))
    with pytest.raises(ValueError): load_selection(path,'REAL_QUOTES',today=date(2026,10,19))


def test_guided_dates_match_file_path_and_prepare_real_detector(tmp_path):
    from app.backtest import Bar
    from app.theme_input import selection_from_days, prepare_days
    data=selection()
    days=data['calendar']['days']
    text=',\n'.join(days)
    parsed,sessions=selection_from_days(text,'REAL_QUOTES','KRX',today=date(2026,10,19))
    assert parsed==data and sessions.days==tuple(days)
    history=HistoryStore(tmp_path/'history.db')
    history.insert_bars([Bar(d,'900001','합성종목','KOSPI',100,110,99,100,1000,100,4000) for d in days],'KRX')
    result=prepare_days(history,text,'REAL_QUOTES','KRX',today=date(2026,10,19))
    assert result['matched']==1 and result['prepared']==1
    assert len(result['missing_market_days'])==7
    path=tmp_path/'selection.json';path.write_text(json.dumps(data))
    same=prepare_selection(history,path,'REAL_QUOTES',today=date(2026,10,19))
    assert same['prepared']==0 and same['results']==result['results']
    from app.theme_store import latest_report
    assert '합성종목' in latest_report(history,mode='REAL_QUOTES')


@pytest.mark.parametrize('change', ['empty','short','duplicate','reverse','invalid','future','wrong_source'])
def test_guided_dates_reject_invalid_evidence(change):
    from app.theme_input import selection_from_days
    days=selection()['calendar']['days']
    source='KRX'
    if change=='empty':days=[]
    elif change=='short':days=days[1:]
    elif change=='duplicate':days=days+[days[-1]]
    elif change=='reverse':days=list(reversed(days))
    elif change=='invalid':days[2]='not-a-date'
    elif change=='future':days[-1]='2026-10-20'
    elif change=='wrong_source':source='DEMO'
    with pytest.raises(ValueError):
        selection_from_days('\n'.join(days),'REAL_QUOTES',source,today=date(2026,10,19))
