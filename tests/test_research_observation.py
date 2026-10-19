from copy import deepcopy
import csv
import pytest
from app.research_observation import signal_coverage
from app.research import evaluate_research,export_report
from test_research_stability import synthetic_history

DAYS=[f'2026-10-{day:02d}' for day in range(1,11)]  # synthetic sessions only


def row(day,code,value,split='TEST'):
    return dict(day=day,code=code,r3=value,split=split)


def test_partial_and_all_missing_days_are_distinct_from_tail():
    signals=[row(DAYS[2],'900001',1),row(DAYS[2],'900002',None),
             row(DAYS[3],'900001',None),row(DAYS[4],'900001',0),row(DAYS[-1],'900001',None)]
    before=deepcopy(signals)
    result=signal_coverage(signals,DAYS,split='TEST',cut=DAYS[1])
    assert result==dict(test_r3_selected=5,test_r3_observed=2,test_r3_missing=2,
                       test_r3_embargo=0,test_r3_tail=1,test_r3_complete_days=1,
                       test_r3_partial_days=1,test_r3_missing_days=1)
    assert signals==before


def test_train_boundary_is_withheld_instead_of_marked_missing():
    signals=[row(DAYS[0],'900001',0,'TRAIN'),row(DAYS[3],'900001',None,'TRAIN')]
    result=signal_coverage(signals,DAYS,split='TRAIN',cut=DAYS[4])
    assert result['train_r3_observed']==1 and result['train_r3_embargo']==1
    assert result['train_r3_missing']==0 and result['train_r3_complete_days']==1
    with pytest.raises(ValueError):
        signal_coverage(signals+signals,DAYS,split='TRAIN',cut=DAYS[4])


def test_actual_engine_and_csv_export_coverage_without_changing_candidate(tmp_path):
    report=evaluate_research(synthetic_history())
    ids=[variant['id'] for variant in report['variants']]
    for variant in report['variants']:
        for prefix in ('train','test'):
            assert variant[prefix+'_r3_selected']==sum(variant[prefix+'_r3_'+key]
                for key in ('observed','missing','embargo','tail'))
    chosen=report['chosen_on_train_only']
    export_report(report,tmp_path/'export')
    with (tmp_path/'export'/'summary.csv').open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    assert [item['id'] for item in rows]==ids
    assert report['chosen_on_train_only']==chosen
    assert int(rows[0]['test_r3_selected'])==report['variants'][0]['test_r3_selected']


def test_missing_selected_stock_exit_bar_is_reported_by_actual_engine():
    bars=synthetic_history()
    baseline=evaluate_research(bars)
    signal=next(item for item in baseline['top_signals'] if item['split']=='TEST' and item['r3'] is not None)
    dates=sorted({bar.date for bar in bars})
    exit_day=dates[dates.index(signal['day'])+3]
    incomplete=evaluate_research([bar for bar in bars if (bar.code,bar.date)!=(signal['code'],exit_day)])
    variant=next(item for item in incomplete['variants'] if item['id']==signal['strategy'])
    assert variant['test_r3_missing']>0
    assert incomplete['chosen_on_train_only']==baseline['chosen_on_train_only']
