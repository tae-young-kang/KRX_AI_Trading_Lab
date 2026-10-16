import csv
from datetime import date, timedelta
from app.config import Settings
from app.backtest import Bar, load_ohlcv_csv, run_backtest, run_experiments, export_results
from app.storage import Database
from app.shadow import ShadowEngine
from app.market import Stock
from app.strategy import evaluate


def fixture_bars():
    bars=[]
    start=date(2026,2,1)
    # 20 days 100 volume, signal on next bar at 300 volume +3% change.
    for i in range(26):
        d=(start+timedelta(days=i)).isoformat()
        c=100 if i<20 else (103 if i==20 else 104)
        v=300 if i==20 else 100
        o=104 if i==21 else c
        h=106 if i==21 else max(o,c)+1
        l=96 if i==21 else min(o,c)-1
        bars.append(Bar(d,'000001','TEST','KOSPI',o,h,l,c,v,300.0,10000.0))
    return bars


def test_next_session_gap_and_stop_conservative(tmp_path):
    cfg=dict(Settings(tmp_path).values)
    cfg.update(market='KOSPI', on_price=False, min_change_pct=2, max_change_pct=8,
               backtest_volume_ratio=200,exit_stop_pct=2,exit_take_pct=4,
               exit_max_days=3,backtest_gap_limit=3)
    r=run_backtest(fixture_bars(),cfg)
    assert r['summary']['bought']==1
    assert r['trades'][0]['entry_day']==fixture_bars()[21].date
    assert r['trades'][0]['exit_reason']=='STOP_ENTRY_DAY'
    assert r['trades'][0]['pnl']<0
    assert r['summary']['winrate']==0
    assert r['summary']['realized_pnl']<0
    cfg['backtest_gap_limit']=0.2
    r2=run_backtest(fixture_bars(),cfg)
    assert r2['summary']['bought']==0
    assert r2['summary']['skip_gap']==1


def test_volume_ratio_expansion(tmp_path):
    cfg=dict(Settings(tmp_path).values)
    cfg.update(min_change_pct=2,max_change_pct=8)
    results=run_experiments(fixture_bars(),cfg)
    assert len(results)==14
    for threshold in (100,120,150,200,300):
        assert any(r['name']==f'거래량 {threshold}%' for r in results)
    files=export_results(tmp_path/'out',results)
    assert len(files)==4
    assert all(p.is_file() for p in files)


def test_csv_and_no_marketcap_is_fail_closed(tmp_path):
    p=tmp_path/'krx.csv'
    with p.open('w',newline='',encoding='utf-8-sig') as f:
        w=csv.writer(f);w.writerow(['날짜','종목코드','종목명','시장','시가','고가','저가','종가','거래량'])
        w.writerow(['2026-10-15','005930','삼성전자','KOSPI','100','105','99','103','100'])
    rows=load_ohlcv_csv(p)
    assert rows[0].code=='005930'
    cfg=dict(Settings(tmp_path).values)
    assert run_backtest(rows,cfg)['summary']['signals']==0


def test_shadow_no_same_day_entry_and_idempotent(tmp_path):
    cfg=dict(Settings(tmp_path).values)
    db=Database(tmp_path/'t.sqlite')
    sh=ShadowEngine(db)
    assert sh.initialize_defaults(cfg,'DEMO')==11
    st=Stock('900001','DEMO','KOSPI',10000,3,400,300,10000,source='DEMO')
    sid=db.save_scan([st],evaluate([st],cfg),cfg,'전체','DEMO')
    assert sh.process_scan(sid,[st],today='2026-10-12')==[]
    assert sh.process_scan(sid,[st],today='2026-10-12')==[]
    assert sh.trade_history()==[]
    assert any(p['status']=='PENDING' for p in sh.pending_history())
    st.price=10100
    sid2=db.save_scan([st],evaluate([st],cfg),cfg,'전체','DEMO')
    events=sh.process_scan(sid2,[st],today='2026-10-13')
    assert any(e.event=='BUY' for e in events)
    assert any(p['status']=='FILLED' for p in sh.pending_history())
    assert len(sh.standings())==11
    assert len(sh.daily_history())>=11


def test_shadow_separates_live_and_demo(tmp_path):
    cfg=dict(Settings(tmp_path).values)
    db=Database(tmp_path/'t.sqlite')
    sh=ShadowEngine(db)
    sh.initialize_defaults(cfg,'DEMO')
    sh.initialize_defaults(cfg,'KIWOOM')
    assert len(sh.strategies())==22
    st=Stock('900001','DEMO','KOSPI',10000,3,400,300,10000,source='DEMO')
    sid=db.save_scan([st],evaluate([st],cfg),cfg,'전체','DEMO')
    sh.process_scan(sid,[st],today='2026-10-12')
    # Only demo strategies have simulated signals.
    assert all(p['strategy_id']<=11 for p in sh.pending_history())
