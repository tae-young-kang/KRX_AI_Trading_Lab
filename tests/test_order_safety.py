"""Safety checks use temporary files, fake money and local child processes only."""
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timedelta, timezone

import pytest

from test_telegram_orders import sandbox, update, nonce
from app.order_engine import OrderEngine, OrderRequest, OrderError, connect
from app.telegram_orders import TelegramOrderService
from app.order_safety import PracticeLimits, usage, latch
from app.runtime_lock import ReceiverLock, ReceiverAlreadyRunning


def submit(sandbox, uid, text='/매수 900001 1 1000', service=None):
    service = service or sandbox[0]
    preview = service.handle(update(sandbox, uid, text))
    return service.handle(update(sandbox, uid+1, '/확인 '+nonce(preview)))


def test_daily_limit_survives_cancel_restart_and_actor_change(sandbox):
    service, broker, engine, now = sandbox
    service.limits = PracticeLimits(daily_count=1)
    submit(sandbox, 1)
    service.emergency_stop()
    service.resume_after_review()
    assert '한도' in submit(sandbox, 3)
    new = TelegramOrderService(service.path, engine, 44, 55, 33, service.catalog,
                               lambda:now[0], limits=PracticeLimits(daily_count=1))
    def msg(uid,text):
        event=update(sandbox,uid,text)
        event['message']['from']['id']=44;event['message']['chat']['id']=55
        return event
    preview=new.handle(msg(5,'/매수 900001 1 1000'))
    assert '한도' in new.handle(msg(6,'/확인 '+nonce(preview)))
    assert broker.calls == ['submit','cancel']


def test_failed_attempt_counts_but_expired_confirmation_does_not(sandbox):
    service, _, _, now = sandbox
    service.limits = PracticeLimits(daily_count=1)
    token=nonce(service.handle(update(sandbox,1,'/매수 900001 1 1000')))
    now[0]+=timedelta(seconds=120)
    assert '만료' in service.handle(update(sandbox,2,'/확인 '+token))
    assert '접수되지' in submit(sandbox,3,'/매도 900001 1 1000')
    assert '한도' in submit(sandbox,5)


def test_kst_daily_gross_amount_and_rollover(sandbox):
    service, broker, engine, now = sandbox
    service.limits=PracticeLimits(daily_count=20,daily_notional=1500)
    submit(sandbox,1)
    assert '한도' in submit(sandbox,3)
    # 15:00 UTC is midnight in Korea; this is a virtual test session.
    now[0]=datetime(2026,10,19,15,0,1,tzinfo=timezone.utc)
    engine.calendar=type('VirtualSession',(),{'is_open':lambda self,m,v:True})()
    assert '체결 0/1주' in submit(sandbox,5)
    with connect(service.path) as db:
        used=usage(db,broker.identity,now[0])
    assert used==dict(day='2026-10-20',count=1,notional=1000)
    assert broker.calls==['submit','submit']


def test_partial_fill_emergency_cancels_only_remainder_and_stays_latched(sandbox):
    service, broker, engine, now = sandbox
    submit(sandbox,1,'/매수 900001 5 1000')
    with connect(engine.path) as db:oid=db.execute('SELECT id FROM orders').fetchone()[0]
    broker.fill(oid,2,900);engine.reconcile(oid)
    report=service.emergency_stop()
    assert report==dict(halted=True,unresolved=0,open_orders=0,account_matches=True)
    assert engine.account()==broker.account()==dict(cash=98200,positions={'900001':2})
    assert engine.get(oid)['state']=='CANCELED'
    service.emergency_stop()
    assert broker.calls==['submit','cancel']
    new=TelegramOrderService(service.path,engine,11,22,33,service.catalog,lambda:now[0])
    assert '긴급 중지' in new.handle(update(sandbox,3,'/매수 900001 1 1000'))
    new.resume_after_review()
    assert '/확인' in new.handle(update(sandbox,4,'/매수 900001 1 1000'))


def test_unresolved_blocks_cancel_and_resume_without_any_resubmit(sandbox,monkeypatch):
    service,broker,engine,_=sandbox
    original=broker.lookup
    monkeypatch.setattr(broker,'lookup',lambda ref:None)
    assert '미확인' in submit(sandbox,1)
    report=service.emergency_stop()
    assert report['halted'] and report['unresolved']>0
    with pytest.raises(OrderError):service.resume_after_review()
    assert broker.calls==['submit']
    monkeypatch.setattr(broker,'lookup',original)
    report=service.recovery_status()
    assert report['unresolved']==0 and report['open_orders']==1
    assert broker.calls==['submit']  # recovery is read-only at broker
    with pytest.raises(OrderError):service.resume_after_review()
    service.emergency_stop()
    service.resume_after_review()
    assert broker.calls==['submit','cancel']


def test_lost_cancel_response_resolves_by_lookup_once(sandbox):
    service,broker,_,_=sandbox
    submit(sandbox,1)
    broker.lose_next_response=True
    report=service.emergency_stop()
    assert report['unresolved']==0 and report['open_orders']==0 and report['halted']
    service.recovery_status()
    service.emergency_stop()
    assert broker.calls==['submit','cancel']


def test_crash_after_acceptance_recovers_reference_without_resubmission(sandbox,monkeypatch):
    service,broker,engine,now=sandbox
    token=nonce(service.handle(update(sandbox,1,'/매수 900001 1 1000')))
    original=engine._apply
    def die(*args,**kwargs):raise SystemExit('simulated process loss')
    monkeypatch.setattr(engine,'_apply',die)
    with pytest.raises(SystemExit):service.handle(update(sandbox,2,'/확인 '+token))
    monkeypatch.setattr(engine,'_apply',original)
    engine2=OrderEngine(engine.path,broker,engine.calendar,lambda:now[0])
    new=TelegramOrderService(service.path,engine2,11,22,33,service.catalog,lambda:now[0])
    report=new.recovery_status(startup=True)
    assert report['halted'] and report['unresolved']==0 and report['open_orders']==1
    with connect(service.path) as db:
        row=db.execute('SELECT state,order_id FROM tg_intents').fetchone()
        assert row['state']=='SENT' and row['order_id']
    assert broker.calls==['submit']
    assert '유효한' in new.handle(update(sandbox,3,'/확인 '+token))
    new.emergency_stop()
    assert broker.calls==['submit','cancel']


def test_confirmed_but_unjournaled_crash_is_discarded_not_replayed(sandbox):
    service,broker,_,_=sandbox
    service.handle(update(sandbox,1,'/매수 900001 1 1000'))
    with connect(service.path) as db:db.execute("UPDATE tg_intents SET state='CLAIMED'")
    report=service.recovery_status(startup=True)
    assert report['halted'] and not broker.calls
    with connect(service.path) as db:
        assert db.execute('SELECT state FROM tg_intents').fetchone()[0]=='CANCELED'


def test_balance_mismatch_and_new_emergency_prevent_resume(sandbox,monkeypatch):
    service,broker,_,_=sandbox
    with connect(broker.path) as db:db.execute("UPDATE meta SET value='99999' WHERE key='cash'")
    with pytest.raises(OrderError):service.resume_after_review()
    with connect(broker.path) as db:db.execute("UPDATE meta SET value='100000' WHERE key='cash'")
    original=service.recovery_status
    def racing_review(*args,**kwargs):
        result=original(*args,**kwargs)
        with connect(service.path) as db:latch(db,broker.identity,'CONCURRENT_STOP')
        return result
    monkeypatch.setattr(service,'recovery_status',racing_review)
    with pytest.raises(OrderError,match='새 중지'):service.resume_after_review()
    with connect(service.path) as db:
        assert db.execute('SELECT halted FROM tg_risk').fetchone()[0]==1


def test_daily_reservation_is_atomic_across_different_users(sandbox):
    service,broker,engine,now=sandbox
    service.limits=PracticeLimits(daily_count=1)
    other=TelegramOrderService(service.path,engine,44,55,33,service.catalog,lambda:now[0],limits=PracticeLimits(daily_count=1))
    one=nonce(service.handle(update(sandbox,1,'/매수 900001 1 1000')))
    msg=update(sandbox,3,'/매수 900001 1 1000')
    msg['message']['from']['id']=44;msg['message']['chat']['id']=55
    two=nonce(other.handle(msg))
    confirm=update(sandbox,4,'/확인 '+two)
    confirm['message']['from']['id']=44;confirm['message']['chat']['id']=55
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(service.handle,update(sandbox,2,'/확인 '+one))
        b=pool.submit(other.handle,confirm)
        replies=[a.result(),b.result()]
    assert sum('한도' in r for r in replies)==1
    assert broker.calls==['submit']


def test_telegram_emergency_and_review_do_not_resume_automatically(sandbox):
    service,broker,_,_=sandbox
    submit(sandbox,1)
    reply=service.handle(update(sandbox,3,'/긴급중지'))
    assert '긴급 중지: 유지' in reply and '미체결 0건' in reply
    service.handle(update(sandbox,4,'/복구확인'))
    assert '긴급 중지 상태' in service.handle(update(sandbox,5,'/매수 900001 1 1000'))
    assert broker.calls==['submit','cancel']


def test_untracked_server_order_blocks_resume_even_when_cash_matches(sandbox):
    service,broker,engine,_=sandbox
    broker.submit('outside-journal',OrderRequest('900001','BUY',1,1000))
    assert engine.account()==broker.account()
    report=service.recovery_status()
    assert report['halted'] and report['unresolved']==1
    with pytest.raises(OrderError):service.resume_after_review()
    assert broker.calls==['submit']  # never adopt or resubmit an unknown reference


def test_emergency_is_persisted_before_waiting_for_inflight_confirmation(sandbox,monkeypatch):
    service,broker,engine,_=sandbox
    token=nonce(service.handle(update(sandbox,1,'/매수 900001 1 1000')))
    entered,release=Event(),Event()
    original=engine.submit
    def slow_submit(*args,**kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args,**kwargs)
    monkeypatch.setattr(engine,'submit',slow_submit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        confirmed=pool.submit(service.handle,update(sandbox,2,'/확인 '+token))
        assert entered.wait(3)
        stopping=pool.submit(service.emergency_stop)
        try:
            until=time.monotonic()+3
            halted=False
            while time.monotonic()<until:
                with connect(service.path) as db:halted=bool(db.execute('SELECT halted FROM tg_risk').fetchone()[0])
                if halted:break
                time.sleep(.01)
            assert halted and not stopping.done()
        finally:
            release.set()
        confirmed.result(timeout=5)
        report=stopping.result(timeout=5)
    assert report['halted'] and report['open_orders']==0 and report['unresolved']==0
    assert broker.calls==['submit','cancel']


def child_options():
    return {'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}


def test_owner_lock_blocks_other_process_and_releases_normally(tmp_path):
    path=tmp_path/'owner.lock'
    script="""import sys
from app.runtime_lock import ReceiverLock,ReceiverAlreadyRunning
try:
 with ReceiverLock(sys.argv[1]): print('acquired')
except ReceiverAlreadyRunning: print('blocked')
"""
    with ReceiverLock(path):
        result=subprocess.run([sys.executable,'-c',script,str(path)],capture_output=True,text=True,timeout=10,**child_options())
        assert result.returncode==0 and result.stdout.strip()=='blocked'
    with ReceiverLock(path):pass


def test_owner_lock_released_after_process_death(tmp_path):
    path,ready=tmp_path/'owner.lock',tmp_path/'ready'
    script="""import sys
from pathlib import Path
from app.runtime_lock import ReceiverLock
with ReceiverLock(sys.argv[1]):
 Path(sys.argv[2]).write_text('ready')
 sys.stdin.read()
"""
    process=subprocess.Popen([sys.executable,'-c',script,str(path),str(ready)],stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,**child_options())
    try:
        until=time.monotonic()+5
        while not ready.exists() and time.monotonic()<until and process.poll() is None:time.sleep(.01)
        assert ready.exists()
        with pytest.raises(ReceiverAlreadyRunning):
            with ReceiverLock(path):pass
        process.kill();process.wait(timeout=5)
        with ReceiverLock(path):pass
    finally:
        if process.poll() is None:process.kill();process.wait(timeout=5)
        process.communicate(timeout=5)
