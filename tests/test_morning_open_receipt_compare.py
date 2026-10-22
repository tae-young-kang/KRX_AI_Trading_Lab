"""Synthetic CSV/user-claim comparison: never an exchange certificate or fill."""
from dataclasses import replace
from datetime import datetime
import csv

import pytest

from app.morning_open_receipt_compare import compare_saved, read_receipts, render
from app.morning_virtual import record_path
from app.trading_clock import KST
from test_morning_venue_policy import new
from test_morning_virtual import trade

DAY = '2026-10-20'
NOW = datetime(2026, 10, 22, 11, 0, tzinfo=KST)
HEADER = ('day','market','code','venue','first_at','price_won',
          'evidence_hash','capture_status')


def receipt_file(tmp_path, rows):
    path = tmp_path / 'user_claim.csv'
    with path.open('w',encoding='utf-8-sig',newline='') as fp:
        wr = csv.DictWriter(fp,fieldnames=list(HEADER))
        wr.writeheader()
        wr.writerows(rows)
    return path


def row(*,code='900001',market='KOSPI',price='1000',capture='CLAIMED_COMPLETE'):
    return dict(day=DAY,market=market,code=code,venue='KRX',
                first_at='2026-10-20T09:00:00+09:00',
                price_won=price,evidence_hash='a'*64,
                capture_status=capture)


def first_entry(tracker):
    start = datetime(2026,10,20,9,0,tzinfo=KST)
    supplied = replace(trade(venue='KRX',hour=9),at=start,fetched_at=start)
    assert tracker.enter(supplied,now=start)['state']=='VIRTUAL_ENTERED'


def test_matching_user_claim_is_not_certified_first_trade_or_actual_fill(tmp_path):
    tracker,_=new(tmp_path)
    first_entry(tracker)
    receipt=receipt_file(tmp_path,[row()])
    existing=record_path(tmp_path,'DEMO',DAY).read_bytes()
    status=compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert status['counts']['MATCHED_USER_CLAIM']==1
    assert status['counts']['NO_RECEIPT']==2
    result=render(status)
    assert '사용자 주장' in result and '인증서가 아닙니다' in result
    assert '실제 주문·수익률은 증명되지 않았어요' in result
    assert record_path(tmp_path,'DEMO',DAY).read_bytes()==existing
    assert len(tracker.db.trades())==1


def test_no_virtual_open_does_not_create_fill_for_user_supplied_receipt(tmp_path):
    tracker,_=new(tmp_path)
    receipt=receipt_file(tmp_path,[row()])
    status=compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert status['counts']['NO_VIRTUAL_ENTRY']==1
    assert status['counts']['NO_RECEIPT']==2
    assert tracker.db.trades()==[]


def test_price_hash_or_timestamp_conflict_remains_visible(tmp_path):
    tracker,_=new(tmp_path)
    first_entry(tracker)
    changed=row(price='1001')
    path=receipt_file(tmp_path,[changed])
    assert compare_saved(tmp_path,'DEMO',DAY,path,now=NOW)['counts']['RECEIPT_MISMATCH']==1
    changed=row()
    changed['evidence_hash']='b'*64
    path=receipt_file(tmp_path,[changed])
    assert compare_saved(tmp_path,'DEMO',DAY,path,now=NOW)['counts']['RECEIPT_MISMATCH']==1
    changed=row()
    changed['first_at']='2026-10-20T09:01:00+09:00'
    path=receipt_file(tmp_path,[changed])
    assert compare_saved(tmp_path,'DEMO',DAY,path,now=NOW)['counts']['RECEIPT_MISMATCH']==1


def test_matching_values_without_session_completeness_are_not_first_trade_proof(tmp_path):
    tracker,_=new(tmp_path)
    first_entry(tracker)
    receipt=receipt_file(tmp_path,[row(capture='UNKNOWN')])
    report=compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert report['counts']['USER_CAPTURE_UNVERIFIED']==1
    assert '수집 완전성 불명 1' in render(report)


@pytest.mark.parametrize('field,value',[
    ('venue','NXT'),
    ('day','2026-10-21'),
    ('market','WRONG'),
    ('first_at','2026-10-20T08:59:00+09:00'),
    ('first_at','2030-10-20T09:00:00+09:00'),
    ('first_at','2026-10-20T09:00:00'),
    ('price_won','0'),
    ('price_won','=1000'),
    ('evidence_hash','invalid'),
    ('capture_status','CERTIFIED'),
])
def test_invalid_csv_is_rejected_without_importing_data(tmp_path,field,value):
    tracker,_=new(tmp_path)
    entry=row()
    entry[field]=value
    receipt=receipt_file(tmp_path,[entry])
    with pytest.raises(ValueError,match='CSV'):
        compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert tracker.db.trades()==[]


def test_duplicate_symbol_or_outside_cohort_fails_closed(tmp_path):
    tracker,_=new(tmp_path)
    duplicate=receipt_file(tmp_path,[row(),row()])
    with pytest.raises(ValueError):
        compare_saved(tmp_path,'DEMO',DAY,duplicate,now=NOW)
    outside=receipt_file(tmp_path,[row(code='999999')])
    with pytest.raises(ValueError,match='목록 밖'):
        compare_saved(tmp_path,'DEMO',DAY,outside,now=NOW)
    assert tracker.db.trades()==[]


def test_missing_cohort_source_modes_and_legacy_are_not_backfilled(tmp_path):
    tracker,_=new(tmp_path)
    receipt=receipt_file(tmp_path,[row()])
    assert compare_saved(tmp_path,'MOCK',DAY,receipt,now=NOW) is None
    assert '고정된 아침 추천 목록이 없어' in render(None)
    assert not record_path(tmp_path,'MOCK',DAY).exists()
    with pytest.raises(ValueError):
        compare_saved(tmp_path,'DEMO','2030-10-20',receipt,now=NOW)


def test_large_invalid_csv_and_display_limit(tmp_path):
    tracker,_=new(tmp_path)
    receipt=receipt_file(tmp_path,[row()])
    receipt.write_bytes(b'X'*(1024*1024+1))
    with pytest.raises(ValueError):
        read_receipts(receipt,day=DAY,now=NOW)
    valid=receipt_file(tmp_path,[row()])
    with pytest.raises(ValueError):
        render(compare_saved(tmp_path,'DEMO',DAY,valid,now=NOW),max_rows=0)


def test_cli_and_gui_entry_are_explicit_read_only_paths():
    import inspect
    from app import morning_open_receipt_compare as module
    from app import gui
    function=inspect.getsource(module.main)
    gui_source=inspect.getsource(gui.MainWindow.compare_morning_open_csv)
    assert '--root' in function and '--csv' in function
    assert 'QFileDialog.getOpenFileName' in gui_source
    assert 'compare_saved(' in gui_source
    assert 'paper_open' not in gui_source and 'order' not in gui_source


@pytest.mark.parametrize('change',['row_market','entry_price','source_mode','after_close'])
def test_corrupted_entry_never_matches_even_when_receipt_values_agree(tmp_path,change):
    import json
    tracker,_=new(tmp_path);first_entry(tracker)
    receipt=receipt_file(tmp_path,[row()])
    with tracker.db.connect() as db:
        stored=db.execute('SELECT entry_json,strat_json FROM paper_trades').fetchone()
        entry=json.loads(stored['entry_json']);settings=json.loads(stored['strat_json'])
        if change=='row_market':db.execute("UPDATE paper_trades SET market='KOSDAQ'")
        if change=='entry_price':entry['price']=9999
        if change=='source_mode':settings.update(data_mode='KIWOOM',kiwoom_env='REAL')
        if change=='after_close':
            entry['stamp']=entry['quote_stamp']='2026-10-20T23:00:00+09:00'
            db.execute('UPDATE paper_trades SET opened_at=?',(entry['stamp'],))
        db.execute('UPDATE paper_trades SET entry_json=?,strat_json=?',(json.dumps(entry),json.dumps(settings)))
    before=tracker.db.path.read_bytes()
    with pytest.raises(ValueError,match='근거'):compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert tracker.db.path.read_bytes()==before


def test_compare_uses_one_snapshot_when_writer_replaces_validated_plan_and_entry(tmp_path,monkeypatch):
    import json
    from app import morning_open_receipt_compare as module
    tracker,_=new(tmp_path);first_entry(tracker)
    receipt=receipt_file(tmp_path,[row()]);original=module.read_receipts;connections=[]
    from app.storage import Database
    connect=Database.connect
    def tracked(self):
        connection=connect(self)
        if self.read_only:connections.append(connection)
        return connection
    monkeypatch.setattr(Database,'connect',tracked)
    def concurrent_change(*args,**kwargs):
        assert len(connections)==1 and connections[0].in_transaction
        # SQLite WAL permits a writer while comparison holds its read snapshot.
        with tracker.db.connect() as db:
            entry=json.loads(db.execute('SELECT entry_json FROM paper_trades').fetchone()[0]);entry['venue']='NXT'
            db.execute('UPDATE paper_trades SET entry_json=?',(json.dumps(entry),))
            plan=json.loads(db.execute('SELECT payload FROM morning_cohort').fetchone()[0]);plan['targets'][0]['name']='동시 변경 이름'
            db.execute('UPDATE morning_cohort SET payload=?',(json.dumps(plan),))
        return original(*args,**kwargs)
    monkeypatch.setattr(module,'read_receipts',concurrent_change)
    report=compare_saved(tmp_path,'DEMO',DAY,receipt,now=NOW)
    assert len(connections)==1 and report['counts']['MATCHED_USER_CLAIM']==1
    assert report['results'][0]['name']!='동시 변경 이름'
    from app.morning_open_evidence_status import inspect
    with pytest.raises(ValueError):inspect(tmp_path,'DEMO',DAY,now=NOW)


@pytest.mark.parametrize('stamp,allowed',[
    ('2026-10-20T15:30:00+09:00',True),('2026-10-20T15:30:01+09:00',False),
    ('2026-10-20T23:00:00+09:00',False)])
def test_csv_regular_session_close_is_inclusive(tmp_path,stamp,allowed):
    item=row();item['first_at']=stamp;path=receipt_file(tmp_path,[item])
    if allowed:assert read_receipts(path,day=DAY,now=NOW)
    else:
        with pytest.raises(ValueError):read_receipts(path,day=DAY,now=NOW)


def test_special_session_uses_calendar_open_close_for_csv_and_stored_rows(tmp_path,monkeypatch):
    import json
    from app import morning_open_evidence_status as inventory
    tracker,_=new(tmp_path);first_entry(tracker)
    # Inject calendar facts, not a hard-coded normal-session substitute.
    class Stamp:
        def __init__(self,hour,minute=0):self.value=datetime(2026,10,20,hour,minute,tzinfo=KST)
        def to_pydatetime(self):return self.value
    fake=type('Calendar',(),dict(session_open=lambda self,day:Stamp(10),session_close=lambda self,day:Stamp(16,30)))()
    import exchange_calendars
    monkeypatch.setattr(exchange_calendars,'get_calendar',lambda *a,**k:fake)
    inventory.session_bounds.cache_clear()
    try:
        with tracker.db.connect() as db:
            entry=json.loads(db.execute('SELECT entry_json FROM paper_trades').fetchone()[0])
            entry['stamp']=entry['quote_stamp']='2026-10-20T16:30:00+09:00'
            db.execute('UPDATE paper_trades SET opened_at=?,entry_json=?',(entry['stamp'],json.dumps(entry)))
        item=row();item['first_at']='2026-10-20T16:30:00+09:00'
        assert compare_saved(tmp_path,'DEMO',DAY,receipt_file(tmp_path,[item]),now=NOW)['counts']['MATCHED_USER_CLAIM']==1
        item['first_at']='2026-10-20T09:50:00+09:00'
        with pytest.raises(ValueError):read_receipts(receipt_file(tmp_path,[item]),day=DAY,now=NOW)
    finally:inventory.session_bounds.cache_clear()


@pytest.mark.parametrize('cancel',['date','blank','file','busy'])
def test_gui_busy_and_cancel_do_not_schedule_or_compare(tmp_path,cancel):
    from types import SimpleNamespace
    from pathlib import Path
    import ast
    tree=ast.parse(Path('app/gui.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
    node=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='compare_morning_open_csv')
    calls=[]
    scope=dict(QInputDialog=SimpleNamespace(getText=lambda *a:(calls.append('date') or (' ' if cancel=='blank' else DAY),cancel!='date')),
        QFileDialog=SimpleNamespace(getOpenFileName=lambda *a:(calls.append('file') or '', 'CSV')),
        Path=Path)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'fake-ui-method','exec'),scope)
    window=SimpleNamespace(work=SimpleNamespace(isRunning=lambda:True) if cancel=='busy' else None,
        run_task=lambda *a:pytest.fail('scheduled after cancel'),statusBar=lambda:SimpleNamespace(showMessage=lambda msg:calls.append('busy')))
    scope[node.name](window)
    assert calls==(['busy'] if cancel=='busy' else ['date','file'] if cancel=='file' else ['date'])


def test_gui_selected_file_executes_readonly_callback_and_displays_actual_result(tmp_path):
    import ast
    from types import SimpleNamespace
    from pathlib import Path
    tracker,_=new(tmp_path);first_entry(tracker);path=receipt_file(tmp_path,[row()]);before=tracker.db.path.read_bytes()
    tree=ast.parse(Path('app/gui.py').read_text(encoding='utf-8'))
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MainWindow')
    node=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='compare_morning_open_csv')
    shown=[];scheduled=[]
    scope=dict(QInputDialog=SimpleNamespace(getText=lambda *a:(' '+DAY+' ',True)),
        QFileDialog=SimpleNamespace(getOpenFileName=lambda *a:(str(path),'CSV')),Path=Path,
        QMessageBox=SimpleNamespace(information=lambda *a:shown.append(a)),
        datetime=SimpleNamespace(now=lambda tz:NOW),KST=KST,
        current_mode=lambda cfg:SimpleNamespace(key='DEMO'),data_dir=lambda:tmp_path)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'fake-ui-method','exec'),scope)
    def run(fn,done,label):scheduled.append(label);done(fn())
    scope[node.name](SimpleNamespace(work=None,cfg={},run_task=run))
    assert len(scheduled)==len(shown)==1 and '자료 일치(사용자 주장) 1' in shown[0][2]
    assert '인증서가 아닙니다' in shown[0][2] and tracker.db.path.read_bytes()==before


@pytest.mark.parametrize('damage',['rows','header','future','holiday'])
def test_csv_constraints_leave_the_supplied_file_unchanged(tmp_path,damage):
    item=row();day=DAY;now=NOW
    if damage=='rows':path=receipt_file(tmp_path,[row(code=f'{100000+i:06d}') for i in range(1001)])
    else:
        if damage=='future':
            day=NOW.date().isoformat();item.update(day=day,first_at=day+'T12:00:00+09:00')
        if damage=='holiday':
            day='2026-10-24';now=datetime(2026,10,26,11,tzinfo=KST);item.update(day=day,first_at=day+'T09:00:00+09:00')
        path=receipt_file(tmp_path,[item])
        if damage=='header':path.write_text('unexpected,columns\n1,2\n',encoding='utf-8')
    before=path.read_bytes()
    with pytest.raises(ValueError,match='CSV'):read_receipts(path,day=day,now=now)
    assert path.read_bytes()==before
