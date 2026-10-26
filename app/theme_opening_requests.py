"""Durable, explicitly unexecuted MOCK opening intents. No broker or quote IO."""
import hashlib
import json
import re
import secrets
from datetime import date, datetime
from decimal import Decimal

from app.budget_preview import equal_budget_preview
from app.morning_request import next_day
from app.morning_venue_policy import krx_open
from app.protection_plan import validate_policy
from app.theme_sources import valid_source
from app.trading_clock import KST

COMMANDS={'/테마1예약','/예약확인','/예약철회','/테마1예약목록','/테마1예약상태'}
NOTICE='미실행 요청 기록이에요. 개장 실행 연결 전이며 주문·예수금 확보·체결은 하지 않았어요.'
USAGE='예: /테마1예약 전체 100만원 또는 /테마1예약 종목명 10만원 익절 2 손절 3\n조회: /테마1예약목록 · 상세: /테마1예약상태 번호\n키움 모의투자 전용이며 실제 주문은 하지 않아요.'


def _json(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False,separators=(',',':'))


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _snapshot(history,mode,run_id,symbol,total,take,stop,now):
    """A single read transaction freezes and rechecks the original run, not latest cache."""
    validate_policy(take,stop)
    with history.connect() as source:
        source.execute('BEGIN')
        run=(source.execute('SELECT * FROM theme_runs WHERE mode=? ORDER BY id DESC LIMIT 1',(mode,)).fetchone()
             if run_id is None else source.execute('SELECT * FROM theme_runs WHERE mode=? AND id=?',(mode,run_id)).fetchone())
        if run is None or not valid_source(mode,run['source']):raise ValueError()
        day=date.fromisoformat(run['day'])
        if day.isoformat()!=run['day'] or day>now.astimezone(KST).date():raise ValueError()
        report=json.loads(run['report_json'])
        scope=report.get('search_scope')
        if not isinstance(scope,dict) or scope.get('include_etf') is not False:raise ValueError()
        ids=json.loads(run['alert_ids_json'])
        if (not isinstance(ids,list) or not ids or len(ids)>5000 or
                any(type(i) is not int or i<=0 for i in ids) or len(set(ids))!=len(ids)):raise ValueError()
        targets=[];evidence=[]
        for identifier in ids:
            row=source.execute('SELECT * FROM theme_alerts WHERE id=?',(identifier,)).fetchone()
            if row is None or any(row[k]!=run[k] for k in ('mode','source','day')):raise ValueError()
            payload=json.loads(row['result_json']);result=payload['result']
            if (payload.get('theme')!=1 or payload.get('version')!=1 or payload.get('mode')!=mode
                    or result.get('status')!='COMPLETE' or result.get('price_source')!=run['source']
                    or result.get('matched') is not True or result.get('detected_on')!=run['day']
                    or result.get('code')!=row['code'] or result.get('market')!=row['market']
                    or _hash(json.dumps(payload,sort_keys=True,ensure_ascii=False,allow_nan=False))!=row['fingerprint']):raise ValueError()
            price=result.get('reference_close')
            if type(price) not in (int,float) or not 0<price<=10**15 or int(price)!=price:raise ValueError()
            targets.append(dict(code=result['code'],name=result['name'],market=result['market'],reference_won=int(price)))
            evidence.append(dict(row))
        if symbol!='전체':
            targets=[item for item in targets if symbol in (item['name'],item['code'])]
            if len(targets)!=1:raise ValueError()
        preview=equal_budget_preview(total_won=total,targets=targets,available_won=None,fee_bps=2,buffer_bps=200)
        if any(item['quantity'] is None or item['quantity']<=0 for item in preview['targets']):raise ValueError()
        return dict(preview,run_id=run['id'],price_asof=run['day'],source=run['source'],mode=mode,
                    take_bps=take,stop_bps=stop,symbol=symbol,venue_policy='KRX_OPEN',
                    source_hash=_hash(_json(dict(run=dict(run),alerts=evidence))))


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS theme_opening_requests(
        id INTEGER PRIMARY KEY,scope TEXT NOT NULL,mode TEXT NOT NULL,session TEXT NOT NULL,
        token_hash TEXT NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,expires REAL NOT NULL,
        state TEXT NOT NULL,created_at TEXT NOT NULL,confirmed_at TEXT)''')


def _checked_payload(row):
    if _hash(row['payload'])!=row['digest']:raise ValueError('저장 요청 근거를 확인해야 해요.')
    return json.loads(row['payload'])


def _render(row,*,preview=False,page=1):
    payload=_checked_payload(row);targets=payload['targets'];pages=max(1,(len(targets)+4)//5)
    if page>pages:return f'마지막 페이지는 {pages}예요.'
    state={'PREVIEW':'확인 전','RECORDED_UNEXECUTED':'미실행 요청 저장됨','CANCELED':'철회됨','EXPIRED':'확인 만료'}[row['state']]
    lines=[f'[테마1 · 키움 모의투자 · {state}] #{row["id"]}',
           f'대상 {payload["target_day"]} KRX 개장 · 전 종목 KRX 시초가 시장가 계획',
           f'판정 #{payload["run_id"]} · 종가 기준일 {payload["price_asof"]}',
           f'총예산 {payload["total_won"]:,}원 · 종목당 {payload["allocated_each_won"]:,}원',
           f'익절 +{payload["take_bps"]/100:g}% · 손절 -{payload["stop_bps"]/100:g}%',
           f'{page}/{pages}페이지 · 저장 종가로 계산한 예상 수량이에요.']
    for item in targets[(page-1)*5:page*5]:
        lines.append(f'{item["name"]} ({item["code"]}) · 예상 {item["quantity"]:,}주 · 기준 {item["reference_won"]:,}원')
    lines.extend(['실제 시초가·수량·예수금은 미확인. 수수료 0.02%·가격 여유 2% 가정.',NOTICE,
                  '실행 연결 시 새 가격·계좌·기존 모의 주문 한도를 다시 검사해야 해요.'])
    if page<pages:lines.append(f'다음: /테마1예약상태 {row["id"]} {page+1}')
    if not preview:lines.append(f'철회: /예약철회 {row["id"]}')
    return '\n'.join(lines)


def handle(history,mode,text,*,database,scope,session,now,active):
    """Called only after private Telegram authentication, using its borrowed transaction."""
    if mode!='MOCK':return '개장 요청 기록은 키움 모의투자 모드에서만 가능해요. 가상 연습·실제 시세 보기에서는 저장하지 않아요.'
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:return '현재 시각을 확인하지 못했어요.'
    if not isinstance(text,str) or len(text)>300:return USAGE
    _schema(database)
    database.execute("UPDATE theme_opening_requests SET state='EXPIRED' WHERE scope=? AND mode=? AND state='PREVIEW' AND (session<>? OR expires<=?)",(scope,mode,session,now.timestamp()))
    if not active:
        database.execute("UPDATE theme_opening_requests SET state='CANCELED' WHERE scope=? AND mode=? AND state='PREVIEW'",(scope,mode))
    command=text.split(' ',1)[0]
    if command=='/테마1예약목록':
        rows=database.execute('SELECT * FROM theme_opening_requests WHERE scope=? AND mode=? ORDER BY id DESC LIMIT 10',(scope,mode)).fetchall()
        if not rows:return '저장된 개장 요청이 없어요. '+USAGE
        labels={'PREVIEW':'확인 전','RECORDED_UNEXECUTED':'미실행','CANCELED':'철회','EXPIRED':'확인 만료'}
        return '[테마1 · 모의 개장 요청 최근 10개]\n'+'\n'.join(f'#{r["id"]} · {_checked_payload(r)["target_day"]} · {labels[r["state"]]}' for r in rows)+'\n상세: /테마1예약상태 번호\n'+NOTICE
    match=re.fullmatch(r'/테마1예약상태 ([1-9][0-9]{0,12})(?: ([1-9][0-9]{0,5}))?',text)
    if match:
        row=database.execute('SELECT * FROM theme_opening_requests WHERE id=? AND scope=? AND mode=?',(int(match[1]),scope,mode)).fetchone()
        return _render(row,page=int(match[2] or 1)) if row else '이 소유자·모드의 요청이 없어요.'
    match=re.fullmatch(r'/예약철회 ([1-9][0-9]{0,12})',text)
    if match:
        row=database.execute('SELECT * FROM theme_opening_requests WHERE id=? AND scope=? AND mode=?',(int(match[1]),scope,mode)).fetchone()
        if not row:return '이 소유자·모드의 요청이 없어요.'
        database.execute("UPDATE theme_opening_requests SET state='CANCELED' WHERE id=? AND state IN ('PREVIEW','RECORDED_UNEXECUTED')",(row['id'],))
        return _render(database.execute('SELECT * FROM theme_opening_requests WHERE id=?',(row['id'],)).fetchone())
    if not active:return '새 개장 요청·확인을 중지했어요. PC에서 개인 수신을 다시 시작해 주세요.'
    match=re.fullmatch(r'/예약확인 ([A-Za-z0-9_-]{16,80})',text)
    if match:
        row=database.execute('SELECT * FROM theme_opening_requests WHERE scope=? AND mode=? AND session=? AND token_hash=?',(scope,mode,session,_hash(match[1]))).fetchone()
        if not row or row['state'] not in ('PREVIEW','RECORDED_UNEXECUTED'):return '유효한 확인이 없어요. 새 요청을 만들어 주세요.'
        if row['state']=='RECORDED_UNEXECUTED':return _render(row)
        try:
            payload=_checked_payload(row)
            if now>=krx_open(payload['target_day']) or next_day(now)!=payload['target_day']:raise ValueError()
            current=_snapshot(history,mode,payload['run_id'],payload['symbol'],payload['total_won'],payload['take_bps'],payload['stop_bps'],now)
            if current!={k:v for k,v in payload.items() if k!='target_day'}:raise ValueError()
        except Exception:
            database.execute("UPDATE theme_opening_requests SET state='EXPIRED' WHERE id=?",(row['id'],))
            return '원본 판정·ETF 제외 근거 또는 개장 기한이 달라 확인하지 않았어요. 다시 요청해 주세요.'
        duplicate=database.execute("SELECT * FROM theme_opening_requests WHERE scope=? AND mode=? AND digest=? AND state='RECORDED_UNEXECUTED'",(scope,mode,row['digest'])).fetchone()
        if duplicate:
            database.execute("UPDATE theme_opening_requests SET state='CANCELED' WHERE id=?",(row['id'],))
            return _render(duplicate)
        database.execute("UPDATE theme_opening_requests SET state='RECORDED_UNEXECUTED',confirmed_at=? WHERE id=?",(now.isoformat(),row['id']))
        return _render(database.execute('SELECT * FROM theme_opening_requests WHERE id=?',(row['id'],)).fetchone())
    match=re.fullmatch(r'/테마1예약 (.{1,100}?) ([1-9][0-9]{0,14})(원|만원)(?: 익절 ([0-9]{1,2}(?:\.[0-9]{1,2})?) 손절 ([0-9]{1,2}(?:\.[0-9]{1,2})?))?(?: 판정 ([1-9][0-9]{0,8}))?',text)
    if not match:return USAGE
    symbol,value,unit,take,stop,run_id=match.groups()
    try:
        payload=_snapshot(history,mode,int(run_id) if run_id else None,symbol,int(value)*(10000 if unit=='만원' else 1),int(Decimal(take)*100) if take else 200,int(Decimal(stop)*100) if stop else 300,now)
        payload['target_day']=next_day(now)
        expires=min(now.timestamp()+120,krx_open(payload['target_day']).timestamp())
    except Exception:return '저장 판정·ETF 제외 근거·종가·종목별 1주 예산·거래일을 확인해 주세요. 요청을 저장하지 않았어요.'
    digest=_hash(_json(payload))
    existing=database.execute("SELECT * FROM theme_opening_requests WHERE scope=? AND mode=? AND digest=? AND state='RECORDED_UNEXECUTED'",(scope,mode,digest)).fetchone()
    if existing:return _render(existing)
    database.execute("UPDATE theme_opening_requests SET state='CANCELED' WHERE scope=? AND mode=? AND state='PREVIEW'",(scope,mode))
    token=secrets.token_urlsafe(18)
    identifier=database.execute("INSERT INTO theme_opening_requests(scope,mode,session,token_hash,payload,digest,expires,state,created_at) VALUES(?,?,?,?,?,?,?,'PREVIEW',?)",(scope,mode,session,_hash(token),_json(payload),digest,expires,now.isoformat())).lastrowid
    row=database.execute('SELECT * FROM theme_opening_requests WHERE id=?',(identifier,)).fetchone()
    return _render(row,preview=True)+f'\n2분 이내·개장 전에 확인: /예약확인 {token}\n철회: /예약철회 {identifier}'
