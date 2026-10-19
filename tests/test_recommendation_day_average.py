"""Offline tests: repeated picks on one day must not dominate a daily average."""
import json
import sqlite3

from app.recommendation_overview import groups, summary


def sample_db():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript("""
        CREATE TABLE recommendation_batches (
            scan_id INTEGER PRIMARY KEY, created_at TEXT, conditions_json TEXT, mode TEXT);
        CREATE TABLE recommendations (
            scan_id INTEGER, code TEXT, market TEXT, rank INTEGER);
        CREATE TABLE recommendation_reviews (
            id INTEGER PRIMARY KEY, scan_id INTEGER, code TEXT,
            price_source TEXT, calendar_source TEXT, results_json TEXT);
    """)
    return db


def add_pick(db, scan_id, day, code, *, state='OBSERVED', change=0.0):
    conditions = json.dumps({'market':'KOSPI','include_etf':False})
    db.execute("INSERT OR IGNORE INTO recommendation_batches VALUES(?,?,?,?)",
               (scan_id,day+'T17:00:00+09:00',conditions,'DEMO'))
    db.execute("INSERT INTO recommendations VALUES(?,?,?,?)",
               (scan_id,code,'KOSPI',1))
    results = [{'horizon':1,'state':state,'change_pct':change}]
    db.execute("INSERT INTO recommendation_reviews(scan_id,code,price_source,calendar_source,results_json)"
               " VALUES (?,?,?,?,?)", (scan_id,code,'DEMO','DEMO',json.dumps(results)))


def test_equal_day_averaging_does_not_overweight_more_picks():
    with sample_db() as db:
        for i in range(3):
            add_pick(db,1,'2026-10-15',f'A{i}',change=10.0)
        add_pick(db,2,'2026-10-16','B',change=-10.0)
        data,_=groups(db,'DEMO')
        assert len(data)==1
        horizon=data[0]['horizons'][1]
        assert horizon['observed']==4 and horizon['observed_days']==2
        assert horizon['mean']==5.0  # Four symbols weighted equally.
        assert horizon['daily_mean']==0.0  # Two recommendation dates equally.
        assert '날짜별 균등 평균' in summary(db,'DEMO')
        assert '평가일 10일 미만' in summary(db,'DEMO')


def test_missing_and_pending_dates_not_counted_as_zero_returns():
    with sample_db() as db:
        add_pick(db,1,'2026-10-15','A',change=20.0)
        add_pick(db,2,'2026-10-16','B',state='NOT_DUE',change=0.0)
        add_pick(db,3,'2026-10-17','C',state='PRICE_MISSING',change=0.0)
        data,_=groups(db,'DEMO')
        horizon=data[0]['horizons'][1]
        assert horizon['observed']==1
        assert horizon['observed_days']==1
        assert horizon['mean']==horizon['daily_mean']==20.0
        assert horizon['waiting']==1 and horizon['missing']==1


def test_empty_daily_observations_unavailable_not_zero():
    with sample_db() as db:
        add_pick(db,1,'2026-10-15','A',state='NOT_DUE')
        data,_=groups(db,'DEMO')
        horizon=data[0]['horizons'][1]
        assert horizon['observed_days']==0
        assert horizon['daily_mean'] is None
        assert horizon['mean'] is None
        assert '날짜별 평균 미확인' in summary(db,'DEMO')


def test_partial_observation_days_do_not_masquerade_as_complete_days():
    with sample_db() as db:
        for code in ('A', 'B'):
            add_pick(db, 1, '2026-10-15', code, change=10.0)
        add_pick(db, 1, '2026-10-15', 'C', state='PRICE_MISSING')
        add_pick(db, 2, '2026-10-16', 'D', change=-10.0)
        data, _ = groups(db, 'DEMO')
        horizon = data[0]['horizons'][1]
        assert horizon['observed'] == 3
        assert horizon['observed_days'] == 2
        assert round(horizon['mean'], 7) == round(10 / 3, 7)
        assert horizon['daily_mean'] == 0.0
        assert horizon['complete_days'] == 1
        assert horizon['complete_daily_mean'] == -10.0
        assert '집계 내 완전 관측일 1/2일' in summary(db, 'DEMO')


def test_unreviewed_or_pending_pick_marks_day_as_incomplete():
    with sample_db() as db:
        add_pick(db, 1, '2026-10-15', 'A', change=8.0)
        add_pick(db, 1, '2026-10-15', 'B', state='NOT_DUE')
        result, _ = groups(db, 'DEMO')
        h = result[0]['horizons'][1]
        assert h['observed_days'] == 1
        assert h['complete_days'] == 0
        assert h['complete_daily_mean'] is None
        assert '완전 관측일 평균 미확인' in summary(db, 'DEMO')


def test_other_source_or_unreviewed_pick_prevents_full_date_status():
    with sample_db() as db:
        add_pick(db, 1, '2026-10-15', 'A', change=10.0)
        # Same condition/day but no price review: placed in the NONE
        # source group; this must still prevent DEMO claiming completeness.
        db.execute('INSERT INTO recommendations VALUES (?,?,?,?)',
                   (1, 'B', 'KOSPI', 2))
        data, _ = groups(db, 'DEMO')
        demo = next(group for group in data if group['source'] == 'DEMO')
        h = demo['horizons'][1]
        assert h['observed'] == 1 and h['observed_days'] == 1
        assert h['complete_days'] == 0
        assert h['complete_daily_mean'] is None


def test_total_day_denominator_includes_dates_without_any_observations():
    with sample_db() as db:
        add_pick(db, 1, '2026-10-15', 'A', change=10.0)
        add_pick(db, 2, '2026-10-16', 'B', state='NOT_DUE')
        data, _ = groups(db, 'DEMO')
        horizon = data[0]['horizons'][1]
        assert horizon['observed_days'] == 1
        assert horizon['selected_days'] == 2
        assert horizon['complete_days'] == 1
        assert horizon['complete_daily_mean'] == 10.0
        assert '집계 내 완전 관측일 1/2일' in summary(db, 'DEMO')


def test_coverage_separates_complete_partial_unobserved_days():
    with sample_db() as db:
        add_pick(db, 1, '2026-10-15', 'A', change=10.0)
        add_pick(db, 1, '2026-10-15', 'B', state='PRICE_MISSING')
        add_pick(db, 2, '2026-10-16', 'C', state='NOT_DUE')
        add_pick(db, 3, '2026-10-17', 'D', change=-5.0)
        data, _ = groups(db, 'DEMO')
        h = data[0]['horizons'][1]
        assert (h['selected_days'], h['observed_days'], h['complete_days']) == (3, 2, 1)
        assert (h['partial_days'], h['unobserved_days']) == (1, 1)
        assert h['partial_days'] + h['unobserved_days'] + h['complete_days'] == h['selected_days']
        message = summary(db, 'DEMO')
        assert '완전 관측일 1/3일 · 부분 관측 1일 · 이 출처에서 미관측 1일' in message
        assert len(message) < 4096


def test_zero_observation_source_still_shows_coverage_denominator():
    with sample_db() as db:
        add_pick(db, 1, '2026-10-15', 'A', state='NOT_DUE')
        add_pick(db, 2, '2026-10-16', 'B', state='PRICE_MISSING')
        data, _ = groups(db, 'DEMO')
        h = data[0]['horizons'][1]
        assert (h['selected_days'], h['complete_days'], h['partial_days'], h['unobserved_days']) == (2, 0, 0, 2)
        message = summary(db, 'DEMO')
        assert '완전 관측일 0/2일 · 부분 관측 0일 · 이 출처에서 미관측 2일' in message
        assert '완전 관측일 평균 미확인' in message
        assert '가격 변화는 비용' in message
