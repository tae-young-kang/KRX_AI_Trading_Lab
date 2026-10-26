"""User-supplied real fills, not broker verification or order execution."""
import argparse
import csv
import hashlib
import io
import json
import re
import sqlite3
from contextlib import closing,contextmanager
from datetime import datetime,date
from pathlib import Path
from app.trading_clock import KST
from app.runtime_lock import ReceiverLock,ReceiverAlreadyRunning
from app.morning_quote_router import _safe_path

FIELDS=('fill_id','filled_at','code','name','venue','side','quantity','price_won','realized_pnl_won','reason')


def normalize(row,now):
    error='실제 체결 입력의 항목·시간·금액을 확인해 주세요.'
    if set(row)!=set(FIELDS):raise ValueError(error)
    row=dict(row)
    if (not isinstance(row['fill_id'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',row['fill_id'])
            or not isinstance(row['code'],str) or not re.fullmatch(r'[A-Za-z0-9]{6}',row['code'])
            or not isinstance(row['name'],str) or not 1<=len(row['name'])<=100
            or any(ord(c)<32 for c in row['name']) or row['venue'] not in ('KRX','NXT','UNKNOWN')
            or row['side'] not in ('BUY','SELL')
            or row['reason'] not in ('TAKE_PROFIT','STOP_LOSS','OTHER','UNKNOWN')):
        raise ValueError(error)
    try:stamp=datetime.fromisoformat(row['filled_at'])
    except (ValueError,TypeError):raise ValueError(error) from None
    if stamp.tzinfo is None or stamp.utcoffset() is None or stamp>now:raise ValueError(error)
    row['filled_at']=stamp.astimezone(KST).isoformat()
    for field in ('quantity','price_won','realized_pnl_won'):
        value=row[field]
        if field=='realized_pnl_won' and value=='':row[field]=None;continue
        pattern=r'-?[0-9]{1,15}' if field=='realized_pnl_won' else r'[1-9][0-9]{0,11}'
        if not isinstance(value,str) or not re.fullmatch(pattern,value):raise ValueError(error)
        row[field]=int(value)
    if row['side']=='BUY' and row['realized_pnl_won'] is not None:raise ValueError(error)
    return row


@contextmanager
def input_lock(path):
    path=Path(path);lock=path.parent/(path.name+'.import.lock')
    _safe_path(path.parent,path);_safe_path(path.parent,lock)
    try:
        with ReceiverLock(lock):yield
    except ReceiverAlreadyRunning:
        raise ValueError('추천 분류 확인 또는 체결 입력이 진행 중이에요. 잠시 뒤 다시 입력하세요.') from None


def import_csv(path,blob,*,now):
    """Atomic import. Account/credential columns are not accepted or retained."""
    if not isinstance(now,datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('시간대가 있는 현재 시각이 필요해요.')
    if not isinstance(blob,bytes) or not 0<len(blob)<=5_000_000:raise ValueError('체결 입력 파일 크기를 확인해 주세요.')
    try:
        reader=csv.DictReader(io.StringIO(blob.decode('utf-8-sig')))
        if reader.fieldnames is None or len(reader.fieldnames)!=len(FIELDS) or set(reader.fieldnames)!=set(FIELDS):raise ValueError()
        rows=[normalize(row,now) for row in reader]
        if not rows or len(rows)>10000 or len({r['fill_id'] for r in rows})!=len(rows):raise ValueError()
    except (UnicodeError,ValueError,csv.Error,TypeError):
        raise ValueError('실제 체결 CSV 형식이 맞지 않아요. 계좌번호·키를 넣지 말고 정해진 항목만 사용해 주세요.') from None
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    added=0
    evidence_hash=hashlib.sha256(blob).hexdigest()
    with input_lock(path),closing(sqlite3.connect(path)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables-{'actual_meta','actual_fills'}:raise ValueError('실제 체결은 별도 기록 파일에 저장해야 해요.')
        db.execute('CREATE TABLE IF NOT EXISTS actual_meta(kind TEXT PRIMARY KEY)')
        if db.execute('SELECT kind FROM actual_meta').fetchall() not in ([],[('REAL_USER_REPORTED',)]):
            raise ValueError('실제 체결 기록 구분이 달라요.')
        db.execute("INSERT OR IGNORE INTO actual_meta VALUES('REAL_USER_REPORTED')")
        db.execute('CREATE TABLE IF NOT EXISTS actual_fills(id TEXT PRIMARY KEY,day TEXT,payload TEXT,evidence_hash TEXT)')
        db.execute('CREATE INDEX IF NOT EXISTS actual_fills_day ON actual_fills(day)')
        for row in rows:
            body=json.dumps(row,ensure_ascii=False,sort_keys=True)
            prior=db.execute('SELECT payload FROM actual_fills WHERE id=?',(row['fill_id'],)).fetchone()
            if prior:
                if prior[0]!=body:raise ValueError('같은 체결 번호의 내용이 달라요. 기존 기록을 덮지 않았어요.')
                continue
            db.execute('INSERT INTO actual_fills VALUES(?,?,?,?)',(row['fill_id'],row['filled_at'][:10],body,evidence_hash))
            added+=1
    return added


def daily_text(path,day,*,page=1):
    if not isinstance(day,str) or date.fromisoformat(day).isoformat()!=day or type(page) is not int or page<1:
        raise ValueError('날짜와 페이지를 확인해 주세요.')
    path=Path(path)
    if not path.is_file():return '실제 체결 입력 기록이 없어요. 가상 손익이나 키움 모의 손익과 구분해 보관합니다.'
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if db.execute('SELECT kind FROM actual_meta').fetchall()!=[('REAL_USER_REPORTED',)]:raise ValueError('실제 체결 기록 구분이 달라요.')
        rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM actual_fills WHERE day=? ORDER BY id',(day,))]
    groups={}
    for row in rows:
        bucket=groups.setdefault(row['code'],dict(name=row['name'],buy=0,sell=0,pnl=0,unknown=0,sales=0,venues=set(),reasons=[]))
        bucket['venues'].add(row['venue'])
        bucket['buy' if row['side']=='BUY' else 'sell']+=row['quantity']*row['price_won']
        if row['side']=='SELL':
            bucket['sales']+=1;bucket['reasons'].append(row['reason'])
            if row['realized_pnl_won'] is None:bucket['unknown']+=1
            else:bucket['pnl']+=row['realized_pnl_won']
    known=[b for b in groups.values() if b['sales'] and not b['unknown']]
    unknown=sum(b['unknown'] for b in groups.values())
    wins=sum(b['pnl']>0 for b in known);losses=sum(b['pnl']<0 for b in known);draws=len(known)-wins-losses
    pages=max(1,(len(groups)+4)//5)
    if page>pages:return f'마지막 페이지는 {pages}예요.'
    pnl=('매도 기록 없음' if not any(b['sales'] for b in groups.values()) else
         '미확인' if unknown else f'{sum(b["pnl"] for b in groups.values()):+,}원')
    rate=f'{wins/len(known)*100:.1f}%' if known else '미확인'
    sales=[r for r in rows if r['side']=='SELL']
    lines=[f'[실제 체결 입력 · 하루 기록] {day} · {page}/{pages}',
        '사용자가 제공한 내역이며 증권사 자동 대조 전이에요. 실제 주문을 실행하지 않아요.',
        f'매수금액 {sum(b["buy"] for b in groups.values()):,}원 · 매도금액 {sum(b["sell"] for b in groups.values()):,}원 (비용 차감 전)',
        f'입력된 실현손익 합계 {pnl} · 손익 없는 매도 체결 {unknown}건',
        f'당일 매도 종목: 승 {wins} / 패 {losses} / 본전 {draws} · 승률 {rate}',
        f'입력된 매도 사유: 익절 {sum(r["reason"]=="TAKE_PROFIT" for r in sales)}건 · 손절 {sum(r["reason"]=="STOP_LOSS" for r in sales)}건 · 기타/미확인 {sum(r["reason"] in ("OTHER","UNKNOWN") for r in sales)}건 (체결 건수)',
        '승패는 당일 매도한 종목별 입력 손익 기준이며 전량 청산 여부·미실현손익은 별도예요.',
        '손익 누락 종목은 승률에서 제외해요. 누락 손익을 0으로 계산하지 않아요.']
    if not rows:lines.append('이 날짜의 입력 기록이 없어요. 0원 성과로 해석하지 마세요.')
    for code,b in sorted(groups.items())[(page-1)*5:page*5]:
        amount='미확인' if b['unknown'] or not b['sales'] else f'{b["pnl"]:+,}원'
        venues='/'.join('시장 미확인' if v=='UNKNOWN' else v for v in sorted(b['venues']))
        lines.extend([f'{b["name"]} ({code}) · {venues}',f'매수 {b["buy"]:,}원 · 매도 {b["sell"]:,}원 · 입력 손익 {amount}'])
    if page<pages:lines.append(f'다음: /실제결산 {day} {page+1}')
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description='실제 체결 CSV를 로컬에 기록합니다. 주문·통신 없음.')
    parser.add_argument('csv_file')
    args=parser.parse_args()
    from app.config import data_dir
    try:
        added=import_csv(data_dir()/'actual_fill_records.db',Path(args.csv_file).read_bytes(),now=datetime.now(KST))
    except (ValueError,OSError,sqlite3.Error):
        print('체결 입력에 실패했어요. CSV 형식·중복 내용과 기록 파일을 확인해 주세요.')
        return 1
    print(f'실제 체결 입력 {added}건 추가. 증권사 자동 대조·주문 실행은 하지 않았어요.')
    return 0


if __name__=='__main__':raise SystemExit(main())
