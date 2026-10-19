"""Synthetic only: quality flags before historical research and report persistence."""
import pytest

from app.backtest import Bar
from app.krx_history import HistoryStore
from app.research_preflight import check_research_coverage, preflight_notice


def bar(day, market='KOSPI', code='900001'):
    return Bar(day, code, '연구용합성', market, 100, 101, 99, 100, 1000, 5, 5000)


def reference(tmp_path, text):
    dest = tmp_path / 'reference.csv'
    dest.write_text(text, encoding='utf-8')
    return dest


def test_no_independent_reference_is_never_a_verified_pass(tmp_path):
    history = HistoryStore(tmp_path / 'hist.db')
    rows = [bar('2026-10-13')]
    history.insert_bars(rows, 'CSV_USER')
    value = check_research_coverage(history, 'CSV_USER', rows)
    assert value['state'] == 'NOT_CHECKED'
    assert value['unreferenced_market_days'] == 1
    assert '대조 안 함' in preflight_notice(value)


def test_missing_reference_market_day_is_a_warning_without_mutation(tmp_path):
    history = HistoryStore(tmp_path / 'hist.db')
    rows = [bar('2026-10-13'), bar('2026-10-14')]
    history.insert_bars(rows, 'CSV_USER')
    ref = reference(tmp_path, 'market,day,min_symbols\nKOSPI,2026-10-13,1\nKOSPI,2026-10-15,1\n')
    before = history.count('CSV_USER')[0]
    value = check_research_coverage(history, 'CSV_USER', rows, reference_path=ref)
    assert value['state'] == 'ALERT'
    assert value['flagged_market_days'] == 1  # 10/15 has no stored price bars.
    assert value['unreferenced_market_days'] == 1  # 10/14 lacks external evidence.
    assert history.count('CSV_USER')[0] == before
    assert '자료 품질 경고' in preflight_notice(value)


def test_reference_floor_met_never_claims_full_exchange_verification(tmp_path):
    history = HistoryStore(tmp_path / 'hist.db')
    rows = [bar('2026-10-13'), bar('2026-10-14', 'KOSDAQ', '900002')]
    history.insert_bars(rows, 'DEMO_SYNTHETIC')
    ref = reference(tmp_path, 'market,day,min_symbols\nKOSPI,2026-10-13,1\nKOSDAQ,2026-10-14,1\n')
    result = check_research_coverage(history, 'DEMO_SYNTHETIC', rows, reference_path=ref)
    assert result['state'] == 'REFERENCE_FLOOR_MET'
    assert result['flagged_market_days'] == 0
    assert result['unreferenced_market_days'] == 0
    assert '검증 완료는 아니에요' in preflight_notice(result)


def test_other_source_cannot_fill_missing_reference_and_errors_fail_closed(tmp_path):
    history = HistoryStore(tmp_path / 'hist.db')
    rows = [bar('2026-10-13')]
    history.insert_bars(rows, 'DEMO_SYNTHETIC')
    ref = reference(tmp_path, 'market,day,min_symbols\nKOSPI,2026-10-13,1\n')
    result = check_research_coverage(history, 'KRX', [], reference_path=ref)
    assert result['state'] == 'ALERT'
    assert result['flagged_market_days'] == 1
    with pytest.raises(ValueError):
        check_research_coverage(history, 'OTHER', rows)
    ref.write_text('market,day,min_symbols\nKOSPI,2026-10-13,0\n')
    with pytest.raises(ValueError, match='대조 파일'):
        check_research_coverage(history, 'DEMO_SYNTHETIC', rows, reference_path=ref)


def test_legacy_saved_results_still_show_not_checked(tmp_path):
    assert '대조 기록이 없어요' in preflight_notice(None)
    assert '확인할 수 없어요' in preflight_notice({'state': 'UNKNOWN'})


def test_normalizer_omissions_are_warning_even_when_floor_met(tmp_path):
    history = HistoryStore(tmp_path / 'hist.db')
    rows = [bar('2026-10-13')]
    history.insert_bars(rows, 'KRX')
    with history.connect() as db:
        db.execute('INSERT INTO daily_ingest_status VALUES(?,?,?,?,?,?,?)',
                   ('KRX', 'KOSPI', '2026-10-13', 3, 1, 2, '2026-10-14T09:00:00+09:00'))
    ref = reference(tmp_path, 'market,day,min_symbols\nKOSPI,2026-10-13,1\n')
    quality = check_research_coverage(history, 'KRX', rows, reference_path=ref)
    assert quality['state'] == 'ALERT'
    assert quality['warning_counts']['NORMALIZATION_OMISSIONS'] == 1


def test_stale_local_reference_cannot_be_silently_accepted(tmp_path):
    history = HistoryStore(tmp_path / 'hist.db')
    rows = [bar('2026-10-13')]
    history.insert_bars(rows, 'KRX')
    ref = reference(tmp_path, 'market,day,min_symbols\nKOSPI,2026-10-13,1\n')
    assert check_research_coverage(history, 'KRX', rows, reference_path=ref)['state'] == 'REFERENCE_FLOOR_MET'
    ref.unlink()
    with pytest.raises(ValueError):
        check_research_coverage(history, 'KRX', rows, reference_path=ref)
