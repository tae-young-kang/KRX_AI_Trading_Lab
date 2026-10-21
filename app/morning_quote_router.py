"""Supplied venue trades -> existing morning virtual holdings, never new entry.

No feed, broker, order, or notification client. This accepts evidence supplied by
another component; a hash is integrity context, not certification of a feed.
Each cohort is independent. Failure after a mark may leave that valid mark;
this module does not claim cross-database or mark/exit atomicity.
"""
from datetime import date,datetime,time
from math import isfinite
from pathlib import Path
import json
import re
from app.morning_virtual import MorningVirtual,VenueTrade,aware,record_path
from app.storage import Database
from app.theme_calendar import recent_sessions
from app.trading_clock import KST,TradingCalendar
from app.modes import current_mode
from app.morning_venue_policy import KRX_OPEN,stored_policy,deadline


def validate_trade(evidence,*,now):
    """Reject invalid common input before touching any cohort database."""
    if (type(evidence) is not VenueTrade or evidence.kind!='TRADE'
            or evidence.mode not in ('DEMO','MOCK','REAL_QUOTES')
            or not isinstance(evidence.code,str) or not re.fullmatch('[A-Za-z0-9]{6}',evidence.code)
            or evidence.venue not in ('NXT','KRX')
            or type(evidence.price_won) is not int or not 0<evidence.price_won<10**12
            or not isinstance(evidence.evidence_hash,str) or not re.fullmatch('[a-f0-9]{64}',evidence.evidence_hash)
            or not aware(now) or not aware(evidence.at) or not aware(evidence.fetched_at)
            or not evidence.at<=evidence.fetched_at<=now
            or not 0<=(now-evidence.at).total_seconds()<=30
            or evidence.at.astimezone(KST).date()!=now.astimezone(KST).date()):
        raise ValueError('당일 일반 체결의 모드·종목·시장·시각·가격·근거를 확인해 주세요. 첫 거래는 이 경로로 진입하지 않아요.')
    day=evidence.at.astimezone(KST).date()
    if day.isoformat() not in recent_sessions(day) or not TradingCalendar(open_dates={day}).is_open(evidence.at,evidence.venue):
        raise ValueError('확인된 거래일의 NXT/KRX 거래 구간이 아니에요.')
    return day.isoformat()


def _safe_path(root,path):
    # Check before resolve: resolving first would conceal a link in the path.
    root,path=Path(root).absolute(),Path(path).absolute()
    if not path.is_relative_to(root):raise ValueError('가상 기록 범위 밖이에요.')
    for part in (path,*path.parents):
        if part.is_symlink() or getattr(part,'is_junction',lambda:False)():
            raise ValueError('가상 기록에 링크 경로를 사용할 수 없어요.')


def _plan_identity(plan,mode,day):
    if (not isinstance(plan,dict) or plan.get('kind')!='VIRTUAL_ONLY'
            or plan.get('mode')!=mode or plan.get('day')!=day
            or not isinstance(plan.get('origin'),str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}',plan['origin'])
            or type(plan.get('total_won')) is not int or not 1<=plan['total_won']<=10**15
            or not isinstance(plan.get('targets'),list) or not plan['targets']):
        raise ValueError('목록 신원을 확인해 주세요.')
    policy=stored_policy(plan)
    frozen=datetime.fromisoformat(plan['frozen_at'])
    if not aware(frozen) or frozen>=deadline(day,policy):
        raise ValueError('목록 고정 시각을 확인해 주세요.')
    targets=plan['targets']
    for row in targets:
        if (not isinstance(row,dict) or not isinstance(row.get('code'),str)
                or not re.fullmatch('[A-Za-z0-9]{6}',row['code'])
                or row.get('market') not in ('KOSPI','KOSDAQ') or row.get('venue') not in ('NXT','KRX','UNKNOWN')):
            raise ValueError('고정 종목을 확인해 주세요.')
        if policy==KRX_OPEN and (row['venue']!='KRX' or 'nxt_eligible' not in row
                or row['nxt_eligible'] is not None and type(row['nxt_eligible']) is not bool):
            raise ValueError('KRX 고정 시장과 NXT 자격 자료를 확인해 주세요.')
    if len({row['code'] for row in targets})!=len(targets):raise ValueError('고정 종목이 중복돼요.')
    cfg=plan['settings']
    if (cfg.get('data_mode') not in ('DEMO','KIWOOM') or cfg.get('kiwoom_env') not in ('MOCK','REAL')
            or current_mode(cfg).key!=mode):raise ValueError('목록 설정 모드가 달라요.')
    for key in ('exit_take_pct','exit_stop_pct','paper_fee_pct','paper_sell_tax_pct','paper_slippage_pct'):
        if type(cfg.get(key)) not in (int,float) or not isfinite(cfg[key]) or not 0<=cfg[key]<100:
            raise ValueError('가상 청산 설정을 확인해 주세요.')
    if (min(cfg['exit_take_pct'],cfg['exit_stop_pct'])<=0 or cfg['paper_fee_pct']+cfg['paper_sell_tax_pct']>=100
            or type(cfg.get('exit_max_days')) is not int or cfg['exit_max_days']<1):
        raise ValueError('가상 청산 설정을 확인해 주세요.')


def route_trade(root,evidence,*,now):
    """Return one explicit result per existing past/today cohort in this mode.

    Nonmatching and unheld records are read only. One damaged cohort produces
    FAILED without blocking other independent holdings. No file is created for
    a missing cohort, another mode, or an unheld target.
    """
    day=validate_trade(evidence,now=now)
    root=Path(root)
    folder=record_path(root,evidence.mode,day).parent
    _safe_path(root,folder)
    if folder.exists() and not folder.is_dir():raise ValueError('가상 기록 폴더를 확인해 주세요.')
    results=[]
    for path in sorted(folder.glob('*.db')):
        if not re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',path.stem):continue
        try:canonical=date.fromisoformat(path.stem).isoformat()
        except ValueError:continue
        if canonical!=path.stem or canonical>day:continue
        row=dict(day=canonical,code=evidence.code,mode=evidence.mode,venue=evidence.venue)
        try:
            _safe_path(root,path)
            reader=Database(path,read_only=True)
            tracker=MorningVirtual(reader);plan=tracker.plan()
            _plan_identity(plan,evidence.mode,canonical)
            target=next((r for r in plan['targets'] if r['code']==evidence.code),None)
            if target is None:row['state']='SKIPPED_NOT_TARGET'
            elif target['venue']!=evidence.venue:row['state']='SKIPPED_VENUE'
            else:
                key=tracker.key(plan,evidence.code)
                with reader.connect() as db:
                    holding=db.execute('SELECT code,source,opened_at,entry_json,closed_at,pnl FROM paper_trades WHERE entry_key=?',(key,)).fetchone()
                if holding is None:row['state']='WAITING_FIRST_TRADE'
                else:
                    entry=json.loads(holding['entry_json']);opened=datetime.fromisoformat(holding['opened_at'])
                    if (holding['code']!=evidence.code or holding['source']!=('DEMO' if evidence.mode=='DEMO' else 'KIWOOM')
                            or not aware(opened) or opened.astimezone(KST).date().isoformat()!=canonical
                            or entry.get('venue')!=target['venue'] or entry.get('price_basis')!='VENUE_FIRST_TRADE'):
                        raise ValueError('보유 신원이 달라요.')
                    if holding['closed_at']:row.update(state='ALREADY_CLOSED',pnl=holding['pnl'])
                    else:
                        # Revalidate the common input/path immediately before writes.
                        validate_trade(evidence,now=now);_safe_path(root,path)
                        writer=MorningVirtual(Database(path))
                        if writer.plan()!=plan:raise ValueError('고정 목록이 바뀌었어요.')
                        row.update(writer.mark(evidence,now=now))
        except Exception:
            row.update(state='FAILED',message='이 목록의 가상 시세 반영을 확인하지 못했어요. 일부 유효 관측은 남을 수 있어요.')
        results.append(row)
    return results
