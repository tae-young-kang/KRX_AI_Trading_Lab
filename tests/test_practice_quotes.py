from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from app.practice_quotes import fresh_practice_price
from app.config import Settings
from app.market import Stock
from app.storage import Database
from app.strategy import auto_paper_exit


@pytest.mark.parametrize('seconds,expected', [(0,True),(180,True),(181,False),(-1,False)])
def test_fetch_time_boundary(seconds, expected):
    now=datetime(2026,10,18,12,tzinfo=timezone.utc)
    stock=SimpleNamespace(price=100,stamp=(now-timedelta(seconds=seconds)).isoformat())
    assert fresh_practice_price(stock,now) is expected


@pytest.mark.parametrize('stamp', ['', 'broken', None])
def test_unknown_timestamp(stamp):
    assert not fresh_practice_price(SimpleNamespace(price=100,stamp=stamp))


@pytest.mark.parametrize('price', [0,-1,float('nan'),float('inf'),True])
def test_invalid_price(price):
    assert not fresh_practice_price(SimpleNamespace(price=price,stamp=datetime.now().isoformat()))


def test_offset_and_local_times():
    now=datetime.now().astimezone().replace(microsecond=0)
    for stamp in (now.isoformat(),now.astimezone(timezone.utc).isoformat(),now.replace(tzinfo=None).isoformat()):
        assert fresh_practice_price(SimpleNamespace(price=100,stamp=stamp),now)


def test_stale_exit_does_not_change_ledger(tmp_path):
    cfg=Settings(tmp_path).values
    db=Database(tmp_path/'practice.db')
    stock=Stock('900001','연습','KOSPI',10000,3,500,300,10000)
    db.paper_open(stock,1,2,4,2,0.015,0.1,cfg,3)
    stock.price=11000
    for stamp in ('', 'broken', (datetime.now()-timedelta(minutes=4)).isoformat(),
                  (datetime.now()+timedelta(minutes=4)).isoformat()):
        stock.stamp=stamp
        assert auto_paper_exit(db,[stock],cfg)==[]
        assert db.stats()['open_count']==1
    stock.stamp=datetime.now().isoformat()
    assert len(auto_paper_exit(db,[stock],cfg))==1


def test_new_ledger_times_have_explicit_korean_offset():
    from app.storage import now
    assert datetime.fromisoformat(now()).utcoffset() == timedelta(hours=9)


def test_holding_days_use_korean_calendar(monkeypatch):
    import app.strategy as strategy

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = datetime(2026, 10, 20, 0, 30, tzinfo=timezone(timedelta(hours=9)))
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)

    calls = []
    trade = dict(id=1, code='900001', name='연습',
                 opened_at='2026-10-18T16:00:00+00:00',
                 buy_fill=100, stop_pct=3, take_pct=2, max_days=2)
    db = SimpleNamespace(open_trades=lambda: [trade],
                         paper_close=lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(strategy, 'datetime', Clock)
    monkeypatch.setattr(strategy, 'fresh_practice_price', lambda stock: True)
    cfg = dict(paper_fee_pct=0, paper_sell_tax_pct=0, paper_slippage_pct=0)
    stock = SimpleNamespace(code='900001', price=100)
    # Entry was Oct 19 in Korea: only one calendar day has elapsed.
    assert auto_paper_exit(db, [stock], cfg) == []
    trade['max_days'] = 1
    assert auto_paper_exit(db, [stock], cfg) == [('연습', '가상 보유기간 청산', 0)]
    assert len(calls) == 1
