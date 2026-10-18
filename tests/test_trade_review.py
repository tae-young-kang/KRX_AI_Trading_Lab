import json
from copy import deepcopy

from app.config import Settings
from app.market import Stock
from app.storage import Database
from app.strategy import create_proposals
from app.trade_review import outcome, review_conditions


def records(cfg, count=40):
    rows = []
    for i in range(count):
        day = i//4 + 1
        rows.append(dict(id=i+1, source='DEMO', code='900001',
                         opened_at=f'2025-01-{day:02d}T09:00:00',
                         closed_at=f'2025-01-{day:02d}T10:00:00',
                         strat_json=json.dumps(cfg), stop_pct=2, take_pct=4, max_days=2,
                         pnl=100., pnl_pct=1., exit_settings_json=json.dumps({
                             k: cfg[k] for k in ('paper_fee_pct', 'paper_sell_tax_pct', 'paper_slippage_pct')})))
    return rows


def test_entry_snapshot_costs_and_realized_outcomes(tmp_path):
    cfg = Settings(tmp_path).values
    cfg.update(telegram_chat_id='private-marker', unexpected_secret='private-marker')
    db = Database(tmp_path/'trades.db')
    stock = Stock('900001', 'example', 'KOSPI', 10000, 3, 200, 150, 4000)
    db.paper_open(stock, 1, 2, 4, 2, .1, 0, cfg, 3)
    first = db.trades()[0]
    cfg['min_value_eok'] = 9999
    stock.price = 99999
    assert json.loads(first['strat_json'])['min_value_eok'] == 100
    assert json.loads(first['entry_json'])['price'] == 10000
    assert 'private-marker' not in first['strat_json']
    assert outcome(first) == '보유 중'
    assert db.condition_reviews()[0]['total']['count'] == 0
    assert db.condition_reviews()[0]['open_count'] == 1
    # An unchanged market price still loses money after fees/tax.
    db.paper_close(first['id'], 10000, 'test', .1, .2, 0)
    closed = db.trades()[0]
    assert outcome(closed) == '패'
    assert json.loads(closed['exit_settings_json'])['paper_sell_tax_pct'] == .2
    assert db.condition_reviews()[0]['total']['losses'] == 1
    assert db.condition_reviews()[0]['open_count'] == 0
    assert outcome(dict(closed, pnl=0)) == '본전'
    assert outcome(dict(closed, pnl=10)) == '승'


def test_condition_identity_ignores_version_and_disabled_thresholds(tmp_path):
    cfg = Settings(tmp_path).values
    cfg['on_cap'] = False
    rows = records(cfg)
    changed = dict(cfg, strategy_version=99, max_cap_eok=77, show_advanced=True)
    rows[0]['strat_json'] = json.dumps(changed)
    assert len(review_conditions(rows)) == 1
    changed['min_value_eok'] = 999
    rows[0]['strat_json'] = json.dumps(changed)
    assert len(review_conditions(rows)) == 2


def test_large_positive_sample_with_mixed_exit_costs_stays_out_of_candidates(tmp_path):
    cfg=Settings(tmp_path).values
    rows=records(cfg)
    first={k:cfg[k] for k in ('paper_fee_pct','paper_sell_tax_pct','paper_slippage_pct')}
    second=dict(first,paper_fee_pct=first['paper_fee_pct']+.01)
    for row in rows: row['exit_settings_json']=json.dumps(dict(mixed=True,segments=[first,second]))
    review=review_conditions(rows)[0]
    assert review['train']['count']>=20 and review['test']['count']>=10
    assert not review['eligible'] and review['state']=='분할매도 비용 가정 다름'


def test_invalid_recorded_exit_costs_do_not_become_positive_candidates(tmp_path):
    cfg=Settings(tmp_path).values
    rows=records(cfg)
    for row in rows:
        costs=json.loads(row['exit_settings_json']);costs['paper_fee_pct']=-1
        row['exit_settings_json']=json.dumps(costs)
    assert review_conditions(rows)==[]


def test_sources_and_environments_are_not_mixed(tmp_path):
    cfg = Settings(tmp_path).values
    rows = records(cfg, 12)
    for i, env in enumerate(('MOCK', 'REAL')):
        row = deepcopy(rows[i])
        row['source'] = 'KIWOOM'
        row['strat_json'] = json.dumps(dict(cfg, data_mode='KIWOOM', kiwoom_env=env))
        rows.append(row)
    reviews = review_conditions(rows)
    assert {r['mode'] for r in reviews} == {'가상 시세로 연습', '모의 시세로 연습', '실제 시세로 연습'}
    assert not any(r['eligible'] for r in reviews)


def test_chronological_test_losses_prevent_a_candidate(tmp_path):
    cfg = Settings(tmp_path).values
    rows = records(cfg)
    review = review_conditions(rows)[0]
    assert review['eligible']
    assert review['train']['count'] == 28 and review['test']['count'] == 12
    # A train-labelled trade closing after test entry is embargoed.
    rows[0]['closed_at'] = '2025-01-09T10:00:00'
    assert review_conditions(rows)[0]['train']['count'] == 27
    for row in rows[-12:]:
        row.update(pnl=-100, pnl_pct=-1)
    review = review_conditions(rows)[0]
    assert not review['eligible']
    assert review['state'] == '수익 기준 미달'


def test_same_day_and_unknown_costs_do_not_qualify(tmp_path):
    cfg = Settings(tmp_path).values
    rows = records(cfg)
    for row in rows:
        row['opened_at'] = '2025-01-01T09:00:00'
        row['closed_at'] = '2025-01-01T10:00:00'
    assert not review_conditions(rows)[0]['eligible']
    rows = records(cfg)
    for row in rows: row['exit_settings_json'] = None
    review = review_conditions(rows)[0]
    assert not review['eligible'] and review['state'] == '청산 비용 기록 미확인'
    rows[0]['strat_json'] = 'broken'
    assert review_conditions(rows)[0]['total']['count'] == 39


def test_proposals_use_only_tried_conditions_without_auto_apply(tmp_path):
    cfg = Settings(tmp_path).values
    rows = records(cfg)
    db = Database(tmp_path/'proposals.db')
    # Use temporary data only, with a complete historical condition snapshot.
    with db.connect() as conn:
        for row in rows:
            conn.execute('''INSERT INTO paper_trades(code,name,market,opened_at,qty,buy_ref,buy_fill,buy_fee,
                stop_pct,take_pct,max_days,highwater,source,strat_json,closed_at,pnl,pnl_pct,exit_settings_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (row['code'], 'example', 'KOSPI', row['opened_at'], 1, 10000, 10000, 0,
                 2, 4, 2, 10000, 'DEMO', row['strat_json'], row['closed_at'], 100, 1, row['exit_settings_json']))
    cfg['min_value_eok'] = 999
    before = dict(cfg)
    create_proposals(db, cfg)
    assert cfg == before
    assert len(db.proposals()) == 1
    assert json.loads(db.proposals()[0]['patch_json']) == {'min_value_eok': 100.}
    create_proposals(db, cfg)
    assert len(db.proposals()) == 1


def test_migration_preserves_old_rows_and_unknown_provenance(tmp_path):
    path = tmp_path/'old.db'
    db = Database(path)
    stock = Stock('900001', 'example', 'KOSPI', 10000, 3, 200, 150, 4000)
    db.paper_open(stock, 1, 2, 4, 2, 0, 0, Settings(tmp_path).values, 3)
    with db.connect() as conn:
        conn.execute('ALTER TABLE paper_trades DROP COLUMN entry_json')
        conn.execute('ALTER TABLE paper_trades DROP COLUMN exit_settings_json')
    reopened = Database(path)
    assert len(reopened.trades()) == 1
    assert reopened.trades()[0]['entry_json'] is None
    assert reopened.trades()[0]['exit_settings_json'] is None
