from datetime import datetime,timedelta
from copy import deepcopy
import pytest
from app.morning_schedule import MorningSchedule
from app.morning_request import freeze_scheduled
from app.morning_virtual import MorningVirtual,record_path
from app.storage import Database
from app.order_engine import connect
from app.config import DEFAULTS
from app.trading_clock import KST
from test_morning_request import rig
from test_theme_kiwoom import history_with_baseline,Client,NOW as COLLECTED
from app.theme_kiwoom import collect_theme
from test_theme_delivery import setup as theme_setup
from test_telegram_orders import sandbox,update


NOW=datetime(2026,10,20,7,50,tzinfo=KST)


def setup(tmp_path,kind='일반'):
    db,history,cfg=rig(tmp_path)
    schedule=MorningSchedule(tmp_path/'jobs.db','owner','DEMO');clock=[NOW];sent=[];prepared=[]
    schedule.handle(f'/아침가상예약 {kind} 1만원 익절 2 손절 3',now=NOW)
    def prepare(day,text):
        prepared.append(day)
        return freeze_scheduled(tmp_path,db,history,'DEMO',cfg,day,text,now=clock[0])
    def tick(active=lambda:True):
        return schedule.tick(now=clock[0],clock=lambda:clock[0],active=active,prepare=prepare,send=lambda text:sent.append(text) or len(sent))
    return schedule,db,history,cfg,clock,sent,prepared,tick


def test_actual_saved_selection_is_frozen_once_without_paper_entry_and_survives_restart(tmp_path):
    schedule,db,history,cfg,clock,sent,prepared,tick=setup(tmp_path)
    clock[0]=NOW-timedelta(seconds=1)
    assert tick()=='NOT_DUE'
    clock[0]=NOW
    assert tick()=='FROZEN' and prepared==['2026-10-20'] and len(sent)==1
    tracker=MorningVirtual(Database(record_path(tmp_path,'DEMO','2026-10-20'),read_only=True))
    assert len(tracker.plan()['targets'])==2 and tracker.db.trades()==[]
    assert '목록·설정만 고정' in sent[0] and '자동 진입은 미연결' in sent[0]
    assert tick()=='FROZEN' and len(prepared)==1 and len(sent)==1
    restarted=MorningSchedule(schedule.path,'owner','DEMO')
    assert restarted.tick(now=NOW,clock=lambda:NOW,active=lambda:True,prepare=lambda *a:pytest.fail('repeated freeze'),send=lambda t:pytest.fail('repeated send'))=='FROZEN'
    assert '저장 목록 고정 확인' in restarted.summary(NOW)
    assert MorningSchedule(schedule.path,'another','DEMO').tick(now=NOW,clock=lambda:NOW,active=lambda:True,prepare=lambda *a:None,send=lambda t:1)=='DISABLED'


@pytest.mark.parametrize('case',['late','closed','inactive','stale'])
def test_no_late_backfill_closed_day_inactive_or_old_recommendation(tmp_path,case):
    schedule,db,history,cfg,clock,sent,prepared,tick=setup(tmp_path)
    if case=='late':clock[0]=NOW.replace(hour=8)
    elif case=='closed':clock[0]=NOW.replace(day=24)
    elif case=='stale':
        with db.connect() as record:record.execute("UPDATE recommendation_batches SET created_at='2026-10-16T17:00:00+09:00'")
    expected={'late':'MISSED','closed':'CLOSED_DAY','inactive':'INACTIVE','stale':'FAILED'}[case]
    assert tick(active=lambda:case!='inactive')==expected
    assert not (tmp_path/'morning_virtual').exists()
    if case=='stale':
        assert '직전 거래일' in sent[0]
        assert tick()=='FAILED' and len(prepared)==1 and len(sent)==1


def test_theme_requires_etf_exclusion_evidence_and_recent_run(tmp_path):
    db,_,cfg=rig(tmp_path)
    unverified,_=theme_setup(tmp_path/'demo-theme')
    with pytest.raises(ValueError,match='ETF'):
        freeze_scheduled(tmp_path,db,unverified,'DEMO',cfg,'2026-10-20','/가상추적 테마1 1만원',now=NOW)
    assert not (tmp_path/'morning_virtual').exists()
    history=history_with_baseline(tmp_path/'real-fixture')
    collect_theme(history,Client(),'REAL_QUOTES',now=COLLECTED)  # Synthetic client; no HTTP.
    cfg=dict(deepcopy(DEFAULTS),data_mode='KIWOOM',kiwoom_env='REAL')
    result=freeze_scheduled(tmp_path,db,history,'REAL_QUOTES',cfg,'2026-10-20','/가상추적 테마1 1만원',now=NOW)
    assert result['day']=='2026-10-20' and result['mode']=='REAL_QUOTES'
    tracker=MorningVirtual(Database(record_path(tmp_path,'REAL_QUOTES','2026-10-20'),read_only=True))
    assert tracker.plan()['selection_info']['kind']=='THEME1' and tracker.db.trades()==[]


def test_unknown_send_and_interrupted_job_are_not_retried(tmp_path):
    schedule,db,history,cfg,clock,sent,prepared,tick=setup(tmp_path)
    prepare=lambda day,text:freeze_scheduled(tmp_path,db,history,'DEMO',cfg,day,text,now=NOW)
    assert schedule.tick(now=NOW,clock=lambda:NOW,active=lambda:True,prepare=prepare,send=lambda t:None)=='FROZEN'
    assert '알림 미확인' in schedule.summary(NOW)
    restarted=MorningSchedule(schedule.path,'owner','DEMO')
    assert restarted.tick(now=NOW,clock=lambda:NOW,active=lambda:True,prepare=lambda *a:pytest.fail('retry'),send=lambda t:pytest.fail('resend'))=='FROZEN'
    with connect(schedule.path) as record:
        record.execute("UPDATE morning_schedule_jobs SET state='BUILDING',delivery=NULL,message=NULL")
    interrupted=MorningSchedule(schedule.path,'owner','DEMO')
    messages=[]
    assert interrupted.tick(now=NOW,clock=lambda:NOW,active=lambda:True,prepare=lambda *a:pytest.fail('rebuild'),send=lambda t:messages.append(t) or 1)=='UNKNOWN'
    assert '중단' in interrupted.summary(NOW) and len(messages)==1


def test_crossing_cutoff_is_not_reported_as_on_time_freeze(tmp_path):
    schedule,db,history,cfg,clock,sent,prepared,tick=setup(tmp_path)
    def prepare(day,text):
        result=freeze_scheduled(tmp_path,db,history,'DEMO',cfg,day,text,now=NOW)
        clock[0]=NOW.replace(hour=8)
        return result
    assert schedule.tick(now=NOW,clock=lambda:clock[0],active=lambda:True,prepare=prepare,send=lambda t:1)=='LATE'
    assert '고정 완료 시각이 마감을 지남' in schedule.summary(clock[0])


def test_private_commands_stop_and_tick_use_existing_receiver_without_orders(tmp_path,sandbox):
    schedule,db,history,cfg,clock,sent,prepared,tick=setup(tmp_path)
    service,broker,_,service_clock=sandbox;service_clock[0]=NOW
    service.morning_schedule=MorningSchedule(service.path,service.scope,'DEMO')
    service.morning_freeze=lambda day,text:freeze_scheduled(tmp_path,db,history,'DEMO',cfg,day,text,now=service_clock[0])
    bad=update(sandbox,1,'/아침가상예약 일반 1만원');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    assert '켜짐' in service.handle(update(sandbox,2,'/아침가상예약 일반 1만원'))
    class Sender:
        def send(self,chat,text):sent.append(text);return len(sent)
    assert service.run_morning_schedule(Sender())=='FROZEN'
    service.handle(update(sandbox,3,'/중지'))
    assert service.run_morning_schedule(Sender())=='INACTIVE'
    assert '고정 확인' in service.handle(update(sandbox,4,'/아침가상상태'))
    assert '중지' in service.handle(update(sandbox,5,'/아침가상예약 일반 2만원'))
    assert '껐어요' in service.handle(update(sandbox,6,'/아침가상중지'))
    assert broker.calls==[]


def test_calendar_failure_is_recorded_without_weekday_guessing(tmp_path,monkeypatch):
    import app.morning_schedule as module
    schedule,db,history,cfg,clock,sent,prepared,tick=setup(tmp_path)
    def unavailable(day):raise ValueError('synthetic calendar failure')
    monkeypatch.setattr(module,'recent_sessions',unavailable)
    assert tick()=='FAILED' and prepared==[] and '거래일 달력' in sent[0]
    assert not (tmp_path/'morning_virtual').exists()


def test_backward_clock_does_not_certify_the_freeze(tmp_path):
    schedule,db,history,cfg,clock,sent,prepared,tick=setup(tmp_path)
    def prepare(day,text):
        result=freeze_scheduled(tmp_path,db,history,'DEMO',cfg,day,text,now=NOW)
        clock[0]=NOW-timedelta(seconds=1)
        return result
    assert schedule.tick(now=NOW,clock=lambda:clock[0],active=lambda:True,prepare=prepare,send=lambda t:1)=='FAILED'
