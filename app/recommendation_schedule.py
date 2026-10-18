"""Opt-in daily cached evaluation, called by the open PC application only."""
from datetime import datetime,time
from app.recommendation_cache import evaluate_saved
from app.theme_input import parse_selection
from app.trading_clock import KST


def run_due(database,history,mode,*,enabled,now):
    if enabled is not True: return dict(state='DISABLED')
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('시간대가 포함된 실행 시각이 필요해요.')
    if mode not in ('DEMO','MOCK','REAL_QUOTES'): raise ValueError('평가 모드를 확인해 주세요.')
    local=now.astimezone(KST)
    if local.time()<time(20,30): return dict(state='NOT_DUE')
    day=local.date().isoformat()
    with database.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT state FROM recommendation_evaluation_jobs WHERE mode=? AND day=?',(mode,day)).fetchone()
        if prior: return dict(state='ALREADY_ATTEMPTED',previous=prior['state'])
        db.execute('INSERT INTO recommendation_evaluation_jobs VALUES(?,?,?,?,NULL)',(mode,day,'RUNNING',local.isoformat()))
    try:
        result=evaluate_saved(database,history,mode,today=local.date())
        state='SUCCEEDED'
    except Exception:
        result=None
        state='FAILED'
    with database.connect() as db:
        db.execute('UPDATE recommendation_evaluation_jobs SET state=?,through_day=? WHERE mode=? AND day=?',
                   (state,result['available_through'] if result else None,mode,day))
    return dict(state=state,result=result)


def summary(database,mode,*,enabled,now):
    if mode not in ('DEMO','MOCK','REAL_QUOTES') or not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('평가 모드와 현재 시각을 확인해 주세요.')
    local=now.astimezone(KST)
    with database.connect() as db:
        db.execute('BEGIN')
        selection=db.execute('SELECT payload FROM recommendation_selections WHERE mode=?',(mode,)).fetchone()
        job=db.execute('SELECT * FROM recommendation_evaluation_jobs WHERE mode=? AND day=?',
                       (mode,local.date().isoformat())).fetchone()
    label={'DEMO':'가상 연습','MOCK':'키움 모의 시세','REAL_QUOTES':'실제 시세'}[mode]
    lines=[f'[{label} · 추천 성적 갱신 상태]', '자동 갱신: '+('켜짐' if enabled is True else '꺼짐'),
           '집 PC 프로그램 실행 중 20:30 이후 확인 · 저장 자료만 사용']
    if selection:
        try:
            data,_=parse_selection(selection['payload'].encode('utf-8'),mode,today=local.date(),minimum_sessions=1)
            lines.append(f"확인한 평가 범위: {data['calendar']['begin']} ~ {data['through']} · 가격 출처 {data['source']}")
            lines.append('이후 날짜는 자동 연장하지 않아요. 새 기간은 PC에서 거래일을 확인해 주세요.')
        except (ValueError,AttributeError,TypeError):
            lines.append('저장한 거래일 설정 확인 필요 · PC에서 다시 확인해 주세요.')
    else:
        lines.append('확인한 거래일 설정 없음 · PC 고급 화면에서 먼저 등록해 주세요.')
    labels={'RUNNING':'시작 기록 있음 · 완료 응답 아직 없음',
            'SUCCEEDED':'저장 자료 평가 완료', 'FAILED':'평가 미완료 · PC에서 확인 후 수동 재평가'}
    lines.append('오늘 시도: '+(labels.get(job['state'],'기록 확인 필요') if job else '아직 시도 기록 없음'))
    if job: lines.append('오늘은 자동 재시도하지 않아요. 이 기록이 현재 작업 실행을 증명하지는 않아요.')
    lines.extend(['/추천성적 · /추천요약 에서 저장된 관측을 확인하세요.',
                  '상태 조회는 평가·수집·발송·주문을 실행하지 않아요. 실제 순수익 검증이 아니에요.'])
    return '\n'.join(lines)
