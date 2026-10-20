"""Authorized Telegram condition commands -> GUI-owned, confirmed queue.

No scan, notification, credential, mode switch, or order execution here.
"""
import hashlib
import json
import math
import re
import secrets
import time

from app.order_engine import connect
from app.modes import current_mode

NUMBERS = {
    '최저가': ('min_price', 0, 1000000), '최고가': ('max_price', 100, 10000000),
    '시총최소': ('min_cap_eok', 0, 10000000), '시총최대': ('max_cap_eok', 0, 10000000),
    '거래대금': ('min_value_eok', 0, 100000), '거래량비율': ('min_vol_ratio', 0, 5000),
    '등락최소': ('min_change_pct', -30, 30), '등락최대': ('max_change_pct', -30, 30),
    '추천수': ('recommend_count', 1, 50),
}
SWITCHES = {'가격사용': 'on_price', '시총사용': 'on_cap', '거래대금사용': 'on_value',
            '거래량사용': 'on_volratio', '등락사용': 'on_change'}
KEYS = ('market', 'include_etf', *[v[0] for v in NUMBERS.values()], *SWITCHES.values())
HELP = ('/조건: 지금 추천 기준 보기\n'
        '/조건기록: 이전 추천 조건 보기 · /조건복귀 기록번호: 확인 후 되돌리기\n'
        '/조건변경 시장=코스닥 ETF=제외 거래대금=100\n'
        '시장: 코스피/코스닥/전체 · ETF: 포함/제외\n'
        '거래대금·시총: 억원, 가격: 원, 거래량비율·등락: %\n'
        '최저가·최고가·시총최소·시총최대·거래량비율·등락최소·등락최대·추천수도 변경 가능\n'
        '가격사용·시총사용·거래대금사용·거래량사용·등락사용: 켜기/끄기\n'
        '내용 확인 → /조건확인 코드 · 철회: /조건철회 코드\n'
        '추천 조건만 바뀝니다. 주문하거나 자동 검색을 시작하지 않아요.')


def state(config):
    return dict(mode=current_mode(config).key, version=config['strategy_version'],
                conditions={key: config[key] for key in KEYS})


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def describe(value):
    c = value['conditions']
    active = lambda key: '사용' if c[key] else '사용 안 함'
    mode = {'DEMO': '가상 연습', 'MOCK': '키움 모의 시세', 'REAL_QUOTES': '실제 시세'}[value['mode']]
    market = {'전체': '코스피+코스닥 전체', 'KOSPI': '코스피 전체', 'KOSDAQ': '코스닥 전체'}[c['market']]
    return (f"[{mode} · 추천 조건 {value['version']}번]\n{market} · ETF {'포함' if c['include_etf'] else '제외'}\n"
        f"가격 {c['min_price']:g}~{c['max_price']:g}원 ({active('on_price')})\n"
        f"시가총액 {c['min_cap_eok']:g}~{c['max_cap_eok']:g}억원 ({active('on_cap')})\n"
        f"거래대금 {c['min_value_eok']:g}억원 이상 ({active('on_value')})\n"
        f"거래량비율 {c['min_vol_ratio']:g}% 이상 ({active('on_volratio')})\n"
        f"등락률 {c['min_change_pct']:g}~{c['max_change_pct']:g}% ({active('on_change')})\n"
        f"추천 최대 {c['recommend_count']}개 · /조건도움말")


def patch(text):
    result, seen = {}, set()
    for token in text.split():
        if token.count('=') != 1:
            raise ValueError('항목=값으로 적어 주세요. 예: 시장=코스닥 거래대금=100')
        label, value = token.split('=')
        if label in seen:
            raise ValueError('같은 항목은 한 번만 적어 주세요.')
        seen.add(label)
        if label == '시장':
            choices = {'코스피':'KOSPI', '코스닥':'KOSDAQ', '전체':'전체', '코스피코스닥':'전체'}
            if value not in choices:
                raise ValueError('시장은 코스피, 코스닥, 전체 중에 골라 주세요.')
            result['market'] = choices[value]
        elif label == 'ETF' or label in SWITCHES:
            choices = {'포함':True, '제외':False} if label == 'ETF' else {'켜기':True, '끄기':False}
            if value not in choices:
                raise ValueError('ETF는 포함/제외, 조건 사용은 켜기/끄기로 적어 주세요.')
            result['include_etf' if label == 'ETF' else SWITCHES[label]] = choices[value]
        elif label in NUMBERS:
            key, low, high = NUMBERS[label]
            if not re.fullmatch(r'-?[0-9]+(?:\.[0-9]{1,2})?', value):
                raise ValueError('숫자는 단위 없이 소수 둘째 자리까지 적어 주세요.')
            number = float(value)
            if not math.isfinite(number) or not low <= number <= high or (key == 'recommend_count' and not number.is_integer()):
                raise ValueError('프로그램 화면에서 설정할 수 있는 범위로 적어 주세요. 추천수는 1~50 사이 정수예요.')
            result[key] = int(number) if key == 'recommend_count' else number
        else:
            raise ValueError('바꿀 수 없는 항목이에요. /조건도움말로 항목을 확인해 주세요.')
    if not result:
        raise ValueError('바꿀 항목을 적어 주세요. 예: /조건변경 거래대금=100')
    return result


def validate_conditions(c):
    if not isinstance(c,dict) or set(c)!=set(KEYS):raise ValueError('이전 추천 조건의 항목을 확인해 주세요.')
    if c['market'] not in ('전체','KOSPI','KOSDAQ') or type(c['include_etf']) is not bool:
        raise ValueError('이전 시장·ETF 조건을 확인해 주세요.')
    if any(type(c[key]) is not bool for key in SWITCHES.values()):raise ValueError('이전 조건 사용 여부를 확인해 주세요.')
    for key,low,high in NUMBERS.values():
        if (type(c[key]) not in (int,float) or not math.isfinite(c[key]) or not low<=c[key]<=high
                or key=='recommend_count' and type(c[key]) is not int):
            raise ValueError('이전 추천 조건의 숫자 범위를 확인해 주세요.')
    for lower,upper in (('min_price','max_price'),('min_cap_eok','max_cap_eok'),('min_change_pct','max_change_pct')):
        if c[lower]>c[upper]:raise ValueError('이전 조건의 최소·최대 값을 확인해 주세요.')


def record_version(db,current,now):
    """Explicit recommendation whitelist only, shared across process restarts."""
    payload=encode(current['conditions'])
    last=db.execute('SELECT version,payload FROM condition_versions WHERE mode=? ORDER BY id DESC LIMIT 1',(current['mode'],)).fetchone()
    if last is None or last['version']!=current['version'] or last['payload']!=payload:
        db.execute('INSERT INTO condition_versions(mode,version,payload,created) VALUES(?,?,?,?)',
            (current['mode'],current['version'],payload,now))


class ConditionCommands:
    def __init__(self, path, config):
        self.path = path
        self.owner = secrets.token_hex(16)
        with connect(path) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS condition_current(id TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE IF NOT EXISTS condition_requests(token TEXT PRIMARY KEY,scope TEXT,session TEXT,
                    base TEXT,target TEXT,expires REAL,state TEXT);
                CREATE TABLE IF NOT EXISTS condition_replies(scope TEXT,session TEXT,uid INTEGER,reply TEXT,
                    PRIMARY KEY(scope,session,uid));
                CREATE TABLE IF NOT EXISTS condition_versions(id INTEGER PRIMARY KEY,mode TEXT,version INTEGER,payload TEXT,created REAL);
            ''')
            record_version(db,self._state(config),time.time())
            db.execute('INSERT OR REPLACE INTO condition_current VALUES (?,?)', (self.owner,encode(self._state(config))))

    def _state(self, config):
        return {**state(config), 'owner': self.owner}

    def cancel_session(self, scope, session):
        with connect(self.path) as db:
            db.execute("UPDATE condition_requests SET state='EXPIRED' WHERE scope=? AND session=? AND state IN ('PREVIEW','QUEUED')", (scope,session))

    def handle(self, text, *, scope, session, uid, now):
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT reply FROM condition_replies WHERE scope=? AND session=? AND uid=?',
                                  (scope,session,uid)).fetchone()
            if previous:
                return previous[0]
            current = json.loads(db.execute('SELECT payload FROM condition_current WHERE id=?', (self.owner,)).fetchone()[0])
            try:
                if text == '/조건':
                    last = db.execute('SELECT state FROM condition_requests WHERE scope=? AND session=? ORDER BY rowid DESC LIMIT 1',
                                      (scope,session)).fetchone()
                    status = {'QUEUED':'변경 반영 대기 중', 'APPLIED':'최근 변경 적용됨', 'REJECTED':'최근 변경 취소됨',
                              'EXPIRED':'이전 변경 만료', 'PREVIEW':'변경안 확인 대기'}.get(last[0], '') if last else ''
                    reply = describe(current) + ('\n'+status if status else '')
                elif text == '/조건도움말':
                    reply = HELP
                elif text.split(' ',1)[0]=='/조건기록':
                    match=re.fullmatch(r'/조건기록(?: ([1-9][0-9]{0,5}))?',text)
                    if not match:raise ValueError('사용법: /조건기록 또는 /조건기록 페이지')
                    page=int(match[1] or 1)
                    count=db.execute('SELECT COUNT(*) FROM condition_versions WHERE mode=?',(current['mode'],)).fetchone()[0]
                    pages=max(1,(count+4)//5)
                    if page>pages:raise ValueError(f'조건 기록은 {pages}페이지까지 있어요.')
                    rows=db.execute('SELECT * FROM condition_versions WHERE mode=? ORDER BY id DESC LIMIT 5 OFFSET ?',
                        (current['mode'],(page-1)*5)).fetchall()
                    lines=[f'[이전 추천 조건] {page}/{pages}페이지 · 현재 시세 모드만 표시',
                        '복귀는 추천 기준만 바꿔요. 익절·손절·비용·모드·테마1 기준은 바꾸지 않아요.']
                    for row in rows:
                        conditions=json.loads(row['payload']);validate_conditions(conditions)
                        lines.append(f'기록 {row["id"]} · '+describe(dict(mode=current['mode'],version=row['version'],conditions=conditions)))
                        lines.append(f'되돌리기: /조건복귀 {row["id"]}')
                    if page<pages:lines.append(f'다음: /조건기록 {page+1}')
                    reply='\n'.join(lines)
                elif text.startswith('/조건변경') or text.split(' ',1)[0]=='/조건복귀':
                    restoring=text.split(' ',1)[0]=='/조건복귀'
                    if restoring:
                        match=re.fullmatch(r'/조건복귀 ([1-9][0-9]{0,8})',text)
                        if not match:raise ValueError('사용법: /조건복귀 기록번호 · 기록 찾기: /조건기록')
                        row=db.execute('SELECT payload FROM condition_versions WHERE id=? AND mode=?',(int(match[1]),current['mode'])).fetchone()
                        if row is None:raise ValueError('현재 시세 모드에 없는 조건 기록이에요. /조건기록에서 골라 주세요.')
                        changes=json.loads(row['payload']);validate_conditions(changes)
                        if changes==current['conditions']:raise ValueError('현재 추천 조건과 같아요. 변경하지 않았어요.')
                    else:changes = patch(text[len('/조건변경'):].strip())
                    target = json.loads(encode(current))
                    target['conditions'].update(changes)
                    for lower, upper in (('min_price','max_price'),('min_cap_eok','max_cap_eok'),('min_change_pct','max_change_pct')):
                        if target['conditions'][lower] > target['conditions'][upper]:
                            raise ValueError('최소값은 최대값보다 클 수 없어요.')
                    target['version'] += 1
                    nonce = secrets.token_urlsafe(12)
                    digest = hashlib.sha256(nonce.encode()).hexdigest()
                    db.execute("UPDATE condition_requests SET state='EXPIRED' WHERE scope=? AND session=? AND state='PREVIEW'", (scope,session))
                    db.execute('INSERT INTO condition_requests VALUES (?,?,?,?,?,?,?)',
                               (digest,scope,session,encode(current),encode(target),now+120,'PREVIEW'))
                    heading=('이전 추천 기준으로 되돌릴까요? 익절·손절·테마1은 바뀌지 않아요.\n' if restoring else '이렇게 바꿀까요?\n')
                    reply = heading+describe(target)+f'\n2분 안에 /조건확인 {nonce}\n철회: /조건철회 {nonce}'
                else:
                    match = re.fullmatch(r'/(조건확인|조건철회) ([A-Za-z0-9_-]{16})', text)
                    if not match:
                        raise ValueError('명령을 확인해 주세요. /조건도움말')
                    digest = hashlib.sha256(match[2].encode()).hexdigest()
                    row = db.execute('SELECT * FROM condition_requests WHERE token=? AND scope=? AND session=?',
                                     (digest,scope,session)).fetchone()
                    if not row or row['state'] != 'PREVIEW' or now >= row['expires']:
                        raise ValueError('확인 코드가 만료됐거나 이미 사용됐어요. 새 변경안을 만들어 주세요.')
                    if row['base'] != encode(current):
                        db.execute("UPDATE condition_requests SET state='REJECTED' WHERE token=?", (digest,))
                        raise ValueError('그사이 조건이나 모드가 바뀌었어요. /조건으로 확인 후 다시 요청해 주세요.')
                    next_state = 'QUEUED' if match[1] == '조건확인' else 'REJECTED'
                    db.execute('UPDATE condition_requests SET state=? WHERE token=?', (next_state,digest))
                    reply = ('변경을 예약했어요. 현재 작업이 끝나면 반영해요. 화면에서 조건을 수정 중이면 먼저 저장해 주세요. /조건으로 결과를 확인해 주세요.'
                             if next_state == 'QUEUED' else '변경을 철회했어요.')
            except ValueError as error:
                reply = str(error)
            db.execute('INSERT INTO condition_replies VALUES (?,?,?,?)', (scope,session,uid,reply))
            return reply

    def sync(self, config, save, *, now, busy=False):
        """GUI thread only; no worker mutates GUI settings. Queues expire safely."""
        changed = False
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE condition_requests SET state='EXPIRED' WHERE state IN ('PREVIEW','QUEUED') AND expires<=?", (now,))
            current = self._state(config)
            record_version(db,current,now)
            if not busy:
                for row in db.execute("SELECT * FROM condition_requests WHERE state='QUEUED' ORDER BY rowid").fetchall():
                    target = json.loads(row['target'])
                    if target['owner'] != self.owner:
                        continue
                    if encode(current) != row['target']:
                        if encode(current) != row['base']:
                            db.execute("UPDATE condition_requests SET state='REJECTED' WHERE token=?", (row['token'],))
                            continue
                        before = dict(config)
                        config.update(target['conditions'])
                        config['strategy_version'] = target['version']
                        try:
                            save()
                        except Exception:
                            config.clear()
                            config.update(before)
                            raise
                        current, changed = self._state(config), True
                    record_version(db,current,now)
                    db.execute("UPDATE condition_requests SET state='APPLIED' WHERE token=?", (row['token'],))
            db.execute('INSERT OR REPLACE INTO condition_current VALUES (?,?)', (self.owner,encode(self._state(config))))
        return changed
