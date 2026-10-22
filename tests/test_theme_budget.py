import json
import pytest
from app.backtest import Bar
from app.krx_history import HistoryStore
from app.recommendation_outcomes import SessionEvidence
from app.theme_store import prepare
from app.theme_budget import preview_latest,preview_text
from test_telegram_orders import sandbox,update


def test_single_name_quantity_and_amount_plan_uses_frozen_selection(tmp_path):
    from app.theme_budget import plan_text
    from test_theme_delivery import setup
    history,args=setup(tmp_path)
    shares=plan_text(history,'DEMO','/테마1계획 연습 10주 익절 4.25 손절 1.75')
    assert '요청 10주' in shares and '예상 10주' in shares
    assert '예상 확보액 1,030원' in shares and '익절 +4.25%' in shares
    money=plan_text(history,'DEMO','/테마1계획 900001 1,000원')
    assert '예상 9주' in money and '남는 예산 73원' in money
    with history.connect() as db:db.execute('UPDATE daily_bars SET close=105')
    assert plan_text(history,'DEMO','/테마1계획 900001 1000원')==money
    assert '총금액' in plan_text(history,'DEMO','/테마1계획 전체 10주')
    assert '사용법' in plan_text(history,'DEMO','/테마1계획 연습 1,00원')
    with pytest.raises(ValueError,match='없는 종목'):
        plan_text(history,'DEMO','/테마1계획 다른종목 10주')


def test_whole_theme_plan_pages_keep_the_same_budget(tmp_path):
    from app.theme_budget import plan_text
    from test_theme_delivery import setup
    history,args=setup(tmp_path)
    with history.connect() as db:original=db.execute('SELECT * FROM daily_bars').fetchall()
    for index in range(2,7):
        history.insert_bars([Bar(r['day'],f'90000{index}',f'종목{index}','KOSPI',100,110,99,100,1000,100,4000)
                            for r in original],'DEMO')
    prepare(history,**args)
    first=plan_text(history,'DEMO','/테마1계획 전체 1만원')
    second=plan_text(history,'DEMO','/테마1계획 전체 1만원 페이지 2')
    assert '총예산 10,000원 · 종목당 1,666원' in first and '총예산 10,000원 · 종목당 1,666원' in second
    assert '900006' not in first and '900006' in second
    assert '/테마1계획 전체 10000원 페이지 2' not in first
    assert '/테마1계획 전체 1만원 페이지 2' in first


def test_theme_plan_command_is_authorized_replayable_and_never_orders(tmp_path,sandbox):
    from app.theme_budget import plan_text
    from test_theme_delivery import setup
    history,_=setup(tmp_path)
    service,broker,_,_=sandbox
    service.orders_enabled=False;service.theme_plan=lambda text:plan_text(history,'DEMO',text)
    bad=update(sandbox,1,'/테마1계획 연습 10주');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    event=update(sandbox,2,'/테마1계획 연습 10주')
    reply=service.handle(event)
    assert '예상 10주' in reply and reply==service.handle(event)
    assert '주문 안 함' in reply and broker.calls==[]


def test_same_name_requires_code_and_missing_reference_cannot_be_guessed(tmp_path):
    from app.theme_budget import plan_text
    from test_theme_delivery import setup
    history,args=setup(tmp_path)
    with history.connect() as db:original=db.execute('SELECT * FROM daily_bars').fetchall()
    history.insert_bars([Bar(r['day'],'900002','연습','KOSPI',100,110,99,100,1000,100,4000) for r in original],'DEMO')
    prepare(history,**args)
    with pytest.raises(ValueError,match='같은 이름'):
        plan_text(history,'DEMO','/테마1계획 연습 10주')
    assert '예상 10주' in plan_text(history,'DEMO','/테마1계획 900001 10주')
    with history.connect() as db:
        rows=db.execute('SELECT id,result_json FROM theme_alerts').fetchall()
        for row in rows:
            payload=json.loads(row['result_json']);payload['result'].pop('reference_close',None)
            db.execute('UPDATE theme_alerts SET result_json=? WHERE id=?',(json.dumps(payload),row['id']))
    with pytest.raises(ValueError,match='기준 가격'):
        plan_text(history,'DEMO','/테마1계획 900001 10주')


def test_budget_uses_frozen_run_close_not_mutated_cache(tmp_path):
    history=HistoryStore(tmp_path/'hist.db')
    days=('2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19')
    history.insert_bars([Bar(d,'900001','연습','KOSPI',100,110,99,100,1000,100,4000) for d in days],'DEMO')
    prepare(history,sessions=SessionEvidence(days,days[0],days[-1],'DEMO'),through=days[-1],mode='DEMO',source='DEMO')
    with history.connect() as db: db.execute('UPDATE daily_bars SET close=105')
    result=preview_latest(history,mode='DEMO',total_won=1000,fee_bps=0,buffer_bps=0)
    assert result['targets'][0]['reference_won']==100 and result['targets'][0]['quantity']==10
    assert result['executable'] is False and result['state']=='FUNDS_UNKNOWN'
    text=preview_text(result)
    assert '현재가·시초가가 아닙니다' in text and '주문 안 함' in text
    assert '익절 +2.00% · 손절 -3.00%' in text
    custom=preview_latest(history,mode='DEMO',total_won=1000,fee_bps=0,buffer_bps=0,take_bps=425,stop_bps=175)
    assert custom['take_bps']==425 and custom['stop_bps']==175
    assert '익절 +4.25% · 손절 -1.75%' in preview_text(custom)
    assert custom['executable'] is False and custom['targets']==result['targets']
    for invalid in (0,10000,True,1.5):
        with pytest.raises(ValueError):
            preview_latest(history,mode='DEMO',total_won=1000,fee_bps=0,buffer_bps=0,take_bps=invalid)
    with history.connect() as db:
        row=db.execute('SELECT id,result_json FROM theme_alerts').fetchone()
        payload=json.loads(row['result_json']);payload['result'].pop('reference_close')
        db.execute('UPDATE theme_alerts SET result_json=? WHERE id=?',(json.dumps(payload),row['id']))
    legacy=preview_latest(history,mode='DEMO',total_won=1000,fee_bps=0,buffer_bps=0)
    assert legacy['targets'][0]['quantity'] is None and legacy['unreserved_won']==1000


def test_preview_paging_keeps_full_budget_totals():
    from app.budget_preview import equal_budget_preview
    rows=[dict(code=f'{900000+i:06}',name='연습',market='KOSPI',reference_won=100) for i in range(6)]
    result=equal_budget_preview(total_won=6000,targets=rows,available_won=None,fee_bps=2,buffer_bps=200)
    result.update(run_id=1,mode='DEMO',source='DEMO',price_asof='2026-10-16')
    first=preview_text(result,page=1);second=preview_text(result,page=2)
    assert '1/2페이지' in first and '/테마1예산 6000 0.02 2.00 2' in first
    assert '900005' not in first and '900005' in second
    assert '예상 확보액 합계' in first and '예상 확보액 합계' in second
    result.update(take_bps=425,stop_bps=175)
    assert '/테마1예산 6000 0.02 2.00 2 익절 4.25 손절 1.75' in preview_text(result,page=1)


def test_plan_navigation_stays_on_same_run_after_new_search(tmp_path):
    from app.theme_budget import plan_text
    from test_theme_delivery import setup
    history,args=setup(tmp_path)
    with history.connect() as db:rows=db.execute('SELECT * FROM daily_bars').fetchall()
    history.insert_bars([Bar(r['day'],f'90000{i}',f'종목{i}','KOSPI',100,110,99,100,1000,100,4000)
                         for i in range(2,7) for r in rows],'DEMO')
    original=prepare(history,**args)
    first=plan_text(history,'DEMO','/테마1계획 전체 6000원 익절 4.25 손절 1.75')
    command=next(line[4:] for line in first.splitlines() if line.startswith('다음: '))
    assert command.endswith(f'판정 {original["run_id"]}')
    with history.connect() as db:all_rows=db.execute('SELECT * FROM daily_bars').fetchall()
    history.insert_bars([Bar(r['day'],r['code'],r['name'],'KOSPI',100,120,99,110,1000,100,4000) for r in all_rows]+
        [Bar(r['day'],'900007','종목7','KOSPI',100,120,99,110,1000,100,4000) for r in rows],'DEMO')
    newer=prepare(history,**args)
    assert newer['run_id']!=original['run_id']
    second=plan_text(history,'DEMO',command)
    assert f'감지 목록 #{original["run_id"]}' in second and '종목당 1,000원' in second
    assert '900006' in second and '900007' not in second
    assert '익절 +4.25% · 손절 -1.75%' in second
    fresh=plan_text(history,'DEMO','/테마1계획 전체 6000원')
    assert f'감지 목록 #{newer["run_id"]}' in fresh and '종목당 857원' in fresh


def test_budget_navigation_run_is_preserved_through_private_telegram_handler(tmp_path,sandbox):
    from test_theme_delivery import setup
    history,args=setup(tmp_path)
    with history.connect() as db:rows=db.execute('SELECT * FROM daily_bars').fetchall()
    history.insert_bars([Bar(r['day'],f'90000{i}',f'종목{i}','KOSPI',100,110,99,100,1000,100,4000)
                         for i in range(2,7) for r in rows],'DEMO')
    original=prepare(history,**args)
    service,broker,_,_=sandbox
    service.theme_budget=lambda total,fee,buffer,page,**kw:preview_text(preview_latest(history,
        mode='DEMO',total_won=total,fee_bps=fee,buffer_bps=buffer,**kw),page=page)
    first=service.handle(update(sandbox,1,'/테마1예산 6000 0.02 2 익절 4.25 손절 1.75'))
    command=next(line[4:] for line in first.splitlines() if line.startswith('다음: '))
    with history.connect() as db:db.execute("UPDATE daily_bars SET high=120,close=110 WHERE source='DEMO'")
    prepare(history,**args)
    second=service.handle(update(sandbox,2,command))
    assert f'판정 번호 {original["run_id"]}' in second and '종목당 1,000원' in second
    assert '900006' in second and '익절 +4.25% · 손절 -1.75%' in second
    assert broker.calls==[]


@pytest.mark.parametrize('run_id',[True,0,-1,999999])
def test_invalid_or_missing_run_never_falls_back_to_latest(tmp_path,run_id):
    from test_theme_delivery import setup
    history,_=setup(tmp_path)
    with pytest.raises(ValueError):
        preview_latest(history,mode='DEMO',total_won=1000,fee_bps=0,buffer_bps=0,run_id=run_id)


def test_plan_blocks_wrong_mode_source_even_with_explicit_run(tmp_path):
    from test_theme_delivery import setup
    history,_=setup(tmp_path)
    with history.connect() as db:db.execute("UPDATE theme_runs SET source='KIWOOM_MOCK_KRX'")
    with pytest.raises(ValueError,match='출처'):
        preview_latest(history,mode='DEMO',total_won=1000,fee_bps=0,buffer_bps=0,run_id=1)
