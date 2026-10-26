"""Read-only current user classifications over independently funded virtual cohorts."""
from datetime import date,datetime
from math import isfinite,isclose
from pathlib import Path
import re
from app.morning_quote_router import _safe_path,_plan_identity
from app.morning_virtual import MorningVirtual,record_path,aware
from app.morning_open_evidence_status import _inspect_rows
from app.morning_venue_policy import stored_policy,KRX_OPEN
from app.recommendation_classification import classifications,latest,source,digest
from app.practice_daily import summarize
from app.storage import Database
from app.trading_clock import KST


def report(root,mode,day,*,now,page=1):
    if mode!='REAL_QUOTES':return '추천 성적 분리는 실제 시세 보기 모드에서 확인해 주세요. 다른 모드와 섞지 않아요.'
    if not aware(now) or not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day or day>now.astimezone(KST).date().isoformat() or type(page) is not int or not 1<=page<=999999:
        raise ValueError('오늘까지의 결산 날짜·페이지를 확인해 주세요.')
    root=Path(root);folder=record_path(root,mode,day).parent;_safe_path(root,folder)
    paths=[]
    for path in sorted(folder.glob('*.db')):
        if not re.fullmatch('[0-9]{4}-[0-9]{2}-[0-9]{2}',path.stem):continue
        if date.fromisoformat(path.stem).isoformat()!=path.stem or path.stem>day:continue
        _safe_path(root,path);paths.append(path)
    if not paths:return f'[추천 성적 분리] 결산일 {day}\n저장된 아침 목록이 없어요. 성과는 미산출이며 0%가 아닙니다.'
    if page>len(paths):return f'추천 성적 분리는 {len(paths)}페이지까지 있어요.'
    path=paths[page-1];cohort=path.stem
    with Database(path,read_only=True).connect() as db:
        db.execute('BEGIN');plan=MorningVirtual.plan_from_connection(db);_plan_identity(plan,mode,cohort)
        if stored_policy(plan)!=KRX_OPEN:
            text=f'[추천 성적 분리] 결산일 {day} · {page}/{len(paths)}페이지\n목록 {cohort}: 이전 시장정책 목록이라 성과 분리는 미검증이에요. 숫자를 추정하지 않았어요.\n기존 가상 조회: /가상결산 {cohort}'
            if page>1:text+=f'\n이전: /추천성적분리 {day} {page-1}'
            if page<len(paths):text+=f'\n다음: /추천성적분리 {day} {page+1}'
            return text
        trades=[dict(row) for row in db.execute('SELECT * FROM paper_trades')]
        _inspect_rows(plan,trades,mode,cohort,now)
        for trade in trades:
            if (type(trade['qty']) is not int or trade['qty']<1 or type(trade['remaining_qty']) is not int or not 0<=trade['remaining_qty']<=trade['qty']
                    or any(type(trade[k]) not in (int,float) or not isfinite(trade[k]) for k in ('buy_fill','buy_fee'))
                    or trade['buy_fill']<=0 or trade['buy_fee']<0):raise ValueError('가상 매수 금액·수량을 확인해 주세요.')
            expected_fill=trade['buy_ref']*(1+plan['settings']['paper_slippage_pct']/100)
            expected_fee=expected_fill*trade['qty']*plan['settings']['paper_fee_pct']/100
            if (not isfinite(expected_fill) or not isfinite(expected_fee)
                    or not isclose(trade['buy_fill'],expected_fill,rel_tol=1e-12,abs_tol=1e-7)
                    or not isclose(trade['buy_fee'],expected_fee,rel_tol=1e-12,abs_tol=1e-7)):
                raise ValueError('추천 당시 가격·비용과 매수원가가 맞지 않아요.')
            if trade['closed_at'] is not None:
                closed=datetime.fromisoformat(trade['closed_at'])
                if not aware(closed) or closed>now or closed<datetime.fromisoformat(trade['opened_at']):
                    raise ValueError('가상 청산 시각을 확인해 주세요.')
        for sale in db.execute('SELECT sold_at FROM paper_exits'):
            stamp=datetime.fromisoformat(sale['sold_at'])
            if not aware(stamp) or stamp>now:raise ValueError('가상 매도 시각을 확인해 주세요.')
        history=latest(root,cohort);labels=classifications(root,cohort,now)
        for code,item in history.items():
            if labels.get(code) in ('실제입력 연결','미구매 사용자확인'):
                current=source(root,cohort,code,item['fill_id'],now)
                if current['plan']!=plan:raise ValueError('추천 원본이 바뀌어 성적을 합산하지 않았어요.')
                if digest(current)!=item['source_hash']:labels[code]='미확인'
        if latest(root,cohort)!=history:raise ValueError('분류가 바뀌었어요. 성적을 다시 조회해 주세요.')
        counts={name:0 for name in ('실제입력 연결','미구매 사용자확인','미확인')}
        unbought=set()
        for target in plan['targets']:
            label=labels.get(target['code'],'미확인')
            if label not in counts:label='미확인'
            counts[label]+=1
            if label=='미구매 사용자확인':unbought.add(target['code'])
        selected=[t for t in trades if t['code'] in unbought and datetime.fromisoformat(t['opened_at']).astimezone(KST).date().isoformat()<=day]
        result=summarize(db,day,trade_ids={t['id'] for t in selected},begin=False)
        held=missing=0
        for trade in selected:
            sold=db.execute('SELECT COALESCE(SUM(qty),0) FROM paper_exits WHERE trade_id=? AND substr(sold_at,1,10)<=?',(trade['id'],day)).fetchone()[0]
            if trade['qty']-sold<=0 or (trade['closed_at'] and trade['closed_at'][:10]<=day):continue
            held+=1
            mark=db.execute('SELECT price,at,evidence_hash FROM morning_marks WHERE code=? AND day=?',(trade['code'],day)).fetchone()
            if mark is None:missing+=1;continue
            stamp=datetime.fromisoformat(mark['at'])
            if (type(mark['price']) is not int or mark['price']<=0 or not aware(stamp) or stamp>now
                    or stamp.astimezone(KST).date().isoformat()!=day or not re.fullmatch('[a-f0-9]{64}',mark['evidence_hash'])):raise ValueError('당일 가상 가격 근거를 확인해 주세요.')
    net_rate=result['realized']/result['allocated_cost']*100 if result['sale_count'] and result['allocated_cost']>0 else None
    if net_rate is not None and not isfinite(net_rate):raise ValueError('수익률 계산 범위를 확인해 주세요.')
    rate=f'{net_rate:+.2f}%' if net_rate is not None else '미산출'
    pnl=f'{result["realized"]:+,.2f}원' if result['sale_count'] else '미산출'
    wins=f'{result["winrate"]:.1f}%' if result['winrate'] is not None else '미산출'
    lines=[f'[추천 성적 분리] 결산일 {day} · {page}/{len(paths)}페이지',f'아침 목록 {cohort} · 실제 시세로 계산한 가상 기록\n출처: 고정 추천/사용자 분류/앱 가상 매도 기록',
        '현재 사용자 분류 기준이에요. 나중에 분류를 바꾸면 과거 조회의 그룹도 바뀔 수 있어요.',
        '목록마다 독립 가상 예산이며 계좌 수익률이나 통합 포트폴리오가 아니에요.',
        f'실제입력 연결 {counts["실제입력 연결"]} · 미구매 사용자확인 {counts["미구매 사용자확인"]} · 미확인 {counts["미확인"]}종목',
        '[미구매 사용자확인 그룹 · 가상 계산만]',f'당일 매도 {result["sale_count"]}건 · 비용 포함 실현손익 {pnl}',
        f'오늘 판 주식의 매수비용(수수료 포함) {result["allocated_cost"]:,.2f}원 · 이 비용 대비 수익률 {rate}',
        f'오늘 전량 청산: 승 {result["wins"]} / 패 {result["losses"]} / 본전 {result["draws"]} · 승률 {wins}',
        f'미진입 {len(unbought)-len(selected)} · 열린 보유 {held} (당일 가격 없음 {missing}) · 완료 손익 미확인 {result["unknown"]} · 매도 근거 없는 완료 {result["legacy"]}건 제외',
        '분할매도 손익은 판 날에, 승패는 전량 청산일에 셉니다. 미진입/열린 보유/누락은 완료 승률에 넣지 않아요.',
        '사용자가 선택한 미구매 그룹의 승률이며 전체 추천 전략 승률이 아니에요.',
        '실제연결/미확인 그룹의 가상 손익을 합산하지 않았어요. BUY 연결만으로 실제 매도 손익을 귀속하지 않아요.',
        f'실제 입력 결산은 별도: /실제결산 {day}',
        '시장초과수익: 미산출 · 같은 시장/기간/기준의 검증된 벤치마크 자료가 없어요.',
        '공급 근거를 사용한 가상 계산이며 공식 첫 체결 인증·실제 계좌 체결이 아니에요.']
    if page>1:lines.append(f'이전: /추천성적분리 {day} {page-1}')
    if page<len(paths):lines.append(f'다음: /추천성적분리 {day} {page+1}')
    return '\n'.join(lines)
