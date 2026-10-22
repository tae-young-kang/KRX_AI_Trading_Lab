"""Read-only comparison of supplied first-trade receipts and frozen virtual entries.

A user CSV and its SHA256 value are not signed or authenticated exchange data.
Matching bytes/timestamps are software consistency checks, NOT proof of the
exchange's first trade, order placement, broker execution, or real PnL.
"""
import argparse
import csv
from datetime import date, datetime
import json
import io
from math import isfinite
from pathlib import Path
import re

from app.morning_open_evidence_status import validated_snapshot, session_bounds
from app.morning_virtual import aware
from app.trading_clock import KST

HEADERS = ('day', 'market', 'code', 'venue', 'first_at', 'price_won',
           'evidence_hash', 'capture_status')
HASH = re.compile(r'[a-f0-9]{64}')
CODE = re.compile(r'[A-Za-z0-9]{6}')
MAX_BYTES = 1024 * 1024
MAX_ROWS = 1000
CAPTURE = ('CLAIMED_COMPLETE', 'UNKNOWN')

LABELS = {
    'NO_RECEIPT': '대조 파일에 해당 종목 기록 없음',
    'NO_VIRTUAL_ENTRY': '대조 파일 기록은 있지만 가상 진입 없음',
    'RECEIPT_MISMATCH': '저장 가상 진입과 가격·시각·해시 불일치',
    'USER_CAPTURE_UNVERIFIED': '값은 일치하지만 장 시작부터 수집했다는 근거 미제공',
    'MATCHED_USER_CLAIM': '사용자가 제출한 기록과 일치 · 공식 첫 거래 인증 아님',
}


def read_receipts(path, *, day, now):
    """Strict user-supplied records, without opening a live feed or writing data."""
    if not aware(now):
        raise ValueError('확인 시각에 시간대가 필요해요.')
    path = Path(path)
    found = {}
    try:
        opened,closed=session_bounds(day)
        if not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise ValueError()
        with path.open('rb') as fp:blob=fp.read(MAX_BYTES+1)
        if len(blob)>MAX_BYTES:raise ValueError()
        with io.StringIO(blob.decode('utf-8-sig'),newline='') as fp:
            records = csv.DictReader(fp)
            if records.fieldnames != list(HEADERS):
                raise ValueError()
            for row in records:
                if len(found) >= MAX_ROWS or None in row or any(v is None for v in row.values()):
                    raise ValueError()
                if (row['day'] != day or row['market'] not in ('KOSPI','KOSDAQ')
                        or not CODE.fullmatch(row['code'])
                        or row['venue'] != 'KRX'
                        or not re.fullmatch(r'[1-9][0-9]{0,11}',row['price_won'])
                        or not HASH.fullmatch(row['evidence_hash'])
                        or row['capture_status'] not in CAPTURE):
                    raise ValueError()
                stamp = datetime.fromisoformat(row['first_at'])
                if (not aware(stamp) or stamp.astimezone(KST).date().isoformat()!=day
                        or not opened<=stamp<=closed or stamp > now):
                    raise ValueError()
                key = (row['market'], row['code'])
                if key in found:
                    raise ValueError()
                found[key] = dict(price=int(row['price_won']), first_at=stamp,
                                  hash=row['evidence_hash'],
                                  capture_status=row['capture_status'])
        if not found:
            raise ValueError()
    except (OSError, UnicodeError, csv.Error, TypeError, KeyError, ValueError, OverflowError):
        raise ValueError(
            '첫 거래 CSV 형식·중복·날짜·가격·시간·시장·해시를 확인해 주세요.'
        ) from None
    return found


def compare_saved(root, mode, day, csv_path, *, now):
    """Compare only against a previously saved, read-only morning cohort."""
    if not isinstance(day,str):
        raise ValueError('고정 목록 날짜를 확인해 주세요.')
    with validated_snapshot(root,mode,day,now=now) as snapshot:
        if snapshot is None:return None
        cohort,plan,rows=snapshot
        if cohort['legacy']:return {'legacy':True,'day':day,'mode':mode,'results':[]}
        claims=read_receipts(csv_path,day=day,now=now)
        targets={(x['market'],x['code']):x for x in plan['targets']}
        if any(key not in targets for key in claims):raise ValueError('대조 파일에 고정 추천 목록 밖의 종목이 있어요.')
        saved={r['code']:r for r in rows}
        results = []
        for key, target in targets.items():
            receipt = claims.get(key)
            stored = saved.get(target['code'])
            if receipt is None:
                status = 'NO_RECEIPT'
            elif stored is None:
                status = 'NO_VIRTUAL_ENTRY'
            else:
                try:
                    entry = json.loads(stored['entry_json'])
                    stamp = datetime.fromisoformat(stored['opened_at'])
                    price = stored['buy_ref']
                except (TypeError, ValueError, json.JSONDecodeError):
                    raise ValueError('가상 진입의 저장 형식이 올바르지 않아요.') from None
                if (type(price) not in (int,float) or not isfinite(price)
                        or price != receipt['price']
                        or stamp != receipt['first_at']
                        or entry.get('evidence_hash') != receipt['hash']):
                    status = 'RECEIPT_MISMATCH'
                elif receipt['capture_status'] != 'CLAIMED_COMPLETE':
                    status = 'USER_CAPTURE_UNVERIFIED'
                else:
                    status = 'MATCHED_USER_CLAIM'
            results.append(dict(market=key[0],code=key[1],name=target['name'],status=status))
        counts = {key: sum(row['status']==key for row in results) for key in LABELS}
        return {'legacy': False, 'day': day, 'mode': mode,
                'submitted': len(claims), 'total': len(results),
                'counts': counts, 'results': results}


def render(report, *, max_rows=8):
    if type(max_rows) is not int or not 1 <= max_rows <= 30:
        raise ValueError('표시 종목 수를 확인해 주세요.')
    if report is None:
        return '고정된 아침 추천 목록이 없어 원본을 대조하지 않았어요.'
    if report['legacy']:
        return '이전 NXT/KRX 혼합 고정 목록은 KRX 첫 거래 근거로 소급 평가하지 않아요.'
    c = report['counts']
    lines = [
        f"[사용자 첫 거래 자료 대조] {report['day']} · {report['mode']}",
        f"고정 추천 {report['total']}종목 · 사용자 CSV {report['submitted']}종목",
        f"자료 일치(사용자 주장) {c['MATCHED_USER_CLAIM']} · 수집 완전성 불명 {c['USER_CAPTURE_UNVERIFIED']}",
        f"가격/시각/해시 불일치 {c['RECEIPT_MISMATCH']} · 가상 진입 없음 {c['NO_VIRTUAL_ENTRY']} · CSV 누락 {c['NO_RECEIPT']}",
        '제출 CSV·해시는 인증서가 아닙니다. 거래소 첫 체결·실제 주문·수익률은 증명되지 않았어요.',
    ]
    for row in report['results'][:max_rows]:
        lines.append(f"{row['name']} ({row['code']}): {LABELS[row['status']]}")
    rest = len(report['results'])-max_rows
    if rest>0:
        lines.append(f"나머지 {rest}종목은 화면에서 생략 (최대 {max_rows}종목 표시)")
    lines.append('계좌·가상 장부·시세·주문 DB를 변경하지 않았어요.')
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description='사용자 제출 첫 거래 CSV와 고정 가상 진입 읽기 전용 대조')
    parser.add_argument('--root', required=True)
    parser.add_argument('--mode', required=True, choices=['DEMO','MOCK','REAL_QUOTES'])
    parser.add_argument('--day', required=True)
    parser.add_argument('--csv', required=True)
    args = parser.parse_args(argv)
    print(render(compare_saved(
        args.root,args.mode,args.day,args.csv,now=datetime.now(KST))))


if __name__ == '__main__':
    main()
