"""Injected daily client, synthetic rows and temporary DB: no external IO."""
from datetime import datetime
from copy import deepcopy
import pytest
from app.backtest import Bar
from app.krx_history import HistoryStore
from app.theme_collection import collect_and_prepare
from app.theme_input import saved_selection
from app.trading_clock import KST
from test_theme_input import selection


class Client:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def fetch_day(self,market,day):
        self.calls.append((market,day))
        if self.fail:raise RuntimeError('synthetic response failure')
        return [Bar(day,'900001' if market=='KOSPI' else '900002','합성',market,100,110,99,100,1000,100,4000)]


def test_collects_only_seven_confirmed_days_and_reuses_finalized_cache(tmp_path):
    history=HistoryStore(tmp_path/'history.db');client=Client();data=selection()
    progress=[]
    now=datetime(2026,10,20,10,tzinfo=KST)
    result=collect_and_prepare(history,client,data,'REAL_QUOTES',now=now,progress=progress.append)
    assert len(client.calls)==14
    assert set(client.calls)=={(m,d) for m in ('KOSPI','KOSDAQ') for d in data['calendar']['days']}
    assert result['collection']==dict(rows=14,requests=14,days=7,provisional=False)
    assert result['matched']==2 and result['missing_market_days']==[]
    assert saved_selection(history,'REAL_QUOTES')==data
    repeat=collect_and_prepare(history,client,data,'REAL_QUOTES',now=now)
    assert len(client.calls)==14 and repeat['collection']['requests']==0
    assert repeat['prepared']==0 and len(progress)==8


def test_same_day_is_provisional_and_refetched(tmp_path):
    history=HistoryStore(tmp_path/'history.db');client=Client();data=selection()
    now=datetime(2026,10,19,17,tzinfo=KST)
    first=collect_and_prepare(history,client,data,'REAL_QUOTES',now=now)
    second=collect_and_prepare(history,client,data,'REAL_QUOTES',now=now)
    assert first['collection']['provisional'] and second['collection']['requests']==2
    assert len(client.calls)==16


@pytest.mark.parametrize('kind',['cancel','failure','mode','source','future','weekend'])
def test_invalid_or_failed_collection_never_saves_new_selection(tmp_path,kind):
    history=HistoryStore(tmp_path/'history.db');client=Client(fail=kind=='failure');data=deepcopy(selection())
    mode='REAL_QUOTES'
    if kind=='mode':mode='DEMO'
    if kind=='source':data['source']='CSV_USER'
    if kind=='future':data['through']=data['calendar']['days'][-1]='2026-10-21';data['calendar']['end']='2026-10-21'
    if kind=='weekend':data['calendar']['days'][0]='2026-10-10';data['calendar']['begin']='2026-10-10'
    with pytest.raises((ValueError,RuntimeError)):
        collect_and_prepare(history,client,data,mode,now=datetime(2026,10,20,10,tzinfo=KST),
                            cancelled=(lambda:len(client.calls)>=2) if kind=='cancel' else None)
    with pytest.raises(ValueError):saved_selection(history,'REAL_QUOTES')
    assert len(client.calls)<=2
