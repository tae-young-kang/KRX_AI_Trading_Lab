import json
from datetime import datetime, timedelta
from app.config import Settings
from app.storage import Database
from app.market import DemoMarket, Stock, numeric, recommendation_text
from app.strategy import evaluate, auto_paper_exit, create_proposals


def test_config_roundtrip(tmp_path):
    a=Settings(tmp_path)
    a.set('min_cap_eok',8000)
    a.save()
    assert Settings(tmp_path).get('min_cap_eok') == 8000
    assert 'secretkey' not in (tmp_path/'settings.json').read_text()


def test_demo_and_screener(tmp_path):
    cfg=Settings(tmp_path).values
    stocks=DemoMarket().scan('전체',30)
    assert len(stocks)>5 and all(s.source=='DEMO' for s in stocks)
    results=evaluate(stocks,cfg)
    assert len(results)==len(stocks)
    assert len({x.stock.code for x in results})==len(results)
    assert any(r.passed for r in results)
    assert all(r.reason for r in results)
    db=Database(tmp_path/'test.db')
    sid=db.save_scan(stocks,results,cfg,'전체','DEMO')
    assert sid==1
    assert db.connect().execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]==len(stocks)


def test_missing_market_cap_fails_closed(tmp_path):
    cfg=Settings(tmp_path).values
    s=Stock('111111','없는시총','KOSPI',10000,3,300,250,None)
    r=evaluate([s],cfg)[0]
    assert not r.passed and '미수집' in r.reason


def test_paper_trade_math_and_idempotency(tmp_path):
    settings=Settings(tmp_path).values
    db=Database(tmp_path/'d.db')
    s=Stock('900001','가상종목','KOSPI',10000,3,500,300,10000)
    db.paper_open(s,1,2,4,2,0.015,0.1,settings,3)
    try:
        db.paper_open(s,1,2,4,2,0.015,0.1,settings,3)
        assert False, 'duplicate position should be blocked'
    except ValueError:
        pass
    assert db.stats()['open_count']==1
    s.price=10500
    events=auto_paper_exit(db,[s],settings)
    assert len(events)==1 and events[0][1]=='가상 익절'
    assert db.stats()['count']==1
    assert db.stats()['pnl']>0
    assert auto_paper_exit(db,[s],settings)==[]
    assert db.stats()['count']==1


def test_proposal_below_minimum(tmp_path):
    db=Database(tmp_path/'p.db')
    msg=create_proposals(db,Settings(tmp_path).values)
    assert '데이터 축적' in msg
    assert db.proposals()==[]


def test_numeric_and_recommendations():
    assert numeric('-1000',abs_value=True)==1000
    assert numeric('+5.21')==5.21
    assert numeric('') is None
    s=Stock('900001','가상종목','KOSPI',10000,2,200,150,5000)
    assert '가상 데이터' in recommendation_text([s],True)


def test_csv_export(tmp_path):
    db=Database(tmp_path/'db.sqlite')
    path=tmp_path/'trades.csv'
    db.export_trades(path)
    assert path.is_file() and 'buy_fill' in path.read_text(encoding='utf-8-sig')
