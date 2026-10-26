"""Synthetic-only rolling research and executable GUI callbacks. No clients."""
from copy import deepcopy
from dataclasses import replace
from datetime import date,datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
import csv
import json
import pytest
from app.backtest import Bar
from app.research_wfo import evaluate_wfo,METHOD,market_sessions
from app.research import evaluate_research,export_report
from app.research_workflow import run_saved,HOLDOUT
from app.krx_history import HistoryStore
from app.trading_clock import KST
from test_recommendation_request import gui_method
from test_research_handoff_validation import window,Table

NOW=datetime(2026,10,26,12,tzinfo=KST)


def synthetic(count=160,*,actual_days=False,markets=('KOSPI','KOSDAQ')):
    if actual_days:days=market_sessions('2025-01-02','2025-12-30')[:count]
    else:
        days=[];day=date(2025,1,2)
        while len(days)<count:
            if day.weekday()<5:days.append(day.isoformat())
            day+=timedelta(days=1)
    bars=[]
    for market in markets:
        for stock in range(6):
            for index,day in enumerate(days):
                price=2000*(1.01+stock*.004)**index
                bars.append(Bar(day,f'{900000+stock:06}',f'합성{stock}',market,price*.997,price*1.02,price*.98,price,400000 if (index+stock)%3==0 else 100000,10000,20000))
    return bars


def test_fixed_rolling_folds_purge_and_future_shocks_cannot_change_earlier_training():
    bars=synthetic();report=evaluate_wfo(bars,source='DEMO_SYNTHETIC',now=NOW)
    assert report['condition_count']==48 and len(report['variants'])==48
    assert report['status']=='RESEARCH_ONLY_UNVERIFIED'
    assert all(m['valid_folds']>=3 for m in report['markets'].values())
    first=report['folds'][0];days=sorted({b.date for b in bars})
    assert first['train_begin']==days[20] and first['train_end']==days[79]
    assert first['test_begin']==days[80] and first['test_end']==days[99]
    assert any(v['train']['coverage']['3']['purged'] for v in first['train_variants'])
    changed=[replace(b,open=b.open*(1+.05*(days.index(b.date)%4)),high=b.high*(1+.05*(days.index(b.date)%4)),low=b.low*(1+.05*(days.index(b.date)%4)),close=b.close*(1+.05*(days.index(b.date)%4))) if b.date>first['train_end'] else b for b in bars]
    shocked=evaluate_wfo(changed,source='DEMO_SYNTHETIC',now=NOW)
    assert first['chosen_id']==shocked['folds'][0]['chosen_id']
    assert first['train_variants']==shocked['folds'][0]['train_variants']
    assert first['test']!=shocked['folds'][0]['test']
    keys=[(s['market'],s['day'],s['code']) for s in report['top_signals']]
    assert len(keys)==len(set(keys))
    assert any(f['test']['coverage']['5']['tail'] for f in report['folds'] if f['test'])
    assert report['data_fingerprint']!=shocked['data_fingerprint']


def test_real_calendar_missing_market_day_holiday_future_and_provisional_do_not_become_returns():
    bars=synthetic(actual_days=True);good=evaluate_wfo(bars,source='CSV_USER',now=NOW)
    assert good['status']=='RESEARCH_ONLY_UNVERIFIED' and '실제 성과' in good['source_notice']
    days=sorted({b.date for b in bars});missing=[b for b in bars if not(b.market=='KOSPI' and b.date==days[90])]
    blocked=evaluate_wfo(missing,source='CSV_USER',now=NOW)
    assert blocked['status']=='DATA_BLOCKED' and blocked['top_signals']==[]
    assert blocked['markets']['KOSPI']['missing_sessions']==[days[90]]
    holiday=bars+[replace(bars[0],date='2025-01-04')]
    assert evaluate_wfo(holiday,source='CSV_USER',now=NOW)['status']=='DATA_BLOCKED'
    assert evaluate_wfo(bars+[replace(bars[0],date='2026-10-27')],source='CSV_USER',now=NOW)['status']=='DATA_BLOCKED'
    current=bars+[replace(bars[0],date='2026-10-26')]
    excluded=evaluate_wfo(current,source='CSV_USER',now=NOW)
    assert excluded['data_end']==good['data_end'] and excluded['folds']==good['folds']
    assert excluded['excluded_provisional_days']==['2026-10-26']
    only=evaluate_wfo([replace(bars[0],date='2026-10-26')],source='CSV_USER',now=NOW)
    assert only['status']=='DATA_BLOCKED' and not only['latest_candidates']
    assert evaluate_wfo(bars,source='CSV_USER',coverage_quality=dict(state='ALERT'),now=NOW)['status']=='DATA_BLOCKED'


@pytest.mark.parametrize('damage',['duplicate','ohlc','future','calendar-fail'])
def test_bad_data_and_calendar_failure_do_not_get_a_candidate(monkeypatch,damage):
    import app.research_wfo as module
    bars=synthetic(actual_days=True)
    if damage=='duplicate':bars.append(bars[0])
    if damage=='ohlc':bars[0]=replace(bars[0],low=bars[0].high*2)
    if damage=='future':bars[0]=replace(bars[0],date='2028-01-02')
    if damage=='calendar-fail':monkeypatch.setattr(module,'market_sessions',lambda *a:(_ for _ in ()).throw(ValueError()))
    if damage in ('duplicate','ohlc'):
        with pytest.raises(ValueError):evaluate_wfo(bars,source='CSV_USER',now=NOW)
    else:assert not evaluate_wfo(bars,source='CSV_USER',now=NOW)['latest_candidates']


def test_stock_missing_day_is_not_shifted_and_market_sample_shortage_is_explicit():
    from app.research import build_features
    bars=synthetic();days=sorted({b.date for b in bars})
    bars=[b for b in bars if not(b.code=='900003' and b.market=='KOSPI' and b.date==days[97])]
    features,_=build_features(bars)
    row=next(r for r in features[('KOSPI',days[96])] if r['code']=='900003')
    assert row['future'][1] is None
    report=evaluate_wfo(bars,source='DEMO_SYNTHETIC',topk=6,now=NOW)
    assert any(f['test']['coverage']['1']['missing'] for f in report['folds'] if f['test'])
    few=evaluate_wfo(synthetic(100,markets=('KOSPI',)),source='DEMO_SYNTHETIC',now=NOW)
    assert few['status']=='INSUFFICIENT_DATA' and few['markets']['KOSDAQ']['issues']==['시장 자료 없음']
    assert few['markets']['KOSPI']['valid_folds']<3


def test_atomic_runs_source_method_reload_export_and_approval_preserve_holdout(tmp_path):
    store=HistoryStore(tmp_path/'research.db');bars=synthetic()
    store.insert_bars(bars,'DEMO_SYNTHETIC');before=store.load_bars('DEMO_SYNTHETIC')
    old=evaluate_research(bars);old_id=store.save_run('DEMO_SYNTHETIC',{},old)
    old_variant=old['variants'][0];store.decide_research('DEMO_SYNTHETIC',old_id,old_variant,True)
    run_id,report=run_saved(store,'DEMO_SYNTHETIC',METHOD,now=NOW)
    assert store.last_run('DEMO_SYNTHETIC',method=HOLDOUT)[0]==old_id
    assert store.last_run('DEMO_SYNTHETIC',method=METHOD)[0]==run_id
    assert store.last_run('CSV_USER',method=METHOD) is None
    chosen=report['latest_candidates']['KOSPI'];variant=next(v for v in report['variants'] if v['id']==chosen)
    store.decide_research('DEMO_SYNTHETIC',run_id,variant,True,method=METHOD)
    assert store.research_states('DEMO_SYNTHETIC')[old_variant['id']]=='APPROVED_RESEARCH'
    assert store.research_states('DEMO_SYNTHETIC',method=METHOD)[chosen]=='APPROVED_RESEARCH'
    with pytest.raises(ValueError):store.decide_research('CSV_USER',run_id,variant,True,method=METHOD)
    with pytest.raises(ValueError):store.decide_research('DEMO_SYNTHETIC',run_id,variant,True,method=HOLDOUT)
    saved=store.last_run('DEMO_SYNTHETIC',method=METHOD)[2]
    export_report(saved,tmp_path/'export')
    manifest=json.loads((tmp_path/'export/manifest.json').read_text(encoding='utf-8'))
    assert manifest['folds']==report['folds'] and manifest['data_fingerprint']==report['data_fingerprint']
    assert store.load_bars('DEMO_SYNTHETIC')==before
    bad=deepcopy(report);bad['top_signals'][0].pop('code')
    with pytest.raises(KeyError):store.save_run('DEMO_SYNTHETIC',{},bad)
    assert store.last_run('DEMO_SYNTHETIC',method=METHOD)[0]==run_id


def test_source_method_changed_during_manual_worker_never_attaches_old_result(tmp_path):
    store=HistoryStore(tmp_path/'research.db');store.insert_bars(synthetic(),'DEMO_SYNTHETIC')
    source=['DEMO_SYNTHETIC'];method=[METHOD];pending=[];display=[]
    fake=SimpleNamespace(history=store,research_source=SimpleNamespace(currentText=lambda:source[0]),research_method=SimpleNamespace(currentData=lambda:method[0]),research_k=SimpleNamespace(value=lambda:5),research_reference=None,run_task=lambda fn,done,label:pending.append((fn,done)),show_research=lambda result:display.append(result))
    gui_method('run_research')(fake)
    source[0]='CSV_USER';output=pending[0][0]();pending[0][1](output)
    assert display==[] and store.last_run('DEMO_SYNTHETIC',method=METHOD)
    source[0]='DEMO_SYNTHETIC';gui_method('run_research')(fake);method[0]=HOLDOUT
    output=pending[1][0]();pending[1][1](output);assert display==[]


def test_actual_wfo_gui_display_detail_and_research_only_approval(tmp_path):
    store=HistoryStore(tmp_path/'research.db');store.insert_bars(synthetic(),'DEMO_SYNTHETIC')
    run_id,report=run_saved(store,'DEMO_SYNTHETIC',METHOD,now=NOW)
    fake,labels=window();fake.history=store;fake.research_method=SimpleNamespace(currentData=lambda:METHOD);fake.research_run_id=run_id
    fake.show_wfo_research=lambda result,show_message:gui_method('show_wfo_research',cell=lambda x:x)(fake,result,show_message=show_message)
    gui_method('show_research',cell=lambda x:x)(fake,report,show_message=False)
    assert len(fake.research_table.rows)==48 and '반복검증' in labels['result']
    gui_method('research_detail')(fake);assert '당시 선택' in labels['detail'] and '지수가 아닙니다' in labels['detail']
    chosen=report['latest_candidates']['KOSPI'];fake.research_table.selected=next(i for i,v in enumerate(report['variants']) if v['id']==chosen)
    fake.show_research=lambda *a,**k:None
    box=SimpleNamespace(StandardButton=SimpleNamespace(Yes=1),question=lambda *a:1)
    gui_method('review_research',QMessageBox=box)(fake,True)
    assert store.research_states('DEMO_SYNTHETIC',method=METHOD)[chosen]=='APPROVED_RESEARCH'
    # A modal confirmation cannot retarget approval to a newly selected source.
    prior=store.research_states('DEMO_SYNTHETIC',method=METHOD)
    box.question=lambda *a:setattr(fake,'research_source',SimpleNamespace(currentText=lambda:'CSV_USER')) or 1
    gui_method('review_research',QMessageBox=box)(fake,False)
    assert store.research_states('DEMO_SYNTHETIC',method=METHOD)==prior



def confirm_krx_snapshots(store,bars,*,checked_at=None):
    counts={}
    for bar in bars:counts[(bar.market,bar.date)]=counts.get((bar.market,bar.date),0)+1
    with store.connect() as db:
        for (market,day),count in counts.items():
            stamp=checked_at or (datetime.fromisoformat(day+'T10:00:00+09:00')+timedelta(days=1)).isoformat()
            db.execute('INSERT OR REPLACE INTO fetched_days(source,market,day,count,checked_at,normalizer_version) VALUES(?,?,?,?,?,1)',('KRX',market,day,count,stamp))


def test_krx_reconfirmation_is_asof_bound_and_matches_actual_evaluation_values(tmp_path,monkeypatch):
    store=HistoryStore(tmp_path/'research.db');bars=synthetic(actual_days=True)
    store.insert_bars(bars,'KRX')
    _,blocked=run_saved(store,'KRX',METHOD,now=NOW)
    assert blocked['status']=='DATA_BLOCKED'
    confirm_krx_snapshots(store,bars)
    _,good=run_saved(store,'KRX',METHOD,now=NOW)
    assert good['status']=='RESEARCH_ONLY_UNVERIFIED'
    confirm_krx_snapshots(store,bars,checked_at=(NOW+timedelta(days=1)).isoformat())
    _,future=run_saved(store,'KRX',METHOD,now=NOW)
    assert future['status']=='DATA_BLOCKED' and future['coverage_quality']['unconfirmed_krx_market_days']>0
    confirm_krx_snapshots(store,bars)
    original=store.recommendation_bars
    monkeypatch.setattr(store,'recommendation_bars',lambda *a,**k:[replace(b,close=b.close*1.0001) for b in original(*a,**k)])
    assert run_saved(store,'KRX',METHOD,now=NOW)[1]['status']=='DATA_BLOCKED'


def test_auto_path_saves_wfo_and_never_retargets_another_method_or_source(tmp_path,monkeypatch):
    store=HistoryStore(tmp_path/'research.db');bars=synthetic(actual_days=True);store.insert_bars(bars,'KRX');confirm_krx_snapshots(store,bars)
    fetch=[];saved_settings=[];shown=[];pending=[];labels=[]
    monkeypatch.setattr(store,'fetch_range',lambda client,start,end:fetch.append((start,end)) or dict(rows=0))
    source=['CSV_USER'];method=[HOLDOUT]
    fake=SimpleNamespace(cfg=dict(research_auto_daily=True),creds=SimpleNamespace(get=lambda name:'synthetic-not-a-real-key'),work=None,history=store,research_reference=None,
        settings=SimpleNamespace(save=lambda:saved_settings.append(True)),research_source=SimpleNamespace(currentText=lambda:source[0]),research_method=SimpleNamespace(currentData=lambda:method[0]),
        show_research=lambda report,show_message:shown.append(report),statusBar=lambda:SimpleNamespace(showMessage=lambda text:labels.append(text)),research_status=SimpleNamespace(setText=lambda text:labels.append(text)),run_task=lambda fn,done,label:pending.append((fn,done)))
    fake_now=NOW.replace(hour=18)
    clock=SimpleNamespace(now=lambda tz=None:fake_now if tz is not None else fake_now.replace(tzinfo=None))
    gui_method('maybe_daily_research',datetime=clock,timedelta=timedelta,KST=KST,KRXDailyClient=lambda key:object())(fake)
    output=pending[0][0]();pending[0][1](output)
    assert fetch and saved_settings and shown==[]
    latest=store.last_run('KRX',method=METHOD)
    assert latest and latest[2]['method_version']==METHOD
    assert store.research_states('KRX',method=METHOD)=={}


def test_no_fold_or_training_observations_is_not_a_best_condition():
    bars=[replace(b,volume=100000) for b in synthetic()]
    report=evaluate_wfo(bars,source='DEMO_SYNTHETIC',now=NOW)
    assert report['status']=='INSUFFICIENT_DATA' and report['latest_candidates']=={}
    assert all(f['chosen_id'] is None and f['test'] is None for f in report['folds'])
    assert all(v['train']['net3'] is None for v in report['variants'])


def test_insufficient_candidate_cannot_be_approved_as_research_and_sources_cannot_mix(tmp_path):
    store=HistoryStore(tmp_path/'research.db');store.insert_bars(synthetic(100),'DEMO_SYNTHETIC')
    run_id,report=run_saved(store,'DEMO_SYNTHETIC',METHOD,now=NOW)
    chosen=report['latest_candidates']['KOSPI'];variant=next(v for v in report['variants'] if v['id']==chosen)
    with pytest.raises(ValueError):store.decide_research('DEMO_SYNTHETIC',run_id,variant,True,method=METHOD)
    with pytest.raises(ValueError):store.save_run('CSV_USER',{},report)
    assert store.last_run('CSV_USER',method=METHOD) is None



def test_switching_wfo_and_holdout_restores_matching_headers_and_values(tmp_path):
    store=HistoryStore(tmp_path/'research.db');bars=synthetic();store.insert_bars(bars,'DEMO_SYNTHETIC')
    run_id,wfo=run_saved(store,'DEMO_SYNTHETIC',METHOD,now=NOW);old=evaluate_research(bars)
    fake,labels=window();fake.history=store;selected=[METHOD];fake.research_method=SimpleNamespace(currentData=lambda:selected[0]);fake.research_run_id=run_id
    headers=[];fake.research_table.setHorizontalHeaderLabels=lambda items:headers.append(items)
    fake.show_wfo_research=lambda result,show_message:gui_method('show_wfo_research',cell=lambda x:x)(fake,result,show_message=show_message)
    for method,report in ((METHOD,wfo),(HOLDOUT,old),(METHOD,wfo)):
        selected[0]=method;gui_method('show_research',cell=lambda x:x)(fake,report,show_message=False)
        assert len(fake.research_table.rows)==len(report['variants'])
        assert ('최신' in headers[-1][2])==(method==METHOD)
        row=fake.research_table.rows[0]
        assert row[0]==report['variants'][0]['name']
        if method==METHOD:assert '상세 확인' in row[8]
        else:assert ' → ' in row[8]
    assert headers[0]==headers[2] and headers[0]!=headers[1]



def test_gui_reload_only_selected_source_method_and_old_approvals_stay_available(tmp_path):
    store=HistoryStore(tmp_path/'research.db');bars=synthetic();store.insert_bars(bars,'DEMO_SYNTHETIC')
    old=evaluate_research(bars);old_id=store.save_run('DEMO_SYNTHETIC',{},old)
    new_id,new=run_saved(store,'DEMO_SYNTHETIC',METHOD,now=NOW)
    source=['DEMO_SYNTHETIC'];method=[METHOD];shown=[]
    fake=SimpleNamespace(history=store,research_report=None,research_source=SimpleNamespace(currentText=lambda:source[0]),research_method=SimpleNamespace(currentData=lambda:method[0]),research_status=SimpleNamespace(setText=lambda text:None,text=lambda:''),show_research=lambda value,show_message:shown.append(value))
    gui_method('refresh_research')(fake)
    assert fake.research_run_id==new_id and shown[-1]['method_version']==METHOD
    method[0]=HOLDOUT;fake.research_report=None;gui_method('refresh_research')(fake)
    assert fake.research_run_id==old_id and 'method_version' not in shown[-1]
    source[0]='CSV_USER';fake.research_report=None;shown.clear();gui_method('refresh_research')(fake);assert shown==[]


def test_last_run_stops_at_first_matching_method_without_fetching_all_bodies(tmp_path,monkeypatch):
    store=HistoryStore(tmp_path/'research.db');bars=synthetic();old=evaluate_research(bars)
    store.save_run('DEMO_SYNTHETIC',{},old)
    wfo=evaluate_wfo(bars,source='DEMO_SYNTHETIC',now=NOW);wfo_id=store.save_run('DEMO_SYNTHETIC',{},wfo)
    old_id=store.save_run('DEMO_SYNTHETIC',{},old)
    connect=store.connect;observed=[]
    class Cursor:
        def __init__(self,inner):self.inner=inner
        def fetchone(self):return self.inner.fetchone()
        def fetchall(self):pytest.fail('unbounded research JSON fetchall')
        def __iter__(self):
            for row in self.inner:observed.append(row['id']);yield row
    class Connection:
        def __enter__(self):self.inner=connect();return self
        def __exit__(self,*args):self.inner.close()
        def execute(self,query,args):return Cursor(self.inner.execute(query,args))
    monkeypatch.setattr(store,'connect',lambda:Connection())
    assert store.last_run('DEMO_SYNTHETIC')[0]==old_id and observed==[]
    assert store.last_run('DEMO_SYNTHETIC',method=METHOD)[0]==wfo_id and observed==[old_id,wfo_id]
