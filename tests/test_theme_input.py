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
