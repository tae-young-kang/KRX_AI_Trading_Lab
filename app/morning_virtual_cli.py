"""Offline supplied-evidence replay into morning virtual paper records."""
import argparse
from datetime import datetime
import json
from pathlib import Path
from app.config import data_dir
from app.storage import Database
from app.morning_virtual import MorningVirtual,VenueTrade,record_path
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
        if trade.fetched_at.tzinfo is None or trade.fetched_at.utcoffset() is None or trade.fetched_at>now:
            raise ValueError('거래 근거 시각을 확인해 주세요.')
        parsed.append(trade)
    if any(a.fetched_at>b.fetched_at for a,b in zip(parsed,parsed[1:])):raise ValueError('거래 근거를 시간 순서로 입력해 주세요.')
    tracker=MorningVirtual(Database(path))
    tracker.prepare(day=plan['day'],mode=plan['mode'],origin=plan['origin'],targets=plan['targets'],
        settings=plan['settings'],total_won=plan['total_won'],now=frozen)
    results=[]
    for trade in parsed:
        result=(tracker.enter(trade,now=trade.fetched_at) if trade.kind=='FIRST_TRADE' else tracker.mark(trade,now=trade.fetched_at))
        results.append(result['state'])
    return results


def main():
    parser=argparse.ArgumentParser(description='공급된 첫 거래/시세 근거로 가상 추적을 재생합니다. 주문 없음.')
    parser.add_argument('plan_json');parser.add_argument('events_json')
    args=parser.parse_args()
    try:
        results=replay(data_dir(),json.loads(Path(args.plan_json).read_text(encoding='utf-8-sig')),
            json.loads(Path(args.events_json).read_text(encoding='utf-8-sig')),now=datetime.now(KST))
    except Exception:
        print('가상 추적 재생을 완료하지 못했어요. 입력 형식·시간·시장·이전 기록을 확인해 주세요. 일부 유효 기록은 남을 수 있어요.')
        return 1
    print(f'가상 추적 {len(results)}개 근거 처리. 실제 주문·시세 수집·메시지 발송은 하지 않았어요.')
    return 0


if __name__=='__main__':raise SystemExit(main())
