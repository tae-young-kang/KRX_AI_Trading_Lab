"""Stored general/Theme1 selection -> frozen virtual cohort. No quote IO."""
from datetime import datetime,timedelta,time
from decimal import Decimal
import json
import re
from app.storage import Database
from app.trade_review import settings_snapshot,EXIT_KEYS,COST_KEYS
from app.trading_clock import KST
from app.morning_virtual import MorningVirtual,record_path
from app.nxt_evidence import latest_for_day
from app.theme_sources import valid_source


def next_day(now):
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('현재 시각의 시간대를 확인해 주세요.')
    local=now.astimezone(KST)
    try:
        import exchange_calendars
        calendar=exchange_calendars.get_calendar('XKRX',start=local.date().isoformat(),end=(local.date()+timedelta(days=30)).isoformat())
        days=[d.strftime('%Y-%m-%d') for d in calendar.sessions]
        return next(day for day in days if day>local.date().isoformat() or local.time()<time(8))
    except Exception:
        raise ValueError('다음 거래일 달력을 확인하지 못했어요. 평일로 추측하지 않았어요.') from None


def latest_targets(database,history,mode,kind,settings,now):
    if kind not in ('일반','테마1'):raise ValueError('일반 또는 테마1 목록을 골라 주세요.')
    cfg=settings_snapshot(settings)
    if kind=='일반':
        with database.connect() as db:
            db.execute('BEGIN')
            batch=db.execute('SELECT * FROM recommendation_batches WHERE mode=? ORDER BY scan_id DESC LIMIT 1',(mode,)).fetchone()
            if not batch:raise ValueError('먼저 일반 추천을 검색해 주세요. /추천검색')
            if batch['source'] not in ('DEMO','KIWOOM') or (mode=='DEMO')!=(batch['source']=='DEMO'):
                raise ValueError('저장 추천의 시세 모드·출처가 달라요.')
            stamp=datetime.fromisoformat(batch['created_at'])
            if stamp.tzinfo is None or stamp.utcoffset() is None or stamp>now:raise ValueError('추천 기록 시각을 확인해 주세요.')
            targets=[dict(row) for row in db.execute('SELECT code,name,market FROM recommendations WHERE scan_id=? ORDER BY rank',(batch['scan_id'],))]
            original=json.loads(batch['conditions_json'])
        # Keep the selection's original filters, current requested exit/cost policy.
        original.update({key:cfg[key] for key in EXIT_KEYS+COST_KEYS if key in cfg})
        cfg=settings_snapshot(original)
        origin=f'scan_{batch["scan_id"]}'
        selection_info=dict(kind='GENERAL',asof=batch['created_at'])
    else:
        with history.connect() as db:
            db.execute('BEGIN')
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='theme_runs'").fetchone():
                raise ValueError('먼저 테마1을 검색해 주세요. /테마1검색')
            run=db.execute('SELECT * FROM theme_runs WHERE mode=? ORDER BY id DESC LIMIT 1',(mode,)).fetchone()
            if run is None:raise ValueError('현재 모드의 테마1 감지 목록이 없어요.')
            if not valid_source(mode,run['source']):raise ValueError('테마1 시세 모드·출처가 달라요.')
            if run['day']>now.astimezone(KST).date().isoformat():raise ValueError('미래 날짜의 테마1 목록은 사용할 수 없어요.')
            targets=[]
            for identifier in json.loads(run['alert_ids_json']):
                row=db.execute('SELECT result_json FROM theme_alerts WHERE id=? AND mode=? AND source=?',
                    (identifier,mode,run['source'])).fetchone()
                if row is None:raise ValueError('테마1 감지 목록 기록을 확인해 주세요.')
                result=json.loads(row['result_json'])['result']
                targets.append({key:result[key] for key in ('code','name','market')})
        origin=f'theme1_{run["id"]}'
        selection_info=dict(kind='THEME1',asof=run['day'])
    if not targets:raise ValueError('선택한 목록에 종목이 없어요. 새 검색 결과를 확인해 주세요.')
    day=now.astimezone(KST).date().isoformat()
    with history.connect() as db:
        rows=[dict(row) for row in db.execute('SELECT * FROM nxt_observations WHERE mode=? AND day=?',(mode,day))]
    latest_for_day(rows,mode,day)
    evidence=latest_for_day([row for row in rows if datetime.fromisoformat(row['observed_at'])<=now],mode,day)
    for target in targets:
        item=evidence.get((target['market'],target['code']))
        # Use only observations available at this request; no future metadata.
        target['nxt_eligible']=(True if item and item['eligible']==1 and datetime.fromisoformat(item['observed_at'])<=now else None)
    return origin,targets,cfg,selection_info


def setup_text(root,database,history,mode,settings,text,*,now):
    if text.split(' ',1)[0]=='/가상하루':
        from app.morning_day import report
        match=re.fullmatch(r'/가상하루(?: ([0-9]{4}-[0-9]{2}-[0-9]{2})(?: ([1-9][0-9]{0,5}))?)?',text)
        if not match:return '사용법: /가상하루 또는 /가상하루 YYYY-MM-DD [페이지]'
        return report(root,mode,match[1] or now.astimezone(KST).date().isoformat(),now=now,page=int(match[2] or 1))
    if text.split(' ',1)[0]=='/가상추적상태':
        match=re.fullmatch(r'/가상추적상태(?: ([0-9]{4}-[0-9]{2}-[0-9]{2}))?',text)
        if not match:return '사용법: /가상추적상태 또는 /가상추적상태 목록날짜'
        today=now.astimezone(KST).date().isoformat()
        day=match[1] or (today if record_path(root,mode,today).is_file() else next_day(now))
        path=record_path(root,mode,day)
        if not path.is_file():return f'{day}의 고정된 아침 가상 목록이 없어요. /가상추적 일반 100만원'
        tracker=MorningVirtual(Database(path,read_only=True))
        plan=tracker.plan()
        if plan['mode']!=mode or plan['day']!=day:raise ValueError('목록 날짜·시세 모드가 달라요.')
    else:
        day=next_day(now)
        match=re.fullmatch(r'/가상추적 (일반|테마1) ([1-9][0-9]{0,14})(원|만원)(?: 익절 ([0-9]{1,2}(?:\.[0-9]{1,2})?) 손절 ([0-9]{1,2}(?:\.[0-9]{1,2})?))?',text)
        if not match:return '사용법: /가상추적 일반 100만원 또는 /가상추적 테마1 100만원 익절 2 손절 3\n가상 목록만 고정하며 실제/키움 모의계좌 주문은 하지 않아요.'
        kind,value,unit,take,stop=match.groups()
        cfg=dict(settings)
        if take is not None:cfg.update(exit_take_pct=float(Decimal(take)),exit_stop_pct=float(Decimal(stop)))
        elif kind=='테마1':cfg.update(exit_take_pct=2,exit_stop_pct=3)
        origin,targets,cfg,selection_info=latest_targets(database,history,mode,kind,cfg,now)
        path=record_path(root,mode,day)
        frozen=now
        if path.is_file():
            reader=Database(path,read_only=True)
            with reader.connect() as db:prior_row=db.execute('SELECT payload FROM morning_cohort WHERE id=1').fetchone()
            if prior_row is not None:
                prior=MorningVirtual(reader).plan()
                frozen=datetime.fromisoformat(prior['frozen_at'])
        tracker=MorningVirtual(Database(path))
        plan=tracker.prepare(day=day,mode=mode,origin=origin,targets=targets,settings=cfg,
            total_won=int(value)*(10000 if unit=='만원' else 1),now=frozen,selection_info=selection_info)
    unknown=sum(t['venue']=='UNKNOWN' for t in plan['targets'])
    return (f'[아침 추천 가상 목록 · {mode}]\n대상 거래일 {day} · 출처 {plan["origin"]}\n'
        f'저장 추천/판정 기준: {plan.get("selection_info",{}).get("asof","미확인")}\n'
        f'고정 종목 {len(plan["targets"])}개 · 총예산 {plan["total_won"]:,}원 균등 배분\n'
        f'익절 +{plan["settings"]["exit_take_pct"]:g}% · 손절 -{plan["settings"]["exit_stop_pct"]:g}%\n'
        f'NXT/KRX 자격 미확인 {unknown}개 · 미확인은 KRX로 추정하지 않아요.\n'
        '추천 목록·설정만 고정했어요. 가상 진입/청산은 별도 첫 거래·시세 근거가 필요해요.\n'
        '현재 첫 거래 자동 수집·매일 실행 예약은 미연결이에요. 실제 주문을 하지 않았어요.\n'
        f'목록 확인: /가상추적상태 {day} · 기록 조회: /가상결산 {day}')
