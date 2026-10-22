"""Quote-only fake authentication/socket -> durable observations -> UI stop."""
import ast
from copy import deepcopy
from datetime import datetime,timedelta
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
import sqlite3
import pytest

from app.config import DEFAULTS
from app.krx_history import HistoryStore
from app.morning_virtual import MorningVirtual,record_path
from app.morning_venue_policy import KRX_OPEN
from app.storage import Database
from app.quote_capture import Capture,summary,targets,AUTH_URL
from app.quote_capture_request import QuoteCaptureRequest
from app.runtime_lock import ReceiverLock
from app.trading_clock import KST
from test_telegram_orders import sandbox,update

NOW=datetime(2026,10,20,9,1,tzinfo=KST)
MARKER='SECRET_MARKER_NEVER_PERSIST'


def rig(root,count=1):
    HistoryStore(root/'research.db')
    cfg=deepcopy(DEFAULTS);cfg.update(data_mode='KIWOOM',kiwoom_env='REAL')
    path=record_path(root,'REAL_QUOTES','2026-10-20');tracker=MorningVirtual(Database(path))
    tracker.prepare(day='2026-10-20',mode='REAL_QUOTES',origin='test-fixed',targets=[dict(code=f'{900000+i:06}',name='합성',market='KOSPI',nxt_eligible=None) for i in range(1,count+1)],settings=cfg,total_won=100000,now=NOW.replace(hour=8),venue_policy=KRX_OPEN)
    return tracker


def frame(code='900001',clock='090000',price='1000',**extra):
    return json.dumps(dict(trnm='REAL',data=[dict(type='0B',item=code,values={'20':clock,'10':price,'16':'1000','1890':'090000','9081':'KRX',**extra})]))


class Auth:
    url=AUTH_URL
    def __init__(self):self.calls=[];self.token=MARKER;self.session=SimpleNamespace(close=lambda:self.calls.append('close'))
    def login(self):self.calls.append('login');self.token=MARKER


class Socket:
    def __init__(self,frames,active):self.frames=list(frames);self.active=active;self.sent=[];self.closed=False
    def send(self,value):self.sent.append(json.loads(value))
    def recv(self,**kw):
        if not self.frames:self.active[0]=False;raise TimeoutError()
        value=self.frames.pop(0)
        if isinstance(value,Exception):raise value
        return value
    def close(self):self.closed=True


def run(root,frames,**kwargs):
    active=[True];auth=Auth();socket=Socket(frames,active)
    capture=Capture(root,lambda:auth,socket_factory=lambda:socket,clock=lambda:NOW,**kwargs)
    result=capture.run(active=lambda:active[0]);return capture,result,auth,socket


def records(root,table):
    with closing(sqlite3.connect(root/'research.db')) as db:return db.execute('SELECT * FROM '+table).fetchall()


LOGIN=json.dumps(dict(trnm='LOGIN',return_code=0,return_msg=MARKER))
REG=json.dumps(dict(trnm='REG',return_code=0,grp_no='1',return_msg=MARKER))


def test_quote_only_capture_records_raw_candidates_and_no_secrets_or_ledger_changes(tmp_path):
    tracker=rig(tmp_path);before=tracker.db.path.read_bytes()
    capture,state,auth,socket=run(tmp_path,[LOGIN,frame(),REG,frame(),frame(clock='090001')])
    assert state=='STOPPED' and auth.calls==['login','close'] and socket.closed
    assert [p['trnm'] for p in socket.sent]==['LOGIN','REG']
    assert socket.sent[1]['data']==[dict(item=['900001'],type=['0B'])]
    frames=records(tmp_path,'quote_capture_frames');assert len(frames)==3 and sum(row[4] for row in frames)==1
    assert frames[0][2]==frame() and frames[0][3]==hashlib.sha256(frame().encode()).hexdigest()
    assert json.loads(frames[0][5])['rows'][0]['registration']=='UNCONFIRMED'
    assert json.loads(frames[1][5])['duplicate'] is True
    assert MARKER.encode() not in (tmp_path/'research.db').read_bytes()
    assert MARKER not in str(records(tmp_path,'quote_capture_events'))+summary(tmp_path)
    assert '공식 KRX 첫 거래' in summary(tmp_path) and tracker.db.path.read_bytes()==before
    assert capture.run()=='FAILED'


@pytest.mark.parametrize('level',['value','escaped-value','row','top'])
def test_duplicate_json_keys_never_persist_hidden_original(tmp_path,level):
    rig(tmp_path)
    raw=frame()
    if level=='value':raw=raw.replace('"10": "1000"','"10": "'+MARKER+'", "10": "1000"')
    elif level=='escaped-value':raw=raw.replace('"10": "1000"','"\\u0031\\u0030": "'+MARKER+'", "10": "1000"')
    elif level=='row':raw=raw.replace('"item": "900001"','"item": "'+MARKER+'", "item": "900001"')
    else:raw=raw.replace('"trnm": "REAL"','"trnm": "'+MARKER+'", "trnm": "REAL"')
    _,state,_,socket=run(tmp_path,[LOGIN,REG,raw])
    assert state=='FAILED' and socket.closed
    assert records(tmp_path,'quote_capture_frames')==[]
    assert 'DUPLICATE_JSON_KEY' in str(records(tmp_path,'quote_capture_events'))
    assert MARKER.encode() not in (tmp_path/'research.db').read_bytes()
    assert MARKER not in summary(tmp_path)+str(records(tmp_path,'quote_capture_events'))


@pytest.mark.parametrize('bad',[json.dumps(dict(trnm='REAL',data=[{'type':'00','account':MARKER}])),
    json.dumps(dict(trnm='UNKNOWN',token=MARKER)),frame(clock='090200'),frame(clock='085959'),frame(code='900099'),
    frame(price='0'),frame(token=MARKER),'X'*65537,OSError(MARKER)],ids=['account','unknown','future','preopen','wrong-code','zero','secret-field','oversize','disconnect'])
def test_invalid_or_disconnected_source_fails_without_persisting_original(tmp_path,bad):
    rig(tmp_path);_,state,auth,socket=run(tmp_path,[LOGIN,REG,bad])
    assert state=='FAILED' and len(auth.calls)==2 and socket.closed
    assert records(tmp_path,'quote_capture_frames')==[]
    assert MARKER not in str(records(tmp_path,'quote_capture_events'))
    assert records(tmp_path,'quote_capture_events')[-1][2]=='FAILED'


def test_reverse_time_and_frame_limit_terminate_without_reconnect(tmp_path):
    rig(tmp_path);_,state,_,socket=run(tmp_path,[LOGIN,REG,frame(clock='090001'),frame(clock='090000')])
    assert state=='FAILED' and len(records(tmp_path,'quote_capture_sessions'))==1
    assert len(records(tmp_path,'quote_capture_frames'))==1 and socket.closed
    _,state,_,_=run(tmp_path,[LOGIN,REG,frame(),frame(clock='090001')],max_frames=1)
    assert state=='FAILED' and 'BUFFER_LIMIT' in str(records(tmp_path,'quote_capture_events'))
    assert len(records(tmp_path,'quote_capture_sessions'))==2


def test_wrong_auth_environment_target_limit_lock_and_missing_db_do_no_auth(tmp_path):
    rig(tmp_path,count=101);called=[]
    cap=Capture(tmp_path,lambda:called.append('auth'),clock=lambda:NOW)
    assert cap.run()=='FAILED' and called==[]
    assert '기록이 없어요' in summary(tmp_path)
    empty=tmp_path/'absent';assert '기록이 없어요' in summary(empty) and not empty.exists()
    with pytest.raises(ValueError):targets(empty,NOW)


def test_owner_cancellation_and_wrong_auth_url_never_connect(tmp_path):
    rig(tmp_path);active=[True];sockets=[]
    def factory():active[0]=False;return Auth()
    assert Capture(tmp_path,factory,socket_factory=lambda:sockets.append(1),clock=lambda:NOW).run(active=lambda:active[0])=='STOPPED'
    bad=Auth();bad.url='https://mockapi.kiwoom.com'
    assert Capture(tmp_path,lambda:bad,socket_factory=lambda:sockets.append(1),clock=lambda:NOW).run()=='FAILED'
    assert bad.calls==['close'] and sockets==[]
    with ReceiverLock(tmp_path/'quote_capture.lock'):
        assert Capture(tmp_path,lambda:pytest.fail('locked auth'),clock=lambda:NOW).run()=='FAILED'


def gui_method(name,**scope):
    tree=ast.parse(Path('app/gui.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
    node=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name==name)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'fake-quote-ui','exec'),scope);return scope[name]


def test_private_command_owner_worker_fake_stream_status_stop(tmp_path,sandbox):
    tracker=rig(tmp_path);service,broker,_,clock=sandbox;clock[0]=NOW;service.daily_mode='REAL_QUOTES'
    control=QuoteCaptureRequest(tmp_path);service.quote_capture=control
    bad=update(sandbox,1,'/시세수집시작');bad['message']['from']['id']=99
    assert service.handle(bad) is None and control.state=='IDLE'
    assert '요청했어요' in service.handle(update(sandbox,2,'/시세수집시작'))
    current=[True];auth=Auth();socket=Socket([LOGIN,REG,frame()],current)
    receiver=SimpleNamespace(service=service,isRunning=lambda:True,isInterruptionRequested=lambda:False)
    window=SimpleNamespace(cfg=dict(data_mode='KIWOOM',kiwoom_env='REAL'),quote_capture=control,quote_capture_worker=None,
        telegram_order_worker=receiver,work=None,broker_keys=lambda env:('SYNTHETIC','SYNTHETIC'))
    class Worker:
        def __init__(self,root,factory,active):self.root=root;self.factory=factory;self.active=active;self.completed=SimpleNamespace(connect=lambda cb:setattr(self,'done',cb))
        def isRunning(self):return False
        def start(self):
            capture=Capture(self.root,self.factory,socket_factory=lambda:socket,clock=lambda:NOW)
            self.done(capture.run(active=lambda:self.active() and current[0]))
    gui_method('poll_quote_capture',current_mode=lambda cfg:SimpleNamespace(key='REAL_QUOTES'),QuoteCaptureWorker=Worker,
               KiwoomMarket=lambda *a,**kw:auth,data_dir=lambda:tmp_path)(window)
    assert control.state=='STOPPED' and '후보 1개' in service.handle(update(sandbox,3,'/시세수집상태'))
    assert '중지' in service.handle(update(sandbox,4,'/시세수집중지'))
    assert tracker.db.trades()==[] and broker.calls==[]


def test_controller_busy_expiry_mode_and_receiver_replacement_reads_no_credentials(tmp_path):
    rig(tmp_path);clock=[0.];control=QuoteCaptureRequest(tmp_path,clock=lambda:clock[0])
    assert '실제 시세' in control.request(mode='MOCK') and control.state=='IDLE'
    control.request(mode='REAL_QUOTES');assert control.take(mode='REAL_QUOTES',busy=True,active=True) is None
    clock[0]=120;assert control.take(mode='REAL_QUOTES',busy=False,active=True) is None
    control.request(mode='REAL_QUOTES',owner=('scope','old'))
    receiver=SimpleNamespace(service=SimpleNamespace(scope='scope',session='new'),isRunning=lambda:True,isInterruptionRequested=lambda:False)
    fake=SimpleNamespace(cfg=dict(data_mode='KIWOOM',kiwoom_env='REAL'),quote_capture=control,quote_capture_worker=None,
                         telegram_order_worker=receiver,work=None,broker_keys=lambda env:pytest.fail('credentials'))
    gui_method('poll_quote_capture',current_mode=lambda cfg:SimpleNamespace(key='REAL_QUOTES'))(fake)
    assert control.state=='CANCELED'


@pytest.mark.parametrize('change',['mode','receiver','stop'])
def test_running_worker_active_guard_immediately_tracks_owner_changes(tmp_path,change):
    rig(tmp_path);control=QuoteCaptureRequest(tmp_path);control.request(mode='REAL_QUOTES',owner=('scope','session'))
    receiver=SimpleNamespace(service=SimpleNamespace(scope='scope',session='session',accepts_search_requests=lambda:True),isRunning=lambda:True,isInterruptionRequested=lambda:False)
    fake=SimpleNamespace(cfg=dict(data_mode='KIWOOM',kiwoom_env='REAL'),quote_capture=control,quote_capture_worker=None,
        telegram_order_worker=receiver,work=None,broker_keys=lambda env:pytest.fail('not requested'))
    class Worker:
        def __init__(self,root,factory,active):self.active=active;self.factory=factory;self.completed=SimpleNamespace(connect=lambda cb:None)
        def start(self):pass
        def isRunning(self):return True
    poll=gui_method('poll_quote_capture',current_mode=lambda cfg:SimpleNamespace(key='REAL_QUOTES'),QuoteCaptureWorker=Worker,data_dir=lambda:tmp_path)
    poll(fake);worker=fake.quote_capture_worker;assert worker.active()
    if change=='mode':fake.cfg['kiwoom_env']='MOCK'
    if change=='receiver':fake.telegram_order_worker=SimpleNamespace()
    if change=='stop':control.cancel()
    poll(fake)  # isRunning early return must not defeat the worker closure.
    assert not worker.active()
    with pytest.raises(ValueError):worker.factory()


def test_pending_stop_never_starts_worker_and_close_clears_deferred_backup(tmp_path):
    control=QuoteCaptureRequest(tmp_path);control.request(mode='REAL_QUOTES');control.cancel()
    assert control.take(mode='REAL_QUOTES',busy=False,active=True) is None
    calls=[]
    fake=SimpleNamespace(timer=SimpleNamespace(stop=lambda:None),quote_capture_worker=SimpleNamespace(isRunning=lambda:True),
        stop_quote_capture=lambda:calls.append('stop'),pending_operating_request='must-not-run-later')
    event=SimpleNamespace(ignore=lambda:calls.append('ignored'),accept=lambda:pytest.fail('active receiver close'))
    gui_method('closeEvent',QMessageBox=SimpleNamespace(information=lambda *a:None))(fake,event)
    assert fake.pending_operating_request is None and calls==['stop','ignored']


def test_capture_tables_are_backed_up_and_live_capture_lock_blocks_backup(tmp_path):
    from app.operating_backup import backup,verify
    root=tmp_path/'data';rig(root);destination=tmp_path/'backups';destination.mkdir()
    run(root,[LOGIN,REG,frame()])
    with ReceiverLock(root/'quote_capture.lock'):
        with pytest.raises(Exception):backup(root,destination)
    bundle=backup(root,destination)
    assert 'research.db' in {row['name'] for row in verify(bundle)['files']}
    with closing(sqlite3.connect(bundle/'research.db')) as db:
        assert db.execute('SELECT COUNT(*) FROM quote_capture_frames').fetchone()[0]==1


def test_date_change_and_handshake_failure_do_not_retry(tmp_path):
    rig(tmp_path);active=[True];auth=Auth();socket=Socket([LOGIN,REG,frame()],active);clock=[NOW]
    original=socket.recv
    def receive(**kwargs):
        result=original(**kwargs)
        if json.loads(result)['trnm']=='REAL':clock[0]+=timedelta(days=1)
        return result
    socket.recv=receive
    cap=Capture(tmp_path,lambda:auth,socket_factory=lambda:socket,clock=lambda:clock[0])
    assert cap.run(active=lambda:active[0])=='FAILED' and records(tmp_path,'quote_capture_frames')==[]
    _,state,auth,_=run(tmp_path,[json.dumps(dict(trnm='LOGIN',return_code=1,return_msg=MARKER))])
    assert state=='FAILED' and auth.calls==['login','close']
    assert MARKER not in str(records(tmp_path,'quote_capture_events'))


def test_current_private_receiver_can_explicitly_stop_gui_collection_but_stale_owner_cannot(tmp_path,sandbox):
    service,_,_,_=sandbox;control=QuoteCaptureRequest(tmp_path);service.quote_capture=control;service.daily_mode='REAL_QUOTES'
    control.request(mode='REAL_QUOTES')
    service.cancel_pending_changes()  # Receiver lifecycle is not a GUI stop request.
    assert control.state=='PENDING'
    assert '중지' in service.handle(update(sandbox,1,'/시세수집중지')) and control.state=='CANCELED'
    control.request(mode='REAL_QUOTES',owner=('scope','new-receiver'))
    control.cancel(owner=('scope','old-receiver'))
    assert control.state=='PENDING'


def test_storage_failure_has_safe_terminal_status_and_no_secret_exception(tmp_path,monkeypatch):
    from app import quote_capture as module
    rig(tmp_path);original=module._connect
    count=[0]
    def broken(path,readonly=False):
        if not readonly:
            count[0]+=1
            if count[0]>=6:raise sqlite3.OperationalError(MARKER)
        return original(path,readonly)
    monkeypatch.setattr(module,'_connect',broken)
    _,state,_,socket=run(tmp_path,[LOGIN,REG,frame()])
    assert state=='FAILED' and socket.closed
    text=summary(tmp_path)
    assert MARKER not in text and '연결 지속은 미확인' in text


def test_handshake_timeout_and_auth_exception_never_log_or_retry_secret(tmp_path):
    rig(tmp_path);active=[True];auth=Auth();socket=Socket([LOGIN],active);mono=[0.]
    receive=socket.recv
    def delayed(**kwargs):mono[0]+=11;return receive(**kwargs)
    socket.recv=delayed
    assert Capture(tmp_path,lambda:auth,socket_factory=lambda:socket,clock=lambda:NOW,monotonic=lambda:mono[0]).run(active=lambda:active[0])=='FAILED'
    assert 'HANDSHAKE_TIMEOUT' in str(records(tmp_path,'quote_capture_events'))
    broken=Auth()
    def login():raise RuntimeError(MARKER)
    broken.login=login
    assert Capture(tmp_path,lambda:broken,socket_factory=lambda:pytest.fail('socket after auth failure'),clock=lambda:NOW).run()=='FAILED'
    assert MARKER not in str(records(tmp_path,'quote_capture_events'))


def test_unknown_numeric_provider_exchange_stays_uninterpreted(tmp_path):
    rig(tmp_path);_,state,_,_=run(tmp_path,[LOGIN,REG,frame(**{'9081':'777'})])
    details=json.loads(records(tmp_path,'quote_capture_frames')[0][5])['rows'][0]
    assert state=='STOPPED' and details['provider_exchange_raw']=='777'
    assert details['provider_exchange_interpretation']=='UNKNOWN' and details['date_basis']=='LOCAL_SESSION_INFERRED'
