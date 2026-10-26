"""Temporary user assertions only; no broker/GUI/network execution."""
import ast
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import pytest
from app.krx_history import HistoryStore
from app.actual_trade_records import import_csv
from app.recommendation_classification import Classification,classifications,latest
from app.morning_reconciliation import report
from app.operating_backup import backup,verify
from test_morning_reconciliation import setup,DAY,NOW
from test_actual_trade_records import row,blob
from test_telegram_orders import sandbox,update


def rig(root):
    tracker,_=setup(root);HistoryStore(root/'research.db')
    import_csv(root/'actual_fill_records.db',blob(row(filled_at=DAY+'T09:00:00+09:00')),now=NOW)
    return tracker


def command(control,text,**changes):
    return control.handle(text,**dict(dict(mode='REAL_QUOTES',scope='private',session='receiver',active=True,now=NOW),**changes))


def confirm(control,text,**changes):
    preview=command(control,text,**changes)
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',preview)
    assert token,preview
    return command(control,'/추천분류확인 '+token[1],**changes)


def test_private_command_confirm_restarts_three_states_and_backup_without_ledger_writes(tmp_path,sandbox):
    tracker=rig(tmp_path);actual=tmp_path/'actual_fill_records.db';before=(tracker.db.path.read_bytes(),actual.read_bytes())
    control=Classification(tmp_path);service,broker,_,clock=sandbox;clock[0]=NOW
    service.recommendation_classification=lambda text,**ctx:control.handle(text,mode='REAL_QUOTES',**ctx)
    service.morning_setup=lambda text:report(tmp_path,'REAL_QUOTES',DAY,now=NOW)
    bad=update(sandbox,1,f'/추천매수연결 {DAY} 900001 example-buy');bad['message']['from']['id']=99
    assert service.handle(bad) is None and latest(tmp_path,DAY)=={}
    preview=service.handle(update(sandbox,2,f'/추천매수연결 {DAY} 900001 example-buy'))
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',preview)[1]
    assert '저장했어요' in service.handle(update(sandbox,3,'/추천분류확인 '+token))
    assert '실제입력 연결' in service.handle(update(sandbox,4,'/추천대조'))
    assert '만료' in service.handle(update(sandbox,5,'/추천분류확인 '+token))
    restarted=Classification(tmp_path)
    assert classifications(tmp_path,DAY,NOW)['900001']=='실제입력 연결'
    assert '미구매 사용자확인' in confirm(restarted,f'/추천미구매 {DAY} 900001')
    assert '미확인 저장' in confirm(restarted,f'/추천분류철회 {DAY} 900001')
    assert classifications(tmp_path,DAY,NOW)['900001']=='미확인'
    with closing(sqlite3.connect(tmp_path/'research.db')) as db:assert db.execute('SELECT COUNT(*) FROM recommendation_classifications').fetchone()[0]==3
    assert before==(tracker.db.path.read_bytes(),actual.read_bytes()) and broker.calls==[]
    folder=tmp_path.parent/(tmp_path.name+'-backup');folder.mkdir()
    output=backup(tmp_path,folder);assert verify(output)['files']
    with closing(sqlite3.connect(output/'research.db')) as db:assert db.execute('SELECT COUNT(*) FROM recommendation_classifications').fetchone()[0]==3


@pytest.mark.parametrize('case',['expired','other-owner','restarted','mode','stopped','source-plan','source-fill','new-buy'])
def test_confirmation_revalidates_owner_time_mode_and_originals(tmp_path,case):
    tracker=rig(tmp_path);time=[0.];control=Classification(tmp_path,clock=lambda:time[0])
    preview=command(control,f'/추천매수연결 {DAY} 900001 example-buy')
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',preview)[1]
    changes={}
    if case=='expired':time[0]=120
    if case=='other-owner':changes['session']='new'
    if case=='restarted':control=Classification(tmp_path)
    if case=='mode':changes['mode']='MOCK'
    if case=='stopped':changes['active']=False
    if case=='source-plan':
        with tracker.db.connect() as db:db.execute("UPDATE morning_cohort SET payload=replace(payload,'scan_1','scan_2')")
    if case=='source-fill':
        with closing(sqlite3.connect(tmp_path/'actual_fill_records.db')) as db,db:db.execute("UPDATE actual_fills SET payload=replace(payload,'1000','1100')")
    if case=='new-buy':import_csv(tmp_path/'actual_fill_records.db',blob(row('second',filled_at=DAY+'T09:01:00+09:00')),now=NOW)
    before=(tmp_path/'research.db').read_bytes()
    assert '저장했어요' not in command(control,'/추천분류확인 '+token,**changes)
    assert (tmp_path/'research.db').read_bytes()==before


@pytest.mark.parametrize('case',['no-research','bad-research','wrong-fill','sell','wrong-code','wrong-day','mode'])
def test_invalid_inputs_never_create_classification(tmp_path,case):
    rig(tmp_path);path=tmp_path/'research.db'
    if case=='no-research':path.unlink()
    if case=='bad-research':
        path.unlink()
        with closing(sqlite3.connect(path)) as db:db.execute('CREATE TABLE unexpected(x)')
    fill='example-buy';code='900001';day=DAY;mode='REAL_QUOTES'
    if case=='wrong-fill':fill='missing'
    if case=='wrong-code':code='900002'
    if case=='wrong-day':day='2026-10-19'
    if case=='mode':mode='DEMO'
    if case=='sell':
        import_csv(tmp_path/'actual_fill_records.db',blob(row('sell',filled_at=DAY+'T09:00:00+09:00',side='SELL')),now=NOW);fill='sell'
    before=path.read_bytes() if path.exists() else None
    result=command(Classification(tmp_path),f'/추천매수연결 {day} {code} {fill}',mode=mode)
    assert '/추천분류확인 ' not in result
    assert (path.read_bytes() if path.exists() else None)==before


def test_stale_display_and_concurrent_confirmation_keep_append_history(tmp_path):
    rig(tmp_path);one=Classification(tmp_path);two=Classification(tmp_path)
    preview=command(one,f'/추천미구매 {DAY} 900001');token=re.search(r'/추천분류확인 ([a-f0-9]{16})',preview)[1]
    assert '저장했어요' in confirm(two,f'/추천매수연결 {DAY} 900001 example-buy')
    assert '저장했어요' not in command(one,'/추천분류확인 '+token)
    import_csv(tmp_path/'actual_fill_records.db',blob(row('later',filled_at=DAY+'T09:02:00+09:00')),now=NOW)
    assert '원본 변경' in classifications(tmp_path,DAY,NOW)['900001']


def test_optional_callback_forwarded_and_stop_clears_preview(tmp_path,sandbox):
    rig(tmp_path);control=Classification(tmp_path);service,_,_,clock=sandbox;clock[0]=NOW
    service.recommendation_classification=lambda text,**ctx:control.handle(text,mode='REAL_QUOTES',**ctx)
    preview=service.handle(update(sandbox,1,f'/추천미구매 {DAY} 900001'))
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',preview)[1]
    service.cancel_pending_changes()
    assert '저장했어요' not in service.handle(update(sandbox,2,'/추천분류확인 '+token))
    for name in ('gui.py','telegram_worker.py'):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'app'/name).read_text(encoding='utf-8'))
        calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call) and any(k.arg=='recommendation_classification' for k in n.keywords)]
        assert len(calls)==1


@pytest.mark.parametrize('at_call',[3,4])
def test_source_change_inside_commit_rolls_back(tmp_path,monkeypatch,at_call):
    import app.recommendation_classification as module
    rig(tmp_path);original=module.source;calls=[0]
    def changed(*args,**kwargs):
        calls[0]+=1
        if calls[0]==at_call:
            import_csv(tmp_path/'actual_fill_records.db',blob(row('racing',filled_at=DAY+'T09:03:00+09:00')),now=NOW)
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'source',changed)
    control=Classification(tmp_path);preview=command(control,f'/추천미구매 {DAY} 900001')
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',preview)[1]
    before=(tmp_path/'research.db').read_bytes()
    assert '저장했어요' not in command(control,'/추천분류확인 '+token)
    assert latest(tmp_path,DAY)=={} and (tmp_path/'research.db').read_bytes()==before


def test_real_gui_callback_expression_checks_exact_mode_without_opening_gui(tmp_path):
    from types import SimpleNamespace
    rig(tmp_path);control=Classification(tmp_path)
    tree=ast.parse((Path(__file__).resolve().parents[1]/'app/gui.py').read_text(encoding='utf-8'))
    from test_recommendation_request import gui_method
    fake=SimpleNamespace(cfg=dict(data_mode='KIWOOM',kiwoom_env='UNKNOWN'),recommendation_classification=control)
    fake.telegram_order_worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:False,service=SimpleNamespace(scope='private',session='receiver'))
    function=lambda text,**ctx:gui_method('classify_recommendation')(fake,text,**ctx)
    context=dict(scope='private',session='receiver',active=True,now=NOW)
    assert '/추천분류확인 ' not in function(f'/추천미구매 {DAY} 900001',**context)
    fake.cfg['kiwoom_env']='REAL'
    assert '/추천분류확인 ' in function(f'/추천미구매 {DAY} 900001',**context)


def test_corrupt_history_is_not_a_valid_classification(tmp_path):
    rig(tmp_path);assert '저장했어요' in confirm(Classification(tmp_path),f'/추천미구매 {DAY} 900001')
    with closing(sqlite3.connect(tmp_path/'research.db')) as db,db:
        db.execute("UPDATE recommendation_classifications SET payload='PRIVATE_INVALID'")
    with pytest.raises(ValueError,match='분류를 추측하지') as error:report(tmp_path,'REAL_QUOTES',DAY,now=NOW)
    assert 'PRIVATE_INVALID' not in str(error.value)


@pytest.mark.parametrize('change',['mode','interrupted','owner'])
def test_gui_rechecks_live_authority_after_source_read(tmp_path,monkeypatch,change):
    import app.recommendation_classification as module
    from types import SimpleNamespace
    from test_recommendation_request import gui_method
    rig(tmp_path);control=Classification(tmp_path);flags=[False]
    worker=SimpleNamespace(isRunning=lambda:True,isInterruptionRequested=lambda:flags[0],service=SimpleNamespace(scope='private',session='receiver'))
    fake=SimpleNamespace(cfg=dict(data_mode='KIWOOM',kiwoom_env='REAL'),recommendation_classification=control,telegram_order_worker=worker)
    invoke=lambda text:gui_method('classify_recommendation')(fake,text,scope='private',session='receiver',active=lambda:True,now=NOW)
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',invoke(f'/추천미구매 {DAY} 900001'))[1]
    original=module.source
    def changed(*args,**kwargs):
        value=original(*args,**kwargs)
        if change=='mode':fake.cfg['kiwoom_env']='MOCK'
        elif change=='interrupted':flags[0]=True
        else:worker.service.session='new-owner'
        return value
    monkeypatch.setattr(module,'source',changed)
    before=(tmp_path/'research.db').read_bytes()
    assert '저장했어요' not in invoke('/추천분류확인 '+token)
    assert (tmp_path/'research.db').read_bytes()==before and latest(tmp_path,DAY)=={}


@pytest.mark.parametrize('damage',['scope-missing','session-missing','confirmed_at-missing','scope-type','session-empty','naive','early','extra','future'])
def test_rehashed_invalid_assertion_metadata_is_not_confirmation(tmp_path,damage):
    from app.recommendation_classification import packed,digest
    rig(tmp_path);assert '저장했어요' in confirm(Classification(tmp_path),f'/추천미구매 {DAY} 900001')
    with closing(sqlite3.connect(tmp_path/'research.db')) as db,db:
        value=json.loads(db.execute('SELECT payload FROM recommendation_classifications').fetchone()[0])
        if damage.endswith('-missing'):value.pop(damage.removesuffix('-missing'))
        elif damage=='scope-type':value['scope']=7
        elif damage=='session-empty':value['session']=''
        elif damage=='naive':value['confirmed_at']=DAY+'T12:00:00'
        elif damage=='early':value['confirmed_at']='2026-10-19T12:00:00+09:00'
        elif damage=='future':value['confirmed_at']='2026-10-21T12:00:00+09:00'
        else:value['unapproved']='extra'
        db.execute('UPDATE recommendation_classifications SET payload=?,hash=?',(packed(value),digest(value)))
    if damage=='future':assert classifications(tmp_path,DAY,NOW)['900001'].startswith('미확인')
    else:
        with pytest.raises(ValueError):classifications(tmp_path,DAY,NOW)


@pytest.mark.parametrize('journal',['DELETE','WAL'])
def test_final_snapshot_reserves_existing_writers_until_commit(tmp_path,monkeypatch,journal):
    import app.recommendation_classification as module
    rig(tmp_path);tracker,_=setup(tmp_path)
    actual=tmp_path/'actual_fill_records.db'
    for path in (tracker.db.path,actual):
        with closing(sqlite3.connect(path)) as db:db.execute('PRAGMA journal_mode='+journal)
    before=(tracker.db.path.read_bytes(),actual.read_bytes());control=Classification(tmp_path)
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',command(control,f'/추천미구매 {DAY} 900001'))[1]
    original=module.source;calls=[0];blocked=[]
    def attempted(*args,**kwargs):
        value=original(*args,**kwargs);calls[0]+=1
        if calls[0]==3:
            with pytest.raises(ValueError,match='진행 중'):import_csv(actual,blob(row('racing',filled_at=DAY+'T09:03:00+09:00')),now=NOW)
            for path in (tracker.db.path,actual):
                with closing(sqlite3.connect(path,timeout=0)) as db:
                    with pytest.raises(sqlite3.OperationalError):db.execute('BEGIN IMMEDIATE')
                blocked.append(path.name)
        return value
    monkeypatch.setattr(module,'source',attempted)
    assert '저장했어요' in command(control,'/추천분류확인 '+token) and len(blocked)==2
    assert before==(tracker.db.path.read_bytes(),actual.read_bytes())
    import_csv(actual,blob(row('after-commit',filled_at=DAY+'T09:03:00+09:00')),now=NOW)
    assert classifications(tmp_path,DAY,NOW)['900001'].startswith('미확인')


def test_absent_actual_file_import_is_blocked_without_creating_database(tmp_path,monkeypatch):
    import app.recommendation_classification as module
    rig(tmp_path);actual=tmp_path/'actual_fill_records.db';actual.unlink();control=Classification(tmp_path)
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',command(control,f'/추천미구매 {DAY} 900001'))[1]
    original=module.source;calls=[0]
    def attempted(*args,**kwargs):
        value=original(*args,**kwargs);calls[0]+=1
        if calls[0]==3:
            with pytest.raises(ValueError,match='진행 중'):import_csv(actual,blob(row(filled_at=DAY+'T09:03:00+09:00')),now=NOW)
        return value
    monkeypatch.setattr(module,'source',attempted)
    assert '저장했어요' in command(control,'/추천분류확인 '+token) and not actual.exists()


def test_source_reservation_failure_does_not_write_classification(tmp_path):
    rig(tmp_path);control=Classification(tmp_path)
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',command(control,f'/추천미구매 {DAY} 900001'))[1]
    before=(tmp_path/'research.db').read_bytes()
    with closing(sqlite3.connect(tmp_path/'actual_fill_records.db')) as writer:
        writer.execute('BEGIN IMMEDIATE')
        assert '저장했어요' not in command(control,'/추천분류확인 '+token)
    assert (tmp_path/'research.db').read_bytes()==before


@pytest.mark.parametrize('entry',['import','classification'])
def test_shared_lock_link_is_rejected_before_touching_target(tmp_path,monkeypatch,entry):
    rig(tmp_path);control=Classification(tmp_path)
    token=re.search(r'/추천분류확인 ([a-f0-9]{16})',command(control,f'/추천미구매 {DAY} 900001'))[1]
    lock=tmp_path/'actual_fill_records.db.import.lock';lock.write_bytes(b'PRIVATE_UNTOUCHED')
    original=Path.is_symlink
    monkeypatch.setattr(Path,'is_symlink',lambda path:path==lock or original(path))
    before=(tmp_path/'research.db').read_bytes()
    if entry=='import':
        with pytest.raises(ValueError):import_csv(tmp_path/'actual_fill_records.db',blob(row('later',filled_at=DAY+'T09:03:00+09:00')),now=NOW)
    else:assert '저장했어요' not in command(control,'/추천분류확인 '+token)
    assert lock.read_bytes()==b'PRIVATE_UNTOUCHED' and (tmp_path/'research.db').read_bytes()==before


def test_import_cli_lock_contention_has_no_traceback(tmp_path,monkeypatch,capsys):
    import sys
    import app.config as config
    from app.actual_trade_records import main,input_lock
    rig(tmp_path);csv=tmp_path/'synthetic.csv';csv.write_bytes(blob(row('later',filled_at=DAY+'T09:03:00+09:00')))
    monkeypatch.setattr(config,'data_dir',lambda:tmp_path)
    monkeypatch.setattr(sys,'argv',['actual_trade_records',str(csv)])
    with input_lock(tmp_path/'actual_fill_records.db'):assert main()==1
    output=capsys.readouterr();assert '실패' in output.out and not output.err
