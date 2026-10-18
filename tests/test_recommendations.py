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
