"""Local cost settings through authenticated fake Telegram and owner sync."""
from copy import deepcopy
import json
import pytest
from app.order_engine import connect
from app.telegram_conditions import ConditionCommands
from app.virtual_cost_commands import KEYS
from test_telegram_conditions import setup,send,code
from test_telegram_orders import sandbox,update


def pending(controller):
    with connect(controller.path) as db:return db.execute('SELECT state FROM condition_requests ORDER BY rowid DESC LIMIT 1').fetchone()[0]


def approve(controller,text,uid=1):
    preview=send(controller,text,uid=uid)
    send(controller,'/조건확인 '+code(preview),uid=uid+1)
    return preview


def test_cost_change_readback_restart_history_restore_and_whitelist(tmp_path):
    cfg,ctl=setup(tmp_path);before=dict(cfg);saved=[deepcopy(cfg)]
    approve(ctl,'/가상비용변경 수수료=0.02 세금=0.3 슬리피지=0.2')
    save=lambda:saved.__setitem__(0,deepcopy(cfg))
    assert not ctl.sync(cfg,lambda:pytest.fail('busy'),now=1001,busy=True,read_saved=lambda:saved[0])
    assert ctl.sync(cfg,save,now=1002,read_saved=lambda:saved[0])
    assert pending(ctl)=='APPLIED' and cfg['paper_fee_pct']==0.02
    assert all(cfg[k]==v for k,v in before.items() if k not in (*KEYS,'strategy_version'))
    ctl=ConditionCommands(ctl.path,cfg)
    assert '0.02%' in send(ctl,'/가상비용',uid=3)
    assert '기록 1' in send(ctl,'/가상비용기록',uid=4)
    approve(ctl,'/가상비용복귀 1',uid=5)
    assert ctl.sync(cfg,save,now=1003,read_saved=lambda:saved[0])
    assert all(cfg[k]==before[k] for k in KEYS) and cfg['strategy_version']==3
    with connect(ctl.path) as db:
        assert all(set(json.loads(r[0]))==set(KEYS) for r in db.execute('SELECT payload FROM virtual_cost_versions'))
        assert all(set(KEYS).isdisjoint(json.loads(r[0])) for r in db.execute('SELECT payload FROM exit_policy_versions'))


@pytest.mark.parametrize('failure',['save','read','mismatch','save_changed','preflight','missing_callback'])
def test_failed_or_unknown_cost_save_is_never_success_or_automatically_reapplied(tmp_path,failure):
    cfg,ctl=setup(tmp_path);before=deepcopy(cfg);stored=[deepcopy(cfg)];calls=[]
    approve(ctl,'/가상비용변경 세금=0.3')
    def save():
        calls.append('save')
        if failure=='save':raise OSError('synthetic')
        stored[0]=deepcopy(cfg)
        if failure=='save_changed':raise OSError('synthetic')
        if failure=='mismatch':stored[0]['strategy_version']+=1
    def read():
        if failure=='preflight' or failure=='read' and calls:raise OSError('synthetic')
        return stored[0]
    assert not ctl.sync(cfg,save,now=1001,read_saved=None if failure=='missing_callback' else read)
    assert cfg==before and pending(ctl) in ('FAILED','UNKNOWN')
    assert not ctl.sync(cfg,lambda:pytest.fail('retry'),now=1002,read_saved=read)
    text=send(ctl,'/가상비용',uid=3)
    assert '최근 변경 적용됨' not in text and ('저장 실패' in text or '미확인' in text)
    assert len(calls)==(0 if failure in ('preflight','missing_callback') else 1)


@pytest.mark.parametrize('command',['/조건변경 거래대금=200','/청산조건변경 익절=5','/가상비용변경 세금=0.3'])
def test_cost_change_invalidates_every_queued_condition_base(tmp_path,command):
    cfg,ctl=setup(tmp_path);approve(ctl,command)
    cfg['paper_fee_pct']=0.025
    assert not ctl.sync(cfg,lambda:pytest.fail('stale save'),now=1001,read_saved=lambda:cfg)
    assert pending(ctl)=='REJECTED'


@pytest.mark.parametrize('args',['수수료=1.1','세금=-1','슬리피지=3.1','수수료=nan','세금=0.2 세금=0.3','실계좌=1','수수료=0.0001'])
def test_invalid_or_unsupported_cost_fields_do_not_queue(tmp_path,args):
    cfg,ctl=setup(tmp_path);before=dict(cfg)
    assert '/조건확인 ' not in send(ctl,'/가상비용변경 '+args)
    assert cfg==before


def test_private_mode_stop_expiry_and_restart_apply_boundary(tmp_path,sandbox):
    cfg,ctl=setup(tmp_path);service,broker,_,clock=sandbox;service.conditions=ctl
    event=update(sandbox,1,'/가상비용변경 세금=0.3');event['message']['from']['id']=99
    assert service.handle(event) is None
    preview=service.handle(update(sandbox,2,'/가상비용변경 세금=0.3'))
    assert '/조건확인 ' in preview
    service.stop()
    assert '중지' in service.handle(update(sandbox,3,'/가상비용변경 세금=0.4'))
    assert '가상 비용' in service.handle(update(sandbox,4,'/가상비용'))
    assert '가상 비용 이력' in service.handle(update(sandbox,5,'/가상비용기록'))
    assert broker.calls==[]
    with connect(ctl.path) as db:db.execute("UPDATE condition_requests SET state='APPLYING'")
    ctl=ConditionCommands(ctl.path,cfg)
    assert pending(ctl)=='UNKNOWN'
    assert not ctl.sync(cfg,lambda:pytest.fail('restart retry'),now=1002,read_saved=lambda:cfg)
    cfg.update(data_mode='KIWOOM',kiwoom_env='REAL')
    ctl.sync(cfg,lambda:None,now=1003,busy=True)
    assert '현재 모드' in send(ctl,'/가상비용복귀 1',uid=7)


def test_cost_preview_exact_expiry_and_withdrawal(tmp_path):
    cfg,ctl=setup(tmp_path)
    preview=send(ctl,'/가상비용변경 세금=0.3')
    assert '만료' in send(ctl,'/조건확인 '+code(preview),uid=2,now=1120)
    preview=send(ctl,'/가상비용변경 세금=0.3',uid=3)
    assert '철회' in send(ctl,'/조건철회 '+code(preview),uid=4)


def test_new_virtual_snapshot_uses_costs_old_fixed_cohort_is_unchanged(tmp_path):
    from app.trade_review import settings_snapshot
    from test_morning_request import rig as morning_setup,call
    from app.morning_virtual import MorningVirtual,record_path
    from app.storage import Database
    from test_theme_delivery import setup as theme_setup
    cfg,ctl=setup(tmp_path)
    database,_,_=morning_setup(tmp_path/'general')
    history,_=theme_setup(tmp_path/'theme')
    call(tmp_path/'old',database,history,cfg,'/가상추적 일반 10만원')
    old_path=record_path(tmp_path/'old','DEMO','2026-10-20')
    old=MorningVirtual(Database(old_path,read_only=True)).plan()
    saved=[deepcopy(cfg)];approve(ctl,'/가상비용변경 세금=0.3')
    assert ctl.sync(cfg,lambda:saved.__setitem__(0,deepcopy(cfg)),now=1001,read_saved=lambda:saved[0])
    assert settings_snapshot(cfg)['paper_sell_tax_pct']==0.3
    call(tmp_path/'new',database,history,cfg,'/가상추적 일반 10만원')
    new=MorningVirtual(Database(record_path(tmp_path/'new','DEMO','2026-10-20'),read_only=True)).plan()
    assert new['settings']['paper_sell_tax_pct']==0.3
    assert MorningVirtual(Database(old_path,read_only=True)).plan()==old


def test_private_confirmation_to_owner_save_readback_and_restart_history(tmp_path,sandbox):
    cfg,ctl=setup(tmp_path);saved=[deepcopy(cfg)]
    service,broker,_,clock=sandbox;service.conditions=ctl
    preview=service.handle(update(sandbox,1,'/가상비용변경 수수료=0.02'))
    assert '예약' in service.handle(update(sandbox,2,'/조건확인 '+code(preview)))
    assert ctl.sync(cfg,lambda:saved.__setitem__(0,deepcopy(cfg)),now=clock[0].timestamp(),read_saved=lambda:saved[0])
    assert '최근 변경 적용됨' in service.handle(update(sandbox,3,'/가상비용'))
    service.conditions=ConditionCommands(ctl.path,saved[0])
    assert '수수료 0.02%' in service.handle(update(sandbox,4,'/가상비용기록'))
    assert broker.calls==[]


def test_cost_preview_is_invalidated_before_confirmation_and_disk_base_checked(tmp_path):
    cfg,ctl=setup(tmp_path)
    preview=send(ctl,'/가상비용변경 세금=0.3')
    cfg['paper_fee_pct']=0.025;ctl.sync(cfg,lambda:None,now=1001,busy=True)
    assert '바뀌었어요' in send(ctl,'/조건확인 '+code(preview),uid=2,now=1002)
    approve(ctl,'/가상비용변경 세금=0.3',uid=3)
    wrong=deepcopy(cfg);wrong['paper_fee_pct']=0.04
    assert not ctl.sync(cfg,lambda:pytest.fail('unsaved base'),now=1003,read_saved=lambda:wrong)
    assert pending(ctl)=='UNKNOWN'


def test_gui_owner_passes_persisted_readback_without_importing_gui():
    import ast
    from pathlib import Path
    tree=ast.parse(Path('app/gui.py').read_text(encoding='utf-8'))
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)
           and n.func.attr=='sync' and isinstance(n.func.value,ast.Attribute) and n.func.value.attr=='condition_commands'
           and any(k.arg=='busy' and isinstance(k.value,ast.Name) and k.value.id=='busy' for k in n.keywords)]
    assert len(calls)==1
    reader=next(k.value for k in calls[0].keywords if k.arg=='read_saved')
    assert isinstance(reader,ast.Attribute) and reader.attr=='strategy_snapshot'


@pytest.mark.parametrize('second',['/조건변경 거래대금=200','/청산조건변경 익절=5','/가상비용변경 수수료=0.02'])
def test_unknown_cost_never_allows_another_queued_save_or_later_tick_overwrite(tmp_path,second):
    cfg,ctl=setup(tmp_path);saved=[deepcopy(cfg)];saved_costs=[]
    approve(ctl,'/가상비용변경 세금=0.3')
    approve(ctl,second,uid=3)
    def save():saved_costs.append(cfg['paper_sell_tax_pct']);saved[0]=deepcopy(cfg)
    def read():
        if saved_costs:raise OSError('synthetic post-save read failure')
        return saved[0]
    assert not ctl.sync(cfg,save,now=1001,read_saved=read)
    assert saved_costs==[0.3] and saved[0]['paper_sell_tax_pct']==0.3 and cfg['paper_sell_tax_pct']==0.2
    with connect(ctl.path) as db:
        assert [r[0] for r in db.execute('SELECT state FROM condition_requests ORDER BY rowid')]==['UNKNOWN','REJECTED']
    assert not ctl.sync(cfg,lambda:pytest.fail('later overwrite'),now=1002,read_saved=lambda:saved[0])
    assert '/조건확인 ' not in send(ctl,'/조건변경 거래대금=300',uid=5)
    assert '미확인' in send(ctl,'/가상비용',uid=6)
    # Recreating the controller with stale memory cannot clear a durable barrier.
    stale=ConditionCommands(ctl.path,cfg)
    assert not stale.sync(cfg,lambda:pytest.fail('stale restart write'),now=1003,read_saved=lambda:saved[0])
    with connect(ctl.path) as db:assert db.execute("SELECT COUNT(*) FROM condition_requests WHERE state='UNKNOWN'").fetchone()[0]==1
    # A fresh saved configuration + matching persisted readback acknowledges
    # only current state, never claims the uncertain request succeeded.
    fresh=deepcopy(saved[0]);restarted=ConditionCommands(ctl.path,fresh)
    assert not restarted.sync(fresh,lambda:pytest.fail('recovery write'),now=1004,read_saved=lambda:saved[0])
    with connect(ctl.path) as db:
        assert [r[0] for r in db.execute('SELECT state FROM condition_requests ORDER BY rowid')]==['REVIEWED','REJECTED']
    approve(restarted,'/조건변경 거래대금=300',uid=7)
    assert restarted.sync(fresh,lambda:saved.__setitem__(0,deepcopy(fresh)),now=1005,read_saved=lambda:saved[0])
    assert saved[0]['paper_sell_tax_pct']==0.3 and saved[0]['min_value_eok']==300


def test_cost_unknown_status_is_not_hidden_by_later_applied_recommendation(tmp_path):
    cfg,ctl=setup(tmp_path)
    approve(ctl,'/가상비용변경 세금=0.3')
    approve(ctl,'/조건변경 거래대금=200',uid=3)
    with connect(ctl.path) as db:
        identifiers=[r[0] for r in db.execute('SELECT token FROM condition_requests ORDER BY rowid')]
        db.execute("UPDATE condition_requests SET state='UNKNOWN' WHERE token=?",(identifiers[0],))
        db.execute("UPDATE condition_requests SET state='APPLIED' WHERE token=?",(identifiers[1],))
    text=send(ctl,'/가상비용',uid=5)
    assert '저장 대조 미확인' in text and '최근 변경 적용됨' not in text


@pytest.mark.parametrize('journal_state',['UNKNOWN','APPLYING'])
def test_changing_mode_cannot_bypass_unknown_shared_settings_barrier(tmp_path,journal_state):
    cfg,ctl=setup(tmp_path);approve(ctl,'/가상비용변경 세금=0.3')
    stored=[deepcopy(cfg)]
    def save():stored[0]=deepcopy(cfg)
    def read():
        if stored[0]['paper_sell_tax_pct']==0.3:raise OSError('synthetic')
        return stored[0]
    assert not ctl.sync(cfg,save,now=1001,read_saved=read)
    with connect(ctl.path) as db:db.execute('UPDATE condition_requests SET state=?',(journal_state,))
    cfg.update(data_mode='KIWOOM',kiwoom_env='MOCK')
    assert not ctl.sync(cfg,lambda:pytest.fail('mode overwrite'),now=1002,read_saved=lambda:stored[0])
    assert '/조건확인 ' not in send(ctl,'/조건변경 거래대금=300',uid=3)
    assert '모든 조건 저장을 차단' in send(ctl,'/가상비용',uid=4)


def test_existing_proposal_saved_base_guard_blocks_after_unknown_cost(tmp_path):
    from test_proposal_application import rig,status
    from app.proposal_application import apply
    database,settings,proposal_id=rig(tmp_path)
    cfg=settings.values;ctl=ConditionCommands(tmp_path/'cost.db',cfg)
    approve(ctl,'/가상비용변경 세금=0.3')
    attempted=[]
    def save_cost():attempted.append(1);settings.save()
    def read_cost():
        if attempted:raise OSError('synthetic read failure')
        return settings.strategy_snapshot()
    assert not ctl.sync(cfg,save_cost,now=1001,read_saved=read_cost)
    before=settings.path.read_bytes()
    assert cfg['paper_sell_tax_pct']==0.2 and settings.strategy_snapshot()['paper_sell_tax_pct']==0.3
    with pytest.raises(ValueError):
        apply(database,cfg,proposal_id,lambda:pytest.fail('proposal overwrite'),settings.strategy_snapshot)
    def fail_read():raise OSError('synthetic')
    with pytest.raises(ValueError):
        apply(database,cfg,proposal_id,lambda:pytest.fail('proposal write without read'),fail_read)
    assert settings.path.read_bytes()==before and status(database)=='PENDING'
