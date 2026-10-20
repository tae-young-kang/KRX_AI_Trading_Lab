"""Private read-only daily MOCK record report; no broker client or broker IO."""
from contextlib import ExitStack,closing
from datetime import date
import json
from pathlib import Path
import re
import sqlite3
from app.kiwoom_mock_chain import project_records
from app.trading_clock import KST


def report(root,scope,day,*,now,page=1):
    if (not isinstance(scope,str) or not scope or len(scope)>128 or type(page) is not int or not 1<=page<=999999
            or now.tzinfo is None or now.utcoffset() is None or date.fromisoformat(day).isoformat()!=day
            or day>now.astimezone(KST).date().isoformat()):raise ValueError('모의 결산의 날짜·개인 범위·페이지를 확인해 주세요.')
    header=f'[키움 모의 하루 기록] {day} · 저장 자료 기준\n실제 돈 거래·증권사 결제 완료 내역이 아니에요.\n'
    paths=[Path(root)/name for name in ('telegram_intents.db','dispatch.db','fills.db')]
    if not all(path.is_file() for path in paths):
        return header+'요청·접수·체결 기록이 아직 모두 없어요. 손익 미산출이며 0원 성과가 아니에요.'
    if any(path.is_symlink() for path in paths):raise ValueError('모의 기록의 링크 경로를 확인해 주세요.')
    with ExitStack() as stack:
        databases=[]
        for path in paths:
            db=stack.enter_context(closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)))
            db.row_factory=sqlite3.Row;db.execute('BEGIN');databases.append(db)
        intents,journal,ledger=databases
        owned={r['id']:dict(r) for r in intents.execute('SELECT * FROM intents WHERE scope=? AND day=?',(scope,day))}
        if not owned:return header+'이 개인 수신 범위에 저장된 요청이 없어요. 다른 사용자의 기록은 포함하지 않아요.'
        tag=journal.execute('SELECT account_tag FROM mock_account').fetchone()
        bound=ledger.execute('SELECT account_tag FROM fill_meta').fetchone()
        if tag is None or bound is None or tag[0]!=bound[0]:raise ValueError('모의 접수·체결 장부의 계좌 연결을 확인해 주세요.')
        receipts=[dict(r) for r in journal.execute('SELECT * FROM mock_dispatch')]
        snapshot_row=ledger.execute('SELECT payload FROM fill_snapshots WHERE day=?',(day,)).fetchone()
        snapshot=json.loads(snapshot_row[0]) if snapshot_row else None
        groups={}
        for key,row in owned.items():
            payload=json.loads(row['payload']);code=payload.get('code')
            if not isinstance(code,str) or not re.fullmatch('[A-Za-z0-9]{6}',code):raise ValueError('요청 종목 코드를 확인해 주세요.')
            if payload.get('action','NEW')!='NEW' or row['state']!='ACKNOWLEDGED':continue
            chain_rows=[r for r in receipts if r['root_key']==key]
            if not chain_rows or any(r['request_key'] not in owned for r in chain_rows):raise ValueError('개인 범위의 접수 계보를 확인해 주세요.')
            view=project_records(chain_rows,ledger,key,account_tag=tag[0])
            if view.day.isoformat()!=day:raise ValueError('모의 계보의 날짜가 달라요.')
            bucket=groups.setdefault(code,dict(code=code,buys=[],sells=[],numbers=set(),name=code,reason='',exit_reasons=set()))
            symbol=payload.get('requested_symbol',{})
            name=symbol.get('name') if isinstance(symbol,dict) else None
            if isinstance(name,str) and 1<=len(name)<=100 and name.isprintable():bucket['name']=name
            bucket['numbers'].update(view.order_nos)
            bucket['buys' if payload['side']=='BUY' else 'sells'].append(view)
            if payload['side']=='SELL' and view.filled_qty and payload.get('protection_reason') in ('TAKE_PROFIT','STOP_LOSS'):
                bucket['exit_reasons'].add(payload['protection_reason'])
        fills=[json.loads(r[0]) for r in ledger.execute('SELECT payload FROM fill_events WHERE day=?',(day,))]
        for code,b in groups.items():
            buys,sells=b['buys'],b['sells']
            b['buy']=sum(v.filled_value for v in buys);b['sell']=sum(v.filled_value for v in sells)
            buy_qty=sum(v.filled_qty for v in buys);sell_qty=sum(v.filled_qty for v in sells)
            b['pnl']=None
            if not snapshot:b['reason']='당일 보유·미체결 조회 근거 없음'
            elif any(f['code']==code and f['order_no'] not in b['numbers'] for f in fills):b['reason']='같은 종목의 다른 체결 대조 필요'
            elif any(json.loads(r['payload']).get('stk_cd')==code and r['state'] in ('UNKNOWN','SENDING')
                     and r['trade_day'] in (day,None) for r in receipts):b['reason']='미확인 전송 대조 필요'
            elif not buy_qty or not sell_qty:b['reason']='매수·매도 체결 한쪽이 없음'
            elif buy_qty!=sell_qty:b['reason']='이월/보유·분할매도 원가 대조 필요'
            elif any(r[0]==code and r[1]>0 for r in snapshot['holdings']) or any(r[1]==code and r[3]>0 for r in snapshot['unfilled']):
                b['reason']='보유 또는 미체결 수량이 남아 있음'
            elif any(v.cost_source!='BROKER_MOCK_STATEMENT' or v.net_cash_flow is None for v in buys+sells if v.filled_qty):
                b['reason']='주문별 비용 근거 미확인 · 합성 비용은 실제 모의 성적으로 제외'
            else:b['pnl']=sum(v.net_cash_flow for v in buys+sells if v.filled_qty)
        rows=sorted(groups.values(),key=lambda b:b['code'])
        pages=max(1,(len(rows)+4)//5)
        if page>pages:return f'모의 결산은 {pages}페이지까지 있어요.'
        known=[b for b in rows if b['pnl'] is not None]
        unknown=len(rows)-len(known)
        wins=sum(b['pnl']>0 for b in known);losses=sum(b['pnl']<0 for b in known);draws=len(known)-wins-losses
        pnl='미확인' if unknown or not known else f'{sum(b["pnl"] for b in known):+,}원'
        rate=f'{wins/len(known)*100:.1f}%' if known else '미산출'
        pending=sum(row['state'] in ('UNKNOWN','CLAIMED','WAITING','QUEUED') for row in owned.values())
        lines=[header+f'개인 요청 {len(owned)}건 · 대기/미확인 {pending}건 · 종목 {len(rows)}개 · {page}/{pages}페이지',
            f'조회된 매수금액 {sum(b["buy"] for b in rows):,}원 · 매도금액 {sum(b["sell"] for b in rows):,}원 (비용 차감 전)',
            f'오늘 사서 모두 판 종목의 손익 합계 {pnl} · 손익 미확인 {unknown}종목',
            f'손익이 확인된 종목: 승 {wins} / 패 {losses} / 본전 {draws} · 승률 {rate}',
            f'자동 조건 매도 체결 관측: 익절 {sum("TAKE_PROFIT" in b["exit_reasons"] for b in rows)}종목 · 손절 {sum("STOP_LOSS" in b["exit_reasons"] for b in rows)}종목 (조건별 집계)',
            '조건 구분과 비용 차감 후 승패는 다를 수 있어요.',
            '수수료·세금은 주문별로 명시한 비용 근거만 사용해요. 누락 비용을 0으로 채우지 않아요.',
            '부분 청산·이월 원가·다른 주문의 같은 종목 체결은 아직 손익 대조 대상이에요. 현재 잔고/결제와 다를 수 있어요.']
        if not rows:lines.append('접수 계보의 체결 근거가 없어요. 0원 수익으로 해석하지 마세요.')
        for b in rows[(page-1)*5:page*5]:
            amount=f'{b["pnl"]:+,}원' if b['pnl'] is not None else '미확인 · '+b['reason']
            lines.extend([f'{b["name"]} ({b["code"]})',f'매수 {b["buy"]:,}원 · 매도 {b["sell"]:,}원 · 손익 {amount}'])
        if page<pages:lines.append(f'다음: /모의결산 {day} {page+1}')
        return '\n'.join(lines)
