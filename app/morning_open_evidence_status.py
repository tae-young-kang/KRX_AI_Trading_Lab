"""Read-only first-trade evidence inventory for frozen v2 KRX morning cohorts.

A paper-trade row or SHA256 is a stored software assertion, NOT independent
exchange feed certification or real Kiwoom/mock-account execution.
"""
from datetime import date, datetime
from contextlib import contextmanager
from math import isfinite
import json
from pathlib import Path
import re

from app.morning_quote_router import _plan_identity, _safe_path
from app.morning_virtual import MorningVirtual, aware, record_path
from app.morning_venue_policy import KRX_OPEN, stored_policy, session_bounds
from app.storage import Database
from app.trading_clock import KST


MODES = ('DEMO', 'MOCK', 'REAL_QUOTES')
EVIDENCE_HASH = re.compile('[a-f0-9]{64}')


def _day(value):
    if not isinstance(value, str):
        raise ValueError('고정 날짜를 확인해 주세요.')
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise ValueError('고정 날짜를 확인해 주세요.') from None
    if value != result.isoformat():
        raise ValueError('고정 날짜를 확인해 주세요.')
    return result


@contextmanager
def validated_snapshot(root,mode,day,*,now):
    """Validate and expose the exact same readonly transaction to consumers."""
    if mode not in MODES or not aware(now):raise ValueError('자료 모드·확인 시각을 확인해 주세요.')
    if _day(day)>now.astimezone(KST).date():raise ValueError('미래 고정 목록을 조회하지 않아요.')
    root=Path(root);path=record_path(root,mode,day);_safe_path(root,path)
    if not path.is_file():
        yield None
        return
    reader=Database(path,read_only=True)
    with reader.connect() as db:
        db.execute('BEGIN')
        plan=MorningVirtual.plan_from_connection(db)
        _plan_identity(plan,mode,day)
        if stored_policy(plan)!=KRX_OPEN:
            yield (dict(mode=mode,day=day,legacy=True,results=[]),plan,[])
            return
        rows=db.execute('SELECT code,market,source,entry_key,entry_json,strat_json,opened_at,closed_at,buy_ref FROM paper_trades').fetchall()
        info=_inspect_rows(plan,rows,mode,day,now)
        yield info,plan,rows


def inspect(root,mode,day,*,now):
    with validated_snapshot(root,mode,day,now=now) as snapshot:
        return snapshot[0] if snapshot is not None else None


def _inspect_rows(plan,rows,mode,day,now):
    opened_at,closed_at=session_bounds(day)
    targets = {t['code']: t for t in plan['targets']}
    by_code = {}
    for row in rows:
        code = row['code']
        if code not in targets or code in by_code:
            raise ValueError('고정 대상 외 가상 진입 기록이나 중복 기록이 있어요.')
        try:
            entry = json.loads(row['entry_json'])
            settings=json.loads(row['strat_json'])
            opened = datetime.fromisoformat(row['opened_at'])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError('저장한 첫 거래 근거의 형식을 확인하지 못했어요.') from None
        try:
            quote_stamp = datetime.fromisoformat(entry['quote_stamp'])
            fetched_stamp = datetime.fromisoformat(entry['stamp'])
        except (KeyError, TypeError, ValueError):
            raise ValueError('저장한 첫 거래 근거의 시각을 확인하지 못했어요.') from None
        if (not isinstance(entry, dict) or not aware(opened)
                or not aware(quote_stamp)
                or quote_stamp != opened
                or opened < opened_at or opened > closed_at
                or not aware(fetched_stamp) or not opened<=fetched_stamp<=now
                or (fetched_stamp-opened).total_seconds()>30
                or row['market']!=targets[code]['market']
                or settings!=plan['settings']
                or type(row['buy_ref']) not in (int,float) or not isfinite(row['buy_ref']) or row['buy_ref']<=0
                or type(entry.get('price')) not in (int,float) or not isfinite(entry['price'])
                or entry['price']!=row['buy_ref']
                or opened > now
                or opened.astimezone(KST).date().isoformat() != day
                or row['entry_key'] != MorningVirtual.key(plan, code)
                or row['source'] != ('DEMO' if mode == 'DEMO' else 'KIWOOM')
                or entry.get('source') != row['source']
                or entry.get('venue') != 'KRX'
                or entry.get('price_basis') != 'VENUE_FIRST_TRADE'
                or not isinstance(entry.get('evidence_hash'), str)
                or not EVIDENCE_HASH.fullmatch(entry['evidence_hash'])):
            raise ValueError('저장한 가상 진입 근거의 모드·날짜·시장·해시를 확인하지 못했어요.')
        by_code[code] = 'VIRTUAL_CLOSED' if row['closed_at'] is not None else 'VIRTUAL_OPEN'
    results = [
        dict(code=t['code'], name=t['name'],
             state=by_code.get(t['code'], 'NO_STORED_FIRST_EVIDENCE'))
        for t in plan['targets']
    ]
    return {'mode': mode, 'day': day, 'legacy': False, 'results': results,
            'stored': len(by_code), 'missing': len(targets) - len(by_code),
            'virtual_open': sum(r['state'] == 'VIRTUAL_OPEN' for r in results),
            'virtual_closed': sum(r['state'] == 'VIRTUAL_CLOSED' for r in results)}


def render(info, *, page=1):
    if type(page) is not int or not 1 <= page <= 999999:
        raise ValueError('목록 페이지를 확인해 주세요.')
    if info is None:
        return '해당 날짜에 고정된 추천 목록이 없어요. 첫 거래가 있었는지 추측하지 않아요.'
    if info['legacy']:
        return '이 목록은 이전 NXT/KRX 혼합 정책이에요. 새 KRX 시초 근거 상태로 소급 판정하지 않아요.'
    results = info['results']
    pages = max(1, (len(results) + 4) // 5)
    if page > pages:
        return f'시초 근거 확인은 {pages}페이지까지 있어요.'
    lines = [
        f"[가상 시초 근거 상태] {info['day']} · {info['mode']} · {page}/{pages}페이지",
        f"고정 추천 {len(results)}종목 · 소프트웨어 근거 첨부 {info['stored']}종목 · 미첨부 {info['missing']}종목",
        f"가상 보유 {info['virtual_open']} · 가상 청산 {info['virtual_closed']} (실제 계좌 체결 아님)",
        '첨부 근거의 해시·기록만 확인했어요. 거래소 공식 첫 체결 인증이나 가격의 진위 검증은 아니에요.',
    ]
    labels = {'VIRTUAL_OPEN': '가상 진입 근거 기록 있음 · 가상 보유',
              'VIRTUAL_CLOSED': '가상 진입 근거 기록 있음 · 가상 청산',
              'NO_STORED_FIRST_EVIDENCE': '첫 거래 근거 미첨부 · 진입 미확인'}
    for item in results[(page-1)*5:page*5]:
        lines.append(f"{item['name']} ({item['code']}): {labels[item['state']]}")
    lines.append('시세 재조회·신규 진입·가상/실제 매수·매도·장부 수정은 하지 않았어요.')
    if page > 1:
        lines.append(f"이전: /가상시초근거 {info['day']} {page-1}")
    if page < pages:
        lines.append(f"다음: /가상시초근거 {info['day']} {page+1}")
    return '\n'.join(lines)


def report_saved(root, mode, day, *, now, page=1):
    return render(inspect(root, mode, day, now=now), page=page)
