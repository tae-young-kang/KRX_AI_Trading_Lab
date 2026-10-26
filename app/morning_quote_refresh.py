"""Read-only ka10079 raw 1-tick prices for existing virtual holdings only.

Contract: https://openapi.kiwoom.com/m/guide/apiguide/07/ka10079
First-page data supplies a fresh ordinary price, never proves first execution.
"""
from datetime import date,datetime
import hashlib
import json
from pathlib import Path
import re
from app.morning_quote_router import _safe_path,_plan_identity,route_trade,validate_trade
from app.morning_virtual import MorningVirtual,VenueTrade,aware,record_path
from app.storage import Database
from app.trading_clock import KST
from app.modes import current_mode


def targets(root,mode,*,now):
    if mode not in ('DEMO','MOCK','REAL_QUOTES') or not aware(now):raise ValueError('가상 시세 모드·시각을 확인해 주세요.')
    day=now.astimezone(KST).date().isoformat();root=Path(root)
    folder=record_path(root,mode,day).parent;_safe_path(root,folder)
    if folder.exists() and not folder.is_dir():raise ValueError('가상 기록 폴더를 확인해 주세요.')
    keys=set();failed=[]
    for path in sorted(folder.glob('*.db')):
        if not re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',path.stem):continue
        try:canonical=date.fromisoformat(path.stem).isoformat()
        except ValueError:continue
        if canonical!=path.stem or canonical>day:continue
        try:
            _safe_path(root,path);reader=Database(path,read_only=True)
            tracker=MorningVirtual(reader);plan=tracker.plan();_plan_identity(plan,mode,canonical)
            with reader.connect() as db:
                rows=db.execute('SELECT * FROM paper_trades WHERE closed_at IS NULL').fetchall()
            cohort=set()
            for holding in rows:
                target=next((r for r in plan['targets'] if r['code']==holding['code']),None)
                if not target or target['venue'] not in ('KRX','NXT'):raise ValueError()
                entry=json.loads(holding['entry_json']);opened=datetime.fromisoformat(holding['opened_at'])
                held_settings=json.loads(holding['strat_json'])
                if (holding['entry_key']!=tracker.key(plan,holding['code'])
                        or holding['source']!=('DEMO' if mode=='DEMO' else 'KIWOOM')
                        or not isinstance(held_settings,dict)
                        or held_settings.get('data_mode') not in ('DEMO','KIWOOM')
                        or held_settings.get('kiwoom_env') not in ('MOCK','REAL')
                        or current_mode(held_settings).key!=mode
                        or type(holding['remaining_qty']) is not int or holding['remaining_qty']<1
                        or not aware(opened) or opened.astimezone(KST).date().isoformat()!=canonical
                        or entry.get('venue')!=target['venue'] or entry.get('price_basis')!='VENUE_FIRST_TRADE'):
                    raise ValueError()
                cohort.add((holding['code'],target['venue']))
            keys.update(cohort)
        except Exception:failed.append(dict(day=canonical,state='FAILED'))
    return sorted(keys),failed


def evidence_from_response(data,*,mode,code,venue,fetched_at):
    requested=code+('_NX' if venue=='NXT' else '')
    if (not isinstance(data,dict) or type(data.get('return_code')) not in (str,int)
            or str(data['return_code'])!='0' or data.get('stk_cd')!=requested):raise ValueError('시세 응답의 종목·성공 여부를 확인해야 해요.')
    rows=data.get('stk_tic_chart_qry')
    if not isinstance(rows,list) or not 1<=len(rows)<=10000:raise ValueError('날짜가 있는 체결 자료가 없어요.')
    parsed=[]
    for row in rows:
        if not isinstance(row,dict):raise ValueError('체결 자료 형식을 확인해야 해요.')
        stamp=row.get('cntr_tm');price=row.get('cur_prc')
        if (not isinstance(stamp,str) or not re.fullmatch('[0-9]{14}',stamp)
                or not isinstance(price,str) or not re.fullmatch('[+-]?[0-9]{1,12}',price)):
            raise ValueError('체결 날짜·가격을 확인해야 해요.')
        at=datetime.strptime(stamp,'%Y%m%d%H%M%S').replace(tzinfo=KST)
        won=abs(int(price))
        if not 0<won<10**12 or at>fetched_at:raise ValueError('체결 시각·가격이 불명확해요.')
        parsed.append((at,won))
    latest=max(at for at,_ in parsed);prices={won for at,won in parsed if at==latest}
    if len(prices)!=1:raise ValueError('같은 최신 시각에 가격이 여러 개여서 반영하지 않았어요.')
    price=prices.pop()
    digest=hashlib.sha256(json.dumps(dict(source='KIWOOM_KA10079_RAW_1TICK',mode=mode,
        requested=requested,at=latest.isoformat(),price_won=price),sort_keys=True).encode()).hexdigest()
    evidence=VenueTrade(mode,code,venue,price,latest,fetched_at,'TRADE',digest)
    validate_trade(evidence,now=fetched_at)
    return evidence


def refresh(root,mode,client_factory,*,clock=lambda:datetime.now(KST),active=lambda:True):
    now=clock();keys,failures=targets(root,mode,now=now)
    result=dict(mode=mode,targets=len(keys),updated=0,closed=0,unconfirmed=0,failed=len(failures),canceled=False)
    if mode=='DEMO':result['unconfirmed']=len(keys);return result
    client=None
    try:
        for code,venue in keys:
            if not active():result['canceled']=True;break
            if mode=='MOCK' and venue=='NXT':result['unconfirmed']+=1;continue
            try:
                if client is None:client=client_factory()
                expected='https://mockapi.kiwoom.com' if mode=='MOCK' else 'https://api.kiwoom.com'
                if getattr(client,'url',None)!=expected:raise ValueError('시세 서버 모드가 달라요.')
                response=client.query('ka10079','/api/dostk/chart',dict(
                    stk_cd=code+('_NX' if venue=='NXT' else ''),tic_scope='1',upd_stkpc_tp='0'),
                    cancelled=lambda:not active())
                quote=evidence_from_response(response,mode=mode,code=code,venue=venue,fetched_at=clock())
                if not active():result['canceled']=True;break
                routed=route_trade(root,quote,now=clock())
                result['updated']+=sum(r['state'] in ('HOLD','VIRTUAL_CLOSED') for r in routed)
                result['closed']+=sum(r['state']=='VIRTUAL_CLOSED' for r in routed)
                result['failed']+=sum(r['state']=='FAILED' for r in routed)
            except ValueError:result['unconfirmed']+=1
            except Exception:
                if not active():result['canceled']=True;break
                result['failed']+=1
    finally:
        if client is not None:
            try:client.session.close()
            except Exception:pass
    return result


def summary(result):
    empty=('확인된 조회 대상이 없어요. 기록 확인 실패로 기존 보유 여부는 미확인이에요.\n' if result['failed'] else
           '조회할 기존 가상 보유가 없어요. 목록 고정만으로 진입하지 않아요.\n') if not result['targets'] else ''
    return (empty+
            f'조회 대상 {result["targets"]}개 · 반영 {result["updated"]}건 (가상 청산 {result["closed"]}건)\n'
            f'시세 미확인/미지원 {result["unconfirmed"]}개 · 처리 실패 {result["failed"]}건\n'
            '종목·시장별로 조회하며 여러 아침 목록에 각각 반영될 수 있어요.\n'
            '기존 가상 보유만 갱신했어요. 신규 매수·실제/모의계좌 주문·첫 거래 자동 수집은 하지 않아요.')
