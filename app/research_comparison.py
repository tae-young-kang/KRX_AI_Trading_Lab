"""Descriptive TRAIN-vs-holdout comparison for stock-screening experiments.

Read-only presentation. No strategy re-ranking, optimization, real orders,
approval changes or false claim of independent holdout validity.
"""
from math import isfinite


QUALITY_LABELS = {
    'NOT_CHECKED': '외부 대조 안 함',
    'ALERT': '자료 품질 경고',
    'REFERENCE_FLOOR_MET': '외부 최소 기준 충족',
}


def _finite_number(value):
    if type(value) not in (int, float):
        return None
    return float(value) if isfinite(value) else None


def compare_variants(report, *, min_train_days=12, min_test_days=5):
    """Copy variants in their ORIGINAL order, adding diagnostic columns only.

    In particular, TEST/holdout results MUST NOT influence selection/ranking.
    The same source-wide coverage status applies to every variant, rather
    than incorrectly claiming per-variant or per-market verification.
    """
    if type(min_train_days) is not int or min_train_days < 1:
        raise ValueError('학습 표본 기준을 확인해 주세요.')
    if type(min_test_days) is not int or min_test_days < 1:
        raise ValueError('검증 표본 기준을 확인해 주세요.')
    quality = report.get('coverage_quality')
    status = quality.get('state') if isinstance(quality, dict) else None
    data_quality = QUALITY_LABELS.get(status, '외부 대조 기록 없음')
    output = []
    for variant in report['variants']:
        row = dict(variant)
        train = _finite_number(row.get('train_excess3'))
        test = _finite_number(row.get('test_excess3'))
        train_days = row.get('train_days')
        test_days = row.get('test_days')
        enough_train = type(train_days) is int and train_days >= min_train_days
        enough_test = type(test_days) is int and test_days >= min_test_days
        # Two individually finite extremes can still overflow on subtraction.
        delta = test-train if train is not None and test is not None else None
        gap = round(delta, 4) if delta is not None and isfinite(delta) else None

        if status == 'ALERT':
            interpretation = '자료 부족·이상 경고'
        elif status not in ('NOT_CHECKED', 'REFERENCE_FLOOR_MET'):
            interpretation = '자료 확인 기록 없음'
        elif not enough_train:
            interpretation = '학습 표본 부족'
        elif not enough_test:
            interpretation = '검증 표본 부족'
        elif train is None or test is None or gap is None:
            interpretation = '비교 불가'
        elif train > 0 and test <= 0:
            interpretation = '학습 양수→검증 비양수'
        elif gap < 0:
            interpretation = '검증 초과수익 감소'
        elif gap > 0:
            interpretation = '검증 초과수익 증가'
        else:
            interpretation = '검증 초과수익 변화 없음'

        row['holdout_gap3'] = gap
        row['data_quality_label'] = data_quality
        row['comparison_note'] = interpretation
        row['adequate_train_days'] = enough_train
        row['adequate_test_days'] = enough_test
        output.append(row)
    return output


def comparison_notice(report):
    """Dataset-wide warning: many related variants on one holdout period."""
    quality = report.get('coverage_quality')
    state = quality.get('state') if isinstance(quality, dict) else None
    if state == 'ALERT':
        prefix = '원본 시세/시장 범위 자료 품질 경고가 있어요. '
    elif state == 'REFERENCE_FLOOR_MET':
        prefix = '입력 외부 최소 기준만 충족했으며 공식 전체시장 검증은 아니에요. '
    else:
        prefix = '자료의 독립 확인이 없거나 기록되지 않았어요. '
    return (
        prefix + f"조건 {len(report['variants'])}개는 같은 과거 구간에서 비교합니다. "
        '학습 수치로만 후보를 정렬하며 검증 구간으로 재선정하지 않아요. '
        '검증 초과수익 및 TEST 초반/후반 변화는 같은 구간의 기술통계예요. '
        '기간 중첩과 적은 관측일 때문에 독립 표본·미래 수익·실계좌 매매 성과가 아니에요.'
    )
