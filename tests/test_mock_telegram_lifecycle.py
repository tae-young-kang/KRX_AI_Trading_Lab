"""Combined authenticated command flow against fake HTTP/socket, not live proof."""
import re
import pytest

from app.mock_telegram_orders import MockTelegramOrders
from app.mock_watch_status import MockWatchStatus
from test_mock_telegram_orders import rig, add_capture, mutations
from test_mock_telegram_actions import snapshot
from test_kiwoom_mock_dispatch import accepted
from test_telegram_orders import sandbox, update


@pytest.mark.parametrize('cancel_uncertain', [False, True])
def test_authenticated_buy_partial_amend_partial_cancel_and_restart(tmp_path, sandbox, cancel_uncertain):
    service, practice_broker, _, _ = sandbox
    cmd, session, http, _, clock = rig(tmp_path)
    service.orders_enabled = False
    service.watch_orders = cmd.handle
    service.watch_cancel_orders = cmd.cancel_session
    view = MockWatchStatus(clock=lambda:clock[0], history_path=tmp_path/'observations.db')
    uid = 0
    def send(text):
        nonlocal uid
        uid += 1
        message = update(sandbox, uid, text)
        reply = service.handle(message)
        assert service.handle(message) == reply  # Telegram redelivery, no duplicate work.
        return reply
    def confirm(text):
        preview = send(text)
        token = re.search('/모의확인 ([A-Za-z0-9_-]+)', preview)[1]
        assert '전송을 요청' in send('/모의확인 '+token)
    def process(data, response):
        add_capture(http, data); http.responses.append(response)
        result = cmd.process(session)
        view.order_result(result)
        assert cmd.process(session) is None
        return result
    def observe(data):
        add_capture(http, data)
        view.finish_refresh(session.refresh())
        cmd.observe(session._last_capture)

    try:
        confirm('/매수 900001 3 1000')
        assert process(None, accepted('0000013')) == 'ACKNOWLEDGED'
        partial = snapshot()
        observe(partial)
        assert '남은 2주' in send('/미체결')
        confirm('/정정 13 2 1100')
        assert process(partial, accepted('0000014', '0000013')) == 'ACKNOWLEDGED'
        later = snapshot(order='14', original='13', quantity=2, remaining=1)
        fill = later[2]['acnt_ord_cntr_prst_array'][0]
        later[2]['acnt_ord_cntr_prst_array'].append(dict(fill, ord_no='0000014', orig_ord_no='0000013',
            cntr_no='0000003', cntr_qty='1', cntr_uv='1100'))
        observe(later)
        pending = send('/미체결')
        assert '0000014' in pending and '0000013' not in pending and '남은 1주' in pending
        confirm('/취소 14')
        response = TimeoutError('PRIVATE') if cancel_uncertain else accepted('0000015', '0000013')
        assert process(later, response) == ('UNKNOWN' if cancel_uncertain else 'ACKNOWLEDGED')
        final = snapshot(remaining=0)
        final[2]['acnt_ord_cntr_prst_array'] = later[2]['acnt_ord_cntr_prst_array']
        observe(final)
        status = send('/주문상태')
        assert ('재주문 금지' if cancel_uncertain else '최종 취소 미확인') in status
        send('/긴급중지')
        cmd.disarm()
        reopened = MockTelegramOrders(tmp_path/'mock', confirmed_day=clock[0].date(), clock=lambda:clock[0])
        reopened.arm(session)
        service.watch_orders = reopened.handle
        service.watch_cancel_orders = reopened.cancel_session
        assert reopened.emergency.event.is_set() and not reopened.armed
        assert '긴급중지' in send('/주문점검')
        assert '취소' in send('/주문목록')
        assert '전송을 요청' not in send('/매수 900001 1 1000')
        assert reopened.process(session) is None
        assert len(mutations(http)) == 3 and practice_broker.calls == []
        assert mutations(http)[-1]['json']['cncl_qty'] == '1'
        assert 'PRIVATE' not in status and 'PRIVATE' not in view.history_summary()
        reopened.disarm()
    finally:
        cmd.disarm(); session.close()
