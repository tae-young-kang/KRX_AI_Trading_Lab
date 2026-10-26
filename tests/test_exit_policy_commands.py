"""Offline default virtual exit-policy preview/approval/history/restore."""
import json
import pytest
from app.order_engine import connect
from app.telegram_conditions import ConditionCommands
from app.exit_policy_commands import KEYS
from test_telegram_conditions import setup,send,code
from test_condition_history import change
from test_telegram_orders import sandbox,update


def test_exit_policy_approved_then_restored_without_filter_or_cost_change(tmp_path):
    cfg,controller=setup(tmp_path);before=dict(cfg)
    preview=send(controller,'/청산조건변경 익절=4 손절=2 보유일=7')
    assert '자동매도를 켜지 않아요' in preview and '기존 보유' in preview
    assert cfg==before
    send(controller,'/조건확인 '+code(preview),uid=2)
    assert not controller.sync(cfg,lambda:pytest.fail('busy'),now=1001,busy=True)
    assert controller.sync(cfg,lambda:None,now=1002)
    assert [cfg[key] for key in KEYS]==[4,2,7]
    assert all(cfg[key]==value for key,value in before.items() if key not in (*KEYS,'strategy_version'))
    assert '익절 +4%' in send(controller,'/청산조건',uid=3)
    assert '기록 1' in send(controller,'/청산조건기록',uid=4)
    # Persistence after restart, new approval and monotonically increasing version.
    controller=ConditionCommands(controller.path,cfg)
    change(controller,cfg,'/청산조건복귀 1',uid=5)
    assert all(cfg[key]==before[key] for key in KEYS) and cfg['strategy_version']==3
    assert '현재 기본' in send(controller,'/청산조건복귀 1',uid=7)
    with connect(controller.path) as db:
        rows=db.execute('SELECT payload FROM exit_policy_versions').fetchall()
    assert len(rows)==3 and all(set(json.loads(row[0]))==set(KEYS) for row in rows)


@pytest.mark.parametrize('policy_text',['익절=0','손절=31','보유일=1.5','익절=2 익절=3','비용=1','익절=nan'])
def test_invalid_exit_policy_never_queues_or_changes_config(tmp_path,policy_text):
    cfg,controller=setup(tmp_path);before=dict(cfg)
    assert '/조건확인 ' not in send(controller,'/청산조건변경 '+policy_text)
    assert not controller.sync(cfg,lambda:pytest.fail('save'),now=1001) and cfg==before


def test_stale_policy_save_failure_and_cross_mode_restore_are_blocked(tmp_path):
    cfg,controller=setup(tmp_path)
    preview=send(controller,'/청산조건변경 익절=5')
    send(controller,'/조건확인 '+code(preview),uid=2)
    cfg['exit_stop_pct']=1.1
    assert not controller.sync(cfg,lambda:pytest.fail('stale save'),now=1001)
    preview=send(controller,'/청산조건변경 익절=5',uid=3)
    send(controller,'/조건확인 '+code(preview),uid=4)
    original=dict(cfg)
    def fail():raise OSError('synthetic settings failure')
    with pytest.raises(OSError):controller.sync(cfg,fail,now=1001)
    assert cfg==original
    cfg.update(data_mode='KIWOOM',kiwoom_env='REAL')
    controller.sync(cfg,lambda:None,now=1002,busy=True)
    assert '합성 연습' not in send(controller,'/청산조건기록',uid=5)
    assert '현재 시세 모드' in send(controller,'/청산조건복귀 1',uid=6)


def test_private_service_stops_policy_changes_but_allows_reading(tmp_path,sandbox):
    cfg,controller=setup(tmp_path);service,broker,_,clock=sandbox;service.conditions=controller
    bad=update(sandbox,1,'/청산조건변경 익절=5');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    preview=service.handle(update(sandbox,2,'/청산조건변경 익절=5'))
    assert '/조건확인 ' in preview
    service.handle(update(sandbox,3,'/중지'))
    assert '중지' in service.handle(update(sandbox,4,'/조건확인 '+code(preview)))
    assert '기본 가상 익절' in service.handle(update(sandbox,5,'/청산조건'))
    assert '기록 1' in service.handle(update(sandbox,6,'/청산조건기록'))
    assert not controller.sync(cfg,lambda:pytest.fail('save'),now=clock[0].timestamp())
    assert broker.calls==[]



def test_new_general_virtual_cohort_uses_policy_while_existing_and_theme_keep_theirs(tmp_path):
    from test_morning_request import rig,call,NOW
    from test_theme_delivery import setup as theme_setup
    from app.morning_virtual import MorningVirtual,record_path
    from app.storage import Database
    db,history,cfg=rig(tmp_path)
    controller=ConditionCommands(tmp_path/'policy.db',cfg)
    call(tmp_path/'old',db,history,cfg,'/가상추적 일반 1만원')
    prior=MorningVirtual(Database(record_path(tmp_path/'old','DEMO','2026-10-20'),read_only=True)).plan()
    change(controller,cfg,'/청산조건변경 익절=5 손절=3 보유일=7')
    call(tmp_path/'new',db,history,cfg,'/가상추적 일반 1만원')
    current=MorningVirtual(Database(record_path(tmp_path/'new','DEMO','2026-10-20'),read_only=True)).plan()
    assert [current['settings'][key] for key in KEYS]==[5,3,7]
    assert MorningVirtual(Database(record_path(tmp_path/'old','DEMO','2026-10-20'),read_only=True)).plan()==prior
    theme,_=theme_setup(tmp_path/'theme')
    call(tmp_path/'theme-plan',db,theme,cfg,'/가상추적 테마1 1만원')
    themed=MorningVirtual(Database(record_path(tmp_path/'theme-plan','DEMO','2026-10-20'),read_only=True)).plan()
    assert themed['settings']['exit_take_pct']==2 and themed['settings']['exit_stop_pct']==3


@pytest.mark.parametrize('action',['expire','withdraw'])
def test_policy_preview_expires_or_can_be_withdrawn(tmp_path,action):
    cfg,controller=setup(tmp_path);before=dict(cfg)
    preview=send(controller,'/청산조건변경 익절=5')
    command='/조건철회 ' if action=='withdraw' else '/조건확인 '
    reply=send(controller,command+code(preview),uid=2,now=1001 if action=='withdraw' else 1120)
    assert ('철회' if action=='withdraw' else '만료') in reply
    assert not controller.sync(cfg,lambda:pytest.fail('save'),now=1121) and cfg==before
