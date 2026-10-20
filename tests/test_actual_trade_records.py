"""Synthetic CSV + temporary real-record store, no broker or credentials."""
import csv
import io
from datetime import datetime
import sqlite3
import pytest
from app.actual_trade_records import FIELDS,import_csv,daily_text
from app.trading_clock import KST
from test_telegram_orders import sandbox,update

NOW=datetime(2026,10,20,12,tzinfo=KST)


def row(fill_id='example-buy',**changes):
    return dict(dict(fill_id=fill_id,filled_at='2026-10-19T09:00:00+09:00',code='900001',
        name='입력 예제',venue='NXT',side='BUY',quantity='10',price_won='1000',realized_pnl_won='',reason='UNKNOWN'),**changes)


def blob(*rows):
    output=io.StringIO()
    writer=csv.DictWriter(output,fieldnames=FIELDS)
    writer.writeheader();writer.writerows(rows)
    return output.getvalue().encode('utf-8')


def test_reported_actual_fills_import_and_daily_report_are_idempotent(tmp_path):
    path=tmp_path/'actual.db'
    data=blob(row(),row('sale-one',side='SELL',quantity='5',price_won='1100',realized_pnl_won='400',reason='TAKE_PROFIT'),
        row('sale-two',side='SELL',quantity='5',price_won='1200',realized_pnl_won='900',venue='KRX',reason='OTHER'))
    assert import_csv(path,data,now=NOW)==3
    assert import_csv(path,data,now=NOW)==0
    before=path.read_bytes()
    text=daily_text(path,'2026-10-19')
    assert '매수금액 10,000원' in text and '매도금액 11,500원' in text
    assert '실현손익 합계 +1,300원' in text and '승 1 / 패 0' in text
    assert 'KRX/NXT' in text and '증권사 자동 대조 전' in text
    assert '익절 1건' in text
    assert path.read_bytes()==before


def test_missing_realized_profit_is_not_fabricated_from_trade_prices(tmp_path):
    path=tmp_path/'actual.db'
    import_csv(path,blob(row(),row('sale',side='SELL',price_won='1100')),now=NOW)
    text=daily_text(path,'2026-10-19')
    assert '실현손익 합계 미확인' in text and '손익 없는 매도 체결 1건' in text
    assert '승률 미확인' in text and '+1,000원' not in text


def test_conflicting_reimport_rolls_back_whole_batch(tmp_path):
    path=tmp_path/'actual.db'
    import_csv(path,blob(row()),now=NOW)
    with pytest.raises(ValueError):
        import_csv(path,blob(row('new'),row(price_won='2000')),now=NOW)
    with sqlite3.connect(path) as db:assert db.execute('SELECT count(*) FROM actual_fills').fetchone()[0]==1


@pytest.mark.parametrize('changes',[dict(venue='SOR'),dict(filled_at='2026-10-19T09:00:00'),
    dict(filled_at='2026-10-21T09:00:00+09:00'),dict(quantity='1.5'),dict(price_won='0'),
    dict(realized_pnl_won='100'),dict(code='900001_NX')])
def test_invalid_input_creates_no_record_store(tmp_path,changes):
    path=tmp_path/'actual.db'
    with pytest.raises(ValueError):import_csv(path,blob(row(**changes)),now=NOW)
    assert not path.exists()


def test_private_columns_are_rejected_without_copying_or_echoing(tmp_path):
    path=tmp_path/'actual.db'
    data=blob(row()).replace(b'reason\r\n',b'reason,account\r\n')
    with pytest.raises(ValueError) as error:import_csv(path,data,now=NOW)
    assert 'account' not in str(error.value) and not path.exists()


def test_missing_file_and_day_are_not_zero_profit(tmp_path):
    path=tmp_path/'actual.db'
    assert '기록이 없어요' in daily_text(path,'2026-10-19') and not path.exists()
    import_csv(path,blob(row()),now=NOW)
    assert '매도 기록 없음' in daily_text(path,'2026-10-20')


def test_actual_daily_telegram_is_authorized_scoped_and_order_free(tmp_path,sandbox):
    path=tmp_path/'actual.db'
    import_csv(path,blob(row()),now=NOW)
    service,broker,_,_=sandbox
    service.actual_daily=lambda day,page=1:daily_text(path,day,page=page)
    bad=update(sandbox,1,'/실제결산 2026-10-19');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    event=update(sandbox,2,'/실제결산 2026-10-19')
    reply=service.handle(event)
    assert reply==service.handle(event) and '실제 체결 입력' in reply
    assert '매도 기록 없음' in reply and broker.calls==[]


def test_actual_store_cannot_share_virtual_trade_database(tmp_path):
    path=tmp_path/'virtual.db'
    with sqlite3.connect(path) as db:db.execute('CREATE TABLE paper_trades(id INTEGER)')
    with pytest.raises(ValueError,match='별도 기록'):
        import_csv(path,blob(row()),now=NOW)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='actual_fills'").fetchone() is None


def test_korean_day_and_pagination_preserve_full_day_totals(tmp_path):
    path=tmp_path/'actual.db'
    rows=[row(f'item-{n}',code=f'90000{n}',filled_at='2026-10-18T16:00:00+00:00') for n in range(1,7)]
    import_csv(path,blob(*rows),now=NOW)
    first=daily_text(path,'2026-10-19');second=daily_text(path,'2026-10-19',page=2)
    assert '매수금액 60,000원' in first and '매수금액 60,000원' in second
    assert '900006' not in first and '900006' in second
    assert '입력 기록이 없어요' in daily_text(path,'2026-10-18')
