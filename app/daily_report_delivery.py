"""PC receiver-owned 20:30 delivery. Stored records only; no broker calls."""
from datetime import time
import json
import re
from app.order_engine import connect
from app.trading_clock import KST
from app.theme_calendar import recent_sessions


def pages(callback,day,command):
    messages=[]
    for number in range(1,1001):
        text=callback(day,number)
        if not isinstance(text,str) or not text or len(text.encode('utf-16-le'))//2>3900:
            raise ValueError('결산 메시지의 길이·내용을 확인해 주세요.')
        messages.append(text)
        follow=re.search(r'^다음: '+re.escape(command+' '+day)+r' ([0-9]+)$',text,re.M)
        if follow is None:return messages
        if int(follow[1])!=number+1:raise ValueError('결산 페이지 순서를 확인해 주세요.')
    raise ValueError('결산 페이지가 너무 많아요. 수동 조회로 확인해 주세요.')


class DailyReportDelivery:
    def __init__(self,path,scope,mode):
        if mode not in ('DEMO','MOCK','REAL_QUOTES'):raise ValueError('결산 시세 모드를 확인해 주세요.')
        self.path,self.scope,self.mode=path,scope,mode
        with connect(path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS daily_report_delivery(
                scope TEXT,mode TEXT,day TEXT,state TEXT,payload TEXT,cursor INTEGER,last_at REAL,
                PRIMARY KEY(scope,mode,day))''')
            db.execute("UPDATE daily_report_delivery SET state='UNKNOWN' WHERE scope=? AND mode=? AND state IN ('BUILDING','SENDING')",(scope,mode))

    def summary(self,now,*,mock_available=False):
        day=now.astimezone(KST).date().isoformat()
        with connect(self.path) as db:
            row=db.execute('SELECT state,cursor,payload FROM daily_report_delivery WHERE scope=? AND mode=? AND day=?',
                (self.scope,self.mode,day)).fetchone()
        labels={'BUILDING':'내용 준비 중','READY':'발송 대기','SENDING':'발송 응답 대기','SENT':'모든 페이지 발송 응답 확인',
                'UNKNOWN':'일부 발송 결과 미확인 · 자동 재전송 안 함','FAILED':'내용 준비 실패 · 자동 재시도 안 함'}
        state=labels.get(row['state'],'기록 확인 필요') if row else '아직 시도 안 함'
        count=f' · 확인된 페이지 {row["cursor"]}개' if row else ''
        return (f'[20:30 결산 알림] {day} · {self.mode}\n{state}{count}\n'
                '거래일 20:30 이후 PC와 개인 텔레그램 수신이 켜져 있을 때 저장 기록을 보내요.\n'
                '실제 체결 입력·아침 가상 기록을 구분해요. '
                +('모의 결산 조회 연결 있음 · 오늘 이미 준비한 보고는 당시 페이지를 유지해요.\n' if mock_available else '키움 모의계좌 결산 자동 집계는 아직 미연결이에요.\n')+
                '수동 조회: /실제결산 · /가상하루'+(' · /모의결산' if mock_available else '')+
                '. 발송 응답은 사용자가 읽었다는 확인이 아니에요.')

    def tick(self,*,now,active,build,send):
        if not active():return 'INACTIVE'
        local=now.astimezone(KST)
        if local.time()<time(20,30):return 'NOT_DUE'
        day=local.date().isoformat()
        if day not in recent_sessions(local.date()):return 'CLOSED_DAY'
        key=(self.scope,self.mode,day)
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM daily_report_delivery WHERE scope=? AND mode=? AND day=?',key).fetchone()
            if row is None:
                db.execute('INSERT INTO daily_report_delivery VALUES(?,?,?,?,?,?,?)',(*key,'BUILDING',None,0,local.timestamp()))
        if row is None:
            try:
                messages=build(day)
                if not isinstance(messages,list) or not messages or any(not isinstance(m,str) or not m for m in messages):raise ValueError()
                payload=json.dumps(messages,ensure_ascii=False)
                state='READY'
            except Exception:
                payload=None;state='FAILED'
            with connect(self.path) as db:
                db.execute("UPDATE daily_report_delivery SET state=?,payload=?,last_at=NULL WHERE scope=? AND mode=? AND day=? AND state='BUILDING'",
                    (state,payload,*key))
        if not active():return 'INACTIVE'
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM daily_report_delivery WHERE scope=? AND mode=? AND day=?',key).fetchone()
            if row['state']!='READY':return row['state']
            if row['last_at'] is not None and local.timestamp()-row['last_at']<2:return 'WAIT'
            messages=json.loads(row['payload']);index=row['cursor']
            db.execute("UPDATE daily_report_delivery SET state='SENDING',last_at=? WHERE scope=? AND mode=? AND day=?",
                (local.timestamp(),*key))
        # Persist uncertain outcome before IO. A process crash never replays it.
        try:
            receipt=send(messages[index]) if active() else None
            confirmed=type(receipt) is int and receipt>0
        except Exception:confirmed=False
        state=('SENT' if index+1==len(messages) else 'READY') if confirmed else 'UNKNOWN'
        with connect(self.path) as db:
            db.execute("UPDATE daily_report_delivery SET state=?,cursor=? WHERE scope=? AND mode=? AND day=? AND state='SENDING'",
                (state,index+1 if confirmed else index,*key))
        return state
