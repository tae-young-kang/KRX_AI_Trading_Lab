"""Offline maintenance UI methods and after-loop execution, never launch Qt."""
import ast
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from app.operating_backup import verify
from app.operating_backup_request import OperatingRequest,execute
from app.runtime_lock import ReceiverLock
from test_operating_backup import rig


def gui_function(name,**scope):
    tree=ast.parse(Path('app/gui.py').read_text(encoding='utf-8'))
    if name=='main':node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
    else:
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
        node=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'offline-ui-method','exec'),scope)
    return scope[name]


class Messages:
    Yes=1;No=2
    def __init__(self,answer=1):self.answer=answer;self.shown=[]
    def question(self,*args):self.shown.append(args);return self.answer
    def information(self,*args):self.shown.append(args)


def test_bundle_includes_unexecuted_request_and_new_folder_restore_without_permissions(tmp_path):
    root,destination,_=rig(tmp_path)
    path=root/'telegram_offline'/'telegram_commands.db';path.parent.mkdir()
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE tg_sessions(scope TEXT);CREATE TABLE tg_updates(uid INTEGER);CREATE TABLE theme_opening_requests(id INTEGER,state TEXT,mode TEXT);')
        db.execute("INSERT INTO theme_opening_requests VALUES(1,'RECORDED_UNEXECUTED','MOCK')")
    saved=execute(OperatingRequest('backup',root,destination))
    assert saved['ok']
    bundle=Path(saved['path']);info=verify(bundle)
    assert 'telegram_offline/telegram_commands.db' in {r['name'] for r in info['files']}
    result=execute(OperatingRequest('restore',root,bundle,tmp_path/'fresh'))
    assert result['ok'] and verify(Path(result['path']))==info
    with sqlite3.connect(Path(result['path'])/'telegram_offline'/'telegram_commands.db') as db:
        assert db.execute('SELECT state,mode FROM theme_opening_requests').fetchall()==[('RECORDED_UNEXECUTED','MOCK')]
    assert not (Path(result['path'])/'settings.json').exists()
    assert not (Path(result['path'])/'.env').exists()
    assert not (Path(result['path'])/'private_export.csv').exists()
    assert (root/'settings.json').read_text()=='PRIVATE_SETTINGS_NOT_FOR_BACKUP'


@pytest.mark.parametrize('action',['backup','restore'])
@pytest.mark.parametrize('lock',['application.lock','telegram_offline/receiver.lock','kiwoom_mock_watch/receiver.lock'])
def test_external_locks_block_deferred_actions(tmp_path,action,lock):
    root,destination,_=rig(tmp_path)
    saved=execute(OperatingRequest('backup',root,destination));bundle=Path(saved['path'])
    request=OperatingRequest(action,root,destination if action=='backup' else bundle,None if action=='backup' else tmp_path/'fresh')
    before=set(destination.iterdir())
    with ReceiverLock(root/lock):assert not execute(request)['ok']
    assert set(destination.iterdir())==before and not (tmp_path/'fresh').exists()


@pytest.mark.parametrize('target_kind',['current','inside_current','existing','inside_bundle'])
def test_restore_never_targets_existing_or_active_root(tmp_path,target_kind):
    root,destination,_=rig(tmp_path)
    bundle=Path(execute(OperatingRequest('backup',root,destination))['path'])
    target={'current':root,'inside_current':root/'new','existing':destination,'inside_bundle':bundle/'new'}[target_kind]
    assert not execute(OperatingRequest('restore',root,bundle,target))['ok']
    assert (root/'practice_demo.db').exists()


@pytest.mark.parametrize('answer,closed',[(2,True),(1,False)])
def test_confirmation_cancel_or_close_refusal_clears_stale_action(tmp_path,answer,closed):
    root,destination,_=rig(tmp_path);request=OperatingRequest('backup',root,destination)
    calls=[];messages=Messages(answer)
    window=SimpleNamespace(pending_operating_request='old',close=lambda:calls.append('close') or closed)
    gui_function('defer_operating_request',QMessageBox=messages)(window,request)
    assert window.pending_operating_request is None
    assert calls==(['close'] if answer==1 else [])


@pytest.mark.parametrize('busy',['work','telegram_order_worker','mock_watch_worker'])
def test_rejected_close_event_clears_pending_and_keeps_existing_stop_path(busy):
    calls=[];window=SimpleNamespace(timer=SimpleNamespace(stop=lambda:calls.append('timer')),
        work=None,telegram_order_worker=None,mock_watch_worker=None,pending_operating_request='stale',
        stop_mock_notices=lambda:calls.append('mock-stop'),stop_telegram_orders=lambda:calls.append('telegram-stop'))
    setattr(window,busy,SimpleNamespace(isRunning=lambda:True))
    event=SimpleNamespace(ignore=lambda:calls.append('ignored'),accept=lambda:calls.append('accepted'))
    gui_function('closeEvent',QMessageBox=Messages())(window,event)
    assert window.pending_operating_request is None and 'ignored' in calls and 'accepted' not in calls
    if busy!='work':assert ('mock-stop' if busy=='mock_watch_worker' else 'telegram-stop') in calls


def test_entrypoint_runs_only_after_loop_and_application_lock_release(tmp_path):
    root,destination,_=rig(tmp_path);request=OperatingRequest('backup',root,destination)
    calls=[];messages=Messages()
    class Window:
        pending_operating_request=request
        def show(self):calls.append('shown')
    class App:
        def setFont(self,*args):pass
        def exec(self):
            # A direct backup is blocked while the app owns its lock.
            assert not execute(request)['ok'];calls.append('loop');return 0
    fn=gui_function('main',QApplication=lambda args:App(),QFont=lambda *a:None,
        MainWindow=Window,QMessageBox=messages,data_dir=lambda:root)
    with pytest.raises(SystemExit) as result:fn()
    assert result.value.code==0 and calls==['shown','loop']
    assert len(list(destination.iterdir()))==1
    assert any('전체 백업 완료' in args for args in messages.shown)
    with ReceiverLock(root/'application.lock'):pass


def test_entrypoint_error_exit_never_executes_pending_work(tmp_path):
    root,destination,_=rig(tmp_path)
    class Window:
        pending_operating_request=OperatingRequest('backup',root,destination)
        def show(self):pass
    fn=gui_function('main',QApplication=lambda _:SimpleNamespace(setFont=lambda *a:None,exec=lambda:1),
        QFont=lambda *a:None,MainWindow=Window,QMessageBox=Messages(),data_dir=lambda:root)
    with pytest.raises(SystemExit) as result:fn()
    assert result.value.code==1 and list(destination.iterdir())==[]


def test_folder_selection_confirmation_inspection_and_restore_request(tmp_path):
    root,destination,_=rig(tmp_path);messages=Messages();answers=[str(destination)]
    dialogs=SimpleNamespace(getExistingDirectory=lambda *a:answers.pop(0))
    window=SimpleNamespace(close=lambda:True,work=None)
    window.defer_operating_request=lambda request:gui_function('defer_operating_request',QMessageBox=messages)(window,request)
    gui_function('request_operating_backup',QFileDialog=dialogs,data_dir=lambda:root,Path=Path)(window)
    assert window.pending_operating_request.action=='backup'
    assert any('프로그램을 종료한 뒤 실행' in str(a) for a in messages.shown)
    result=execute(window.pending_operating_request);assert result['ok']
    bundle=Path(result['path']);answers.extend([str(bundle)])
    seen=[]
    window.run_task=lambda fn,ok,label:seen.append(fn()) or ok(seen[-1])
    gui_function('inspect_operating_backup',QFileDialog=dialogs,QMessageBox=messages)(window)
    assert len(seen)==1 and seen[0]==verify(bundle)
    assert any('전체 백업 검사 완료' in a for a in messages.shown)
    answers.extend([str(bundle),str(destination)])
    gui_function('request_operating_restore',QFileDialog=dialogs,data_dir=lambda:root,Path=Path)(window)
    request=window.pending_operating_request
    assert request.action=='restore' and request.target.parent==destination and not request.target.exists()
    assert execute(request)['ok']
    # A cancelled picker erases a previously selected action too.
    answers.append('')
    gui_function('request_operating_backup',QFileDialog=dialogs,data_dir=lambda:root,Path=Path)(window)
    assert window.pending_operating_request is None


def test_failed_deferred_restore_is_reported_as_failure_without_creation(tmp_path):
    root,destination,_=rig(tmp_path);messages=Messages()
    class Window:
        pending_operating_request=OperatingRequest('restore',root,destination,tmp_path/'fresh')
        def show(self):pass
    fn=gui_function('main',QApplication=lambda _:SimpleNamespace(setFont=lambda *a:None,exec=lambda:0),
        QFont=lambda *a:None,MainWindow=Window,QMessageBox=messages,data_dir=lambda:root)
    with pytest.raises(SystemExit):fn()
    assert not (tmp_path/'fresh').exists() and any('기록 작업 실패' in a for a in messages.shown)


@pytest.mark.parametrize('inside',[False,True])
@pytest.mark.parametrize('action',['backup','restore'])
def test_existing_bundle_is_never_mutated_as_an_output_parent(tmp_path,inside,action):
    root,destination,_=rig(tmp_path)
    bundle=Path(execute(OperatingRequest('backup',root,destination))['path'])
    other=Path(execute(OperatingRequest('backup',root,destination))['path']) if action=='restore' else None
    output=bundle/'empty' if inside else bundle
    if inside:output.mkdir()
    original_info=verify(bundle)
    original_files={p.relative_to(bundle).as_posix():p.read_bytes() for p in bundle.rglob('*') if p.is_file()}
    before={p.relative_to(bundle).as_posix() for p in bundle.rglob('*')}
    request=OperatingRequest(action,root,output if action=='backup' else other,
                             None if action=='backup' else output/'new')
    assert not execute(request)['ok']
    assert verify(bundle)==original_info
    assert {p.relative_to(bundle).as_posix() for p in bundle.rglob('*')}==before
    assert {p.relative_to(bundle).as_posix():p.read_bytes() for p in bundle.rglob('*') if p.is_file()}==original_files
    if other is not None:assert verify(other)['files']==original_info['files']


@pytest.mark.parametrize('action',['backup','restore'])
def test_manifest_signal_blocks_output_even_when_it_is_not_a_valid_bundle(tmp_path,action):
    root,destination,_=rig(tmp_path)
    source=Path(execute(OperatingRequest('backup',root,destination))['path'])
    broken=tmp_path/'broken';broken.mkdir();marker=broken/'manifest.json';marker.write_bytes(b'not-json')
    child=broken/'nested';child.mkdir()
    request=OperatingRequest(action,root,child if action=='backup' else source,
                             None if action=='backup' else child/'new')
    assert not execute(request)['ok']
    assert marker.read_bytes()==b'not-json' and list(child.iterdir())==[]
