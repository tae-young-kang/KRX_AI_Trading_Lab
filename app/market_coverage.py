"""Read-only comparison of stored bars against an *externally supplied* day/count list.

This is an evidence-quality warning, not proof of full KRX coverage, an official
exchange calendar, a tradable signal or permission to access a broker.
"""
import csv
import re
from collections import Counter
from pathlib import Path
from datetime import datetime

from app.recommendation_outcomes import session_day
from app.trading_clock import KST

MARKETS = ('KOSPI', 'KOSDAQ')
SOURCES = ('KRX', 'CSV_USER', 'DEMO_SYNTHETIC')
HEADERS = ['market', 'day', 'min_symbols']
MAX_FILE_BYTES = 1024 * 1024
MAX_REFERENCE_ROWS = 10000
STATUS_LABELS = {
    'MISSING_DAY': '저장 일봉 없음',
    'BELOW_REFERENCE': '최소 종목 수 미달',
    'COUNT_DROP': '직전 입력일 대비 급감 의심',
    'NORMALIZATION_OMISSIONS': '정규화에서 일부 제외',
}


def read_reference(path):
    """Each row is an independently confirmed market session with a count FLOOR.

    This CSV cannot certify its own source; callers must verify provenance.
    """
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError()
        with path.open('r', encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != HEADERS:
                raise ValueError()
            entries = {}
            for row in reader:
                if len(entries) >= MAX_REFERENCE_ROWS or None in row:
                    raise ValueError()
                market, day, floor = (row[key] for key in HEADERS)
                if market not in MARKETS or not isinstance(day, str):
                    raise ValueError()
                if session_day(day) > datetime.now(KST).date():
                    raise ValueError()
                if not isinstance(floor, str) or not re.fullmatch(r'[0-9]+', floor):
                    raise ValueError()
                minimum = int(floor)
                if not 1 <= minimum <= 30000:
                    raise ValueError()
                key = (market, day)
                if key in entries:
                    raise ValueError()
                entries[key] = minimum
        if not entries:
            raise ValueError()
        return entries
    except (OSError, UnicodeError, csv.Error, TypeError, KeyError, ValueError):
        raise ValueError('대조 파일을 확인해 주세요. 헤더 market,day,min_symbols와 중복 없는 실제 거래일·최소 종목 수가 필요해요.') from None


def audit_coverage(history, source, reference_path):
    """Read a stable DB snapshot; never fetch, save, rank, order or mutate."""
    if source not in SOURCES:
        raise ValueError('현재 연구 자료 출처를 확인해 주세요.')
    expected = read_reference(reference_path)
    start = min(day for _, day in expected)
    end = max(day for _, day in expected)
    with history.connect() as db:
        db.execute('BEGIN')
        actual = {(r['market'], r['day']): r['count'] for r in db.execute(
            """SELECT market,day,COUNT(*) AS count FROM daily_bars
               WHERE source=? AND day BETWEEN ? AND ?
               GROUP BY market,day""", (source, start, end))}
        ingest = {(r['market'], r['day']): r['omitted_count'] for r in db.execute(
            """SELECT market,day,omitted_count FROM daily_ingest_status
               WHERE source=? AND day BETWEEN ? AND ?""", (source, start, end))}
    counts = Counter()
    flagged = []
    previous = {}
    for (market, day), minimum in sorted(expected.items()):
        count = actual.get((market, day), 0)
        omissions = ingest.get((market, day))
        statuses = []
        if count == 0:
            statuses.append('MISSING_DAY')
        elif count < minimum:
            statuses.append('BELOW_REFERENCE')
        prior = previous.get(market)
        # Advisory only; prices, calendar and corporate-action coverage remain unproven.
        if prior is not None and prior > 0 and count > 0 and count * 10 < prior * 7:
            statuses.append('COUNT_DROP')
        if omissions is not None and omissions > 0:
            statuses.append('NORMALIZATION_OMISSIONS')
        if not statuses:
            statuses.append('MEETS_REFERENCE_FLOOR')
        for status in statuses:
            counts[status] += 1
        if statuses != ['MEETS_REFERENCE_FLOOR']:
            flagged.append({'market': market, 'day': day, 'stored': count,
                            'minimum': minimum, 'statuses': tuple(statuses)})
        previous[market] = count
    return {'source': source, 'reference_days': len(expected), 'counts': dict(counts),
            'flagged': flagged}


def render_audit(report, *, max_rows=8):
    if type(max_rows) is not int or not 1 <= max_rows <= 30:
        raise ValueError('표시 건수를 확인해 주세요.')
    c = report['counts']
    lines = [
        '[외부 확인 거래일·최소 종목 수 대조]',
        f"저장 출처: {report['source']} · 외부 입력 {report['reference_days']}개 시장/날짜",
        f"저장 자료 없는 날 {c.get('MISSING_DAY',0)} · 최소 종목 수 미달 {c.get('BELOW_REFERENCE',0)}",
        f"직전 입력 거래일 대비 종목 수 30% 초과 급감 의심 {c.get('COUNT_DROP',0)} · 정규화 제외 관측 {c.get('NORMALIZATION_OMISSIONS',0)}",
        '표시 항목은 중복 경고할 수 있어요. 종목 수 하한을 넘겨도 완전 수집을 보증하지 않아요.',
    ]
    for item in report['flagged'][:max_rows]:
        lines.append(f"{item['day']} {item['market']}: 저장 {item['stored']}개 / 외부 최소 {item['minimum']}개 · "
                     + ','.join(STATUS_LABELS[s] for s in item['statuses']))
    remaining = len(report['flagged']) - max_rows
    if remaining > 0:
        lines.append(f'그 외 확인 필요 {remaining}개 시장/날짜 (최대 {max_rows}개만 표시)')
    lines.append('외부 CSV의 진위/거래일/상장 종목·가격·기업행동은 별도 검증해야 합니다. 실제 거래/자동 승인 없음.')
    return '\n'.join(lines)
