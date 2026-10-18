"""Explicitly armed MOCK-only queue. Broker IO belongs to the watch owner."""
from datetime import datetime
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import secrets
import threading
import uuid

from app.order_engine import OrderRequest, connect
from app.kiwoom_mock_contract import ContractError, order_plan, amend_plan, cancel_plan
from app.kiwoom_mock_journal import MockDispatchJournal
from app.kiwoom_mock_ledger import MockFillLedger
from app.protection_watch import ProtectionWatch
from app.kiwoom_mock_chain import project_chain
from app.kiwoom_mock_review import order_number
from app.kiwoom_mock_status import money_view
from app.runtime_lock import ReceiverLock
from app.trading_clock import KST, TradingCalendar
from app import mock_order_safety
from app.mock_emergency import MockEmergencyStop


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
        self.ledger = self._capture = None
        self.protection = None
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
                mock_order_safety.initialize(db)
                self._latched=mock_order_safety.load(db) is not None
        self.emergency=MockEmergencyStop(self.path,self.clock)

    @property
    def needs_review(self):
        return self._latched

    def _latch(self,reason,db=None):
        self._latched=True
        self.armed=False
        if db is not None:
            mock_order_safety.latch(db,reason,self.clock())
        else:
            with connect(self.path) as record:
                mock_order_safety.latch(record,reason,self.clock())

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
            self.ledger = MockFillLedger(self.root/'fills.db',account_tag=self.account_tag)
            self.protection = ProtectionWatch(self.root/'protection.db',mode='MOCK',account_tag=self.account_tag)
            self._capture = None
            self.armed = not self._latched and not self.emergency.event.is_set()
            session.transport.mutation_allowed = self.can_dispatch

    def can_dispatch(self):
        if self.emergency.event.is_set() or self._latched or not self.armed or not self._sending or not self._open(): return False
        scope,session,expires,captured=self._sending
        now=self.clock().timestamp()
        return captured<=now<=expires and now-captured<=5 and not self._canceled(scope,session)

    def disarm(self):
        with self.lock:
            self.emergency.cancel()
            self.armed = False
            self.journal = None
            self.generation = None
            self.account_tag = None
            self._sending = None
            self.ledger = self._capture = None
            with connect(self.path) as db:
                db.execute("UPDATE intents SET state='CANCELED' WHERE state IN ('WAITING','QUEUED')")

    def cancel_session(self,scope,session):
        self.emergency.cancel(scope,session)
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

    @staticmethod
    def _request(payload):
        return OrderRequest(**{k:payload[k] for k in ('code','side','quantity','limit_price')})

    def _target(self,db,scope,number):
        if not self.journal: raise ContractError('모의 연결 확인 필요')
        number=order_number(number)
        for row in db.execute("SELECT * FROM intents WHERE scope=? AND day=? AND state='ACKNOWLEDGED'",(scope,self.day.isoformat())):
            receipt=self.journal.get(row['id'])
            if not receipt or receipt['order_no']!=number: continue
            if receipt['api_id']=='kt10003': break
            _,chain=self.journal.read_chain(receipt['root_key'])
            if any(x['parent_key']==row['id'] and x['state']!='REJECTED' for x in chain): break
            return row,receipt
        raise ContractError('본인이 이 프로그램에서 접수한 오늘의 최신 주문번호를 확인해 주세요.')

    def _target_view(self,capture,receipt,target_payload):
        if (capture is None or capture.profile_id!=self.profile or capture.auth_generation!=self.generation
                or capture.observations.account_tag!=self.account_tag or capture.observations.day!=self.day):
            raise ContractError('같은 모의 계좌의 새 조회가 필요해요.')
        self.ledger.ingest(capture.observations)
        chain=project_chain(self.journal,self.ledger,receipt['root_key'])
        rows=[x for x in capture.observations.unfilled if x[0] in chain.order_nos]
        if len(rows)!=1 or rows[0][0]!=receipt['order_no']:
            raise ContractError('최신 주문의 남은 수량을 확인할 수 없어요.')
        parent=json.loads(receipt['payload'])
        size=int(parent['ord_qty'] if receipt['api_id'] in ('kt10000','kt10001') else parent['mdfy_qty'])
        if rows[0][2]!=size or rows[0][1]!=parent['stk_cd']:
            raise ContractError('원주문 수량·종목을 다시 확인해 주세요.')
        if chain.filled_qty+rows[0][3]!=target_payload.get('target_total',target_payload['quantity']):
            raise ContractError('전체 체결량과 남은 수량이 맞지 않아요. 별도 대조가 필요해요.')
        return chain,rows[0]

    def _usage_block(self,db,payload):
        usage=[json.loads(x[0]) for x in db.execute("SELECT payload FROM intents WHERE day=? AND state IN ('CLAIMED','UNKNOWN','ACKNOWLEDGED','REJECTED','FAILED')",(self.day.isoformat(),))]
        if payload.get('action')=='CANCEL':
            return '오늘 모의 취소 시험 한도 10회를 사용했어요.' if sum(x.get('action')=='CANCEL' for x in usage)>=10 else None
        counted=[x for x in usage if x.get('action')!='CANCEL']
        gross=payload['quantity']*payload['limit_price']
        if len(counted)>=3 or sum(x['quantity']*x['limit_price'] for x in counted)+gross>200_000:
            return '오늘 모의 시험 한도 3회·20만원을 사용했어요. 매수·매도·정정을 함께 계산해요.'

    def _unresolved_cancel(self,db):
        for row in db.execute("SELECT payload FROM intents WHERE day=? AND state='ACKNOWLEDGED'",(self.day.isoformat(),)):
            if json.loads(row[0]).get('action')=='CANCEL':
                return True
        return False

    def _observation_pending(self,db):
        return db.execute('SELECT 1 FROM observation_blocks b JOIN intents i ON i.id=b.id WHERE i.day=?',
                          (self.day.isoformat(),)).fetchone() is not None

    def _preview_action(self,text,db,scope,session,uid,now):
        match=re.fullmatch(r'/정정 ([0-9]{1,7}) ([1-9][0-9]?) ([1-9][0-9,]{0,9})',text)
        cancel=re.fullmatch(r'/취소 ([0-9]{1,7})',text)
        if not match and not cancel:
            return '정정: /정정 주문번호 남길잔량 지정가\n취소: /취소 주문번호\n먼저 /잔고 조회 후 /주문상태에서 최신 주문번호를 확인해 주세요.'
        if match and self._unresolved_cancel(db):
            return '취소 접수 뒤 최종 상태 대조가 남아 있어 새 주문·정정을 차단했어요. 모의 HTS/MTS에서 확인해 주세요.'
        if match and self._observation_pending(db):
            return '새 조회 대조가 필요해 새 주문·정정을 차단했어요. /주문점검을 확인해 주세요.'
        if db.execute("SELECT 1 FROM intents WHERE state IN ('UNKNOWN','CLAIMED','WAITING','QUEUED')").fetchone():
            return '미확인 또는 대기 중인 모의 요청이 있어요. /주문상태를 확인해 주세요.'
        if db.execute('SELECT 1 FROM intents WHERE scope=? AND session=? AND uid=?',(scope,session,uid)).fetchone():
            return '이미 처리한 모의 요청이에요. /주문상태로 확인해 주세요.'
        try:
            target,receipt=self._target(db,scope,(match or cancel)[1])
            if self._capture is None or not 0<=now-self._capture.finished_at.timestamp()<=30:
                raise ContractError('먼저 /잔고로 새 조회를 끝낸 뒤 30초 안에 요청해 주세요.')
            previous=json.loads(target['payload'])
            chain,pending=self._target_view(self._capture,receipt,previous)
            quantity=int(match[2]) if match else pending[3]
            if match and not re.fullmatch(r'(?:[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)',match[3]):
                raise ContractError('가격의 쉼표 위치를 확인해 주세요.')
            price=int(match[3].replace(',','')) if match else previous['limit_price']
            if quantity>pending[3] or quantity>10 or (match and quantity*price>100_000):
                raise ContractError('정정 잔량은 현재 남은 수량 이하·10주 이하, 정정 금액은 10만원 이하여야 해요.')
            payload=dict(code=previous['code'],side=previous['side'],quantity=quantity,limit_price=price,
                         action='AMEND' if match else 'CANCEL',parent_key=target['id'],root_key=receipt['root_key'],
                         order_no=receipt['order_no'],expected_remaining=pending[3],expected_filled=chain.filled_qty,
                         target_total=chain.filled_qty+quantity if match else previous.get('target_total',previous['quantity']))
            blocked=self._usage_block(db,payload)
            if blocked: return blocked
            token=secrets.token_urlsafe(18)
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,scope,session,uid,hashlib.sha256(token.encode()).hexdigest(),json.dumps(payload),now+120,'WAITING',self.day.isoformat()))
            change=f'남길 잔량 {quantity}주 · 지정가 {price:,}원' if match else f'조회된 남은 {quantity}주 전부 취소 요청'
            return (f'[키움 모의투자 정정·취소 · 실제 돈 사용 안 함]\n주문번호 {receipt["order_no"]} · 종목 {previous["code"]}\n'
                    f'지금까지 체결 {chain.filled_qty}주 · 남은 {pending[3]}주\n{change}\n'
                    f'2분 안에 /모의확인 {token}\n철회: /모의철회 {token}\n추가 체결·잔량 변화가 생기면 전송을 차단해요. 취소 접수는 완료가 아니에요.')
        except ContractError as error:
            return str(error)

    def _followup_plan(self,payload,capture,scope):
        with connect(self.path) as db:
            parent,receipt=self._target(db,scope,payload['order_no'])
        if parent['id']!=payload['parent_key'] or receipt['root_key']!=payload['root_key']:
            raise ContractError('원주문 연결 변경')
        previous=json.loads(parent['payload'])
        chain,pending=self._target_view(capture,receipt,previous)
        if pending[3]!=payload['expected_remaining'] or chain.filled_qty!=payload['expected_filled']:
            raise ContractError('내용 확인 후 추가 체결·잔량 변화가 생겼어요. 새 조회와 새 확인이 필요해요.')
        if payload['code']!=previous['code'] or payload['side']!=previous['side']:
            raise ContractError('원주문 종목·방향 변경')
        quantity=payload['quantity']
        if not 0<quantity<=pending[3]: raise ContractError('잔량 변경')
        if payload['action']=='CANCEL':
            if quantity!=pending[3]: raise ContractError('조회된 잔량 전부만 취소 요청할 수 있어요.')
            return cancel_plan(payload['code'],receipt['order_no'],remaining_quantity=quantity)
        observed=capture.observations
        if any(x[0]!=receipt['order_no'] for x in observed.unfilled):
            raise ContractError('다른 남은 주문의 예약 대조가 필요해요.')
        if payload['side']=='BUY':
            known={receipt['order_no']:OrderRequest(payload['code'],'BUY',pending[2],previous['limit_price'])}
            available=money_view(observed,known).order_money
            extra=max(0,quantity*payload['limit_price']-pending[3]*previous['limit_price'])
            if available is None or available<(extra*101+99)//100:
                raise ContractError('정정에 추가로 필요한 금액을 확인해 주세요.')
        else:
            holding=next((x for x in observed.holdings if x[0]==payload['code']),None)
            if holding is None or holding[1]<pending[3] or holding[2]+pending[3]<quantity:
                raise ContractError('정정 매도 가능 수량을 확인해 주세요.')
        return amend_plan(payload['code'],receipt['order_no'],remaining_quantity=quantity,limit_price=payload['limit_price'])

    def handle(self,text,*,scope,session,uid,now):
        if text=='/긴급중지':
            self.armed=False
            return self.emergency.stop()
        if text=='/주문점검':
            # No owner lock or broker IO: remains responsive during a long query.
            with connect(self.path) as db:
                db.execute('BEGIN')
                return mock_order_safety.summary(db,armed=self.armed,open_now=self._open(),day=self.day,now=self.clock())
        # Never hold Telegram's single receiver behind a long broker preflight.
        # A busy reply is final for this update: no deferred/automatic execution.
        if not self.lock.acquire(timeout=0.1):
            return ('모의 조회 또는 주문을 처리 중이에요. 이번 명령은 처리하지 않았고 자동 실행하지 않아요.\n'
                    '/주문점검으로 상태를 확인해 주세요. 긴급히 새 전송을 막으려면 /긴급중지를 보내 주세요.')
        try:
            return self._handle_ready(text,scope=scope,session=session,uid=uid,now=now)
        finally:
            self.lock.release()

    def _handle_ready(self,text,*,scope,session,uid,now):
        if text.split(' ',1)[0]=='/보호상태':
            match=re.fullmatch(r'/보호상태(?: ([1-9][0-9]{0,5}))?',text)
            if not match: return '사용법: /보호상태 또는 /보호상태 2'
            return self.protection.summary(page=int(match[1] or 1)) if self.protection else '모의 보호 기록 연결 전이에요. 자동 매도는 연결되지 않았어요.'
        if text.split(' ',1)[0] in ('/주문목록','/미체결'):
            from app.mock_order_views import render
            return render(self,text,scope,now)
        if text=='/모의재개' or text.startswith('/모의재개확인'):
            with self.lock:
                return self._resume(text,scope,session)
        with self.lock, connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE intents SET state='EXPIRED' WHERE state IN ('WAITING','QUEUED') AND expires<?",(now,))
            if text=='/주문상태':
                row=db.execute('SELECT * FROM intents WHERE scope=? ORDER BY rowid DESC LIMIT 1',(scope,)).fetchone()
                labels={'WAITING':'내용 확인 대기','QUEUED':'전송 대기','CLAIMED':'처리 중 · 중복 주문 금지',
                        'UNKNOWN':'결과 미확인 · 재주문 금지','ACKNOWLEDGED':'접수 응답 확인 · 체결은 별도 조회',
                        'REJECTED':'증권사 거절','FAILED':'전송 전 확인 실패 · 새 조회와 새 확인 필요','CANCELED':'확인 철회 또는 연결 중지','EXPIRED':'확인 시간 만료'}
                detail=''
                if row:
                    payload=json.loads(row['payload'])
                    action=payload.get('action','NEW')
                    if action in ('AMEND','CANCEL'):
                        detail+='\n정정·취소 대상(당시) '+payload['order_no']
                        if row['state']=='ACKNOWLEDGED':
                            labels['ACKNOWLEDGED']=('취소 접수 응답 확인 · 최종 취소 미확인' if action=='CANCEL' else '정정 접수 응답 확인 · 새 조회 필요')
                    receipt=self.journal.get(row['id']) if self.journal else None
                    if receipt and receipt['order_no']: detail+='\n모의 주문번호 '+receipt['order_no']
                    seen=db.execute('SELECT * FROM observations WHERE id=?',(row['id'],)).fetchone()
                    if seen: detail+=f"\n{seen['checked_at']} 조회: {seen['state']} · 체결 {seen['filled']}/{seen['quantity']}주"
                gate='오늘 모의 주문 허용 중' if self.armed and self._open() else '새 모의 주문 잠금'
                if self._latched: gate='기록 충돌로 새 모의 주문 차단 · /주문점검으로 이유 확인'
                return '[키움 모의투자 · 실제 돈 사용 안 함]\n'+gate+'\n'+(labels[row['state']] if row else '주문 기록 없음')+detail+'\n/잔고: 잔고·주문 상태 재조회 · 접수는 체결 완료가 아니에요.'
            if self._latched:
                return '모의 기록 충돌로 주문을 차단했어요. /주문점검으로 이유를 확인해 주세요.'
            if self.emergency.event.is_set():
                return '키움 모의 긴급중지 상태예요. /잔고 새 조회 뒤 /모의재개로 확인해 주세요.'
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
            if text.split(' ',1)[0] in ('/정정','/취소'):
                return self._preview_action(text,db,scope,session,uid,now)
            match=re.fullmatch(r'/(매수|매도) ([A-Za-z0-9]{6}) ([1-9][0-9]?) ([1-9][0-9,]{0,9})',text)
            if not match: return '모의 주문 예: /매수 900001 1 1000\n순서: 종목코드 수량 지정가(원) · 종목명 대신 6자리 코드를 써 주세요.'
            side,code,quantity,price=match.groups()
            if not re.fullmatch(r'(?:[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)',price):
                return '가격의 쉼표 위치를 확인해 주세요.'
            request=OrderRequest(code,'BUY' if side=='매수' else 'SELL',int(quantity),int(price.replace(',','')))
            request.validate()
            if request.quantity>10 or request.quantity*request.limit_price>100_000:
                return '모의 시험 한도는 한 주문 10주·10만원이에요.'
            if self._unresolved_cancel(db):
                return '취소 접수 뒤 최종 상태 대조가 남아 있어 새 주문·정정을 차단했어요. 모의 HTS/MTS에서 확인해 주세요.'
            if self._observation_pending(db):
                return '새 조회 대조가 필요해 새 주문·정정을 차단했어요. /주문점검을 확인해 주세요.'
            if db.execute("SELECT 1 FROM intents WHERE state IN ('UNKNOWN','CLAIMED')").fetchone():
                return '미확인 주문이 있어 새 모의 주문을 차단했어요. 재주문하지 말고 확인해 주세요.'
            if db.execute("SELECT 1 FROM intents WHERE state IN ('WAITING','QUEUED')").fetchone():
                return '이미 확인 또는 전송 대기 중인 모의 주문이 있어요. 먼저 확인하거나 철회해 주세요.'
            if db.execute('SELECT 1 FROM intents WHERE scope=? AND session=? AND uid=?',(scope,session,uid)).fetchone():
                return '이미 처리한 모의 주문 요청이에요. /주문상태로 확인해 주세요.'
            gross=request.quantity*request.limit_price
            request_payload=dict(code=request.code,side=request.side,quantity=request.quantity,limit_price=request.limit_price)
            blocked=self._usage_block(db,request_payload)
            if blocked: return blocked
            code_token=secrets.token_urlsafe(18)
            payload=json.dumps(dict(code=request.code,side=request.side,quantity=request.quantity,limit_price=request.limit_price))
            db.execute('INSERT INTO intents VALUES (?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex,scope,session,uid,hashlib.sha256(code_token.encode()).hexdigest(),payload,now+120,'WAITING',self.day.isoformat()))
            return (f'[키움 모의투자 주문 · 실제 돈 사용 안 함]\n{request.code} {side} {request.quantity}주 · 지정가 {request.limit_price:,}원\n'
                    f'주문금액 {gross:,}원 · 비용 별도\n2분 안에 /모의확인 {code_token}\n철회: /모의철회 {code_token}\n접수와 체결은 달라요.')

    def process(self,session):
        """Only called on the sole mock watch thread, never Telegram's thread."""
        with self.lock:
            if self.emergency.event.is_set():
                self.armed=False
                return
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
                with connect(self.path) as db:
                    has_receipts=db.execute("SELECT 1 FROM intents WHERE state='ACKNOWLEDGED' AND day=?",(self.day.isoformat(),)).fetchone()
                if self._capture is not None or has_receipts:
                    self.observe(capture)
                    if self._latched: raise ContractError('기록 충돌 차단')
                payload=json.loads(row['payload'])
                request=self._request(payload)
                observed=capture.observations
                action=payload.get('action','NEW')
                if action!='CANCEL':
                    with connect(self.path) as db:
                        if self._observation_pending(db):
                            raise ContractError('새 조회 대조가 필요해요.')
                if action!='NEW':
                    plan=self._followup_plan(payload,capture,row['scope'])
                elif observed.unfilled:
                    raise ContractError('남은 주문 확인 필요')
                elif request.side=='BUY':
                    available=money_view(observed,{}).order_money
                    if available is None or available < (request.quantity*request.limit_price*101+99)//100:
                        raise ContractError('주문 가능 금액 확인 필요')
                elif dict((r[0],r[2]) for r in observed.holdings).get(request.code,0)<request.quantity:
                    raise ContractError('매도 가능 수량 확인 필요')
                if action=='NEW': plan=order_plan(request)
                session._guard()
                if (self.emergency.event.is_set() or not self._open() or self.clock().timestamp()>row['expires']
                        or self._canceled(row['scope'],row['session']) or session.poll()
                        or capture.auth_generation!=self.generation or capture.profile_id!=self.profile):
                    raise ContractError('확인 만료')
                sent=True  # Journal owns durable SENDING before the one HTTP attempt.
                self._sending=(row['scope'],row['session'],row['expires'],capture.finished_at.timestamp())
                try:
                    result=(self.journal.dispatch(row['id'],plan) if action=='NEW' else
                            self.journal.dispatch(row['id'],plan,parent_key=payload['parent_key']))
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

    def _resume(self,text,scope,session):
        if not self.emergency.event.is_set():
            return '모의 긴급중지 상태가 아니에요. /주문점검으로 확인해 주세요.'
        if (not self.journal or not self.ledger or not self._open() or self._canceled(scope,session)):
            return '집 PC에서 오늘의 모의 연결·주문 허용과 텔레그램 수신을 먼저 확인해 주세요.'
        with connect(self.path) as db:
            db.execute("UPDATE intents SET state='EXPIRED' WHERE state IN ('WAITING','QUEUED') AND expires<?",(self.clock().timestamp(),))
            if (self._latched or mock_order_safety.load(db) or self._unresolved_cancel(db) or self._observation_pending(db)
                    or db.execute("SELECT 1 FROM intents WHERE state IN ('UNKNOWN','CLAIMED','WAITING','QUEUED')").fetchone()):
                return '기록 충돌·미확인 처리·대조 대기가 남아 있어 재개하지 않아요. /주문점검을 확인해 주세요.'
        capture=self._capture
        stopped=self.emergency.stopped_at()
        if (capture is None or stopped is None or capture.started_at.timestamp()<=stopped
                or not 0<=self.clock().timestamp()-capture.finished_at.timestamp()<=30):
            return '긴급중지 뒤 /잔고 새 조회를 끝내고 30초 안에 /모의재개를 요청해 주세요.'
        transport=self.journal.transport
        try:
            transport.ready()
            if (capture.auth_generation!=transport.auth_generation or capture.profile_id!=transport.profile_id
                    or capture.observations.account_tag!=self.account_tag or transport.current_day!=self.day):
                raise ContractError('context')
        except ContractError:
            return '현재 모의 인증·계좌 연결을 다시 확인해야 해요. 재개하지 않았어요.'
        proof=hashlib.sha256(json.dumps(asdict(capture),sort_keys=True,default=str).encode()).hexdigest()
        if text=='/모의재개':
            return self.emergency.prepare(scope,session,proof,min(self.clock().timestamp()+30,capture.finished_at.timestamp()+30))
        match=re.fullmatch(r'/모의재개확인 ([A-Za-z0-9_-]{20,32})',text)
        if not match or not self.emergency.confirm(scope,session,match[1],proof):
            return '유효한 재개 확인이 없어요. 만료·새 중지·조회 변경을 확인하고 /모의재개부터 다시 요청해 주세요.'
        if self.emergency.event.is_set():
            self.armed=False
            return '확인 중 새 긴급중지 요청이 와서 중지를 유지했어요. 재개하지 않았어요.'
        self.armed=True
        return '키움 모의 긴급중지를 해제했어요. 새 주문은 별도 확인이 필요해요. 실제 돈 주문은 계속 잠겨 있어요.'

    def observe(self,capture,*,stream=None,notices=()):
        """Fresh read observations only; never clear UNKNOWN or infer cancellation."""
        with self.lock:
            if not self.journal or not self.ledger:
                return
            if (capture.profile_id!=self.profile or capture.auth_generation!=self.generation
                    or capture.observations.account_tag!=self.account_tag or capture.observations.day!=self.day):
                self._latch('CONTEXT_CONFLICT')
                return
            try:
                self.ledger.ingest(capture.observations)
            except ContractError:
                self._latch('LEDGER_CONFLICT')
                return
            self._capture=capture
            with connect(self.path) as db:
                rows=db.execute("SELECT * FROM intents WHERE state='ACKNOWLEDGED' AND day=?",(self.day.isoformat(),)).fetchall()
                superseded={json.loads(x['payload']).get('parent_key') for x in rows}
                for row in rows:
                    receipt=self.journal.get(row['id'])
                    if not receipt or receipt['state']!='ACKNOWLEDGED': continue
                    payload=json.loads(row['payload'])
                    req=self._request(payload)
                    if row['id'] in superseded:
                        db.execute('DELETE FROM observation_blocks WHERE id=?',(row['id'],))
                    if payload.get('action') in ('AMEND','CANCEL'):
                        try:
                            chain=project_chain(self.journal,self.ledger,payload['root_key'])
                            pending=sum(x[3] for x in capture.observations.unfilled if x[0] in chain.order_nos)
                            qty=chain.filled_qty
                            if payload['action']=='CANCEL':
                                state=f'취소 접수 응답 확인 · 조회된 남은 주문 {pending}주 · 최종 취소 미확인'
                                notice=next((n for n in reversed(notices) if n.order_no==receipt['order_no']),None)
                                if notice is not None and stream is not None:
                                    try:
                                        review=stream.review_captured_cancellation(self.journal,self.ledger,payload['root_key'],
                                                                                 notice,capture,known_orders={})
                                        state=f'취소 알림 뒤 새 조회 대조 · {review.title} · 남은 {review.remaining_quantity}주 · 최종 취소 미확인'
                                    except ContractError:
                                        state=f'취소 알림 뒤 새 조회 대조 필요 · 조회된 남은 주문 {pending}주 · 최종 취소 미확인'
                            elif qty==payload['target_total'] and not pending:
                                state='정정 계보 체결 수량 조회 일치 · 비용/결제 별도 확인'
                            else:
                                state=f'정정 접수 응답 확인 · 조회된 남은 주문 {pending}주 · 최종 상태 별도 확인'
                            if payload['action']=='AMEND' and row['id'] not in superseded:
                                if qty+pending>payload['target_total']:
                                    self._latch('ORDER_CONFLICT',db)
                                pending_rows=[x for x in capture.observations.unfilled if x[0] in chain.order_nos]
                                valid_pending=(not pending_rows or (len(pending_rows)==1 and
                                    pending_rows[0][0]==receipt['order_no'] and pending_rows[0][1]==req.code and
                                    pending_rows[0][2]==req.quantity))
                                if qty+pending==payload['target_total'] and valid_pending:
                                    db.execute('DELETE FROM observation_blocks WHERE id=?',(row['id'],))
                                else:
                                    db.execute('INSERT OR IGNORE INTO observation_blocks VALUES (?)',(row['id'],))
                        except ContractError:
                            qty=0
                            state='정정·취소 계보 대조 필요 · 최종 상태 미확인'
                            self._latch('CHAIN_CONFLICT',db)
                        db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?)',
                            (row['id'],state,qty,payload['target_total'],capture.finished_at.astimezone(KST).strftime('%m/%d %H:%M:%S')))
                        continue
                    fills=[x for x in capture.observations.executions if x.order_no==receipt['order_no']]
                    pending=[x for x in capture.observations.unfilled if x[0]==receipt['order_no']]
                    qty=sum(x.quantity for x in fills)
                    valid=all(x.code==req.code and x.side==req.side for x in fills)
                    valid=valid and all(x[1]==req.code and x[2]==req.quantity for x in pending)
                    remaining=sum(x[3] for x in pending)
                    if not valid or qty>req.quantity or qty+remaining>req.quantity:
                        self._latch('ORDER_CONFLICT',db)
                    if row['id'] not in superseded:
                        if valid and qty+remaining==req.quantity:
                            db.execute('DELETE FROM observation_blocks WHERE id=?',(row['id'],))
                        else:
                            db.execute('INSERT OR IGNORE INTO observation_blocks VALUES (?)',(row['id'],))
                    state='최종 상태 미확인 · 별도 확인 필요'
                    if valid and 0<=qty<=req.quantity:
                        if qty==req.quantity and not pending: state='체결 수량 조회 일치 · 비용/결제 별도 확인'
                        elif qty+remaining==req.quantity:
                            state='부분체결·남은 주문 조회' if qty else '접수·미체결 조회'
                    db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?)',
                        (row['id'],state,qty,req.quantity,capture.finished_at.astimezone(KST).strftime('%m/%d %H:%M:%S')))
