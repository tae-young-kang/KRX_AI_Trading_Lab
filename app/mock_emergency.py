"""Persistent user stop. No broker IO and no financial-conflict unlock."""
import hashlib
import secrets
import threading
from app.order_engine import connect


class MockEmergencyStop:
    def __init__(self,path,clock):
        self.path,self.clock=path,clock
        self.event=threading.Event()
        self._control=threading.Lock()
        self._signal=threading.Lock()
        self._pending=False
        self._epoch=0
        with connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS manual_halt (id INTEGER PRIMARY KEY CHECK(id=1),stopped_at REAL)')
            db.execute('''CREATE TABLE IF NOT EXISTS resume_intent (
                id INTEGER PRIMARY KEY CHECK(id=1),scope TEXT,session TEXT,token_hash TEXT,
                proof TEXT,expires REAL)''')
            db.execute('DELETE FROM resume_intent')  # A restart never restores permission.
            if db.execute('SELECT 1 FROM manual_halt').fetchone(): self.event.set()

    def stop(self):
        # Signal before waiting for SQLite or an order owner's work.
        with self._signal:
            self._epoch+=1
            epoch=self._epoch
            self._pending=True
            self.event.set()
        with self._control:
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('INSERT OR REPLACE INTO manual_halt VALUES (1,?)',(self.clock().timestamp(),))
                db.execute('DELETE FROM resume_intent')
                db.execute("UPDATE intents SET state='CANCELED' WHERE state IN ('WAITING','QUEUED')")
            with self._signal:
                if self._epoch==epoch: self._pending=False
        return ('[키움 모의 긴급중지]\n새 전송을 차단하고 대기 요청을 철회했어요. 재시작해도 유지해요.\n'
                '이미 전송 중이거나 접수된 주문을 취소한 것은 아니에요. 모의 HTS/MTS와 /주문상태를 확인해 주세요.\n'
                '재개하려면 /잔고 새 조회 뒤 /모의재개를 요청하세요. 실제 돈 주문은 계속 잠겨 있어요.')

    def stopped_at(self):
        with connect(self.path) as db:
            row=db.execute('SELECT stopped_at FROM manual_halt WHERE id=1').fetchone()
            return row[0] if row else None

    def cancel(self,scope=None,session=None):
        with self._control,connect(self.path) as db:
            if scope is None: db.execute('DELETE FROM resume_intent')
            elif session is None: db.execute('DELETE FROM resume_intent WHERE scope=?',(scope,))
            else: db.execute('DELETE FROM resume_intent WHERE scope=? AND session=?',(scope,session))

    def prepare(self,scope,session,proof,expires):
        with self._control,self._signal:
            if self._pending or not self.event.is_set():
                return '긴급중지 처리 중이거나 이미 재개됐어요. /주문점검으로 확인해 주세요.'
            token=secrets.token_urlsafe(18)
            with connect(self.path) as db:
                db.execute('INSERT OR REPLACE INTO resume_intent VALUES (1,?,?,?,?,?)',
                           (scope,session,hashlib.sha256(token.encode()).hexdigest(),proof,expires))
            seconds=max(0,int(expires-self.clock().timestamp()))
            return (f'[키움 모의 긴급중지 재개 확인]\n지금 확인한 모의 연결의 새 주문 허용만 복구해요. 주문은 보내지 않아요.\n'
                    f'{seconds}초 안에 /모의재개확인 {token}\n기록 충돌·미확인 주문·최종 취소 차단은 해제하지 않아요.')

    def confirm(self,scope,session,token,proof):
        with self._control,self._signal:
            if self._pending or not self.event.is_set(): return False
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT * FROM resume_intent WHERE id=1').fetchone()
                if (not row or row['scope']!=scope or row['session']!=session or row['proof']!=proof
                        or row['expires']<self.clock().timestamp()
                        or not secrets.compare_digest(row['token_hash'],hashlib.sha256(token.encode()).hexdigest())):
                    return False
                db.execute('DELETE FROM manual_halt')
                db.execute('DELETE FROM resume_intent')
            self.event.clear()
            return True
