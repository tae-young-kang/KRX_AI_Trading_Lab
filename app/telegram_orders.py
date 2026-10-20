"""Telegram-shaped commands for the OFFLINE engine only; no networking here."""
import hashlib
import json
import re
import secrets
import uuid
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from app.order_engine import OrderEngine, OrderRequest, OrderError, connect
from app.trading_clock import KST
from app import order_safety
from app import telegram_menu
from app.theme_one import RULES_TEXT as THEME_ONE_RULES

HELP = ('[가짜 서버 주문 연습 · 실제 돈 사용 안 함]\n'
        '/매수 900001 1 1000 또는 /매도 900001 1 1000\n'
        '순서: 종목코드(또는 이름) 수량 지정가(원)\n'
        '예: 연습종목 1주 1000원 매수\n'
        '내용을 확인한 뒤 /확인 코드 · 철회하려면 /철회 코드\n'
        '/상태: 가짜 잔액·한도 /중지: 새 주문 중지\n'
        '/긴급중지: 새 주문 차단·미체결 취소 시도 /복구확인: 조회만\n'
        '접수와 체결은 다릅니다. 자동 체결·장 마감 예약은 하지 않습니다.')
ORDER_LABELS = {'OPEN':'접수·미체결', 'PARTIAL':'일부 체결', 'FILLED':'체결 완료',
                'UNKNOWN':'결과 미확인 · 재전송 안 함', 'REJECTED':'거절', 'CANCELED':'취소'}


class TelegramOrderService:
    def __init__(self, path, engine, user_id, chat_id, bot_id, catalog, clock=None, limits=None,
                 conditions=None, orders_enabled=True, watch_status=None, watch_refresh=None, watch_result=None,
                 watch_reconnect=None, watch_validation=None, watch_orders=None, watch_cancel_orders=None,
                 watch_history=None, watch_funds=None, recommendation_report=None, recommendation_overview=None, practice_sales=None, theme_report=None, theme_status=None, theme_budget=None, practice_daily=None, recommendation_request=None, recommendation_status=None, theme_request=None, outcome_request=None, theme_plan=None, watch_delivery=None):
        if (type(engine) is not OrderEngine or engine.broker.environment != 'SIMULATED'):
            raise ValueError('가짜 서버 주문 엔진만 연결할 수 있습니다.')
        if any(type(x) is not int or x <= 0 for x in (user_id, chat_id, bot_id)):
            raise ValueError('허용 사용자와 개인 대화방 번호를 먼저 등록하세요.')
        self.path = Path(path)
        if self.path.resolve() in (engine.path.resolve(), engine.broker.path.resolve()):
            raise ValueError('텔레그램 기록은 주문·가짜 계좌 기록과 별도로 보관해야 합니다.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine, self.user_id, self.chat_id = engine, user_id, chat_id
        self.conditions, self.orders_enabled = conditions, orders_enabled
        self.watch_status = watch_status
        self.watch_refresh, self.watch_result = watch_refresh, watch_result
        self.watch_reconnect = watch_reconnect
        self.watch_validation = watch_validation
        self.watch_history = watch_history
        self.watch_funds = watch_funds
        self.recommendation_report = recommendation_report
        self.recommendation_overview = recommendation_overview
        self.practice_sales = practice_sales
        self.theme_report = theme_report
        self.theme_request = theme_request
        self.outcome_request=outcome_request
        self.theme_status = theme_status
        self.theme_budget = theme_budget
        self.theme_plan=theme_plan
        self.practice_daily = practice_daily
        self.recommendation_request = recommendation_request
        self.recommendation_status = recommendation_status
        self.watch_orders, self.watch_cancel_orders = watch_orders, watch_cancel_orders
        self.watch_delivery=watch_delivery
        from app.theme_delivery import destination_scope
        self.theme_destination = destination_scope(bot_id,chat_id)
        self.broker_identity = engine.broker.identity
        self.clock = clock or (lambda: datetime.now(KST))
        if self.clock().tzinfo is None:
            raise ValueError('시간대가 포함된 시각이 필요합니다.')
        self.limits = limits or order_safety.PracticeLimits()
        self._action_lock = threading.RLock()
        self.started = int(self.clock().timestamp())
        self.scope = hashlib.sha256(f'{bot_id}:{user_id}:{chat_id}'.encode()).hexdigest()
        self.session = uuid.uuid4().hex
        self.catalog = dict(catalog)
        self.catalog.setdefault('900001', '연습종목')
        with connect(self.path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS tg_sessions(scope TEXT PRIMARY KEY,session TEXT,active INTEGER,cursor INTEGER);
                CREATE TABLE IF NOT EXISTS tg_updates(scope TEXT,uid INTEGER,reply TEXT,PRIMARY KEY(scope,uid));
                CREATE TABLE IF NOT EXISTS tg_intents(id TEXT PRIMARY KEY,scope TEXT,session TEXT,
                    token_hash TEXT,payload TEXT,name TEXT,expires REAL,state TEXT,order_id TEXT);
            ''')
            order_safety.initialize(db, self.broker_identity)
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE tg_intents SET state='EXPIRED' WHERE scope=? AND state='WAITING'", (self.scope,))
            # Re-read queued updates on a new session, but accept only messages
            # created since startup. Telegram IDs can reset after a long idle.
            db.execute('INSERT INTO tg_sessions VALUES(?,?,1,NULL) ON CONFLICT(scope) DO UPDATE SET session=excluded.session,active=1,cursor=NULL',
                       (self.scope, self.session))
        if self.watch_cancel_orders:
            self.watch_cancel_orders(self.scope,None)

    def stop(self):
        if self.watch_cancel_orders:
            self.watch_cancel_orders(self.scope,self.session)
        if self.conditions is not None:
            self.conditions.cancel_session(self.scope, self.session)
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE tg_sessions SET active=0 WHERE scope=? AND session=?', (self.scope, self.session))
            db.execute("UPDATE tg_intents SET state='CANCELED' WHERE scope=? AND session=? AND state='WAITING'",
                       (self.scope, self.session))

    def cursor(self, new=None):
        with connect(self.path) as db:
            if new is not None:
                db.execute('UPDATE tg_sessions SET cursor=MAX(COALESCE(cursor,0),?) WHERE scope=? AND session=?',
                           (new, self.scope, self.session))
            return db.execute('SELECT cursor FROM tg_sessions WHERE scope=?', (self.scope,)).fetchone()[0]

    def accepts_search_requests(self):
        with connect(self.path) as db:
            row = db.execute('SELECT active FROM tg_sessions WHERE scope=? AND session=?',
                             (self.scope, self.session)).fetchone()
            return bool(row and row['active'])

    def _authorized(self, update, now):
        if not isinstance(update, dict) or type(update.get('update_id')) is not int or update['update_id'] < 0:
            return False
        if 'edited_message' in update or 'callback_query' in update:
            return False
        m = update.get('message')
        if (not isinstance(m, dict) or any(k.startswith('forward_') for k in m)
                or any(k in m for k in ('via_bot', 'edit_date', 'sender_chat', 'is_automatic_forward'))):
            return False
        sender, chat = m.get('from', {}), m.get('chat', {})
        return (isinstance(sender, dict) and isinstance(chat, dict)
                and type(sender.get('id')) is int and sender['id'] == self.user_id and sender.get('is_bot') is False
                and type(chat.get('id')) is int and chat['id'] == self.chat_id and chat.get('type') == 'private'
                and type(m.get('date')) is int and self.started <= m['date'] <= now+5
                and now-m['date'] <= 120 and isinstance(m.get('text'), str) and len(m['text']) <= 200)

    def _order(self, text):
        command = re.fullmatch(r'/?(매수|매도)\s+(\S+)\s+([1-9][0-9]{0,2})\s+([1-9][0-9,]{0,11})', text)
        natural = re.fullmatch(r'(\S+)\s+([1-9][0-9]{0,2})주\s+([1-9][0-9,]{0,11})원\s+(매수|매도)', text)
        if command:
            side, name, qty, price = command.groups()
        elif natural:
            name, qty, price, side = natural.groups()
        else:
            raise ValueError('종목·수량·지정가를 함께 적어주세요. 예: /매수 900001 1 1000')
        if not re.fullmatch(r'(?:[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)', price):
            raise ValueError('지정가의 쉼표 위치를 확인하세요.')
        matches = [code for code, label in self.catalog.items() if code == name or label == name]
        if len(matches) != 1:
            raise ValueError('이름이 없거나 중복됩니다. 조회한 목록의 6자리 종목코드를 적어주세요.')
        request = OrderRequest(matches[0], 'BUY' if side == '매수' else 'SELL', int(qty), int(price.replace(',', '')))
        request.validate()
        if request.quantity > 100 or request.quantity*request.limit_price > 1_000_000:
            raise ValueError('가짜 서버 연습 한도는 한 주문 100주·100만원입니다.')
        return request

    def handle(self, update):
        with self._action_lock:
            return self._handle(update)

    def send_order_notice(self, transport):
        """Only to the configured private destination of the active receiver."""
        if self.orders_enabled or self.watch_delivery is None:return
        with self._action_lock:
            with connect(self.path) as db:
                owner=db.execute('SELECT session,active FROM tg_sessions WHERE scope=?',(self.scope,)).fetchone()
            if not owner or owner['session']!=self.session or not owner['active']:return
            context=dict(scope=self.scope,session=self.session)
            notice=self.watch_delivery('claim',**context)
            if notice is None:return
            delivered=False
            try:
                receipt=transport.send(self.chat_id,notice['text'])
                delivered=type(receipt) is int and receipt>0
            except Exception:
                pass
            finally:
                self.watch_delivery('finish',key=notice['key'],delivered=delivered,**context)

    def _handle(self, update):
        now = self.clock().timestamp()
        if not self._authorized(update, now):
            return None
        if (self.engine.broker.environment != 'SIMULATED' or self.engine.broker.identity != self.broker_identity):
            self.stop()
            return '연습 서버 연결이 달라져 수신을 중지했습니다. 주문하지 않았습니다.'
        uid, text = update['update_id'], update['message']['text'].strip()
        text=telegram_menu.COMMANDS.get(text,text)
        text=telegram_menu.CONDITION_COMMANDS.get(text,text)
        action = None
        operation = None
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            owner = db.execute('SELECT * FROM tg_sessions WHERE scope=?', (self.scope,)).fetchone()
            if owner['session'] != self.session:
                return None
            previous = db.execute('SELECT reply FROM tg_updates WHERE scope=? AND uid=?', (self.scope, uid)).fetchone()
            if previous:
                return previous['reply']
            mock_command = text.split(' ',1)[0] in ('/매수','/매도','/정정','/취소','/모의확인','/모의철회','/주문상태','/주문점검','/주문목록','/미체결',
                                                   '/긴급중지','/모의재개','/모의재개확인')
            if text in ('/메뉴','/start'):
                reply=telegram_menu.text(practice=self.orders_enabled,active=bool(owner['active']))
            elif text=='/메뉴숨기기':
                reply=telegram_menu.HIDDEN
            elif text=='/조건바꾸기':
                reply=telegram_menu.condition_text()
            elif not self.orders_enabled and mock_command:
                reply = (self.watch_orders(text,scope=self.scope,session=self.session,uid=uid,now=now)
                         if self.watch_orders and (owner['active'] or text.split(' ',1)[0] in ('/주문상태','/주문점검','/긴급중지','/주문목록','/미체결')) else '주문은 할 수 없어요. 키움 모의 주문이 중지됐거나 연결되지 않았어요.')
            elif text.split(' ',1)[0] == '/보호상태':
                reply=(self.watch_orders(text,scope=self.scope,session=self.session,uid=uid,now=now)
                       if not self.orders_enabled and self.watch_orders else '모의 보호 기록 연결이 없어요. 자동 매도는 아직 연결되지 않았어요.')
            elif text.split(' ',1)[0] == '/연습결산':
                match=re.fullmatch(r'/연습결산(?: ([0-9]{4}-[0-9]{2}-[0-9]{2})(?: ([1-9][0-9]{0,5}))?)?',text)
                if not match:
                    reply='사용법: /연습결산 또는 /연습결산 YYYY-MM-DD [페이지]'
                elif not self.practice_daily:
                    reply='연습 결산 연결이 없어요. 집 PC에서 확인해 주세요.'
                else:
                    try:
                        day=match[1] or self.clock().astimezone(KST).date().isoformat()
                        page=int(match[2] or 1)
                        reply=self.practice_daily(day) if page==1 else self.practice_daily(day,page=page)
                    except (ValueError,TypeError,OverflowError):
                        reply='날짜와 연습 결산 기록을 확인해야 해요. 실제 계좌 결산은 아닙니다.'
            elif text.split(' ',1)[0]=='/테마1계획':
                if not self.theme_plan:reply='테마1 계획 연결이 없어요. PC 프로그램을 업데이트하고 다시 실행해 주세요.'
                else:
                    try:reply=self.theme_plan(text)
                    except ValueError as error:reply=str(error)
            elif text.split(' ',1)[0] == '/테마1예산':
                match=re.fullmatch(r'/테마1예산 ([1-9][0-9]{0,14}) ([0-9]{1,3}(?:\.[0-9]{1,2})?) ([0-9]{1,3}(?:\.[0-9]{1,2})?)(?: ([1-9][0-9]{0,5}))?(?: 익절 ([0-9]{1,2}(?:\.[0-9]{1,2})?) 손절 ([0-9]{1,2}(?:\.[0-9]{1,2})?))?',text)
                if not match:
                    reply='사용법: /테마1예산 총예산 수수료% 가격여유% [페이지] [익절 % 손절 %]\n예: /테마1예산 1000000 0.02 2 익절 4 손절 3\n기본 익절 2%·손절 3%. 예상 계획이며 주문하지 않아요.'
                elif not self.theme_budget:
                    reply='테마1 예산 미리보기 연결이 없어요. 집 PC에서 확인해 주세요.'
                else:
                    from decimal import Decimal
                    try:
                        args=(int(match[1]),int(Decimal(match[2])*100),int(Decimal(match[3])*100),int(match[4] or 1))
                        reply=(self.theme_budget(*args,take_bps=int(Decimal(match[5])*100),stop_bps=int(Decimal(match[6])*100))
                               if match[5] else self.theme_budget(*args))
                    except ValueError:
                        reply='테마1 판정과 총예산·수수료·가격 여유·익절/손절 비율을 확인해 주세요. 주문하지 않았어요.'
            elif text=='/테마1조건':
                reply=THEME_ONE_RULES
            elif text in ('/테마1검색','/테마1검색상태'):
                if self.theme_request is None:
                    reply='테마1 검색 연결이 없어요. PC에서 수신을 다시 시작해 주세요.'
                elif text=='/테마1검색상태':
                    reply=self.theme_request.summary()
                elif not owner['active']:
                    reply='새 테마1 검색 요청을 중지했어요. PC에서 수신을 다시 시작해 주세요.'
                else:
                    reply=self.theme_request.request()
            elif text == '/테마1알림상태':
                reply=self.theme_status(self.theme_destination) if self.theme_status else '테마1 알림 상태 연결이 없어요. 집 PC에서 확인해 주세요.'
            elif text.split(' ',1)[0] == '/테마1':
                match=re.fullmatch(r'/테마1(?: ([1-9][0-9]{0,5}))?',text)
                if not match:
                    reply='사용법: /테마1 또는 /테마1 2 (저장된 결과 조회)'
                else:
                    reply=self.theme_report(int(match[1] or 1)) if self.theme_report else '테마1 기록 연결이 없어요. 집 PC에서 확인해 주세요.'
            elif text.split(' ',1)[0] == '/연습내역':
                listing=re.fullmatch(r'/연습내역 목록(?: ([1-9][0-9]{0,5}))?',text)
                match=re.fullmatch(r'/연습내역(?: ([1-9][0-9]{0,8})(?: ([1-9][0-9]{0,5}))?)?',text)
                if not match and not listing:
                    reply='사용법: /연습내역 또는 /연습내역 기록번호 페이지\n오래된 기록 찾기: /연습내역 목록 2'
                elif not self.practice_sales:
                    reply='연습 기록 연결이 없어요. 집 PC에서 확인해 주세요.'
                else:
                    reply=(self.practice_sales(None,int(listing[1] or 1)) if listing else
                           self.practice_sales(int(match[1]) if match[1] else None,int(match[2] or 1)))
            elif text in ('/추천검색', '/검색상태', '/검색취소'):
                if not self.recommendation_request:
                    reply = '추천 검색 연결이 없어요. 집 PC에서 수신을 다시 시작해 주세요.'
                elif text == '/검색상태':
                    reply = self.recommendation_request.summary()
                elif text == '/검색취소':
                    reply = self.recommendation_request.cancel()
                elif not owner['active']:
                    reply = '새 검색 요청을 중지했어요. 집 PC에서 수신을 다시 시작해 주세요.'
                else:
                    reply = self.recommendation_request.request()
            elif text.split(' ',1)[0] in ('/추천결과','/추천상세'):
                detail=text.startswith('/추천상세 ')
                match = re.fullmatch(r'/(?:추천결과|추천상세) ([1-9][0-9]{0,11})(?: ([1-9][0-9]{0,5}))?', text)
                if not match:
                    reply = '사용법: /추천결과 검색번호 [페이지] · 예: /추천결과 12 2\n자세히: /추천상세 12 2'
                elif not self.recommendation_report:
                    reply = '추천 기록 연결이 없어요. 집 PC에서 확인해 주세요.'
                else:
                    args=dict(scan_id=int(match[1]),page=int(match[2] or 1))
                    reply = self.recommendation_report(False,**args,details=True) if detail else self.recommendation_report(False,**args)
            elif text in ('/성적갱신','/성적갱신상태','/성적자료보완'):
                if self.outcome_request is None:
                    reply='성적 갱신 연결이 없어요. PC에서 프로그램을 업데이트하고 수신을 다시 시작해 주세요.'
                elif text=='/성적갱신상태':reply=self.outcome_request.summary()
                elif not self.accepts_search_requests():reply='수신이 중지됐어요. PC에서 다시 시작해 주세요.'
                else:reply=self.outcome_request.request(collect=text=='/성적자료보완')
            elif text == '/평가상태':
                reply=self.recommendation_status() if self.recommendation_status else '추천 성적 상태 연결이 없어요. 집 PC에서 수신을 다시 시작해 주세요.'
            elif text == '/추천요약':
                reply = (self.recommendation_overview() if self.recommendation_overview
                         else '추천 요약 연결이 없어요. 집 PC에서 확인해 주세요.')
            elif text.split(' ',1)[0] in ('/추천기록','/추천성적'):
                match = re.fullmatch(r'/(추천기록|추천성적)(?: ([1-9][0-9]{0,5}))?',text)
                if not match:
                    reply = '사용법: /추천기록 또는 /추천성적\n다음 페이지 예: /추천기록 2'
                elif not self.recommendation_report:
                    reply = '추천 기록 연결이 없어요. 집 PC에서 확인해 주세요.'
                else:
                    page = int(match[2] or 1)
                    reply = (self.recommendation_report(match[1]=='추천성적') if page==1 else
                             self.recommendation_report(match[1]=='추천성적',page=page))
            elif text == '/모의자금':
                reply = (self.watch_funds() if not self.orders_enabled and self.watch_funds
                         else '모의 자금 조회가 없어요. 집 PC에서 키움 모의투자 모드를 확인해 주세요.')
            elif text == '/모의기록':
                reply = (self.watch_history() if not self.orders_enabled and self.watch_history
                         else '저장된 모의 기록을 볼 수 없어요. 집 PC에서 키움 모의투자 모드를 확인해 주세요.')
            elif text == '/모의점검':
                reply = (self.watch_validation() if not self.orders_enabled and self.watch_validation
                         else '모의 연결 점검이 없어요. 집 PC에서 키움 모의투자 모드를 확인해 주세요.')
            elif text == '/모의재연결':
                reply = (self.watch_reconnect() if owner['active'] and not self.orders_enabled and self.watch_reconnect
                         else '모의 재연결이 중지됐거나 허용되지 않았어요. 집 PC에서 확인해 주세요.')
            elif text == '/잔고':
                reply = (self.watch_refresh() if owner['active'] and not self.orders_enabled and self.watch_refresh
                         else '모의 잔고 조회 연결이 없거나 중지됐어요. 집 PC에서 키움 모의투자 연결을 확인해 주세요.')
            elif text == '/잔고결과':
                reply = (self.watch_result() if not self.orders_enabled and self.watch_result
                         else '모의 잔고 조회 결과가 없어요.')
            elif text in ('/프로그램상태','/모의상태') or (text == '/상태' and not self.orders_enabled):
                reply = self.watch_status() if self.watch_status is not None else '프로그램이 켜져 있어요. 모의 알림 상태 연결은 아직 없어요. 주문은 보내지 않았어요.'
            elif text.startswith('/조건') and self.conditions is not None:
                reply = (self.conditions.handle(text, scope=self.scope, session=self.session, uid=uid, now=now)
                         if owner['active'] or text in ('/조건','/조건도움말') else '조건 변경을 중지했어요. 프로그램에서 수신을 다시 시작해 주세요.')
            elif not self.orders_enabled:
                if text == '/중지':
                    if self.watch_cancel_orders:
                        self.watch_cancel_orders(self.scope,self.session)
                    if self.conditions is not None:
                        self.conditions.cancel_session(self.scope, self.session)
                    db.execute('UPDATE tg_sessions SET active=0 WHERE scope=?', (self.scope,))
                    reply = '조건 변경·새 모의 주문 요청을 중지하고 대기 중인 변경·모의 주문 확인을 철회했어요. 이미 전송한 주문을 취소한 것은 아니에요.'
                else:
                    reply = '실제 돈 주문은 잠겨 있어요. 키움 모의 주문은 PC의 당일 허용과 별도 확인이 필요해요.\n/프로그램상태 · /모의점검 · /모의기록 · /잔고 · /잔고결과 · /모의재연결 · /주문목록 · /미체결 · /주문상태 · /주문점검 · /긴급중지 · /모의재개 · /조건 · /조건도움말'
                    reply += '\n/모의자금: 예수금·주문 가능 금액·다음 결제일 예상값 구분'
                    reply += '\n/추천기록 · /추천성적: 저장된 추천과 이후 가격 관측'
                    reply += '\n/추천요약: 조건별 관측 수·가격 변화·자료 부족 보기\n/연습내역: 앱 안의 연습 매도 내역\n/연습결산: 연습 하루 금액·실현손익·승패\n/테마1: 저장된 감지 결과 조회\n/테마1알림상태: 발송 응답·미확인·미시도\n/테마1예산: 총예산 미리보기 사용법'
            elif text in ('/도움말', '/start', '/help'):
                reply = HELP + ('\n/조건 · /조건도움말: 추천 기준 확인·변경' if self.conditions else '')
                reply += '\n/추천검색: 새 검색 요청 · /검색상태: 진행 확인'
                reply += '\n/검색취소: 대기·진행 중인 원격 검색 취소'
                reply += '\n/보호상태: 모의 보호 판단 기록 (자동 매도 미연결)'
                reply += '\n/평가상태: 추천 성적 자동 갱신 상태'
                reply += '\n/성적갱신: 저장 자료로 추천 성적 다시 계산 · /성적갱신상태: 진행 확인'
                reply += '\n/성적자료보완: 빠진 KRX 일봉 수집 후 다시 평가 (별도 KRX 키 필요)'
                reply += '\n/테마1계획 종목명 10주 또는 전체 100만원: 매수 계획 보기 (주문 안 함)'
                reply += '\n/추천결과 검색번호: 해당 검색의 종목만 보기'
                reply += '\n/추천기록 · /추천성적: 저장된 추천과 이후 가격 관측'
                reply += '\n/추천요약: 조건별 관측 수·가격 변화·자료 부족 보기\n/연습내역: 앱 안의 연습 매도 내역\n/연습결산: 연습 하루 금액·실현손익·승패\n/테마1: 저장된 감지 결과 조회\n/테마1알림상태: 발송 응답·미확인·미시도\n/테마1예산: 총예산 미리보기 사용법'
            elif text == '/상태':
                account = self.engine.account()
                last = db.execute('SELECT * FROM tg_intents WHERE scope=? ORDER BY rowid DESC LIMIT 1', (self.scope,)).fetchone()
                detail = '주문 없음'
                if last:
                    detail = {'WAITING':'확인 대기', 'EXPIRED':'확인 만료', 'CANCELED':'확인 철회',
                              'CLAIMED':'결과 확인 필요 · 재주문 금지', 'UNKNOWN':'결과 미확인 · 재전송 안 함',
                              'FAILED':'미접수', 'SENT':'주문 처리됨'}.get(last['state'], '확인 필요')
                    if last['order_id']:
                        order = self.engine.get(last['order_id'])
                        detail = f"{ORDER_LABELS.get(order['state'], '확인 필요')} · 체결 {order['filled_qty']}/{order['quantity']}주"
                used = order_safety.usage(db, self.broker_identity, self.clock())
                halted = db.execute('SELECT halted FROM tg_risk WHERE broker=?', (self.broker_identity,)).fetchone()[0]
                reply = (f"[가짜 서버만 연결]\n연습 잔액 {account['cash']:,}원 · 보유 {sum(account['positions'].values())}주\n"
                         f"최근 주문: {detail}\n오늘 확인 {used['count']}/{self.limits.daily_count}회 · "
                         f"금액 {used['notional']:,}/{self.limits.daily_notional:,}원\n"
                         f"긴급 중지: {'켜짐' if halted else '꺼짐'}\n실제 계좌·키움 모의 서버와 연결하지 않았습니다.")
            elif text in ('/긴급중지', '/복구확인'):
                operation = text
                if text == '/긴급중지':
                    order_safety.latch(db, self.broker_identity, 'USER_EMERGENCY')
                reply = '안전 상태 확인 중 · 주문을 새로 보내지 않습니다.'
            elif text == '/중지':
                if self.conditions is not None:
                    self.conditions.cancel_session(self.scope, self.session)
                db.execute('UPDATE tg_sessions SET active=0 WHERE scope=?', (self.scope,))
                db.execute("UPDATE tg_intents SET state='CANCELED' WHERE scope=? AND state='WAITING'", (self.scope,))
                reply = '새 주문을 중지하고 확인 대기를 철회했습니다. 이미 접수한 미체결 주문을 취소한 것은 아닙니다. 다시 시작하려면 프로그램에서 수신을 중지한 뒤 시작하세요.'
            elif not owner['active']:
                reply = '새 주문이 중지돼 있습니다. 프로그램에서 다시 시작해야 합니다.'
            else:
                try:
                    token_command = re.fullmatch(r'/(확인|철회)\s+([A-Za-z0-9_-]{16,32})', text)
                    if token_command:
                        verb, token = token_command.groups()
                        token_hash = hashlib.sha256(token.encode()).hexdigest()
                        intent = db.execute('SELECT * FROM tg_intents WHERE scope=? AND session=? AND token_hash=?',
                                            (self.scope, self.session, token_hash)).fetchone()
                        if not intent or intent['state'] != 'WAITING':
                            raise ValueError('유효한 확인 대기가 없습니다. 이미 사용한 코드는 다시 주문하지 않습니다.')
                        if now >= intent['expires']:
                            db.execute("UPDATE tg_intents SET state='EXPIRED' WHERE id=?", (intent['id'],))
                            raise ValueError('2분이 지나 확인이 만료됐습니다. 새로 요청하세요.')
                        if verb == '철회':
                            db.execute("UPDATE tg_intents SET state='CANCELED' WHERE id=?", (intent['id'],))
                            reply = '확인 대기를 철회했습니다. 주문은 보내지 않았습니다.'
                        else:
                            request = OrderRequest(**json.loads(intent['payload']))
                            order_safety.reserve(db, self.broker_identity, intent['id'], request, self.clock(), self.limits)
                            db.execute("UPDATE tg_intents SET state='CLAIMED' WHERE id=?", (intent['id'],))
                            action = (intent['id'], request)
                            reply = '확인 처리 중입니다. 결과가 없으면 /상태로 확인하세요. 같은 주문을 다시 요청하지 마세요.'
                    else:
                        if db.execute('SELECT halted FROM tg_risk WHERE broker=?', (self.broker_identity,)).fetchone()[0]:
                            raise OrderError('긴급 중지 상태입니다. 프로그램에서 복구 확인 후 재개해야 합니다.')
                        request = self._order(text)
                        nonce = secrets.token_urlsafe(12)
                        db.execute("UPDATE tg_intents SET state='CANCELED' WHERE scope=? AND state='WAITING'", (self.scope,))
                        db.execute('INSERT INTO tg_intents VALUES(?,?,?,?,?,?,?,?,NULL)',
                                   (uuid.uuid4().hex, self.scope, self.session, hashlib.sha256(nonce.encode()).hexdigest(),
                                    json.dumps(asdict(request)), self.catalog[request.code], now+120, 'WAITING'))
                        side = '매수' if request.side == 'BUY' else '매도'
                        reply = (f'[주문 내용 확인 · 가짜 서버 연습]\n{self.catalog[request.code]} ({request.code}) {side}\n'
                                 f'{request.quantity}주 · 지정가 {request.limit_price:,}원 · 주문금액 {request.quantity*request.limit_price:,}원\n'
                                 f'KRX 형식 연습 · 수수료 미포함 · 실제 돈/키움 모의 주문 아님\n'
                                 f'2분 안에 /확인 {nonce}\n철회: /철회 {nonce}\n새 요청을 보내면 이전 확인은 무효입니다.')
                except (ValueError, OrderError) as exc:
                    # Only locally generated validation errors reach the user.
                    reply = str(exc)
            db.execute('INSERT INTO tg_updates VALUES(?,?,?)', (self.scope, uid, reply))
        if operation:
            report = self.emergency_stop() if operation == '/긴급중지' else self.recovery_status()
            reply = self.safety_text(report)
            with connect(self.path) as db:
                db.execute('UPDATE tg_updates SET reply=? WHERE scope=? AND uid=?', (reply, self.scope, uid))
        if action:
            intent_id, request = action
            state, order_id = 'UNKNOWN', None
            try:
                with connect(self.path) as db:
                    if db.execute('SELECT halted FROM tg_risk WHERE broker=?', (self.broker_identity,)).fetchone()[0]:
                        raise OrderError('긴급 중지 상태입니다.')
                order = self.engine.submit('telegram:'+intent_id, request)
                state, order_id = ('UNKNOWN' if order['state'] == 'UNKNOWN' else 'SENT'), order['id']
                label = ORDER_LABELS.get(order['state'], '확인 필요')
                reply = (f"[가짜 서버 주문 결과]\n처리 상태: {label} · 체결 {order['filled_qty']}/{order['quantity']}주\n"
                         '접수는 매수·매도 체결 완료가 아닙니다. 실제 돈은 사용하지 않았습니다.')
            except OrderError:
                state = 'FAILED'
                reply = '가짜 주문이 접수되지 않았습니다. 연습 잔액·보유 수량·미확인 주문을 확인하세요. 자동 재시도하지 않습니다.'
            except Exception:
                reply = '주문 결과 확인이 필요합니다. 재전송하지 않습니다. /상태로 확인하세요.'
            with connect(self.path) as db:
                db.execute('UPDATE tg_intents SET state=?,order_id=? WHERE id=?', (state, order_id, intent_id))
                db.execute('UPDATE tg_updates SET reply=? WHERE scope=? AND uid=?', (reply, self.scope, uid))
        return reply

    def _engine_rows(self):
        with connect(self.engine.path) as db:
            return [dict(r) for r in db.execute("SELECT * FROM orders WHERE state IN ('SENDING','UNKNOWN','OPEN','PARTIAL')")]

    def safety_banner(self):
        with connect(self.path) as db:
            halted = db.execute('SELECT halted FROM tg_risk WHERE broker=?', (self.broker_identity,)).fetchone()[0]
            used = order_safety.usage(db, self.broker_identity, self.clock())
        orders = self._engine_rows()
        unknown = sum(r['state'] in ('SENDING','UNKNOWN') for r in orders)
        opened = sum(r['state'] in ('OPEN','PARTIAL') for r in orders)
        return (f"긴급 중지 {'유지' if halted else '꺼짐'} · 미확인 {unknown}건 · 미체결 {opened}건\n"
                f"오늘 확인 {used['count']}/{self.limits.daily_count}회 · 누적 {used['notional']:,}/{self.limits.daily_notional:,}원")

    def recovery_status(self, startup=False):
        """Under the receiver owner: read existing broker references, never submit."""
        with self._action_lock:
            if self.engine.broker.environment != 'SIMULATED' or self.engine.broker.identity != self.broker_identity:
                raise OrderError('다른 서버로 복구할 수 없습니다.')
            interrupted = self._engine_rows()
            with connect(self.path) as db:
                claimed = db.execute("SELECT 1 FROM tg_intents WHERE state='CLAIMED'").fetchone()
                if startup and (interrupted or claimed):
                    order_safety.latch(db, self.broker_identity, 'RESTART_REVIEW')
            self.engine.recover()
            for order in self._engine_rows():
                self.engine.reconcile(order['id'])
            # Repair the UI linkage after a crash, without repeating the action.
            with connect(self.path) as db:
                intents = db.execute("SELECT * FROM tg_intents WHERE state IN ('CLAIMED','UNKNOWN')").fetchall()
                for intent in intents:
                    with connect(self.engine.path) as journal:
                        action = journal.execute("SELECT order_id FROM actions WHERE key=?", ('telegram:'+intent['id'],)).fetchone()
                    if action:
                        order = self.engine.get(action['order_id'])
                        state = 'UNKNOWN' if order['state'] in ('UNKNOWN','SENDING') else 'SENT'
                        db.execute('UPDATE tg_intents SET state=?,order_id=? WHERE id=?', (state, order['id'], intent['id']))
                    else:
                        # Confirmed locally but not journaled: discard, do not replay.
                        db.execute("UPDATE tg_intents SET state='CANCELED' WHERE id=?", (intent['id'],))
            remaining = self._engine_rows()
            with connect(self.engine.path) as db:
                uncertain_actions = db.execute("SELECT COUNT(*) FROM actions WHERE state IN ('SENDING','UNKNOWN')").fetchone()[0]
            agrees = self.engine.account() == self.engine.broker.account()
            uncertain = sum(r['state'] in ('SENDING','UNKNOWN') for r in remaining)
            known_refs = {r['id'] for r in remaining}
            foreign = sum(o.client_ref not in known_refs for o in self.engine.broker.open_orders())
            with connect(self.path) as db:
                if uncertain or uncertain_actions or foreign or not agrees:
                    order_safety.latch(db, self.broker_identity, 'UNRESOLVED')
                halted = bool(db.execute('SELECT halted FROM tg_risk WHERE broker=?', (self.broker_identity,)).fetchone()[0])
            return dict(halted=halted, unresolved=max(uncertain, uncertain_actions)+foreign,
                        open_orders=sum(r['state'] in ('OPEN','PARTIAL') for r in remaining), account_matches=agrees)

    def emergency_stop(self):
        # Persist before waiting for a previously confirmed in-flight request.
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            generation = order_safety.latch(db, self.broker_identity, 'USER_EMERGENCY')
        with self._action_lock:
            self.recovery_status()
            for order in self._engine_rows():
                if order['state'] in ('OPEN','PARTIAL'):
                    try:
                        self.engine.cancel('emergency:'+generation+':'+order['id'], order['id'])
                    except OrderError:
                        break  # an unresolved action blocks further mutations
            return self.recovery_status()

    def resume_after_review(self):
        with self._action_lock:
            with connect(self.path) as db:
                revision = db.execute('SELECT revision FROM tg_risk WHERE broker=?', (self.broker_identity,)).fetchone()[0]
            report = self.recovery_status()
            if report['unresolved'] or report['open_orders'] or not report['account_matches']:
                raise OrderError('미확인·미체결 주문 또는 잔액 불일치가 남아 있습니다. 재개하지 않습니다.')
            with connect(self.path) as db:
                db.execute('BEGIN IMMEDIATE')
                owner = db.execute('SELECT session FROM tg_sessions WHERE scope=?', (self.scope,)).fetchone()[0]
                if owner != self.session:
                    raise OrderError('다른 창에서 수신 중입니다. 재개하지 않습니다.')
                current = db.execute('SELECT revision FROM tg_risk WHERE broker=?', (self.broker_identity,)).fetchone()[0]
                if current != revision:
                    raise OrderError('확인 중 새 중지 요청이 들어왔습니다. 중지를 유지합니다.')
                db.execute('UPDATE tg_risk SET halted=0,reason=NULL WHERE broker=?', (self.broker_identity,))
                db.execute('UPDATE tg_sessions SET active=1 WHERE scope=?', (self.scope,))
            return '복구 확인 완료 · 새 연습 주문을 허용했습니다. 오늘 사용한 한도는 그대로 유지합니다.'

    @staticmethod
    def safety_text(report):
        return (f"[가짜 서버 안전 확인]\n긴급 중지: {'유지' if report['halted'] else '꺼짐'}\n"
                f"미확인 {report['unresolved']}건 · 미체결 {report['open_orders']}건\n"
                f"잔액·보유 대조: {'일치' if report['account_matches'] else '불일치'}\n"
                '보유 주식을 강제로 팔지 않습니다. 새 매수·매도 주문을 재전송하지 않았습니다.\n'
                '미확인·미체결이 남으면 해소 후 프로그램에서 복구 확인·재개하세요.')
