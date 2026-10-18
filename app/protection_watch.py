"""Persistent observation-only protection plans. No order dispatch or rearming."""
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from app.order_engine import connect
from app.protection_plan import FilledPosition, ObservedPrice, evaluate, validate_policy


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


class ProtectionWatch:
    def __init__(self,path,*,mode,account_tag):
        if mode not in ('DEMO','MOCK','REAL_QUOTES') or not isinstance(account_tag,str) or not re.fullmatch(r'[a-f0-9]{64}',account_tag):
            raise ValueError('별도 모드와 계좌 구분값이 필요해요.')
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.mode,self._tag=mode,account_tag
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables-{'protection_meta','protection_plans','protection_observations'}:
                raise ValueError('보호 관측 기록은 별도 파일에 저장해야 해요.')
            db.execute('CREATE TABLE IF NOT EXISTS protection_meta(mode TEXT,account_tag TEXT)')
            prior=db.execute('SELECT * FROM protection_meta').fetchone()
            if prior and tuple(prior)!=(mode,account_tag):
                raise ValueError('다른 모드·계좌의 보호 기록이에요.')
            if not prior: db.execute('INSERT INTO protection_meta VALUES(?,?)',(mode,account_tag))
            db.execute('''CREATE TABLE IF NOT EXISTS protection_plans(
                plan_key TEXT PRIMARY KEY,payload TEXT,state TEXT,last_at TEXT,trigger_json TEXT)''')
            db.execute('''CREATE TABLE IF NOT EXISTS protection_observations(
                plan_key TEXT,evidence_hash TEXT,at TEXT,decision_json TEXT,
                PRIMARY KEY(plan_key,evidence_hash))''')

    def register(self,key,*,code,venue,take_bps=200,stop_bps=300):
        validate_policy(take_bps,stop_bps)
        if (not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',key)
                or not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9]{6}',code) or venue not in ('KRX','NXT')):
            raise ValueError('계획 구분값·종목·시장을 확인해 주세요.')
        payload=canonical(dict(code=code,venue=venue,take_bps=take_bps,stop_bps=stop_bps))
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT payload FROM protection_plans WHERE plan_key=?',(key,)).fetchone()
            if prior:
                if prior['payload']!=payload: raise ValueError('같은 계획을 다른 조건으로 덮을 수 없어요.')
                return
            existing=db.execute('SELECT payload FROM protection_plans').fetchall()
            if any(json.loads(row['payload'])['code']==code for row in existing):
                raise ValueError('같은 종목의 보호 계획이 이미 있어요. 중복 계획은 만들지 않아요.')
            db.execute('INSERT INTO protection_plans VALUES(?,?,?,NULL,NULL)',(key,payload,'OBSERVING'))

    def observe(self,key,position,quote,*,now):
        if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('시간대가 있는 관측 시각이 필요해요.')
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            plan=db.execute('SELECT * FROM protection_plans WHERE plan_key=?',(key,)).fetchone()
            if not plan: raise ValueError('저장된 보호 계획이 없어요.')
            cfg=json.loads(plan['payload'])
            if (not isinstance(position,FilledPosition) or (position.mode,position.code,position.venue)!=
                    (self.mode,cfg['code'],cfg['venue'])):
                raise ValueError('계획과 체결 모드·종목·시장이 달라요.')
            if plan['state']=='TRIGGERED':
                return dict(state='TRIGGERED',new_trigger=False,decision=json.loads(plan['trigger_json']),executable=False)
            if plan['last_at'] and now<datetime.fromisoformat(plan['last_at']):
                raise ValueError('이전 관측보다 과거 시각이에요.')
            decision=evaluate(position,quote,now=now,take_bps=cfg['take_bps'],stop_bps=cfg['stop_bps'])
            result=asdict(decision)
            # Same inputs do not generate another record merely because polled later.
            evidence=dict(position=asdict(position),quote=asdict(quote) if isinstance(quote,ObservedPrice) else None)
            if evidence['quote'] is not None:
                for field in ('quote_at','fetched_at'):
                    stamp=evidence['quote'][field]
                    evidence['quote'][field]=stamp.isoformat() if isinstance(stamp,datetime) else None
            digest=hashlib.sha256(canonical(dict(evidence=evidence,decision=result)).encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO protection_observations VALUES(?,?,?,?)',
                       (key,digest,now.isoformat(),canonical(result)))
            triggered=decision.state in ('TAKE_PROFIT','STOP_LOSS')
            state='TRIGGERED' if triggered else 'OBSERVING'
            db.execute('UPDATE protection_plans SET state=?,last_at=?,trigger_json=? WHERE plan_key=?',
                       (state,now.isoformat(),canonical(result) if triggered else None,key))
            return dict(state=state,new_trigger=triggered,decision=result,executable=False)

    def observe_mock(self,key,ledger,capture,quote,*,code,buy_orders,sell_orders=(),now):
        from app.mock_protection import position_from_capture
        if self.mode!='MOCK' or ledger._account_tag!=self._tag:
            raise ValueError('계획과 모의 체결 장부의 계좌가 달라요.')
        position=position_from_capture(ledger,capture,code=code,buy_orders=buy_orders,sell_orders=sell_orders,now=now)
        return self.observe(key,position,quote,now=now)

    def summary(self,*,page=1):
        if type(page) is not int or not 1<=page<=999999:
            raise ValueError('페이지 번호를 확인해 주세요.')
        with connect(self.path) as db:
            db.execute('BEGIN')
            count=db.execute('SELECT COUNT(*) FROM protection_plans').fetchone()[0]
            pages=max(1,(count+4)//5)
            if page>pages: return f'보호 기록은 {pages}페이지까지 있어요. /보호상태 {pages}'
            plans=db.execute('SELECT * FROM protection_plans ORDER BY rowid DESC LIMIT 5 OFFSET ?',((page-1)*5,)).fetchall()
            lines=[f'[{self.mode} · 보호 판단 기록 · {page}/{pages}]',
                   '저장된 관측이에요. 자동 감시·매도 전송은 아직 연결되지 않았어요.']
            for plan in plans:
                cfg=json.loads(plan['payload'])
                latest=db.execute('SELECT decision_json FROM protection_observations WHERE plan_key=? ORDER BY rowid DESC LIMIT 1',
                                  (plan['plan_key'],)).fetchone()
                decision=json.loads(plan['trigger_json'] or latest['decision_json']) if plan['trigger_json'] or latest else None
                lines.append(f"{cfg['code']} · {cfg['venue']} · 익절 +{cfg['take_bps']/100:.2f}% / 손절 -{cfg['stop_bps']/100:.2f}%")
                if decision:
                    label=('조건 도달 기록 · 매도 접수/체결 아님' if plan['state']=='TRIGGERED' else '마지막 판단')
                    lines.append(f"{label}: {decision['reason']} · 당시 수량 {decision['quantity']}주")
                    lines.append('마지막 관측: '+plan['last_at'])
                else:
                    lines.append('아직 관측 기록이 없어요. 계획 등록만으로 보호가 작동하지 않아요.')
            if not plans: lines.append('등록된 보호 계획이 없어요.')
            if page<pages: lines.append(f'다음 기록: /보호상태 {page+1}')
            return '\n'.join(lines)
