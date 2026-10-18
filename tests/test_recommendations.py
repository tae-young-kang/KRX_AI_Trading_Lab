"""Temporary synthetic recommendation history; no market or order clients."""
from copy import deepcopy
import json
import pytest
from app.config import DEFAULTS
from app.market import Stock
from app.strategy import ScreeningResult
from app.storage import Database


def candidates(source='DEMO'):
    return [ScreeningResult(Stock(f'90000{i}',f'시험{i}','KOSPI',1000*i,3,200,250,5000,
        source=source,stamp='2026-10-19T10:00:00',quote_stamp='095959',is_etf=i==2),90-i,i!=3,'시험')
        for i in range(1,5)]


def save(db, rows, cfg):
    return db.save_scan([r.stock for r in rows],rows,cfg,'전체',rows[0].stock.source)


def test_top_n_and_conditions_are_frozen_with_quote_time_and_no_private_settings(tmp_path):
    db = Database(tmp_path/'practice.db')
    cfg = {**deepcopy(DEFAULTS),'recommend_count':2,'private_field':'PRIVATE'}
    rows = candidates()
    sid = save(db,rows,cfg)
    cfg['min_value_eok'] = 999
    rows[0].stock.price = 99999
    saved = db.recommendation_history('DEMO')
    assert [r['code'] for r in saved] == ['900001','900002']
    assert saved[0]['price'] == 1000 and saved[0]['quote_stamp'] == '095959'
    assert saved[1]['is_etf'] == 1 and saved[0]['scan_id'] == sid
    assert json.loads(saved[0]['conditions_json'])['min_value_eok'] == DEFAULTS['min_value_eok']
    assert 'PRIVATE' not in str(saved)
    with db.connect() as record:
        assert 'PRIVATE' not in record.execute('SELECT strategy_json FROM scans').fetchone()[0]


def test_modes_and_reopen_never_mix_or_backfill_old_scans(tmp_path):
    path = tmp_path/'practice.db'
    db = Database(path)
    for mode,env,source in [('DEMO','MOCK','DEMO'),('KIWOOM','MOCK','KIWOOM'),('KIWOOM','REAL','KIWOOM')]:
        save(db,candidates(source),{**DEFAULTS,'data_mode':mode,'kiwoom_env':env})
    reopened = Database(path)
    assert all(r['mode']=='MOCK' for r in reopened.recommendation_history('MOCK'))
    assert len(reopened.recommendation_history('REAL_QUOTES')) == 3
    with db.connect() as record:
        record.execute("INSERT INTO scans VALUES(99,'old','DEMO','전체','{}',0)")
    Database(path)
    with db.connect() as record:
        assert record.execute('SELECT COUNT(*) FROM recommendation_batches').fetchone()[0] == 3


@pytest.mark.parametrize('damage',['price','duplicate','source','count'])
def test_invalid_new_history_rolls_back_entire_scan(tmp_path,damage):
    db = Database(tmp_path/'practice.db')
    rows = candidates(); cfg = deepcopy(DEFAULTS)
    if damage=='price': rows[0].stock.price=float('nan')
    elif damage=='duplicate': rows[1].stock.code=rows[0].stock.code
    elif damage=='source': rows[1].stock.source='KIWOOM'
    elif damage=='count': cfg['recommend_count']=0
    with pytest.raises(ValueError): save(db,rows,cfg)
    with db.connect() as record:
        for table in ('scans','snapshots','recommendations','recommendation_batches'):
            assert record.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] == 0


def test_empty_recommendations_record_complete_batch_without_inventing_entries(tmp_path):
    db = Database(tmp_path/'practice.db')
    rows = candidates()
    for row in rows: row.passed=False
    save(db,rows,deepcopy(DEFAULTS))
    assert db.recommendation_history('DEMO') == []
    with db.connect() as record:
        assert record.execute('SELECT selected_count FROM recommendation_batches').fetchone()[0] == 0


def test_latest_empty_search_is_visible_above_older_recommendations(tmp_path):
    db = Database(tmp_path/'history.db')
    rows = candidates()
    first = save(db, rows, deepcopy(DEFAULTS))
    for row in rows:
        row.passed = False
    latest = save(db, rows, {**DEFAULTS, 'min_value_eok': 777})
    text = db.recommendation_summary('DEMO')
    assert f'마지막 검색 #{latest}' in text and '추천 0개' in text
    assert '거래대금 777억 이상' in text
    assert '이전 검색 기록' in text and f'검색 #{first}' in text
    assert '새로 검색한 결과는 아니에요' in text
    assert '마지막 검색' not in db.recommendation_summary('MOCK')


def test_exact_search_results_keep_old_symbols_prices_and_mode(tmp_path):
    db = Database(tmp_path/'results.db')
    rows = candidates()
    first = save(db, rows, deepcopy(DEFAULTS))
    frozen = db.recommendation_search_result('DEMO', first)
    rows[0].stock.name = '나중이름'
    rows[0].stock.price = 99999
    for row in rows:
        row.passed = False
    second = save(db, rows, deepcopy(DEFAULTS))
    assert db.recommendation_search_result('DEMO', first) == frozen
    assert '시험1' in frozen and '나중이름' not in frozen
    assert '추천 0개' in db.recommendation_search_result('DEMO', second)
    assert '기록이 없어요' in db.recommendation_search_result('MOCK', first)
    assert '페이지까지' in db.recommendation_search_result('DEMO', first,page=2)
    assert len(frozen) < 3500
    assert '저장 시세 4개 (KOSPI 4개)' in frozen
    assert '조건 통과 3개 중 추천 3개' in frozen
    assert '합성 예제 범위' in frozen


@pytest.mark.parametrize('damage', ['missing', 'source', 'market'])
def test_scope_mismatch_is_reported_without_claiming_complete_coverage(tmp_path, damage):
    db = Database(tmp_path/'scope.db')
    sid = save(db,candidates(),deepcopy(DEFAULTS))
    with db.connect() as conn:
        if damage == 'missing':
            conn.execute('DELETE FROM snapshots WHERE scan_id=? AND code=?',(sid,'900001'))
        elif damage == 'source':
            conn.execute("UPDATE snapshots SET source='KIWOOM' WHERE scan_id=?",(sid,))
        else:
            conn.execute("UPDATE snapshots SET market='UNKNOWN' WHERE scan_id=?",(sid,))
    text = db.recommendation_search_result('DEMO',sid)
    assert '조회 범위 기록 확인 필요' in text
    assert '저장 시세 4개' not in text


def test_exact_search_pages_cover_all_selected_symbols(tmp_path):
    db = Database(tmp_path/'pages.db')
    rows = []
    for i in range(1,8):
        row = deepcopy(candidates()[0])
        row.stock.code = f'90000{i}'
        row.stock.name = f'페이지종목{i}'
        rows.append(row)
    sid = save(db, rows, {**DEFAULTS, 'recommend_count': 7})
    first = db.recommendation_search_result('DEMO',sid)
    second = db.recommendation_search_result('DEMO',sid,page=2)
    assert first.count('위 페이지종목') == 5 and second.count('위 페이지종목') == 2
    assert '900001' not in second and '900007' not in first
    assert f'/추천결과 {sid} 2' in first and f'/추천결과 {sid} 1' in second


@pytest.mark.parametrize('damage',['missing','extra','different_price','duplicate_ranked','duplicate_population'])
def test_rankings_cannot_be_saved_against_a_different_scan_population(tmp_path,damage):
    db=Database(tmp_path/'practice.db')
    ranked=candidates();stocks=[row.stock for row in ranked]
    if damage=='missing': ranked=ranked[:-1]
    elif damage=='extra': stocks=stocks[:-1]
    elif damage=='different_price':
        ranked=deepcopy(ranked);ranked[0].stock.price=999
    elif damage=='duplicate_ranked': ranked.append(ranked[-1])
    elif damage=='duplicate_population': stocks.append(stocks[-1])
    with pytest.raises(ValueError,match='자료가 맞지 않아요'):
        db.save_scan(stocks,ranked,deepcopy(DEFAULTS),'전체','DEMO')
    with db.connect() as record:
        for table in ('scans','snapshots','recommendations','recommendation_batches'):
            assert record.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]==0


def test_equivalent_copies_and_generators_do_not_lose_recommendations(tmp_path):
    db=Database(tmp_path/'practice.db')
    rows=candidates()
    stocks=(row.stock for row in rows)
    ranked=(row for row in deepcopy(rows))
    db.save_scan(stocks,ranked,deepcopy(DEFAULTS),'전체','DEMO')
    assert len(db.recommendation_history('DEMO'))==3
