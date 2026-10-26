"""Read-only classified performance over synthetic temporary ledgers."""
from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
import ast
import re
import pytest
from app.config import DEFAULTS
from app.classified_performance import report
from app.krx_history import HistoryStore
from app.morning_virtual import MorningVirtual,VenueTrade,record_path
from app.morning_venue_policy import KRX_OPEN
from app.storage import Database
from app.recommendation_classification import Classification
from app.actual_trade_records import import_csv
from app.trading_clock import KST
from test_actual_trade_records import blob,row
from test_telegram_orders import sandbox,update

NOW=datetime(2026,10,22,16,tzinfo=KST)


def rig(root,day='2026-10-20',*,legacy=False,fee=0):
    HistoryStore(root/'research.db')
    cfg=deepcopy(DEFAULTS);cfg.update(data_mode='KIWOOM',kiwoom_env='REAL',paper_fee_pct=fee,paper_sell_tax_pct=fee,paper_slippage_pct=0)
    tracker=MorningVirtual(Database(record_path(root,'REAL_QUOTES',day)))
    at=datetime.fromisoformat(day+'T09:00:00+09:00')
    tracker.prepare(day=day,mode='REAL_QUOTES',origin='fixed',targets=[dict(code=f'90000{i}',name='합성'+str(i),market='KOSPI',nxt_eligible=False) for i in (1,2,3)],settings=cfg,total_won=30000,now=at.replace(hour=7),venue_policy=None if legacy else KRX_OPEN)
    for code in ('900001','900002'):tracker.enter(VenueTrade('REAL_QUOTES',code,'KRX',1000,at,at,'FIRST_TRADE','a'*64),now=at)
    return tracker


def classify(root,day,code,state):
    command={'NOT_BOUGHT':f'/추천미구매 {day} {code}','ACTUAL_LINK':f'/추천매수연결 {day} {code} buy-{day}'}[state]
    control=Classification(root);context=dict(mode='REAL_QUOTES',scope='private',session='receiver',active=True,now=NOW)
    preview=control.handle(command,**context);token=re.search(r'/추천분류확인 ([a-f0-9]{16})',preview)[1]
    assert '저장했어요' in control.handle('/추천분류확인 '+token,**context)


def before(root):return {p:p.read_bytes() for p in root.rglob('*.db') if p.name!='telegram.db'}


def test_split_sales_current_classification_cost_denominator_and_future_sales_excluded(tmp_path):
    tracker=rig(tmp_path);day='2026-10-20'
    import_csv(tmp_path/'actual_fill_records.db',blob(row('buy-'+day,filled_at=day+'T09:00:00+09:00')),now=NOW)
    classify(tmp_path,day,'900001','ACTUAL_LINK');classify(tmp_path,day,'900002','NOT_BOUGHT')
    trade=next(t for t in tracker.db.trades() if t['code']=='900002')
    tracker.db.paper_sell(trade['id'],4,1100,'부분 매도',0,0,0,observed_at=datetime(2026,10,20,10,tzinfo=KST))
    tracker.db.paper_close(trade['id'],900,'가상 손절',0,0,0,observed_at=datetime(2026,10,21,10,tzinfo=KST))
    saved=before(tmp_path)
    text=report(tmp_path,'REAL_QUOTES',day,now=NOW)
    assert '실제입력 연결 1 · 미구매 사용자확인 1 · 미확인 1종목' in text
    assert '+400.00원' in text and '4,000.00원' in text and '+10.00%' in text
    assert '승 0 / 패 0 / 본전 0 · 승률 미산출' in text and '열린 보유 1' in text
    tomorrow=report(tmp_path,'REAL_QUOTES','2026-10-21',now=NOW)
    assert '-600.00원' in tomorrow and '6,000.00원' in tomorrow and '-10.00%' in tomorrow
    assert '승 0 / 패 1 / 본전 0 · 승률 0.0%' in tomorrow
    assert '시장초과수익: 미산출' in text and '전체 추천 전략 승률이 아니에요' in text
    assert saved==before(tmp_path)


def test_unentered_and_missing_prices_are_not_zero_return_or_completed_winrate(tmp_path):
    tracker=rig(tmp_path);classify(tmp_path,'2026-10-20','900002','NOT_BOUGHT');classify(tmp_path,'2026-10-20','900003','NOT_BOUGHT')
    text=report(tmp_path,'REAL_QUOTES','2026-10-21',now=NOW)
    assert '미진입 1 · 열린 보유 1 (당일 가격 없음 1)' in text
    assert '실현손익 미산출' in text and '수익률 미산출' in text and '승률 미산출' in text


def test_today_and_carryover_pages_and_legacy_do_not_block_new_cohorts(tmp_path):
    rig(tmp_path,'2026-10-19',legacy=True);rig(tmp_path,'2026-10-20');rig(tmp_path,'2026-10-21')
    first=report(tmp_path,'REAL_QUOTES','2026-10-21',now=NOW)
    assert '이전 시장정책 목록' in first and '/추천성적분리 2026-10-21 2' in first
    assert '목록 2026-10-20' in report(tmp_path,'REAL_QUOTES','2026-10-21',now=NOW,page=2)
    assert '목록 2026-10-21' in report(tmp_path,'REAL_QUOTES','2026-10-21',now=NOW,page=3)
    assert '3페이지까지' in report(tmp_path,'REAL_QUOTES','2026-10-21',now=NOW,page=4)


@pytest.mark.parametrize('damage',['amount','sale','mode','classification','mark'])
def test_corrupt_evidence_never_becomes_zero_performance(tmp_path,damage):
    tracker=rig(tmp_path);classify(tmp_path,'2026-10-20','900002','NOT_BOUGHT')
    with tracker.db.connect() as db:
        if damage=='amount':db.execute('UPDATE paper_trades SET buy_fee=-1')
        elif damage=='mode':db.execute("UPDATE morning_cohort SET payload=replace(payload,'REAL_QUOTES','MOCK')")
        elif damage=='mark':db.execute("UPDATE morning_marks SET price=-1")
    if damage=='sale':
        tracker.db.paper_sell(next(t for t in tracker.db.trades() if t['code']=='900002')['id'],1,1100,'매도',0,0,0,observed_at=datetime(2026,10,20,10,tzinfo=KST))
        with tracker.db.connect() as db:db.execute('UPDATE paper_exits SET pnl=999999')
    if damage=='classification':
        with Database(tmp_path/'research.db').connect() as db:db.execute("UPDATE recommendation_classifications SET hash='bad'")
    with pytest.raises(ValueError):report(tmp_path,'REAL_QUOTES','2026-10-20',now=NOW)


def test_stale_classification_and_missing_files_keep_unknown_without_creation(tmp_path):
    empty=tmp_path/'empty';assert '0%가 아닙니다' in report(empty,'REAL_QUOTES','2026-10-20',now=NOW) and not empty.exists()
    rig(tmp_path);classify(tmp_path,'2026-10-20','900002','NOT_BOUGHT')
    import_csv(tmp_path/'actual_fill_records.db',blob(row('new',code='900002',filled_at='2026-10-20T09:00:00+09:00')),now=NOW)
    assert '미구매 사용자확인 0 · 미확인 3종목' in report(tmp_path,'REAL_QUOTES','2026-10-20',now=NOW)
    assert '다른 모드와 섞지' in report(tmp_path,'MOCK','2026-10-20',now=NOW)
    for day,page in [('2026-10-23',1),('2026-10-20',0),('2026-10-20',True)]:
        with pytest.raises(ValueError):report(tmp_path,'REAL_QUOTES',day,now=NOW,page=page)


def test_private_telegram_gui_callback_defaults_korean_today_and_never_orders(tmp_path,sandbox):
    from test_recommendation_request import gui_method
    tracker=rig(tmp_path);classify(tmp_path,'2026-10-20','900002','NOT_BOUGHT')
    service,broker,_,clock=sandbox;clock[0]=NOW
    tree=ast.parse((Path(__file__).resolve().parents[1]/'app/gui.py').read_text(encoding='utf-8'))
    callback=next(k.value for n in ast.walk(tree) if isinstance(n,ast.Call) for k in n.keywords if k.arg=='classified_performance')
    fake=SimpleNamespace(cfg=dict(data_mode='KIWOOM',kiwoom_env='REAL'))
    function=eval(compile(ast.Expression(callback),'fake-gui','eval'),{'self':fake,'classified_performance_report':report,'data_dir':lambda:tmp_path})
    service.classified_performance=function
    wrong=update(sandbox,1,'/추천성적분리');wrong['message']['from']['id']=99
    assert service.handle(wrong) is None
    saved=before(tmp_path)
    text=service.handle(update(sandbox,2,'/추천성적분리'))
    assert '결산일 2026-10-22' in text and '당일 가격 없음 1' in text
    assert '사용법' in service.handle(update(sandbox,3,'/추천성적분리 잘못'))
    fake.cfg['kiwoom_env']='MOCK'
    assert '다른 모드와 섞지' in service.handle(update(sandbox,4,'/추천성적분리'))
    assert saved==before(tmp_path) and broker.calls==[]
    worker=ast.parse((Path(__file__).resolve().parents[1]/'app/telegram_worker.py').read_text(encoding='utf-8'))
    assert any(isinstance(n,ast.keyword) and n.arg=='classified_performance' for n in ast.walk(worker))



def test_positive_costs_use_allocated_entry_cost_not_sale_value(tmp_path):
    tracker=rig(tmp_path,fee=1);classify(tmp_path,'2026-10-20','900002','NOT_BOUGHT')
    trade=next(t for t in tracker.db.trades() if t['code']=='900002')
    tracker.db.paper_sell(trade['id'],3,1100,'부분 매도',1,1,0,observed_at=datetime(2026,10,20,10,tzinfo=KST))
    text=report(tmp_path,'REAL_QUOTES','2026-10-20',now=NOW)
    assert '+204.00원' in text and '3,030.00원' in text and '+6.73%' in text


def test_legacy_completion_without_sales_is_excluded_not_counted_as_win(tmp_path):
    tracker=rig(tmp_path);classify(tmp_path,'2026-10-20','900002','NOT_BOUGHT')
    with tracker.db.connect() as db:
        db.execute("UPDATE paper_trades SET closed_at='2026-10-20T10:00:00+09:00',remaining_qty=0,pnl=NULL WHERE code='900002'")
    text=report(tmp_path,'REAL_QUOTES','2026-10-20',now=NOW)
    assert '매도 근거 없는 완료 1건 제외' in text and '승률 미산출' in text and '열린 보유 0' in text


@pytest.mark.parametrize('field',['buy_fill','buy_fee'])
def test_entry_cost_must_match_frozen_reference_and_settings(tmp_path,field):
    tracker=rig(tmp_path,fee=1);classify(tmp_path,'2026-10-20','900002','NOT_BOUGHT')
    with tracker.db.connect() as db:db.execute('UPDATE paper_trades SET '+field+'='+field+"+10 WHERE code='900002'")
    # Even self-consistent per-sale arithmetic cannot repair altered entry costs.
    trade=next(t for t in tracker.db.trades() if t['code']=='900002')
    tracker.db.paper_sell(trade['id'],3,1100,'부분 매도',1,1,0,observed_at=datetime(2026,10,20,10,tzinfo=KST))
    with pytest.raises(ValueError,match='매수원가'):report(tmp_path,'REAL_QUOTES','2026-10-20',now=NOW)


@pytest.mark.parametrize('kind',['partial','closed','naive'])
def test_future_or_naive_sales_are_not_todays_performance(tmp_path,kind):
    tracker=rig(tmp_path);classify(tmp_path,'2026-10-20','900002','NOT_BOUGHT')
    trade=next(t for t in tracker.db.trades() if t['code']=='900002')
    future=NOW.replace(hour=17)
    if kind=='closed':tracker.db.paper_close(trade['id'],1100,'미래 매도',0,0,0,observed_at=future)
    else:
        tracker.db.paper_sell(trade['id'],1,1100,'미래 매도',0,0,0,observed_at=future)
        if kind=='naive':
            with tracker.db.connect() as db:db.execute("UPDATE paper_exits SET sold_at='2026-10-22T10:00:00'")
    with pytest.raises(ValueError,match='시각'):report(tmp_path,'REAL_QUOTES','2026-10-22',now=NOW)
