from copy import deepcopy
import json
import pytest
from app.config import DEFAULTS
from app.order_engine import connect
from app.telegram_conditions import ConditionCommands
from test_telegram_conditions import setup,send,code
from test_telegram_orders import sandbox,update


def change(controller,cfg,text,uid=1,now=1000):
    preview=send(controller,text,uid=uid,now=now)
    send(controller,'/조건확인 '+code(preview),uid=uid+1,now=now+1)
    assert controller.sync(cfg,lambda:None,now=now+2)


def test_restore_is_previewed_persistent_and_uses_new_version_without_exit_changes(tmp_path):
    cfg,controller=setup(tmp_path)
    original=dict(cfg)
    change(controller,cfg,'/조건변경 시장=코스닥 거래대금=200 ETF=제외')
    cfg['exit_stop_pct']=9
    controller=ConditionCommands(controller.path,cfg)
    history=send(controller,'/조건기록',uid=10)
    assert '기록 1' in history and '기록 2' in history
    preview=send(controller,'/조건복귀 1',uid=11)
    assert '되돌릴까요' in preview and '익절·손절·테마1' in preview
    assert cfg['market']=='KOSDAQ'
    send(controller,'/조건확인 '+code(preview),uid=12)
    assert not controller.sync(cfg,lambda:None,now=1001,busy=True)
    assert controller.sync(cfg,lambda:None,now=1002)
    assert cfg['market']==original['market'] and cfg['min_value_eok']==original['min_value_eok']
    assert cfg['include_etf']==original['include_etf'] and cfg['strategy_version']==3
    assert cfg['exit_stop_pct']==9
    assert not controller.sync(cfg,lambda:pytest.fail('repeat save'),now=1003)
    assert '현재 추천 조건과 같아요' in send(controller,'/조건복귀 1',uid=13)


def test_external_pc_filter_change_is_captured_and_private_settings_not_stored(tmp_path):
    cfg=deepcopy(DEFAULTS);cfg['local_private_marker']='DO_NOT_PERSIST_THIS'
    controller=ConditionCommands(tmp_path/'conditions.db',cfg)
    cfg.update(min_value_eok=250,strategy_version=2)
    assert not controller.sync(cfg,lambda:pytest.fail('external save'),now=1000,busy=True)
    with connect(controller.path) as db:
        rows=db.execute('SELECT * FROM condition_versions ORDER BY id').fetchall()
    assert len(rows)==2 and json.loads(rows[1]['payload'])['min_value_eok']==250
    assert 'DO_NOT_PERSIST_THIS' not in str([dict(row) for row in rows])


def test_history_mode_isolation_and_corrupted_payload_are_blocked(tmp_path):
    cfg,controller=setup(tmp_path)
    cfg.update(data_mode='KIWOOM',kiwoom_env='REAL')
    controller.sync(cfg,lambda:None,now=1000,busy=True)
    assert '현재 시세 모드에 없는' in send(controller,'/조건복귀 1')
    assert '가상 연습' not in send(controller,'/조건기록',uid=2)
    with connect(controller.path) as db:
        db.execute("UPDATE condition_versions SET payload=? WHERE mode='REAL_QUOTES'",(json.dumps({'unexpected':'field'}),))
    assert '항목을 확인' in send(controller,'/조건복귀 2',uid=3)
    assert '/조건확인' not in send(controller,'/조건기록',uid=4)


def test_stale_restore_and_failed_save_never_claim_applied(tmp_path):
    cfg,controller=setup(tmp_path)
    change(controller,cfg,'/조건변경 거래대금=200')
    preview=send(controller,'/조건복귀 1',uid=3)
    send(controller,'/조건확인 '+code(preview),uid=4)
    def fail():raise OSError('fixture save failed')
    with pytest.raises(OSError):controller.sync(cfg,fail,now=1002)
    assert cfg['min_value_eok']==200
    cfg.update(min_value_eok=300,strategy_version=3)
    assert not controller.sync(cfg,lambda:pytest.fail('stale overwrite'),now=1003)
    assert cfg['min_value_eok']==300
    assert '최근 변경 취소됨' in send(controller,'/조건',uid=5)


def test_private_history_read_allowed_after_stop_but_restore_is_not(tmp_path,sandbox):
    cfg,controller=setup(tmp_path)
    change(controller,cfg,'/조건변경 거래대금=200')
    service,broker,_,_=sandbox
    service.conditions=controller
    unauthorized=update(sandbox,1,'/조건기록');unauthorized['message']['from']['id']=99
    assert service.handle(unauthorized) is None
    service.handle(update(sandbox,2,'/중지'))
    assert '이전 추천 조건' in service.handle(update(sandbox,3,'/조건기록'))
    assert '중지' in service.handle(update(sandbox,4,'/조건복귀 1'))
    assert broker.calls==[]


def test_history_pagination_keeps_every_record(tmp_path):
    cfg,controller=setup(tmp_path)
    for version in range(2,8):
        cfg.update(strategy_version=version,min_value_eok=100+version)
        controller.sync(cfg,lambda:None,now=1000+version,busy=True)
    assert '다음: /조건기록 2' in send(controller,'/조건기록')
    last=send(controller,'/조건기록 2',uid=2)
    assert '기록 1' in last and '기록 2' in last
    assert '2페이지까지' in send(controller,'/조건기록 3',uid=3)
