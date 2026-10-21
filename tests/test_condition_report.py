"""Condition report over temporary synthetic ledgers, no GUI or clients."""
from copy import deepcopy
import pytest
from app.config import DEFAULTS
from app.storage import Database
from app.condition_report import report
from test_trade_review import records
from test_telegram_orders import sandbox,update


def add(db,cfg):
    with db.connect() as conn:
        for row in records(cfg):
            conn.execute('''INSERT INTO paper_trades(code,name,market,opened_at,qty,buy_ref,buy_fill,buy_fee,
                stop_pct,take_pct,max_days,highwater,source,strat_json,closed_at,pnl,pnl_pct,exit_settings_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (row['code'],'합성','KOSPI',row['opened_at'],1,10000,10000,0,2,4,2,10000,
                 'DEMO' if cfg['data_mode']=='DEMO' else 'KIWOOM',row['strat_json'],row['closed_at'],100,1,row['exit_settings_json']))


def test_condition_report_shares_review_engine_keeps_modes_and_reads_only(tmp_path):
    db=Database(tmp_path/'practice.db');cfg=deepcopy(DEFAULTS);cfg['unexpected_secret']='PRIVATE_MARKER'
    add(db,cfg)
    add(db,dict(cfg,data_mode='KIWOOM',kiwoom_env='REAL',min_value_eok=222))
    before=db.path.read_bytes()
    text=report(db,'DEMO')
    assert '조건 비교 1개' in text and '완료 40건' in text and '승 40/패 0/본전 0' in text
    assert '추가 검증 후보' in text and '승률 100.0%' in text
    assert 'AI 학습·독립 미래 검증·최적 조건 확정은 아니에요' in text
    assert 'PRIVATE_MARKER' not in text and len(text)<3900
    assert db.path.read_bytes()==before and db.proposals()==[]
    assert '조건 비교 1개' in report(db,'REAL_QUOTES') and '조건 비교 0개' in report(db,'MOCK')


def test_missing_costs_open_trades_and_pages_never_fabricate_results(tmp_path):
    db=Database(tmp_path/'practice.db');cfg=deepcopy(DEFAULTS)
    for value in (100,200,300):add(db,dict(cfg,min_value_eok=value))
    assert '다음: /조건성적 2' in report(db,'DEMO')
    assert '2/2페이지' in report(db,'DEMO',page=2)
    assert '2페이지까지' in report(db,'DEMO',page=3)
    with db.connect() as conn:conn.execute('UPDATE paper_trades SET exit_settings_json=NULL')
    assert '청산 비용 가정 미확인' in report(db,'DEMO')
    with db.connect() as conn:conn.execute('UPDATE paper_trades SET closed_at=NULL,pnl=NULL,pnl_pct=NULL')
    text=report(db,'DEMO')
    assert '승률 미산출' in text and '앞 기록 평균 미산출' in text and '승률 0.0%' not in text


def test_private_command_is_readable_after_stop_without_order_or_strategy_change(tmp_path,sandbox):
    db=Database(tmp_path/'practice.db');add(db,deepcopy(DEFAULTS))
    service,broker,_,_=sandbox;service.condition_report=lambda page:report(db,'DEMO',page=page)
    bad=update(sandbox,1,'/조건성적');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    service.handle(update(sandbox,2,'/중지'))
    assert '완료 40건' in service.handle(update(sandbox,3,'/조건성적'))
    assert '사용법' in service.handle(update(sandbox,4,'/조건성적 0'))
    assert broker.calls==[] and db.proposals()==[]


def test_invalid_page_or_minimum_is_rejected(tmp_path):
    db=Database(tmp_path/'practice.db')
    with pytest.raises(ValueError):report(db,'DEMO',page=True)
    with pytest.raises(ValueError):report(db,'DEMO',min_train=1)
