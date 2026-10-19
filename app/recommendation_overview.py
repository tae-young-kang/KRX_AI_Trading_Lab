"""Descriptive price observations only; no ranking, forecasts or auto promotion."""
from collections import Counter
from datetime import datetime
import hashlib
import json
from statistics import mean
from app.trade_review import FILTER_KEYS, condition_label
from app.trading_clock import KST


SOURCES = {'DEMO':'가상 자료','KRX':'KRX 자료','CSV_USER':'사용자 제공','NONE':'자료 없음'}
CALENDARS = {'DEMO':'가상 거래일','USER_CONFIRMED':'사용자 확인 거래일','OFFICIAL':'공식 거래일','NONE':'거래일 미확인'}


def change_label(value):
    return f'{value:+.3g}%' if abs(value)>=1_000_000 else f'{value:+.2f}%'


def price_label(value):
    if 0<abs(value)<1:
        return f'{value:.6g}원'
    return f'{value:.3g}원' if abs(value)>=1_000_000_000_000 else f'{value:,.0f}원'


def groups(db,mode):
    if mode not in ('DEMO','MOCK','REAL_QUOTES'):
        raise ValueError('추천 모드를 확인해 주세요.')
    def kst_day(value):
        try:
            stamp=datetime.fromisoformat(value)
            return stamp.astimezone(KST).date().isoformat() if stamp.tzinfo is not None else None
        except (TypeError,ValueError,OverflowError):
            return None
    def signature(value):
        cfg=json.loads(value)
        return json.dumps({key:cfg[key] for key in FILTER_KEYS if key in cfg},sort_keys=True)
    db.create_function('overview_kst_day',1,kst_day,deterministic=True)
    db.create_function('overview_signature',1,signature,deterministic=True)
    unverified_time=db.execute('''SELECT COUNT(*) FROM recommendation_batches b
        JOIN recommendations r ON r.scan_id=b.scan_id
        WHERE b.mode=? AND overview_kst_day(b.created_at) IS NULL''',(mode,)).fetchone()[0]
    # Select first recommendations over full history BEFORE applying the display window.
    rows = db.execute('''WITH ranked AS (
        SELECT b.scan_id,b.created_at,b.conditions_json,r.code,r.market,r.rank,
        ROW_NUMBER() OVER (PARTITION BY overview_kst_day(b.created_at),
            overview_signature(b.conditions_json),r.code,r.market ORDER BY b.scan_id) AS first_number
        FROM recommendation_batches b JOIN recommendations r ON r.scan_id=b.scan_id
        WHERE b.mode=? AND overview_kst_day(b.created_at) IS NOT NULL
        ), selected AS (SELECT * FROM ranked WHERE first_number=1
            ORDER BY scan_id DESC,rank LIMIT 3000)
        SELECT b.scan_id,b.created_at,b.conditions_json,b.code,b.market,
        v.price_source,v.calendar_source,v.results_json FROM selected b
        LEFT JOIN recommendation_reviews v ON v.id=(
          SELECT MAX(id) FROM recommendation_reviews WHERE scan_id=b.scan_id AND code=b.code)
        ORDER BY b.scan_id DESC,b.rank''',(mode,)).fetchall()
    seen = set()
    population_by_day = Counter()
    selected_days_by_condition = {}
    grouped = {}
    for row in reversed(rows):
        stamp = datetime.fromisoformat(row['created_at'])
        if stamp.tzinfo is None:
            unverified_time += 1
            continue
        cfg = json.loads(row['conditions_json'])
        condition_signature = json.dumps({key:cfg[key] for key in FILTER_KEYS if key in cfg},sort_keys=True)
        identity = (stamp.astimezone(KST).date(),row['code'],row['market'],condition_signature)
        if identity in seen:
            continue
        seen.add(identity)
        population_by_day[(identity[0],condition_signature)] += 1
        selected_days_by_condition.setdefault(condition_signature,set()).add(identity[0])
        source = row['price_source'] or 'NONE'
        calendar = row['calendar_source'] or 'NONE'
        key = (condition_signature,source,calendar)
        if key not in grouped:
            grouped[key] = dict(condition_id=hashlib.sha256(condition_signature.encode()).hexdigest()[:8],
                label=condition_label(cfg),source=source,calendar=calendar,count=0,last_scan=0,
                horizons={h:dict(observed=0,up=0,down=0,flat=0,waiting=0,missing=0,unreviewed=0,values=[],by_day={}) for h in (1,3,5)})
        group = grouped[key]
        group['count'] += 1
        group['last_scan'] = max(group['last_scan'],row['scan_id'])
        results = {r['horizon']:r for r in json.loads(row['results_json'])} if row['results_json'] else {}
        for horizon,bucket in group['horizons'].items():
            result = results.get(horizon)
            if not result:
                bucket['unreviewed'] += 1
            elif result['state']=='OBSERVED':
                change = result['change_pct']
                bucket['observed'] += 1
                bucket['up' if change>1e-9 else 'down' if change< -1e-9 else 'flat'] += 1
                bucket['values'].append(change)
                bucket['by_day'].setdefault(identity[0],[]).append(change)
            elif result['state']=='NOT_DUE':
                bucket['waiting'] += 1
            else:
                bucket['missing'] += 1
    for (signature,source,calendar),group in grouped.items():
        for bucket in group['horizons'].values():
            bucket['selected_days'] = len(selected_days_by_condition[signature])
            values = bucket.pop('values')
            bucket['mean'] = mean(values) if values else None
            by_day = bucket.pop('by_day')
            bucket['observed_days'] = len(by_day)
            # Each recommendation date gets equal weight, regardless of how
            # many stocks were recommended on that date.
            bucket['daily_mean'] = mean(mean(changes) for changes in by_day.values()) if by_day else None
            # Complete means every selected pick for this condition/day was
            # observed, even if another pick sits in a different source group.
            # Completeness is limited to the recent 3,000 selected records.
            complete = [mean(changes) for day,changes in by_day.items()
                        if len(changes) == population_by_day[(day,signature)]]
            bucket['complete_days'] = len(complete)
            bucket['complete_daily_mean'] = mean(complete) if complete else None
    return sorted(grouped.values(),key=lambda g:g['last_scan'],reverse=True),unverified_time


def summary(db,mode):
    data,unverified = groups(db,mode)
    label = {'DEMO':'가상 연습','MOCK':'키움 모의 시세','REAL_QUOTES':'실제 시세'}[mode]
    lines = [f'[{label} · 추천 관측 요약]',
             '전체 이력의 날짜/조건/종목별 첫 추천 중 최근 3,000개 계산',
             '조건·가격 출처·거래일 출처별로 분리 · 최근 조건 묶음 3개 표시']
    if not data:
        lines.append('집계할 추천 기록이 없어요.')
    for group in data[:3]:
        lines.append(f"조건 {group['condition_id']} · {group['count']}개 종목/날짜 · {group['label'][:140]}")
        lines.append(SOURCES[group['source']]+' · '+CALENDARS[group['calendar']])
        for horizon,bucket in group['horizons'].items():
            mean_label = '평균 미확인' if bucket['mean'] is None else '평균 변화 '+change_label(bucket['mean'])
            short = ' · 관측 20개 미만' if bucket['observed']<20 else ''
            daily = ('날짜별 평균 미확인' if bucket['daily_mean'] is None else
                     '날짜별 균등 평균 '+change_label(bucket['daily_mean']))
            if bucket['observed'] and bucket['observed_days']<10:
                short += ' · 평가일 10일 미만'
            lines.append(f"{horizon}거래일: 관측 {bucket['observed']}건/{bucket['observed_days']}일 · 상승 {bucket['up']}/하락 {bucket['down']}/동일 {bucket['flat']} · {mean_label} · {daily}{short}")
            complete = ('완전 관측일 평균 미확인' if bucket['complete_daily_mean'] is None else
                        '완전 관측일 평균 '+change_label(bucket['complete_daily_mean']))
            lines.append(f"대기 {bucket['waiting']} · 자료 부족 {bucket['missing']} · 미평가 {bucket['unreviewed']}")
            if bucket['observed_days']:
                lines.append(f"집계 내 완전 관측일 {bucket['complete_days']}/{bucket['selected_days']}일 · {complete}")
    if len(data)>3:
        lines.append(f'나머지 {len(data)-3}개 묶음은 표시를 생략했어요.')
    if unverified:
        lines.append(f'시각 확인이 필요한 {unverified}개 기록은 집계하지 않았어요.')
    lines.append('날짜별 균등 평균은 일부 종목만 관측된 날짜도 포함합니다. '
                 '완전 관측일의 분모는 모든 선택된 추천일이며 집계 밖 전체 시장 완전성은 아니에요. '
                 '가격 변화는 비용·배당·분할 미반영이며 실제 순수익·기대수익률이 아니에요. '
                 '겹친 종목/보유 기간은 독립 표본이 아니며 조건을 자동 변경하지 않아요.')
    return '\n'.join(lines)
