"""Synthetic-only market-day reference auditing. Never talks to KRX or a broker."""
import sqlite3
from pathlib import Path

import pytest

from app.backtest import Bar
from app.krx_history import HistoryStore
from app.market_coverage import audit_coverage, read_reference, render_audit


def sample(day, market='KOSPI', code='900001'):
    return Bar(day, code, '가상종목', market, 100, 110, 95, 100, 1000, 100, 4000)


def reference(tmp_path, rows):
    path = tmp_path / 'independent_sessions.csv'
    path.write_text('market,day,min_symbols\n' + ''.join(
        f'{m},{d},{n}\n' for m,d,n in rows), encoding='utf-8')
    return path


def test_detects_whole_missing_market_day_and_count_drop_without_cross_source_fill(tmp_path):
    store=HistoryStore(tmp_path/'hist.db')
    a,b,c='2026-10-13','2026-10-14','2026-10-15'
    store.insert_bars([sample(a,code=f'90000{i}') for i in range(1,5)] +
                      [sample(b,code=f'90000{i}') for i in range(1,3)] +
                      [sample(b,'KOSDAQ','800001')], 'CSV_USER')
    store.insert_bars([sample(c,code=f'90000{i}') for i in range(1,5)], 'DEMO_SYNTHETIC')
    ref=reference(tmp_path,[('KOSPI',a,3),('KOSPI',b,3),
                            ('KOSPI',c,3),('KOSDAQ',b,1)])
    original_count=store.count('CSV_USER')[0]
    report=audit_coverage(store,'CSV_USER',ref)
    assert report['reference_days']==4
    assert report['counts']=={'MEETS_REFERENCE_FLOOR':2,'BELOW_REFERENCE':1,
                               'COUNT_DROP':1,'MISSING_DAY':1}
    flagged={(r['market'],r['day']):r for r in report['flagged']}
    assert flagged[('KOSPI',c)]['statuses']==('MISSING_DAY',)
    assert flagged[('KOSPI',b)]['statuses']==('BELOW_REFERENCE','COUNT_DROP')
    assert store.count('CSV_USER')[0]==original_count
    assert '저장 자료 없는 날 1' in render_audit(report)
    assert '외부 CSV의 진위' in render_audit(report)


def test_ingest_omissions_counted_but_not_hidden_behind_minimum_floor(tmp_path):
    store=HistoryStore(tmp_path/'hist.db')
    day='2026-10-13'
    store.insert_bars([sample(day)],'KRX')
    with store.connect() as db:
        db.execute('INSERT INTO daily_ingest_status VALUES(?,?,?,?,?,?,?)',
                   ('KRX','KOSPI',day,2,1,1,'2026-10-14T09:00:00+09:00'))
    ref=reference(tmp_path,[('KOSPI',day,1)])
    report=audit_coverage(store,'KRX',ref)
    assert report['counts']=={'NORMALIZATION_OMISSIONS':1}
    assert report['flagged'][0]['statuses']==('NORMALIZATION_OMISSIONS',)
    assert audit_coverage(store,'CSV_USER',ref)['counts']=={'MISSING_DAY':1}


@pytest.mark.parametrize('data',[
    '', 'day,market,min_symbols\n2026-10-13,KOSPI,1\n',
    'market,day,min_symbols\nKOSPI,20261013,1\n',
    'market,day,min_symbols\nKOSPI,2026-10-13,0\n',
    'market,day,min_symbols\nKOSPI,2026-10-13,30001\n',
    'market,day,min_symbols\nKOSDAQ,2026-10-13,-1\n',
    'market,day,min_symbols\nKOSPI,2026-10-13,1\nKOSPI,2026-10-13,2\n',
    'market,day,min_symbols\nWRONG,2026-10-13,1\n',
    'market,day,min_symbols\nKOSPI,2026-10-13,1,extra\n',
])
def test_bad_external_reference_is_rejected(tmp_path,data):
    path=tmp_path/'x.csv'
    path.write_text(data,encoding='utf-8')
    with pytest.raises(ValueError,match='대조 파일'):
        read_reference(path)


def test_csv_size_and_source_limits_fail_closed(tmp_path):
    p=reference(tmp_path,[('KOSPI','2026-10-13',1)])
    with pytest.raises(ValueError):
        audit_coverage(HistoryStore(tmp_path/'hist.db'),'UNKNOWN',p)
    with pytest.raises(ValueError):
        render_audit({'source':'KRX','reference_days':1,'counts':{},'flagged':[]},max_rows=0)
    p.write_bytes(b'X' * (1024 * 1024 + 1))
    with pytest.raises(ValueError):
        read_reference(p)


def test_flagged_list_truncated_for_user_display(tmp_path):
    store=HistoryStore(tmp_path/'hist.db')
    days=[f'2026-10-{d:02d}' for d in range(1,12)]
    ref=reference(tmp_path,[('KOSPI',day,1) for day in days])
    report=audit_coverage(store,'CSV_USER',ref)
    display=render_audit(report,max_rows=3)
    assert len(report['flagged'])==11
    assert '그 외 확인 필요 8개' in display
    assert days[3] not in display
