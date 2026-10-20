"""Receiver-owned daily virtual-list freeze. No quote collection or orders."""
from datetime import datetime,time
from contextlib import nullcontext
from decimal import Decimal
import re
from app.order_engine import connect
from app.theme_calendar import recent_sessions
from app.trading_clock import KST


class MorningSchedule:
    def __init__(self,path,scope,mode):
        if mode not in ('DEMO','MOCK','REAL_QUOTES'):raise ValueError('아침 가상 예약의 시세 모드를 확인해 주세요.')
        self.path,self.scope,self.mode=path,scope,mode
        with connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS morning_schedule(scope TEXT,mode TEXT,command TEXT,enabled INTEGER,PRIMARY KEY(scope,mode))')
            db.execute('CREATE TABLE IF NOT EXISTS morning_schedule_jobs(scope TEXT,mode TEXT,day TEXT,state TEXT,message TEXT,delivery TEXT,PRIMARY KEY(scope,mode,day))')
            db.execute("UPDATE morning_schedule_jobs SET state='UNKNOWN',message=? WHERE scope=? AND mode=? AND state='BUILDING'",
                ('아침 가상 목록 고정 도중 프로그램이 종료됐어요. 성공 여부를 확인하지 못했어요. /가상추적상태로 확인해 주세요.',scope,mode))
            db.execute("UPDATE morning_schedule_jobs SET delivery='UNKNOWN' WHERE scope=? AND mode=? AND delivery='SENDING'",(scope,mode))

    def handle(self,text,*,now,database=None):
        if text=='/아침가상중지':
            with (nullcontext(database) if database is not None else connect(self.path)) as db:db.execute('UPDATE morning_schedule SET enabled=0 WHERE scope=? AND mode=?',(self.scope,self.mode))
            return '아침 가상 목록 자동 고정을 껐어요. 이미 저장한 목록/기록은 남겨요. 실제 주문은 하지 않아요.'
        if text=='/아침가상상태':return self.summary(now,database=database)
        match=re.fullmatch(r'/아침가상예약 (일반|테마1) ([1-9][0-9]{0,14})(원|만원)(?: 익절 ([0-9]{1,2}(?:\.[0-9]{1,2})?) 손절 ([0-9]{1,2}(?:\.[0-9]{1,2})?))?',text)
        if not match:return '사용법: /아침가상예약 일반 100만원 또는 /아침가상예약 테마1 100만원 익절 2 손절 3\n상태: /아침가상상태 · 중지: /아침가상중지'
        kind,value,unit,take,stop=match.groups()
        if int(value)*(10000 if unit=='만원' else 1)>10**15:return '가상 예산이 너무 커요. 금액을 확인해 주세요.'
        if take is not None and not all(0<Decimal(v)<100 for v in (take,stop)):return '익절·손절은 0보다 크고100보다 작게 입력해 주세요.'
        command=text.replace('/아침가상예약','/가상추적',1)
        with (nullcontext(database) if database is not None else connect(self.path)) as db:
            db.execute('INSERT INTO morning_schedule VALUES(?,?,?,1) ON CONFLICT(scope,mode) DO UPDATE SET command=excluded.command,enabled=1',
                (self.scope,self.mode,command))
        return ('[아침 가상 예약 켜짐 · 실제 주문 안 함]\n거래일07:50~08:00에 저장된 추천 목록·예산·조건을 고정해요.\n'
            f'선택: {command}\n새 검색/첫 거래 수집/가상 진입을 시작한 것은 아니에요.\n'
            'PC와 개인 수신이 켜져 있어야 해요. 이미 오늘 시도했다면 자동으로 다시 시도하지 않아요.\n/아침가상상태 · /아침가상중지')

    def summary(self,now,*,database=None):
        day=now.astimezone(KST).date().isoformat()
        with (nullcontext(database) if database is not None else connect(self.path)) as db:
            rule=db.execute('SELECT * FROM morning_schedule WHERE scope=? AND mode=?',(self.scope,self.mode)).fetchone()
            job=db.execute('SELECT * FROM morning_schedule_jobs WHERE scope=? AND mode=? AND day=?',(self.scope,self.mode,day)).fetchone()
        labels={'BUILDING':'고정 시도 기록 있음 · 완료 미확인','FROZEN':'저장 목록 고정 확인 · 진입 완료 아님',
            'FAILED':'목록 고정 실패','MISSED':'08:00 마감 이후 시작 · 건너뜀','UNKNOWN':'중단으로 완료 미확인',
            'LATE':'고정 완료 시각이 마감을 지남 · 자동 사용 확인 필요'}
        delivery={'SENT':'알림 발송 응답 확인','SENDING':'알림 응답 대기','UNKNOWN':'알림 미확인 · 재전송 안 함'}
        return (f'[아침 가상 예약] {self.mode}\n자동 고정: '+('켜짐' if rule and rule['enabled'] else '꺼짐')+'\n'
            +(f'선택: {rule["command"]}\n' if rule else '')
            +f'오늘 {day}: '+(labels.get(job['state'],'기록 확인 필요') if job else '아직 시도 없음')+'\n'
            +(delivery.get(job['delivery'],'알림 시도 전')+'\n' if job else '')
            +'거래일07:50~08:00 · 날짜별 한 번만 시도해요. 변경/실패 후 오늘은 자동 재시도하지 않아요.\n'
            '저장 추천만 사용하며 새 검색/첫 거래 수집/자동 진입은 아직 연결 중이에요. 실제 주문은 하지 않아요.')

    def tick(self,*,now,clock,active,prepare,send):
        if not active():return 'INACTIVE'
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:raise ValueError('예약 시각의 시간대를 확인해 주세요.')
        local=now.astimezone(KST);day=local.date().isoformat()
        with connect(self.path) as db:
            rule=db.execute('SELECT * FROM morning_schedule WHERE scope=? AND mode=?',(self.scope,self.mode)).fetchone()
        if not rule or not rule['enabled']:return 'DISABLED'
        if local.time()<time(7,50):return 'NOT_DUE'
        calendar_error=False
        try:
            if day not in recent_sessions(local.date()):return 'CLOSED_DAY'
        except ValueError:calendar_error=True
        key=(self.scope,self.mode,day)
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            job=db.execute('SELECT * FROM morning_schedule_jobs WHERE scope=? AND mode=? AND day=?',key).fetchone()
            if job is None:
                state='FAILED' if calendar_error else 'BUILDING' if local.time()<time(8) else 'MISSED'
                message=('거래일 달력을 확인하지 못해 아침 가상 목록을 고정하지 않았어요. 날짜를 평일로 추측하지 않았어요.' if calendar_error else
                    None if state=='BUILDING' else '08:00이 지나 오늘 아침 가상 목록 자동 고정을 건너뛰었어요. 소급 진입/매수는 하지 않았어요.')
                db.execute('INSERT INTO morning_schedule_jobs VALUES(?,?,?,?,?,NULL)',(*key,state,message))
        if job is None and state=='BUILDING':
            try:
                result=prepare(day,rule['command'])
                finished=clock()
                if not isinstance(finished,datetime) or finished.tzinfo is None or finished.utcoffset() is None:raise ValueError('완료 시각의 시간대를 확인해 주세요.')
                finished=finished.astimezone(KST)
                stamp=datetime.fromisoformat(result['frozen_at'])
                if (result['day']!=day or result['mode']!=self.mode or stamp.tzinfo is None or finished<local or stamp>finished
                        or stamp>=datetime.combine(local.date(),time(8),KST)
                        or not isinstance(result['text'],str) or not result['text']):raise ValueError('고정한 목록의 날짜·모드·시각을 확인해 주세요.')
                state='FROZEN' if finished.date()==local.date() and finished.time()<time(8) else 'LATE'
                message=('[아침 가상 목록 자동 고정]\n' if state=='FROZEN' else '[아침 가상 목록 · 마감 확인 필요]\n')+result['text']
            except ValueError as error:
                state='FAILED';message='아침 가상 목록을 고정하지 못했어요. '+str(error)+'\n실제 주문/가상 진입은 하지 않았어요. 오늘 자동 재시도는 하지 않아요.'
            except Exception:
                state='FAILED';message='아침 가상 목록을 고정하지 못했어요. 저장 추천·자료 모드·기록을 확인해 주세요. 실제 주문은 하지 않았어요.'
            with connect(self.path) as db:db.execute("UPDATE morning_schedule_jobs SET state=?,message=? WHERE scope=? AND mode=? AND day=? AND state='BUILDING'",(state,message,*key))
        if not active():return 'INACTIVE'
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            job=db.execute('SELECT * FROM morning_schedule_jobs WHERE scope=? AND mode=? AND day=?',key).fetchone()
            rule=db.execute('SELECT enabled FROM morning_schedule WHERE scope=? AND mode=?',(self.scope,self.mode)).fetchone()
            if not rule or not rule[0] or job['delivery'] is not None or job['state']=='BUILDING':return job['state']
            db.execute("UPDATE morning_schedule_jobs SET delivery='SENDING' WHERE scope=? AND mode=? AND day=?",key)
        try:
            receipt=send(job['message']) if active() else None
            confirmed=type(receipt) is int and receipt>0
        except Exception:confirmed=False
        with connect(self.path) as db:db.execute('UPDATE morning_schedule_jobs SET delivery=? WHERE scope=? AND mode=? AND day=?',('SENT' if confirmed else 'UNKNOWN',*key))
        return job['state']
