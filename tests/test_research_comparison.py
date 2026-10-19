"""No live data or broker use: compare report diagnostics without selection."""
from copy import deepcopy

import pytest

from app.research_comparison import compare_variants, comparison_notice


def experiment(*, train=1.5, test=0.5, train_days=20, test_days=8, code='A'):
    return {
        'id': code, 'name': '가상 조건 '+code, 'market': 'KOSPI',
        'train_excess3': train, 'test_excess3': test,
        'train_days': train_days, 'test_days': test_days,
    }


def report(*variants, state='NOT_CHECKED'):
    return {'chosen_on_train_only': 'A', 'coverage_quality': {'state': state},
            'variants': list(variants)}


def test_comparison_does_not_reorder_or_reselect_using_holdout_results():
    raw = report(experiment(code='A', train=2, test=-4),
                 experiment(code='B', train=1, test=30),
                 state='REFERENCE_FLOOR_MET')
    untouched = deepcopy(raw)
    result = compare_variants(raw)
    assert [r['id'] for r in result] == ['A', 'B']
    assert raw == untouched and raw['chosen_on_train_only'] == 'A'
    assert result[0]['holdout_gap3'] == -6
    assert result[1]['holdout_gap3'] == 29
    assert result[0]['comparison_note'] == '학습 양수→검증 비양수'
    assert result[1]['comparison_note'] == '검증 초과수익 증가'


def test_low_train_and_test_samples_are_not_called_validated():
    rows = compare_variants(report(
        experiment(code='A', train_days=11, test_days=4),
        experiment(code='B', train_days=20, test_days=4),
        experiment(code='C', train_days=20, test_days=5)))
    assert [r['comparison_note'] for r in rows] == [
        '학습 표본 부족', '검증 표본 부족', '검증 초과수익 감소']
    assert rows[0]['adequate_train_days'] is False
    assert rows[2]['adequate_test_days'] is True


def test_source_quality_alert_overrides_apparent_high_returns():
    rows = compare_variants(report(experiment(train=100, test=100), state='ALERT'))
    assert rows[0]['comparison_note'] == '자료 부족·이상 경고'
    assert rows[0]['data_quality_label'] == '자료 품질 경고'
    assert '자료 품질 경고' in comparison_notice(report(*[experiment()], state='ALERT'))


def test_not_checked_is_not_equated_to_official_validation():
    r = report(experiment(), state='NOT_CHECKED')
    rows = compare_variants(r)
    assert rows[0]['data_quality_label'] == '외부 대조 안 함'
    assert '독립 확인이 없거나' in comparison_notice(r)
    assert '공식 전체시장 검증은 아니에요' in comparison_notice(
        report(experiment(), state='REFERENCE_FLOOR_MET'))


def test_legacy_report_without_quality_is_flagged_as_unverified():
    old = {'chosen_on_train_only':'A', 'variants': [experiment()]}
    result = compare_variants(old)[0]
    assert result['data_quality_label'] == '외부 대조 기록 없음'
    assert result['comparison_note'] == '자료 확인 기록 없음'
    assert '자료의 독립 확인이 없거나' in comparison_notice(old)


@pytest.mark.parametrize('train,test',[
    (None,1), (1,None), (float('inf'),1), (1,float('nan')),
    ('9',1), (True,1),
])
def test_unavailable_nonfinite_or_bad_returns_never_make_comparison(train,test):
    row=compare_variants(report(experiment(train=train,test=test)))[0]
    assert row['holdout_gap3'] is None


def test_same_market_and_outcome_fields_remain_intact():
    original=experiment(train=1.23567,test=-0.12345)
    original['other_metadata']={'foo': 'bar'}
    output=compare_variants(report(original))[0]
    assert output['train_excess3']==original['train_excess3']
    assert output['test_excess3']==original['test_excess3']
    assert output['other_metadata']==original['other_metadata']
    assert output['holdout_gap3']==round(-0.12345-1.23567,4)


@pytest.mark.parametrize('train,test',[(0,-2),(1,1),(1,2),(-1,-2)])
def test_holdout_change_described_without_auto_promotion(train,test):
    row=compare_variants(report(experiment(train=train,test=test)))[0]
    assert row['comparison_note'] in (
        '검증 초과수익 감소', '검증 초과수익 변화 없음', '검증 초과수익 증가')


@pytest.mark.parametrize('options',[
    {'min_train_days':0}, {'min_test_days':0},
    {'min_train_days':True}, {'min_test_days':-1},
])
def test_invalid_sample_thresholds_rejected(options):
    with pytest.raises(ValueError):
        compare_variants(report(experiment()),**options)
