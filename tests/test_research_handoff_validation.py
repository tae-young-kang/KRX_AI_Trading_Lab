"""Windows-safe handoff checks. No QApplication, clients or private records."""
import csv
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from app.research import evaluate_research,export_report
from test_research_stability import synthetic_history
from test_recommendation_request import gui_method


def test_test_price_shocks_do_not_change_train_metrics_or_chosen_order():
    bars=synthetic_history()
    baseline=evaluate_research(bars)
    cut=baseline['cuts']['KOSPI']
    days=sorted({bar.date for bar in bars if bar.date>cut})
    changed=[]
    for bar in bars:
        if bar.date<=cut:
            changed.append(bar)
            continue
        factor=(1+.1*(int(bar.code)%3))*(1+.08*(days.index(bar.date)%4))
        changed.append(replace(bar,open=bar.open*factor,high=bar.high*factor,
                               low=bar.low*factor,close=bar.close*factor))
    shocked=evaluate_research(changed)
    assert shocked['cuts']==baseline['cuts']
    assert shocked['chosen_on_train_only']==baseline['chosen_on_train_only']
    assert [row['id'] for row in shocked['variants']]==[row['id'] for row in baseline['variants']]
    for before,after in zip(baseline['variants'],shocked['variants']):
        assert {k:v for k,v in before.items() if k.startswith('train_')}=={
            k:v for k,v in after.items() if k.startswith('train_')}
    assert any(before['test_excess3']!=after['test_excess3']
               for before,after in zip(baseline['variants'],shocked['variants']))


class Table:
    def __init__(self): self.rows=[];self.selected=0
    def setRowCount(self,count): self.rows=self.rows[:count]
    def rowCount(self): return len(self.rows)
    def insertRow(self,index): self.rows.insert(index,{})
    def setItem(self,row,column,value): self.rows[row][column]=value
    def currentRow(self): return self.selected


def window():
    labels={}
    def label(name): return SimpleNamespace(setText=lambda value:labels.__setitem__(name,value))
    return SimpleNamespace(research_table=Table(),
        history=SimpleNamespace(research_states=lambda source:{}),
        research_source=SimpleNamespace(currentText=lambda:'DEMO_SYNTHETIC'),
        research_result=label('result'),research_status=label('status'),
        research_detail_label=label('detail')),labels


def test_actual_gui_methods_and_csv_keep_same_variant_order_and_values(tmp_path):
    report=evaluate_research(synthetic_history())
    before=deepcopy(report)
    fake,labels=window()
    gui_method('show_research',cell=lambda value:value)(fake,report,show_message=False)
    assert len(fake.research_table.rows)==len(report['variants'])
    assert all(set(row)==set(range(11)) for row in fake.research_table.rows)
    for index,variant in enumerate(report['variants']):
        assert fake.research_table.rows[index][0]==variant['name']
        assert ' → ' in fake.research_table.rows[index][8]
    fake.research_table.selected=len(report['variants'])-1
    gui_method('research_detail')(fake)
    assert 'TEST 초반 시장대비 초과수익' in labels['detail']
    assert '독립적인 추가 검증' in labels['detail']
    export_report(report,tmp_path/'export')
    with (tmp_path/'export'/'summary.csv').open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    assert [row['id'] for row in rows]==[row['id'] for row in report['variants']]
    for row,variant in zip(rows,report['variants']):
        for key in ('test_early_excess3','test_late_excess3','test_late_gap3'):
            assert row[key]==('' if variant[key] is None else str(variant[key]))
    assert report==before


def test_old_report_without_new_diagnostics_renders_without_fabricated_returns():
    report=evaluate_research(synthetic_history())
    for variant in report['variants']:
        for key in tuple(variant):
            if key.startswith(('test_early_','test_late_')) or key=='test_stability_note':
                del variant[key]
    fake,labels=window()
    gui_method('show_research',cell=lambda value:value)(fake,report,show_message=False)
    assert all(row[8]=='— → —' for row in fake.research_table.rows)
    gui_method('research_detail')(fake)
    assert '구간 분리 기록 없음' in labels['detail']
    assert 'TEST 초반 시장대비 초과수익 자료 없음' in labels['detail']
