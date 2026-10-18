"""Explicitly armed MOCK-only queue. Broker IO belongs to the watch owner."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import uuid

from app.order_engine import OrderRequest, connect
from app.kiwoom_mock_contract import ContractError, order_plan
from app.kiwoom_mock_journal import MockDispatchJournal
from app.kiwoom_mock_status import money_view
from app.runtime_lock import ReceiverLock
from app.trading_clock import KST, TradingCalendar


class MockTelegramOrders:
    """Small fixed trial limits; no real-account destinations or retry path."""
    def __init__(self, root, *, confirmed_day, clock=None):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.clock = clock or (lambda:datetime.now(KST))
        self.day = confirmed_day
        self.calendar = TradingCalendar(open_dates={confirmed_day})
        self.lock = threading.RLock()
        self._cancel_lock = threading.Lock()
        self._canceled_sessions = set()
        self.armed = False
        self.journal = None
        self.generation = None
        self.account_tag = None
        self._sending = None
        self.path = self.root/'telegram_intents.db'
        with ReceiverLock(self.root/'setup.lock'):
            identity = self.root/'profile.id'
            if not identity.exists(): identity.write_text(uuid.uuid4().hex,encoding='ascii')
            self.profile = identity.read_text(encoding='ascii').strip()
            if not re.fullmatch('[a-f0-9]{32}',self.profile):
                raise ContractError('모의 주문 기록을 확인해야 해요.')
            with connect(self.path) as db:
                db.execute('''CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, scope TEXT, session TEXT, uid INTEGER,
                    token_hash TEXT, payload TEXT, expires REAL, state TEXT, day TEXT,
                    UNIQUE(scope,session,uid))''')
                db.execute("UPDATE intents SET state='UNKNOWN' WHERE state='CLAIMED'")
                db.execute("UPDATE intents SET state='CANCELED' WHERE state IN ('WAITING','QUEUED')")
                db.execute('CREATE TABLE IF NOT EXISTS observations (id TEXT PRIMARY KEY,state TEXT,filled INTEGER,quantity INTEGER,checked_at TEXT)')

    def _open(self):
        now = self.clock().astimezone(KST)
        return now.date()==self.day and now.weekday()<5 and self.calendar.is_open(now,'KRX')

    def arm(self, session):
        from app.kiwoom_mock_watch import MockWatchSession
        with self.lock:
            if type(session) is not MockWatchSession or session.state!='RECEIVING':
                raise ContractError('모의 알림 연결 확인이 필요해요.')
            session._guard()
            if session.transport.profile_id != self.profile or self.clock().astimezone(KST).date()!=self.day:
                raise ContractError('오늘 허용한 모의 연결이 아니에요.')
            self.journal = MockDispatchJournal(self.root/'dispatch.db',session.transport)
            self.journal.bind_account(session._account_tag)
            self.generation = session.transport.auth_generation
            self.account_tag = session._account_tag
            self.armed = True
            session.transport.mutation_allowed = self.can_dispatch

    def can_dispatch(self):
        if not self.armed or not self._sending or not self._open(): return False
        scope,session,expires,captured=self._sending
        now=self.clock().timestamp()
        return captured<=now<=expires and now-captured<=5 and not self._canceled(scope,session)

    def disarm(self):
        with self.lock:
            self.armed = False
            self.journal = None
            self.generation = None
            self.account_tag = None
            self._sending = None
            with connect(self.path) as db:
                db.execute("UPDATE intents SET state='CANCELED' WHERE state IN ('WAITING','QUEUED')")

    def cancel_session(self,scope,session):
        sessions=[session]
        if session is None:
            with connect(self.path) as db:
                sessions=[row[0] for row in db.execute('SELECT DISTINCT session FROM intents WHERE scope=?',(scope,))]
        with self._cancel_lock:
            self._canceled_sessions.update((scope,item) for item in sessions)
        with self.lock, connect(self.path) as db:
            if session is None:
                db.execute("UPDATE intents SET state='CANCELED' WHERE scope=? AND state IN ('WAITING','QUEUED')",(scope,))
            else:
                db.execute("UPDATE intents SET state='CANCELED' WHERE scope=? AND session=? AND state IN ('WAITING','QUEUED')",(scope,session))

    def _canceled(self,scope,session):
        with self._cancel_lock:
            return (scope,session) in self._canceled_sessions

    def handle(self,text,*,scope,session,uid,now):
        with self.lock, connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE intents SET state='EXPIRED' WHERE state IN ('WAITING','QUEUED') AND expires<?",(now,))
            if text=='/주문상태':
                row=db.execute('SELECT * FROM intents WHERE scope=? ORDER BY rowid DESC LIMIT 1',(scope,)).fetchone()
                labels={'WAITING':'내용 확인 대기','QUEUED':'전송 대기','CLAIMED':'처리 중 · 중복 주문 금지',
                        'UNKNOWN':'결과 미확인 · 재주문 금지','ACKNOWLEDGED':'접수 응답 확인 · 체결은 별도 조회',
                        'REJECTED':'증권사 거절','FAILED':'전송 전 확인 실패','CANCELED':'확인 철회 또는 연결 중지','EXPIRED':'확인 시간 만료'}
                detail=''
                if row:
                    seen=db.execute('SELECT * FROM observations WHERE id=?',(row['id'],)).fetchone()
                    if seen: detail=f"\n{seen['checked_at']} 조회: {seen['state']} · 체결 {seen['filled']}/{seen['quantity']}주"
                gate='오늘 모의 주문 허용 중' if self.armed and self._open() else '새 모의 주문 잠금'
                return '[키움 모의투자 · 실제 돈 사용 안 함]\n'+gate+'\n'+(labels[row['state']] if row else '주문 기록 없음')+detail+'\n/잔고: 잔고·주문 상태 재조회 · 접수는 체결 완료가 아니에요.'
            if not self.armed or not self._open() or self._canceled(scope,session):
                return '키움 모의 주문이 잠겨 있거나 거래 가능 시간이 아니에요. 집 PC에서 오늘의 모의 주문 허용을 확인해 주세요.'
            token=re.fullmatch(r'/(모의확인|모의철회) ([A-Za-z0-9_-]{20,32})',text)
            if token:
                action,code=token.groups()
                row=db.execute("SELECT * FROM intents WHERE scope=? AND session=? AND token_hash=? AND state='WAITING'",
                    (scope,session,hashlib.sha256(code.encode()).hexdigest())).fetchone()
                if not row: return '유효한 모의 확인 코드가 없어요. 만료·사용·철회 여부를 확인해 주세요.'
                state='QUEUED' if action=='모의확인' else 'CANCELED'
                db.execute('UPDATE intents SET state=? WHERE id=?',(state,row['id']))
                return '모의 주문 전송을 요청했어요. /주문상태로 확인해 주세요.' if state=='QUEUED' else '모의 주문 확인을 철회했어요.'
            match=re.fullmatch(r'/(매수|매도) ([A-Za-z0-9]{6}) ([1-9][0-9]?) ([1-9][0-9,]{0,9})',text)
            if not match: return '모의 주문 예: /매수 900001 1 1000\n순서: 종목코드 수량 지정가(원) · 종목명 대신 6자리 코드를 써 주세요.'
            side,code,quantity,price=match.groups()
            if not re.fullmatch(r'(?:[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)',price):
                return '가격의 쉼표 위치를 확인해 주세요.'
            request=OrderRequest(code,'BUY' if side=='매수' else 'SELL',int(quantity),int(price.replace(',','')))
            request.validate()
            if request.quantity>10 or request.quantity*request.limit_price>100_000:
                return '모의 시험 한도는 한 주문 10주·10만원이에요.'
            if db.execute("SELECT 1 FROM intents WHERE state IN ('UNKNOWN','CLAIMED')").fetchone():
                return '미확인 주문이 있어 새 모의 주문을 차단했어요. 재주문하지 말고 확인해 주세요.'
            if db.execute("SELECT 1 FROM intents WHERE state IN ('WAITING','QUEUED')").fetchone():
                return '이미 확인 또는 전송 대기 중인 모의 주문이 있어요. 먼저 확인하거나 철회해 주세요.'
            if db.execute('SELECT 1 FROM intents WHERE scope=? AND session=? AND uid=?',(scope,session,uid)).fetchone():
                return '이미 처리한 모의 주문 요청이에요. /주문상태로 확인해 주세요.'
            usage=db.execute("SELECT payload FROM intents WHERE day=? AND state IN ('CLAIMED','UNKNOWN','ACKNOWLEDGED','REJECTED','FAILED')",(self.day.isoformat(),)).fetchall()
            gross=request.quantity*request.limit_price
            if len(usage)>=3 or sum(json.loads(x[0])['quantity']*json.loads(x[0])['limit_price'] for x in usage)+gross>200_000:
                return '오늘 모의 시험 한도 3회·20만원을 사용했어요. 매수·매도를 함께 계산해요.'
            code_token=secrets.token_urlsafe(18)
            payload=json.dumps(dict(code=request.code,side=request.side,quantity=request.quantity,limit_price=request.limit_price))
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,scope,session,uid,hashlib.sha256(code_token.encode()).hexdigest(),payload,now+120,'WAITING',self.day.isoformat()))
            return (f'[키움 모의투자 주문 · 실제 돈 사용 안 함]\n{request.code} {side} {request.quantity}주 · 지정가 {request.limit_price:,}원\n'
                    f'주문금액 {gross:,}원 · 비용 별도\n2분 안에 /모의확인 {code_token}\n철회: /모의철회 {code_token}\n접수와 체결은 달라요.')

    def process(self,session):
        """Only called on the sole mock watch thread, never Telegram's thread."""
        with self.lock:
            if not self.armed: return
            if not self._open():
                self.disarm()
                return
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE intents SET state='EXPIRED' WHERE state='QUEUED' AND expires<?",(self.clock().timestamp(),))
                row=db.execute("SELECT * FROM intents WHERE state='QUEUED' ORDER BY rowid LIMIT 1").fetchone()
                if not row: return
                db.execute("UPDATE intents SET state='CLAIMED' WHERE id=?",(row['id'],))
            sent=False
            try:
                if session.transport.auth_generation!=self.generation or session.transport.profile_id!=self.profile:
                    raise ContractError('모의 연결 변경')
                if session.poll(): raise ContractError('새 알림 뒤 잔고 확인 필요')
                capture=session.capture_for_order()
                request=OrderRequest(**json.loads(row['payload']))
                observed=capture.observations
                if observed.unfilled: raise ContractError('남은 주문 확인 필요')
                if request.side=='BUY':
                    available=money_view(observed,{}).order_money
                    if available is None or available < (request.quantity*request.limit_price*101+99)//100:
                        raise ContractError('주문 가능 금액 확인 필요')
                elif dict((r[0],r[2]) for r in observed.holdings).get(request.code,0)<request.quantity:
                    raise ContractError('매도 가능 수량 확인 필요')
                session._guard()
                if (not self._open() or self.clock().timestamp()>row['expires']
                        or self._canceled(row['scope'],row['session']) or session.poll()
                        or capture.auth_generation!=self.generation or capture.profile_id!=self.profile):
                    raise ContractError('확인 만료')
                sent=True  # Journal owns durable SENDING before the one HTTP attempt.
                self._sending=(row['scope'],row['session'],row['expires'],capture.finished_at.timestamp())
                try:
                    result=self.journal.dispatch(row['id'],order_plan(request))
                finally:
                    self._sending=None
                state=result['state']
            except Exception:
                state='UNKNOWN' if sent else 'FAILED'
                self.armed=False
            with connect(self.path) as db:
                db.execute('UPDATE intents SET state=? WHERE id=?',(state,row['id']))
            session.balance_stale=True
            return state

    def observe(self,capture):
        """Fresh read observations only; never clear UNKNOWN or infer cancellation."""
        with self.lock:
            if (not self.armed or not self.journal or capture.profile_id!=self.profile
                    or capture.auth_generation!=self.generation or capture.observations.account_tag!=self.account_tag
                    or capture.observations.day!=self.day):
                return
            with connect(self.path) as db:
                rows=db.execute("SELECT * FROM intents WHERE state='ACKNOWLEDGED' AND day=?",(self.day.isoformat(),)).fetchall()
                for row in rows:
                    receipt=self.journal.get(row['id'])
                    if not receipt or receipt['state']!='ACKNOWLEDGED': continue
                    req=OrderRequest(**json.loads(row['payload']))
                    fills=[x for x in capture.observations.executions if x.order_no==receipt['order_no']]
                    pending=[x for x in capture.observations.unfilled if x[0]==receipt['order_no']]
                    qty=sum(x.quantity for x in fills)
                    valid=all(x.code==req.code and x.side==req.side for x in fills)
                    valid=valid and all(x[1]==req.code and x[2]==req.quantity for x in pending)
                    remaining=sum(x[3] for x in pending)
                    state='최종 상태 미확인 · 별도 확인 필요'
                    if valid and 0<=qty<=req.quantity:
                        if qty==req.quantity and not pending: state='체결 수량 조회 일치 · 비용/결제 별도 확인'
                        elif qty+remaining==req.quantity:
                            state='부분체결·남은 주문 조회' if qty else '접수·미체결 조회'
                    db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?)',
                        (row['id'],state,qty,req.quantity,capture.finished_at.astimezone(KST).strftime('%m/%d %H:%M:%S')))
