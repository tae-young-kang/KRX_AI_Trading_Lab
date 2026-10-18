from datetime import datetime
from app.backtest import Bar
from app.krx_history import HistoryStore
from app.trading_clock import KST
import pytest


def bar(day,code='900001',price=100):
    return Bar(day,code,'연습','KOSPI',price,price+10,price-1,price,1000,100,4000)


class Client:
    def __init__(self,rows): self.rows=rows;self.calls=[]
    def fetch_day(self,market,day): self.calls.append((market,day));return self.rows


def test_same_day_is_refetched_after_restart_and_next_day_then_reused(tmp_path):
    history=HistoryStore(tmp_path/'history.db');day='2026-10-19'
    client=Client([bar(day)])
    history.fetch_range(client,day,day,markets=('KOSPI',),now=datetime(2026,10,19,10,tzinfo=KST))
    client.rows=[bar(day,price=110)]
    history=HistoryStore(history.path)
    history.fetch_range(client,day,day,markets=('KOSPI',),now=datetime(2026,10,19,18,tzinfo=KST))
    assert history.load_bars('KRX')[0].close==110 and len(client.calls)==2
    history.fetch_range(client,day,day,markets=('KOSPI',),now=datetime(2026,10,20,10,tzinfo=KST))
    history.fetch_range(client,day,day,markets=('KOSPI',),now=datetime(2026,10,20,11,tzinfo=KST))
    assert len(client.calls)==3


def test_empty_response_is_not_permanent_holiday_and_snapshot_replaces_missing_codes(tmp_path):
    history=HistoryStore(tmp_path/'history.db');day='2026-10-19';now=datetime(2026,10,19,18,tzinfo=KST)
    client=Client([])
    history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    client.rows=[bar(day),bar(day,'900002')]
    history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    client.rows=[bar(day)]
    history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    assert len(client.calls)==3 and len(history.load_bars('KRX'))==1


def test_mismatched_payload_does_not_replace_previous_cache(tmp_path):
    history=HistoryStore(tmp_path/'history.db');day='2026-10-19';now=datetime(2026,10,19,18,tzinfo=KST)
    client=Client([bar(day)])
    history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    client.rows=[bar('2026-10-20')]
    with pytest.raises(ValueError): history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    assert history.load_bars('KRX')[0].date==day


def test_snapshot_and_completion_marker_roll_back_together_on_storage_failure(tmp_path):
    import sqlite3
    history=HistoryStore(tmp_path/'history.db');day='2026-10-19';now=datetime(2026,10,19,18,tzinfo=KST)
    client=Client([bar(day)])
    history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    with history.connect() as db:
        db.execute("CREATE TRIGGER reject_test BEFORE INSERT ON daily_bars BEGIN SELECT RAISE(ABORT,'offline injected failure'); END")
        before=tuple(db.execute('SELECT * FROM fetched_days').fetchone())
    client.rows=[bar(day,price=200)]
    with pytest.raises(sqlite3.Error): history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    assert history.load_bars('KRX')[0].close==100
    with history.connect() as db:
        assert tuple(db.execute('SELECT * FROM fetched_days').fetchone())==before


@pytest.mark.parametrize('patch',[
    {'high':99},{'low':101},{'open':float('inf')},{'volume':-1},
    {'trade_value_eok':-1},{'cap_eok':float('nan')},{'date':'20261019'},
    {'name':''},{'code':'bad-code'}])
def test_bad_cache_batches_preserve_previous_rows(tmp_path,patch):
    from dataclasses import replace
    history=HistoryStore(tmp_path/'history.db');day='2026-10-19'
    history.insert_bars([bar(day)],'CSV_USER')
    before=history.load_bars('CSV_USER')
    with pytest.raises(ValueError): history.insert_bars([bar(day),replace(bar(day,'900002'),**patch)],'CSV_USER')
    assert history.load_bars('CSV_USER')==before


def test_duplicate_write_batch_is_not_silently_last_row_wins(tmp_path):
    history=HistoryStore(tmp_path/'history.db');day='2026-10-19'
    with pytest.raises(ValueError): history.insert_bars([bar(day),bar(day,price=110)],'CSV_USER')
    assert history.load_bars('CSV_USER')==[]


@pytest.mark.parametrize('code',['00088K','0115E0','005930'])
def test_alphanumeric_short_codes_survive_normalization_and_cache(tmp_path,code):
    from app.krx_history import normalize_krx
    raw=dict(ISU_CD=code,ISU_NM='합성 시험 종목',TDD_OPNPRC='100',TDD_HGPRC='110',
             TDD_LWPRC='99',TDD_CLSPRC='100',ACC_TRDVOL='1000',ACC_TRDVAL='10000000000',MKTCAP='400000000000')
    normalized=normalize_krx(raw,'KOSPI','2026-10-19')
    assert normalized.code==code
    history=HistoryStore(tmp_path/'hist.db');history.insert_bars([normalized],'CSV_USER')
    assert history.load_bars('CSV_USER')[0].code==code


@pytest.mark.parametrize('code',['KR7005930003','００５９３０','00593','005930X'])
def test_invalid_code_shapes_are_not_coerced_into_other_symbols(code):
    from app.krx_history import normalize_krx
    assert normalize_krx({'ISU_CD':code},'KOSPI','2026-10-19') is None


def test_old_normalizer_cache_is_refetched_once(tmp_path):
    history=HistoryStore(tmp_path/'history.db');day='2026-10-19';now=datetime(2026,10,20,10,tzinfo=KST)
    client=Client([bar(day)])
    history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    with history.connect() as db:
        db.execute('UPDATE fetched_days SET normalizer_version=0')
    client.rows=[bar(day),bar(day,'00088K')]
    history=HistoryStore(history.path)
    history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    history.fetch_range(client,day,day,markets=('KOSPI',),now=now)
    assert len(client.calls)==2 and len(history.load_bars('KRX'))==2
