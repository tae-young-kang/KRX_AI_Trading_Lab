"""Read-only recommendation vs user-reported purchases. Never broker proof."""
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3
from app.actual_trade_records import normalize
from app.morning_virtual import MorningVirtual,record_path,aware
from app.storage import Database
from app.trading_clock import KST


def checked(path,root):
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('기록 파일의 저장 위치를 확인해 주세요.')
    return path


def reported_buys(path,day,now):
    """No DB creation/repair; accept only normalized same-day imported fills."""
    if not path.is_file():return None
    groups={}
    try:
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
            db.execute('BEGIN')
            if db.execute('SELECT kind FROM actual_meta').fetchall()!=[('REAL_USER_REPORTED',)]:raise ValueError()
            rows=db.execute('SELECT id,payload FROM actual_fills WHERE day=? ORDER BY id',(day,)).fetchall()
        for identifier,payload in rows:
            raw=json.loads(payload)
            values=dict(raw)
            for field in ('quantity','price_won','realized_pnl_won'):
                values[field]='' if raw[field] is None else str(raw[field])
            fill=normalize(values,now)
            if fill['fill_id']!=identifier or fill['filled_at'][:10]!=day:raise ValueError()
            if fill['side']!='BUY':continue
            bucket=groups.setdefault(fill['code'],dict(quantity=0,gross_won=0,venues=set()))
            bucket['quantity']+=fill['quantity'];bucket['gross_won']+=fill['quantity']*fill['price_won']
            bucket['venues'].add(fill['venue'])
    except Exception:
        raise ValueError('실제 체결 입력을 확인하지 못했어요. 매수 여부를 추측하지 않았어요.') from None
    return groups


def report(root,mode,day,*,now,page=1):
    if (not aware(now) or not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day
            or day>now.astimezone(KST).date().isoformat() or type(page) is not int or not 1<=page<=999999):
        raise ValueError('오늘까지의 목록 날짜와 페이지를 확인해 주세요.')
    if mode not in ('DEMO','MOCK','REAL_QUOTES'):raise ValueError('추천 대조의 시세 모드를 확인해 주세요.')
    if mode!='REAL_QUOTES':return '추천 대조는 실제 시세 모드에서 확인해 주세요. 합성·키움 모의 추천과 실제 체결을 연결하지 않아요.'
    root=Path(root)
    path=checked(record_path(root,mode,day),root)
    if not path.is_file():return f'{day}의 고정된 아침 추천 목록이 없어요. /가상목록에서 목록 날짜를 확인해 주세요.'
    tracker=MorningVirtual(Database(path,read_only=True));plan=tracker.plan()
    if plan['mode']!=mode or plan['day']!=day:raise ValueError('추천 목록의 날짜·모드를 확인해 주세요.')
    targets=plan['targets'];pages=max(1,(len(targets)+4)//5)
    if page>pages:return f'추천 대조는 {pages}페이지까지 있어요.'
    from app.recommendation_classification import classifications
    labels=classifications(root,day,now)
    buys=reported_buys(checked(root/'actual_fill_records.db',root),day,now)
    with tracker.db.connect() as db:
        virtual={r['code']:r['count'] for r in db.execute('SELECT code,COUNT(*) AS count FROM paper_trades WHERE substr(opened_at,1,10)=? GROUP BY code',(day,))}
    count=sum(target['code'] in (buys or {}) for target in targets)
    lines=[f'[추천과 매수 입력 비교] 목록 {day} · {page}/{pages}페이지',
        f'추천 {len(targets)}종목 · 날짜·코드가 같은 실제 매수 입력 '+(f'{count}종목' if buys is not None else '미확인'),
        '실제 체결은 사용자 입력이며 증권사 자동 대조 전이에요.',
        '같은 날짜·코드여도 이 추천으로 산 거래인지는 아직 확인하지 못했어요.',
        '입력 없음은 미매수 증명이 아니에요. 가상 기록을 제외·변경하거나 손익을 합산하지 않아요.']
    for target in targets[(page-1)*5:page*5]:
        code=target['code'];buy=(buys or {}).get(code)
        lines.append(f'\n{" ".join(target["name"].split())} ({code})')
        lines.append('사용자 분류: '+labels.get(code,'미확인'))
        if buy:
            venues='/'.join('시장 미확인' if value=='UNKNOWN' else value for value in sorted(buy['venues']))
            lines.append(f'매수 입력 {buy["quantity"]:,}주 · {buy["gross_won"]:,}원 (비용 전) · {venues}')
        else:lines.append('실제 매수 여부 미확인 · '+('체결 입력 파일 없음' if buys is None else '같은 날짜·코드의 매수 입력 없음'))
        lines.append(f'별도 가상 진입 기록 {virtual.get(code,0)}건 · 실제 매수와 자동 연계/제외 전')
    lines.append('분류: /추천매수연결 날짜 코드 체결번호 · /추천미구매 날짜 코드 · /추천분류철회 날짜 코드')
    lines.append('사용자 분류는 증권사 검증/미구매 증명이 아니며 기존 손익을 바꾸지 않아요.')
    lines.extend([f'실제 입력 손익: /실제결산 {day}',f'별도 가상 손익: /가상결산 {day}'])
    if page>1:lines.append(f'이전: /추천대조 {day} {page-1}')
    if page<pages:lines.append(f'다음: /추천대조 {day} {page+1}')
    return '\n'.join(lines)
