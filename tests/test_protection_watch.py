from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import pytest
from app.order_engine import connect
from app.protection_watch import ProtectionWatch
from test_protection_plan import NOW,POSITION,QUOTE
from test_mock_protection import setup


def watch(path):
    result=ProtectionWatch(path,mode='MOCK',account_tag='a'*64)
    result.register('theme1-example',code='900001',venue='KRX')
    return result


def test_trigger_latches_across_restart_and_parallel_observations(tmp_path):
    path=tmp_path/'watch.db'
    store=watch(path)
    assert store.observe('theme1-example',POSITION,QUOTE,now=NOW)['state']=='OBSERVING'
    assert not store.observe('theme1-example',POSITION,QUOTE,now=NOW)['new_trigger']
    quote=replace(QUOTE,price_won=1020)
    def observe(_):
        return store.observe('theme1-example',POSITION,quote,now=NOW)
    with ThreadPoolExecutor(max_workers=4) as pool: results=list(pool.map(observe,range(4)))
    assert sum(row['new_trigger'] for row in results)==1
    assert all(not row['executable'] for row in results)
    reopened=watch(path)
    result=reopened.observe('theme1-example',POSITION,quote,now=NOW+timedelta(minutes=1))
    assert not result['new_trigger'] and result['state']=='TRIGGERED'
    with connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM protection_observations').fetchone()[0]==2


def test_blocked_observation_never_latches_and_scope_is_separate(tmp_path):
    path=tmp_path/'watch.db';store=watch(path)
    result=store.observe('theme1-example',replace(POSITION,reconciled=False),QUOTE,now=NOW)
    assert result['decision']['state']=='BLOCKED' and not result['new_trigger']
    with pytest.raises(ValueError):
        ProtectionWatch(path,mode='REAL_QUOTES',account_tag='a'*64)
    with pytest.raises(ValueError):
        store.register('theme1-example',code='900001',venue='KRX',take_bps=400)
    with pytest.raises(ValueError):
        store.register('another-plan',code='900001',venue='NXT')
    with pytest.raises(ValueError):
        store.observe('theme1-example',POSITION,QUOTE,now=NOW-timedelta(seconds=1))


def test_current_mock_capture_bridge_observes_without_orders(tmp_path):
    ledger,capture=setup(tmp_path)
    store=watch(tmp_path/'watch.db')
    result=store.observe_mock('theme1-example',ledger,capture,replace(QUOTE,price_won=1100),
                               code='900001',buy_orders=('12',),now=NOW)
    assert result['new_trigger'] and result['decision']['quantity']==3
    assert result['executable'] is False


def test_summary_distinguishes_unobserved_blocked_and_triggered(tmp_path):
    store=watch(tmp_path/'watch.db')
    assert '아직 관측 기록이 없어요' in store.summary()
    store.observe('theme1-example',replace(POSITION,reconciled=False),QUOTE,now=NOW)
    assert '체결과 보유 잔량을 먼저 대조' in store.summary()
    store.observe('theme1-example',POSITION,replace(QUOTE,price_won=1020),now=NOW)
    text=store.summary()
    assert '매도 접수/체결 아님' in text and '당시 수량 10주' in text
    assert 'a'*64 not in text and 'theme1-example' not in text
    for i in range(5): store.register(f'other-{i}',code=f'90001{i}',venue='KRX')
    assert '1/2' in store.summary() and '/보호상태 2' in store.summary()
    assert '900001' not in store.summary() and '900001' in store.summary(page=2)
    assert '2페이지까지' in store.summary(page=3)
