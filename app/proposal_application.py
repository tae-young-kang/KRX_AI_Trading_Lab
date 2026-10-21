"""Owner-only proposal application with a durable settings/readback journal.

No orders. SQLite and settings are separate resources: unresolved transitions
are recorded, never presented as atomic completion or automatically retried.
"""
import json
from math import isfinite
import uuid
from datetime import datetime
from app.trade_review import SNAPSHOT_KEYS,FILTER_KEYS,EXIT_KEYS,COST_KEYS,settings_snapshot,review_conditions
from app.telegram_conditions import KEYS,validate_conditions
from app.exit_policy_commands import validate as validate_exit
from app.modes import current_mode
from app.trading_clock import KST


def snapshot(config):
    result=settings_snapshot(config)
    if (set(result)!=set(SNAPSHOT_KEYS) or result['data_mode'] not in ('DEMO','KIWOOM')
            or result['kiwoom_env'] not in ('MOCK','REAL') or type(result['strategy_version']) is not int
            or result['strategy_version']<1):raise ValueError('개선안의 기본 조건·모드를 확인해 주세요.')
    validate_conditions({key:result[key] for key in KEYS})
    validate_exit({key:result[key] for key in EXIT_KEYS})
    if (any(type(result[key]) not in (int,float) or not isfinite(result[key]) or not 0<=result[key]<100 for key in COST_KEYS)
            or result['paper_fee_pct']+result['paper_sell_tax_pct']>=100):raise ValueError('비용 가정을 확인해 주세요.')
    return result


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS proposal_applications(
        id TEXT PRIMARY KEY,proposal_id INTEGER,mode TEXT,state TEXT,base_json TEXT,target_json TEXT)''')


def fresh_candidate(rows,config,patch):
    minimum=max(20,int(config['min_strategy_trades']))
    for item in review_conditions(rows,minimum):
        old=item['settings']
        if (not item['eligible'] or any(old[key]!=config[key] for key in ('data_mode','kiwoom_env')+COST_KEYS)
                or any(item['exit_costs'][key]!=config[key] for key in COST_KEYS)):continue
        if {key:old[key] for key in FILTER_KEYS+EXIT_KEYS if old[key]!=config[key]}==patch:return True
    return False


def apply(database,config,proposal_id,save,read_saved):
    """Call only after the user confirms this proposal on the owning UI thread."""
    if type(proposal_id) is not int or proposal_id<=0:raise ValueError('개선안 번호를 확인해 주세요.')
    base=snapshot(config);mode=current_mode(base).key
    try:stored_base=snapshot(read_saved())
    except Exception:raise ValueError('저장된 기본 조건을 확인하지 못했어요. 조건을 저장하고 다시 비교해 주세요.') from None
    if stored_base!=base:raise ValueError('현재 조건의 저장 상태가 달라요. 저장한 조건을 확인하고 다시 비교해 주세요.')
    with database.connect() as db:
        initialize(db);db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM proposal_applications WHERE state IN ('APPLYING','UNKNOWN')").fetchone():
            raise ValueError('이전 개선안 적용 상태가 미확인이에요. 재시작 후 저장 조건을 확인해 주세요.')
        row=db.execute('SELECT p.state,p.patch_json,c.mode,c.base_json FROM proposals p JOIN proposal_context c ON c.proposal_id=p.id WHERE p.id=?',(proposal_id,)).fetchone()
        if not row or row['state']!='PENDING' or row['mode']!=mode or snapshot(json.loads(row['base_json']))!=base:
            raise ValueError('개선안 생성 당시 조건과 현재 조건이 달라요. /조건비교로 새로 비교해 주세요.')
        patch=json.loads(row['patch_json'])
        if not isinstance(patch,dict) or not patch or set(patch)-set(FILTER_KEYS+EXIT_KEYS):raise ValueError('허용된 개선안 변경값이 아니에요.')
        target=snapshot(dict(base,**patch,strategy_version=base['strategy_version']+1))
        rows=[dict(item) for item in db.execute('SELECT * FROM paper_trades ORDER BY id')]
        if not fresh_candidate(rows,config,patch):raise ValueError('최신 기록에서 비교 기준을 만족하지 않아요. 다시 비교해 주세요.')
        operation=uuid.uuid4().hex
        db.execute('INSERT INTO proposal_applications VALUES(?,?,?,?,?,?)',(operation,proposal_id,mode,'APPLYING',encoded(base),encoded(target)))
        db.execute("UPDATE proposals SET state='APPLYING' WHERE id=?",(proposal_id,))
    before=dict(config);config.update(target)
    try:save()
    except Exception:pass  # The persisted readback, not the exception alone, decides.
    try:stored=snapshot(read_saved())
    except Exception:stored=None
    state='APPLIED' if stored==target else 'FAILED' if stored==base else 'UNKNOWN'
    if state!='APPLIED':config.clear();config.update(before)
    try:
        with database.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE proposal_applications SET state=? WHERE id=?',(state,operation))
            db.execute('UPDATE proposals SET state=?,reviewed_at=? WHERE id=?',
                ('APPROVED' if state=='APPLIED' else 'PENDING' if state=='FAILED' else 'UNKNOWN',
                 None if state!='APPLIED' else datetime.now(KST).isoformat(),proposal_id))
    except Exception:
        raise ValueError('설정과 승인 기록의 대조가 미확인이에요. 다시 적용하지 말고 재시작 후 확인해 주세요.') from None
    if state=='FAILED':raise ValueError('설정이 저장되지 않아 적용하지 않았어요. 기존 조건을 유지했어요.')
    if state=='UNKNOWN':raise ValueError('저장 상태가 미확인이에요. 다시 적용하지 말고 재시작 후 확인해 주세요.')
    return target


def recover(database,read_saved):
    """Reconcile journal metadata against saved settings; never write settings."""
    counts=dict(APPLIED=0,FAILED=0,UNKNOWN=0)
    with database.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='proposal_applications'").fetchone():return counts
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute("SELECT * FROM proposal_applications WHERE state IN ('APPLYING','UNKNOWN')").fetchall()
        if not rows:return counts
        try:stored=snapshot(read_saved())
        except Exception:stored=None
        for row in rows:
            try:
                base=snapshot(json.loads(row['base_json']));target=snapshot(json.loads(row['target_json']))
                context=db.execute('SELECT p.state,p.patch_json,c.mode,c.base_json FROM proposals p JOIN proposal_context c ON c.proposal_id=p.id WHERE p.id=?',(row['proposal_id'],)).fetchone()
                if not context or context['mode']!=row['mode'] or current_mode(base).key!=row['mode'] or snapshot(json.loads(context['base_json']))!=base:raise ValueError()
                patch=json.loads(context['patch_json'])
                if not isinstance(patch,dict) or not patch or set(patch)-set(FILTER_KEYS+EXIT_KEYS):raise ValueError()
                if snapshot(dict(base,**patch,strategy_version=base['strategy_version']+1))!=target:raise ValueError()
                state='APPLIED' if stored==target else 'FAILED' if stored==base else 'UNKNOWN'
                existing=db.execute('SELECT state FROM proposals WHERE id=?',(row['proposal_id'],)).fetchone()
                if not existing or existing['state'] not in ('APPLYING','UNKNOWN'):state='UNKNOWN'
            except Exception:state='UNKNOWN'
            counts[state]+=1
            db.execute('UPDATE proposal_applications SET state=? WHERE id=?',(state,row['id']))
            if state!='UNKNOWN':
                db.execute('UPDATE proposals SET state=?,reviewed_at=? WHERE id=?',
                    ('APPROVED' if state=='APPLIED' else 'PENDING',datetime.now(KST).isoformat() if state=='APPLIED' else None,row['proposal_id']))
    return counts
