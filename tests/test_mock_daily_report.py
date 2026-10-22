from hashlib import sha256
from datetime import timedelta
import pytest
from app.mock_daily_report import report
from app.mock_watch_status import MockWatchStatus
from app.daily_report_delivery import DailyReportDelivery
from test_mock_telegram_orders import rig,add_capture
from test_mock_auto_exit import auto_buy
from test_mock_protection_flow import evidence
from test_kiwoom_mock_dispatch import accepted,NOW
from test_kiwoom_mock_review import wire
from test_telegram_orders import sandbox,update


def closed(tmp_path,source=None,foreign=False,exit_price=1100,trigger=1020):
    cmd,session,http,_,_=rig(tmp_path)
    auto_buy(cmd,session,http)
    evidence(http,price=trigger);cmd.monitor_protection(session)
    evidence(http,price=trigger);http.responses.append(accepted('0000014'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    data=wire()
    for row in data[2]['acnt_ord_cntr_prst_array']:row['ord_no']='0000013'
    sale=dict(data[2]['acnt_ord_cntr_prst_array'][0],ord_no='0000014',cntr_no='0000003',
        io_tp_nm='현금매도',cntr_qty='3',cntr_uv=str(exit_price))
    data[2]['acnt_ord_cntr_prst_array'].append(sale)
    data[1]['acnt_evlt_remn_indv_tot']=[];data[3]['cntr']=[]
    if foreign:
        data[2]['acnt_ord_cntr_prst_array'].append(dict(sale,ord_no='0000015',cntr_no='0000004',io_tp_nm='현금매수',cntr_qty='1',cntr_uv='1000'))
        data[1]['acnt_evlt_remn_indv_tot']=[dict(stk_cd='A900001',rmnd_qty='1',trde_able_qty='1')]
    add_capture(http,data);session.refresh();cmd.observe(session._last_capture)
    if source:
        for number,gross,commission,tax,item in [('13',2980,10,0,'buy-cost'),('14',3*exit_price,20,30,'sell-cost')]:
            cmd.ledger.confirm_costs(cmd.day,number,filled_quantity=3,gross=gross,commission=commission,tax=tax,
                source=source,evidence_hash='a'*64,evidence_item=item)
    return cmd,session,http


def test_closed_pair_with_declared_broker_costs_reports_260_without_mutation(tmp_path):
    cmd,session,http=closed(tmp_path,'BROKER_MOCK_STATEMENT')
    before={p:sha256(p.read_bytes()).hexdigest() for p in (cmd.path,cmd.root/'dispatch.db',cmd.root/'fills.db')}
    calls=len(http.calls)
    text=report(cmd.root,'owner',cmd.day.isoformat(),now=NOW)
    assert '매수금액 2,980원' in text and '매도금액 3,300원' in text
    assert '손익 합계 +260원' in text and '승 1 / 패 0 / 본전 0' in text and '승률 100.0%' in text
    assert '익절 1종목 · 손절 0종목' in text
    assert cmd.account_tag not in text and '계좌번호' not in text
    assert len(http.calls)==calls
    assert {p:sha256(p.read_bytes()).hexdigest() for p in before}==before
    root=cmd.root;cmd.disarm();session.close()
    status=MockWatchStatus(clock=lambda:NOW,order_root=root)
    assert '+260원' in status.daily_report(cmd.day.isoformat(),scope='owner')
    assert '다른 사용자의 기록은 포함하지' in report(root,'other',cmd.day.isoformat(),now=NOW)


def test_stop_sale_costs_produce_loss_and_zero_winrate_from_fills(tmp_path):
    cmd,session,http=closed(tmp_path,'BROKER_MOCK_STATEMENT',exit_price=960,trigger=960)
    text=report(cmd.root,'owner',cmd.day.isoformat(),now=NOW)
    assert '손익 합계 -160원' in text and '승 0 / 패 1 / 본전 0' in text and '승률 0.0%' in text
    assert '익절 0종목 · 손절 1종목' in text
    cmd.disarm();session.close()


@pytest.mark.parametrize('source',[None,'SYNTHETIC_TEST'])
def test_missing_or_synthetic_costs_never_become_zero_cost_profit(tmp_path,source):
    cmd,session,http=closed(tmp_path,source)
    text=report(cmd.root,'owner',cmd.day.isoformat(),now=NOW)
    assert '손익 합계 미확인' in text and '비용 근거 미확인' in text and '승률 미산출' in text
    assert '합계 +320원' not in text
    cmd.disarm();session.close()


def test_unowned_same_symbol_fill_blocks_closed_pair_pnl(tmp_path):
    cmd,session,http=closed(tmp_path,'BROKER_MOCK_STATEMENT',foreign=True)
    text=report(cmd.root,'owner',cmd.day.isoformat(),now=NOW)
    assert '다른 체결 대조 필요' in text and '손익 합계 미확인' in text
    cmd.disarm();session.close()


def test_absent_records_no_creation_and_future_or_bad_pages_rejected(tmp_path):
    root=tmp_path/'absent'
    assert '0원 성과가 아니에요' in report(root,'owner','2026-10-19',now=NOW)
    assert not root.exists()
    with pytest.raises(ValueError):report(root,'owner','2026-10-20',now=NOW)
    with pytest.raises(ValueError):report(root,'owner','2026-10-19',now=NOW,page=True)


def test_account_mismatch_blocks_pnl_and_returns_sanitized_status_warning(tmp_path):
    from app.order_engine import connect
    cmd,session,http=closed(tmp_path,'BROKER_MOCK_STATEMENT')
    with connect(cmd.root/'fills.db') as db:db.execute('UPDATE fill_meta SET account_tag=?',('f'*64,))
    with pytest.raises(ValueError):report(cmd.root,'owner',cmd.day.isoformat(),now=NOW)
    status=MockWatchStatus(clock=lambda:NOW,order_root=cmd.root)
    text=status.daily_report(cmd.day.isoformat(),scope='owner')
    assert '대조 필요' in text and '손익은 미확인' in text and 'f'*64 not in text
    cmd.disarm();session.close()


def test_private_receiver_report_after_stop_and_2030_includes_mock_separately(tmp_path,sandbox):
    service,broker,_,clock=sandbox
    cmd,session,http=closed(tmp_path,'BROKER_MOCK_STATEMENT')
    from app.order_engine import connect
    with connect(cmd.path) as db:db.execute('UPDATE intents SET scope=?',(service.scope,))
    cmd.disarm();session.close()
    owner_calls=len(http.calls)
    status=MockWatchStatus(clock=lambda:clock[0],order_root=cmd.root)
    clock[0]=NOW
    captured=[]
    service.mock_daily=lambda day,scope,page=1:captured.append(scope) or status.daily_report(day,scope=scope,page=page)
    service.orders_enabled=False
    bad=update(sandbox,1,'/모의결산');bad['message']['from']['id']=99
    assert service.handle(bad) is None and captured==[]
    service.handle(update(sandbox,2,'/중지'))
    assert '+260원' in service.handle(update(sandbox,3,'/모의결산'))
    assert captured==[service.scope] and broker.calls==[]
    # A separate active receiver fixture session for scheduling, no Telegram IO.
    with connect(service.path) as db:db.execute('UPDATE tg_sessions SET active=1 WHERE scope=?',(service.scope,))
    clock[0]=NOW.replace(hour=20,minute=30)
    service.daily_delivery=DailyReportDelivery(service.path,service.scope,'DEMO')
    service.actual_daily=lambda day,page=1:'[실제 입력] 별도 기록'
    service.morning_setup=lambda text:'[가상 하루] 별도 예산'
    class Sender:
        def __init__(self):self.messages=[]
        def send(self,chat,text):self.messages.append(text);return len(self.messages)
    sender=Sender()
    for _ in range(4):service.send_daily_notice(sender);clock[0]+=timedelta(seconds=2)
    assert len(sender.messages)==4 and '키움 모의 하루 기록' in sender.messages[-1]
    assert '+260원' in sender.messages[-1] and '익절 1종목' in sender.messages[-1]
    assert '실제 입력' in sender.messages[1] and '가상 하루' in sender.messages[2]
    assert broker.calls==[] and len(http.calls)==owner_calls
