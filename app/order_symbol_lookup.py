"""Resolve exact names from today's same-server saved quotes; no network."""
from datetime import datetime
import re
from app.theme_sources import KIWOOM_SOURCES
from app.trading_clock import KST


def saved_symbol(history,mode,name,now):
    if (mode not in KIWOOM_SOURCES or not isinstance(name,str) or not 1<=len(name)<=100
            or not name.isprintable() or now.tzinfo is None or now.utcoffset() is None):
        raise ValueError('종목명과 조회 시세 모드를 확인해 주세요.')
    matches=set()
    with history.connect() as db:
        tables=['daily_bars']
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='theme_pruning_bars' AND type='table'").fetchone():
            tables.append('theme_pruning_bars')
        for table in tables:
            for row in db.execute(f'SELECT code,market,name,fetched_at FROM {table} WHERE source=? AND name=?',
                                  (KIWOOM_SOURCES[mode],name)):
                try:
                    stamp=datetime.fromisoformat(row['fetched_at'])
                    if (stamp.tzinfo is None or stamp.utcoffset() is None or stamp>now
                            or stamp.astimezone(KST).date()!=now.astimezone(KST).date()):continue
                    if not re.fullmatch('[A-Za-z0-9]{6}',row['code']) or row['market'] not in ('KOSPI','KOSDAQ'):raise ValueError()
                    matches.add((row['code'],row['market']))
                except (TypeError,ValueError):
                    raise ValueError('저장된 종목 정보가 불명확해요. 6자리 종목코드로 입력해 주세요.') from None
    if not matches:
        raise ValueError('오늘 같은 시세 모드에서 확인한 종목명이 없어요. 테마1 검색을 완료하거나 6자리 종목코드로 입력해 주세요.')
    if len(matches)!=1:raise ValueError('같은 이름의 종목이 여러 개예요. 6자리 종목코드로 입력해 주세요.')
    code,market=matches.pop()
    return dict(code=code,name=name,market=market)
