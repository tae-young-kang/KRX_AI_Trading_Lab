from copy import deepcopy
from datetime import datetime,timedelta
import pytest
from app.config import DEFAULTS
from app.storage import Database
from app.krx_history import HistoryStore
from app.morning_request import setup_text,next_day
from app.morning_virtual import MorningVirtual,record_path
from app.trading_clock import KST
from test_recommendations import candidates,save
from test_theme_delivery import setup as theme_setup
from test_telegram_orders import sandbox,update

NOW=datetime(2026,10,20,7,30,tzinfo=KST)


def rig(tmp_path):
    db=Database(tmp_path/'practice.db');history=HistoryStore(tmp_path/'history.db')
    cfg=dict(deepcopy(DEFAULTS),recommend_count=2)
    save(db,candidates(),cfg)
    with db.connect() as record:record.execute("UPDATE recommendation_batches SET created_at='2026-10-19T17:00:00+09:00'")
    return db,history,cfg


def call(root,db,history,cfg,text,now=NOW):
    return setup_text(root,db,history,'DEMO',cfg,text,now=now)


def test_saved_general_recommendations_and_exit_policy_are_frozen(tmp_path):
    db,history,cfg=rig(tmp_path)
    cfg['min_value_eok']=999
    with history.connect() as record:
        for stamp in ('2026-10-20T07:20:00+09:00','2026-10-20T07:40:00+09:00'):
            record.execute('INSERT INTO nxt_observations(mode,market,code,day,observed_at,eligible) VALUES(?,?,?,?,?,?)',
                ('DEMO','KOSPI','900001','2026-10-20',stamp,1))
    text=call(tmp_path,db,history,cfg,'/가상추적 일반 100만원 익절 2 손절 3')
    plan=MorningVirtual(Database(record_path(tmp_path,'DEMO','2026-10-20'),read_only=True)).plan()
    assert [r['code'] for r in plan['targets']]==['900001','900002']
    assert plan['targets'][0]['venue']=='NXT' and plan['targets'][1]['venue']=='UNKNOWN'
    assert plan['settings']['min_value_eok']==DEFAULTS['min_value_eok']
    assert plan['settings']['exit_take_pct']==2 and plan['total_won']==1000000
    assert plan['selection_info']['asof'].startswith('2026-10-19')
    assert '목록·설정만 고정' in text and '자동 수집·자동 진입은 미연결' in text
    assert call(tmp_path,db,history,cfg,'/가상추적 일반 100만원 익절 2 손절 3',NOW+timedelta(seconds=5))==text
    assert '대상 거래일 2026-10-20' in call(tmp_path,db,history,cfg,'/가상추적상태',NOW.replace(hour=10))
    with pytest.raises(ValueError):call(tmp_path,db,history,cfg,'/가상추적 일반 200만원 익절 2 손절 3')


def test_theme_selection_uses_all_matches_and_separate_default_policy(tmp_path):
    db,_,cfg=rig(tmp_path)
    history,_=theme_setup(tmp_path/'theme')
    text=call(tmp_path,db,history,cfg,'/가상추적 테마1 1만원')
    tracker=MorningVirtual(Database(record_path(tmp_path,'DEMO','2026-10-20'),read_only=True))
    plan=tracker.plan()
    assert plan['origin']=='theme1_1' and plan['selection_info']==dict(kind='THEME1',asof='2026-10-19')
    assert len(plan['targets'])==1 and plan['targets'][0]['code']=='900001'
    assert plan['settings']['exit_take_pct']==2 and plan['settings']['exit_stop_pct']==3
    assert '총예산 10,000원' in text


@pytest.mark.parametrize('stamp,expected',[
    ('2026-10-20T07:59:00+09:00','2026-10-20'),('2026-10-20T08:00:00+09:00','2026-10-21'),
    ('2026-10-23T20:00:00+09:00','2026-10-26'),('2026-10-24T07:00:00+09:00','2026-10-26')])
def test_target_day_skips_closed_days_and_passed_opening(stamp,expected):
    assert next_day(datetime.fromisoformat(stamp))==expected


@pytest.mark.parametrize('case',['empty','future','other_mode'])
def test_bad_or_missing_selection_does_not_create_cohort(tmp_path,case):
    db,history,cfg=rig(tmp_path)
    with db.connect() as record:
        if case=='empty':record.execute('DELETE FROM recommendations')
        elif case=='future':record.execute("UPDATE recommendation_batches SET created_at='2026-10-21T17:00:00+09:00'")
        else:record.execute("UPDATE recommendation_batches SET mode='MOCK'")
    with pytest.raises(ValueError):call(tmp_path,db,history,cfg,'/가상추적 일반 100만원')
    assert not (tmp_path/'morning_virtual').exists()


def test_setup_from_telegram_is_authorized_replayable_and_stoppable(tmp_path,sandbox):
    db,history,cfg=rig(tmp_path)
    service,broker,_,clock=sandbox
    clock[0]=NOW
    service.morning_setup=lambda text:call(tmp_path,db,history,cfg,text,clock[0])
    bad=update(sandbox,1,'/가상추적 일반 1만원');bad['message']['from']['id']=99
    assert service.handle(bad) is None and not (tmp_path/'morning_virtual').exists()
    event=update(sandbox,2,'/가상추적 일반 1만원')
    reply=service.handle(event)
    assert reply==service.handle(event) and '고정 종목 2개' in reply
    service.handle(update(sandbox,3,'/중지'))
    assert '중지했어요' in service.handle(update(sandbox,4,'/가상추적 일반 2만원'))
    assert '총예산 10,000원' in service.handle(update(sandbox,5,'/가상추적상태 2026-10-20'))
    assert broker.calls==[]


def test_invalid_policy_can_be_corrected_without_deleting_files(tmp_path):
    db,history,cfg=rig(tmp_path)
    with pytest.raises(ValueError):call(tmp_path,db,history,cfg,'/가상추적 일반 1만원 익절 0 손절 3')
    text=call(tmp_path,db,history,cfg,'/가상추적 일반 1만원 익절 2 손절 3')
    assert '익절 +2%' in text


def test_frozen_list_pages_cover_every_symbol_with_no_quote_or_database_mutation(tmp_path):
    import re
    db,history,cfg=rig(tmp_path)
    path=record_path(tmp_path,'DEMO','2026-10-20')
    tracker=MorningVirtual(Database(path))
    targets=[dict(code=str(900000+i),name=f'목록{i}',market='KOSPI' if i%2==0 else 'KOSDAQ',
                  nxt_eligible=(True if i==0 else False if i==1 else None)) for i in range(501)]
    tracker.prepare(day='2026-10-20',mode='DEMO',origin='theme1_1',targets=targets,settings=cfg,
                    total_won=501001,now=NOW,selection_info=dict(kind='THEME1',asof='2026-10-19'))
    before=path.read_bytes();codes=[]
    cfg.update(exit_take_pct=99)
    for page in range(1,102):
        text=call(tmp_path,db,history,cfg,f'/가상목록 2026-10-20 {page}')
        codes.extend(re.findall(r'\(([0-9]{6})\)',text))
        assert len(text)<3900 and '테마1' in text and '실제 주문 안 함' in text
        assert '종목당 1,000원 · 배분하지 않은 잔액 1원' in text
        if page==1:
            assert 'NXT 첫 거래' in text and 'KRX 시초가' in text and '진입 대기' in text
            assert '다음: /가상목록 2026-10-20 2' in text
        if page==101:assert '목록500 (900500)' in text and '다음:' not in text
    assert codes==[target['code'] for target in targets]
    assert path.read_bytes()==before and tracker.db.trades()==[]
    assert '101페이지까지' in call(tmp_path,db,history,cfg,'/가상목록 2026-10-20 102')
    assert '사용법' in call(tmp_path,db,history,cfg,'/가상목록 2026-10-20 0')
    assert '가상 목록이 없어요' in call(tmp_path,db,history,cfg,'/가상목록 2026-10-21')


def test_private_frozen_list_command_remains_readable_when_stopped(tmp_path,sandbox):
    db,history,cfg=rig(tmp_path);service,broker,_,clock=sandbox;clock[0]=NOW
    service.morning_setup=lambda text:call(tmp_path,db,history,cfg,text,clock[0])
    service.handle(update(sandbox,1,'/가상추적 일반 1만원'))
    bad=update(sandbox,2,'/가상목록');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    service.handle(update(sandbox,3,'/중지'))
    reply=service.handle(update(sandbox,4,'/가상목록'))
    assert '전체 2종목' in reply and '합성 연습 시세' in reply and '거래 시장 미확인' in reply
    assert broker.calls==[]
