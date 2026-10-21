"""Offline supplied-evidence replay into morning virtual paper records."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from app.config import data_dir
from app.storage import Database
from app.morning_virtual import MorningVirtual,VenueTrade,record_path
from app.morning_quote_router import route_trade,validate_trade
from app.trading_clock import KST


def replay(root,plan,events,*,now):
    # Validate shape/time first; later per-event failures can leave earlier
    # valid paper records. No input can select an arbitrary database path.
    path=record_path(root,plan['mode'],plan['day'])
    frozen=datetime.fromisoformat(plan['frozen_at'])
    if frozen.tzinfo is None or frozen.utcoffset() is None or frozen>now:raise ValueError('추천 고정 시각을 확인해 주세요.')
    if not isinstance(events,list) or len(events)>100000:raise ValueError('거래 근거 목록을 확인해 주세요.')
    parsed=[]
    for event in events:
        item=dict(event)
        item['at']=datetime.fromisoformat(item['at']);item['fetched_at']=datetime.fromisoformat(item['fetched_at'])
        trade=VenueTrade(**item)
        if trade.mode!=plan['mode']:raise ValueError('선택한 아침 목록과 거래 근거의 시세 모드가 달라요.')
        if trade.fetched_at.tzinfo is None or trade.fetched_at.utcoffset() is None or trade.fetched_at>now:
            raise ValueError('거래 근거 시각을 확인해 주세요.')
        if trade.kind!='FIRST_TRADE':
            validate_trade(trade,now=trade.fetched_at)
            selected=next((row for row in plan['targets'] if isinstance(row,dict) and row.get('code')==trade.code),None)
            eligible=selected.get('nxt_eligible') if selected else None
            venue='NXT' if eligible is True else 'KRX' if eligible is False else 'UNKNOWN'
            if selected is None or venue!=trade.venue:
                raise ValueError('선택한 아침 목록과 거래 근거의 종목·거래 시장이 달라요.')
        parsed.append(trade)
    if any(a.fetched_at>b.fetched_at for a,b in zip(parsed,parsed[1:])):raise ValueError('거래 근거를 시간 순서로 입력해 주세요.')
    tracker=MorningVirtual(Database(path))
    tracker.prepare(day=plan['day'],mode=plan['mode'],origin=plan['origin'],targets=plan['targets'],
        settings=plan['settings'],total_won=plan['total_won'],now=frozen)
    results=[]
    for trade in parsed:
        if trade.kind=='FIRST_TRADE':result=tracker.enter(trade,now=trade.fetched_at)
        else:
            # A selected-plan mismatch must not update other carryover cohorts.
            tracker._check(trade,trade.fetched_at,opening=False)
            routed=route_trade(root,trade,now=trade.fetched_at)
            if any(item['state']=='FAILED' for item in routed):
                raise ValueError('일부 아침 목록의 가상 시세 반영이 미확인이에요. 성공한 목록은 이미 반영됐을 수 있어요.')
            result=next((item for item in routed if item['day']==plan['day']),None)
            if result is None or result['state'].startswith('SKIPPED_'):
                raise ValueError('선택한 아침 목록과 시세 종목·시장이 달라요.')
        results.append(result['state'])
    return results


def main():
    parser=argparse.ArgumentParser(description='과거 공급 근거로 가상 추적을 재생합니다. 일반 시세는 이월 보유도 갱신. 실시간 공급/주문 없음.')
    parser.add_argument('plan_json');parser.add_argument('events_json')
    args=parser.parse_args()
    try:
        results=replay(data_dir(),json.loads(Path(args.plan_json).read_text(encoding='utf-8-sig')),
            json.loads(Path(args.events_json).read_text(encoding='utf-8-sig')),now=datetime.now(KST))
    except Exception:
        print('가상 추적 재생을 완료하지 못했어요. 입력 형식·시간·시장·이전 기록을 확인해 주세요. 일부 유효 기록은 남을 수 있어요.')
        return 1
    print(f'과거 가상 추적 {len(results)}개 근거 재생. 일반 시세는 같은 모드의 이월 보유에도 반영했어요. 실제 주문·실시간 시세 수집·메시지 발송은 하지 않았어요.')
    return 0


if __name__=='__main__':raise SystemExit(main())
