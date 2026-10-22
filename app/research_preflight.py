"""Non-trading data-coverage preflight for historical screening research.

No network access, credentials, market data edits, orders, or strategy promotion.
An independent user CSV is only a *user-provided lower-bound reference*.
"""
from app.market_coverage import SOURCES, audit_coverage, read_reference


def check_research_coverage(history, source, bars, *, reference_path=None):
    """Check coverage before computing research labels.

    NOT_CHECKED means no independent reference was supplied.
    ALERT means an explicit reference flagged missing/suspicious data or did
    not cover all stored market-days. Even REFERENCE_FLOOR_MET is NOT proof
    of complete listings, official sessions, or accurate prices.
    """
    if source not in SOURCES:
        raise ValueError('연구 자료 출처를 확인해 주세요.')
    stored_days = {(bar.market, bar.date) for bar in bars}
    result = {
        'state': 'NOT_CHECKED',
        'stored_market_days': len(stored_days),
        'reference_market_days': 0,
        'unreferenced_market_days': len(stored_days),
        'flagged_market_days': 0,
        'warning_counts': {},
    }
    if reference_path is None:
        return result
    external = read_reference(reference_path)
    audit = audit_coverage(history, source, reference_path)
    result.update(
        reference_market_days=len(external),
        unreferenced_market_days=len(stored_days - external.keys()),
        flagged_market_days=len(audit['flagged']),
        warning_counts={k: v for k, v in audit['counts'].items()
                        if k != 'MEETS_REFERENCE_FLOOR'},
    )
    result['state'] = ('ALERT' if result['flagged_market_days']
                       or result['unreferenced_market_days'] else 'REFERENCE_FLOOR_MET')
    return result


def preflight_notice(quality):
    """Short, safe status for the desktop GUI and saved research reports."""
    if not isinstance(quality, dict):
        return '과거 연구 결과 · 외부 거래일/종목 수 대조 기록이 없어요.'
    state = quality.get('state')
    if state == 'NOT_CHECKED':
        return '자료 품질: 독립 거래일/종목 수 대조 안 함 · 백테스트 결과는 연구용이에요.'
    if state == 'ALERT':
        return (f"자료 품질 경고: 외부 기준 이상 {quality['flagged_market_days']}개 시장/날짜 · "
                f"기준에 없는 저장 시장/날짜 {quality['unreferenced_market_days']}개 · "
                '조건별 수익률을 검증 완료로 판단하지 마세요.')
    if state == 'REFERENCE_FLOOR_MET':
        return ('자료 품질: 입력한 외부 최소 기준에 대한 경고 없음 · '
                '공식 전체 종목/거래일/기업행동/시세 검증 완료는 아니에요.')
    return '자료 품질을 확인할 수 없어요. 조건 검증 결과는 연구용으로만 보세요.'


def auto_research_blocked(quality):
    """Fail closed on alerts/unknown states, without promoting unchecked data."""
    return (not isinstance(quality, dict) or
            quality.get('state') not in ('NOT_CHECKED', 'REFERENCE_FLOOR_MET'))
