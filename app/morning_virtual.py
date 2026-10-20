"""Frozen morning cohort -> venue evidence -> existing paper trades/exits.

No broker client or order interface. FIRST_TRADE is an upstream evidence
assertion; this module cannot certify an exchange feed by itself.
"""
from dataclasses import dataclass
from datetime import date,datetime,time
from decimal import Decimal
import hashlib
import json
from math import isfinite
import re
from app.storage import Database
from app.market import Stock
from app.trade_review import settings_snapshot
from app.theme_calendar import recent_sessions
from app.trading_clock import KST,TradingCalendar
from app.modes import current_mode


@dataclass(frozen=True)
class VenueTrade:
    mode:str
    code:str
    venue:str
    price_won:int
    at:datetime
    fetched_at:datetime
    kind:str
    evidence_hash:str


def aware(value):
    return isinstance(value,datetime) and value.tzinfo is not None and value.utcoffset() is not None


class MorningVirtual:
    def __init__(self,database):
        if type(database) is not Database:raise ValueError('가상 추적 전용 연습 기록이 필요해요.')
        self.db=database
        if database.read_only:return
        with self.db.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS morning_cohort(id INTEGER PRIMARY KEY CHECK(id=1),payload TEXT)')
            db.execute('CREATE TABLE IF NOT EXISTS morning_marks(code TEXT,day TEXT,price INTEGER,at TEXT,evidence_hash TEXT,PRIMARY KEY(code,day))')

    def prepare(self,*,day,mode,origin,targets,settings,total_won,now,selection_info=None):
        target_day=date.fromisoformat(day)
        if (target_day.isoformat()!=day or mode not in ('DEMO','MOCK','REAL_QUOTES') or not aware(now)
                or not isinstance(origin,str) or not re.fullmatch('[A-Za-z0-9_-]{1,100}',origin)
                or type(total_won) is not int or not 1<=total_won<=10**15
                or not isinstance(targets,list) or not 1<=len(targets)<=500):
            raise ValueError('가상 추적 날짜·출처·예산·추천 목록을 확인해 주세요.')
        if day not in recent_sessions(target_day):raise ValueError('확인된 거래일이 아니에요.')
        if now.astimezone(KST)>=datetime.combine(target_day,time(8),KST):
            raise ValueError('첫 거래 뒤 추천을 소급 선택하지 않아요. 다음 거래일을 선택해 주세요.')
        cfg=settings_snapshot(settings)
        if current_mode(cfg).key!=mode:raise ValueError('추천 모드와 가상 추적의 시세 모드가 달라요.')
        for key in ('exit_take_pct','exit_stop_pct','paper_fee_pct','paper_sell_tax_pct','paper_slippage_pct'):
            value=cfg.get(key)
            if type(value) not in (int,float) or not isfinite(value) or not 0<=value<100:
                raise ValueError('가상 손절·익절·비용 설정을 확인해 주세요.')
        if (min(cfg['exit_take_pct'],cfg['exit_stop_pct'])<=0 or
                cfg['paper_fee_pct']+cfg['paper_sell_tax_pct']>=100
                or type(cfg.get('exit_max_days')) is not int or cfg['exit_max_days']<1):
            raise ValueError('가상 손절·익절·보유 기간을 확인해 주세요.')
        chosen=[]
        for row in targets:
            if (not isinstance(row,dict) or not isinstance(row.get('code'),str)
                    or not re.fullmatch('[A-Za-z0-9]{6}',row['code'])
                    or not isinstance(row.get('name'),str) or not 1<=len(row['name'])<=100
                    or row.get('market') not in ('KOSPI','KOSDAQ')):
                raise ValueError('추천 종목의 이름·시장·코드를 확인해 주세요.')
            eligible=row.get('nxt_eligible')
            # Unknown eligibility never silently turns into KRX.
            venue='NXT' if eligible is True else 'KRX' if eligible is False else 'UNKNOWN'
            chosen.append(dict(code=row['code'],name=row['name'],market=row['market'],venue=venue))
        if len({r['code'] for r in chosen})!=len(chosen):raise ValueError('추천 종목이 중복됐어요.')
        payload=dict(version=1,kind='VIRTUAL_ONLY',day=day,mode=mode,origin=origin,frozen_at=now.astimezone(KST).isoformat(),
                     targets=chosen,settings=cfg,total_won=total_won)
        if selection_info is not None:
            if (not isinstance(selection_info,dict) or set(selection_info)!= {'kind','asof'}
                    or selection_info['kind'] not in ('GENERAL','THEME1') or not isinstance(selection_info['asof'],str)):
                raise ValueError('저장 추천의 출처·기준 시각을 확인해 주세요.')
            if selection_info['kind']=='GENERAL':
                stamp=datetime.fromisoformat(selection_info['asof'])
                if not aware(stamp) or stamp>now:raise ValueError('저장 추천 시각이 미래이거나 불명확해요.')
            elif date.fromisoformat(selection_info['asof'])>now.astimezone(KST).date():raise ValueError('테마1 기준일이 미래예요.')
            payload['selection_info']=dict(selection_info)
        encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True)
        with self.db.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT payload FROM morning_cohort WHERE id=1').fetchone()
            if prior and prior['payload']!=encoded:raise ValueError('고정한 아침 추천·설정을 다른 내용으로 덮지 않아요.')
            if not prior:
                if db.execute('SELECT 1 FROM paper_trades').fetchone():raise ValueError('기존 연습과 별도의 아침 추적 기록을 사용해 주세요.')
                db.execute('INSERT INTO morning_cohort VALUES(1,?)',(encoded,))
        return payload

    def plan(self):
        with self.db.connect() as db:row=db.execute('SELECT payload FROM morning_cohort WHERE id=1').fetchone()
        if row is None:raise ValueError('먼저 아침 추천 목록을 고정해 주세요.')
        plan=json.loads(row['payload'])
        if plan.get('kind')!='VIRTUAL_ONLY':raise ValueError('별도의 아침 가상 추적 기록이 필요해요.')
        return plan

    @staticmethod
    def key(plan,code):
        return hashlib.sha256(f'{plan["mode"]}:{plan["day"]}:{plan["origin"]}:{code}'.encode()).hexdigest()

    def _check(self,evidence,now,*,opening):
        plan=self.plan()
        if (type(evidence) is not VenueTrade or not aware(now) or not aware(evidence.at) or not aware(evidence.fetched_at)
                or evidence.mode!=plan['mode'] or type(evidence.price_won) is not int or not 0<evidence.price_won<10**12
                or not isinstance(evidence.evidence_hash,str) or not re.fullmatch('[a-f0-9]{64}',evidence.evidence_hash)
                or not evidence.at<=evidence.fetched_at<=now or not 0<=(now-evidence.at).total_seconds()<=30
                or evidence.at.astimezone(KST).date()!=now.astimezone(KST).date()
                or evidence.kind!=('FIRST_TRADE' if opening else 'TRADE')):
            raise ValueError('같은 모드의 시각·가격·거래 근거를 확인해 주세요.')
        target=next((r for r in plan['targets'] if r['code']==evidence.code),None)
        if target is None or target['venue']=='UNKNOWN' or target['venue']!=evidence.venue:
            raise ValueError('추천 목록과 확인한 NXT/KRX 거래 시장이 달라요. 미확인 시장은 진입하지 않아요.')
        day=evidence.at.astimezone(KST).date()
        if (day.isoformat() not in recent_sessions(day) or not TradingCalendar(open_dates={day}).is_open(evidence.at,evidence.venue)
                or (opening and day.isoformat()!=plan['day'])):
            raise ValueError('추천일의 확인된 거래 구간을 확인해 주세요.')
        return plan,target

    def enter(self,evidence,*,now):
        plan,target=self._check(evidence,now,opening=True)
        cfg=plan['settings'];allocation=plan['total_won']//len(plan['targets'])
        unit=Decimal(evidence.price_won)*(1+Decimal(str(cfg['paper_slippage_pct']))/100)*(1+Decimal(str(cfg['paper_fee_pct']))/100)
        quantity=int(Decimal(allocation)//unit)
        # Match the existing ledger's floating cost arithmetic conservatively.
        while quantity and evidence.price_won*(1+cfg['paper_slippage_pct']/100)*quantity*(1+cfg['paper_fee_pct']/100)>allocation:
            quantity-=1
        if not quantity:return dict(state='BELOW_ONE_SHARE',quantity=0)
        stock=Stock(target['code'],target['name'],target['market'],evidence.price_won,0,0,None,None,
                    source='DEMO' if plan['mode']=='DEMO' else 'KIWOOM',stamp=evidence.fetched_at.isoformat(),quote_stamp=evidence.at.isoformat())
        fill=self.db.paper_open(stock,quantity,cfg['exit_stop_pct'],cfg['exit_take_pct'],cfg['exit_max_days'],
            cfg['paper_fee_pct'],cfg['paper_slippage_pct'],cfg,len(plan['targets']),
            entry_key=self.key(plan,target['code']),observed_at=evidence.at,venue=evidence.venue,price_basis='VENUE_FIRST_TRADE',evidence_hash=evidence.evidence_hash)
        self._record_mark(evidence)
        return dict(state='VIRTUAL_ENTERED',quantity=quantity,fill=fill,venue=evidence.venue)

    def _record_mark(self,evidence):
        stamp=evidence.at.astimezone(KST).isoformat()
        with self.db.connect() as db:
            db.execute('''INSERT INTO morning_marks VALUES(?,?,?,?,?) ON CONFLICT(code,day) DO UPDATE SET
                price=excluded.price,at=excluded.at,evidence_hash=excluded.evidence_hash WHERE excluded.at>=morning_marks.at''',
                (evidence.code,stamp[:10],evidence.price_won,stamp,evidence.evidence_hash))

    def mark(self,evidence,*,now):
        plan,target=self._check(evidence,now,opening=False)
        key=self.key(plan,target['code'])
        with self.db.connect() as db:trade=db.execute('SELECT * FROM paper_trades WHERE entry_key=?',(key,)).fetchone()
        if trade is None:return dict(state='WAITING_FIRST_TRADE')
        if trade['closed_at']:return dict(state='ALREADY_CLOSED',pnl=trade['pnl'])
        if evidence.at<datetime.fromisoformat(trade['opened_at']):raise ValueError('진입보다 과거 시세예요.')
        with self.db.connect() as db:
            latest=db.execute('SELECT at FROM morning_marks WHERE code=? ORDER BY day DESC LIMIT 1',(evidence.code,)).fetchone()
        if latest and evidence.at<datetime.fromisoformat(latest['at']):raise ValueError('마지막 관측보다 과거 시세예요.')
        self._record_mark(evidence)
        cfg=plan['settings']
        change=(Decimal(evidence.price_won)/Decimal(str(trade['buy_fill']))-1)*100
        age=(evidence.at.astimezone(KST).date()-datetime.fromisoformat(trade['opened_at']).date()).days
        reason=('가상 손절' if change<=-Decimal(str(trade['stop_pct'])) else
                '가상 익절' if change>=Decimal(str(trade['take_pct'])) else
                '가상 보유기간 청산' if age>=trade['max_days'] else None)
        if reason is None:return dict(state='HOLD')
        pnl=self.db.paper_close(trade['id'],evidence.price_won,reason,cfg['paper_fee_pct'],cfg['paper_sell_tax_pct'],
            cfg['paper_slippage_pct'],request_key='morning_exit_'+key,observed_at=evidence.at)
        return dict(state='VIRTUAL_CLOSED',reason=reason,pnl=pnl)

    def daily_text(self,day,page=1):
        plan=self.plan()
        report=self.db.practice_daily_report(day,page=page)
        with self.db.connect() as db:
            count=db.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0]
        header=(f'[아침 추천 가상 추적] 목록 {plan["day"]} · {plan["mode"]}\n'
                '실제 계좌·키움 모의계좌 주문/체결이 아닙니다. 공급된 거래 근거를 사용한 가상 계산입니다.\n'
                f'고정 추천 {len(plan["targets"])}종목 · 진입 기록 {count}종목 · 미진입 {len(plan["targets"])-count}종목\n')
        if not count:
            return header+'첫 거래·시장 근거 대기 또는 예산 미달로 진입 기록이 없어요. 손익을 계산하지 않았으며 0% 수익이 아닙니다.'
        cfg=plan['settings'];unrealized=0;missing=0;open_count=0
        with self.db.connect() as db:
            trades=db.execute('SELECT * FROM paper_trades WHERE substr(opened_at,1,10)<=?',(day,)).fetchall()
            for trade in trades:
                sold=db.execute('SELECT COALESCE(SUM(qty),0) FROM paper_exits WHERE trade_id=? AND substr(sold_at,1,10)<=?',(trade['id'],day)).fetchone()[0]
                remaining=trade['qty']-sold
                if remaining<=0:continue
                open_count+=1
                mark=db.execute('SELECT price FROM morning_marks WHERE code=? AND day=?',(trade['code'],day)).fetchone()
                if not mark:missing+=1;continue
                proceeds=mark['price']*(1-cfg['paper_slippage_pct']/100)*remaining
                cost=(trade['buy_fill']*trade['qty']+trade['buy_fee'])*remaining/trade['qty']
                unrealized+=proceeds*(1-(cfg['paper_fee_pct']+cfg['paper_sell_tax_pct'])/100)-cost
        estimate='미확인' if missing else f'{unrealized:+,.2f}원'
        return (header+
                f'해당일 보유 {open_count}종목 · 미실현손익 추정 {estimate} · 당일 가격 없음 {missing}종목\n'
                '미실현은 당일 마지막 관측가와 가정 비용 기준이며 확정 손익이 아닙니다.\n'+report)


def record_path(root,mode,day):
    if mode not in ('DEMO','MOCK','REAL_QUOTES') or date.fromisoformat(day).isoformat()!=day:
        raise ValueError('가상 추적 모드와 날짜를 확인해 주세요.')
    return root/'morning_virtual'/mode/(day+'.db')


def report_saved(root,mode,day,*,page=1):
    path=record_path(root,mode,day)
    if not path.is_file():return '이 날짜의 아침 가상 추적 기록이 없어요. 실제 체결 손익과 별도예요.'
    tracker=MorningVirtual(Database(path,read_only=True))
    if tracker.plan()['mode']!=mode or tracker.plan()['day']!=day:raise ValueError('다른 날짜·시세 모드의 가상 추적 기록이에요.')
    return tracker.daily_text(day,page=page)
