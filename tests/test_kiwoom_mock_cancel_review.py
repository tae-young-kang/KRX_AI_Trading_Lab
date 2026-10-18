"""Fresh notice -> fake read-only HTTP -> temporary ledger; no real endpoints."""
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib

import pytest

from app.kiwoom_mock_cancel_review import review_after_notice, validate_pair
from app.kiwoom_mock_contract import UncertainResponse, order_plan, cancel_plan
from app.kiwoom_mock_journal import MockDispatchJournal
from app.kiwoom_mock_ledger import MockFillLedger
from app.kiwoom_mock_review import MockAccountReviewer, CapturedObservations
from app.kiwoom_mock_status import order_notice
from app.order_engine import OrderRequest, connect
from app.trading_clock import KST
from test_kiwoom_mock_dispatch import transport, accepted, Response
from test_kiwoom_mock_review import wire, ACCOUNT, DAY
from test_kiwoom_mock_status import event, SESSION, START, NOW


def rig(tmp_path, *, remaining=0, notice_status='취소'):
    client, http, _ = transport()
    tag = hashlib.sha256(('MOCK:' + client.profile_id + ':' + ACCOUNT).encode()).hexdigest()
    journal = MockDispatchJournal(tmp_path/'dispatch.db', client)
    journal.bind_account(tag)
    http.responses.extend([accepted(), accepted('0000013', '0000012')])
    request = OrderRequest('900001', 'BUY', 3, 1000)
    journal.dispatch('buy', order_plan(request))
    journal.dispatch('cancel', cancel_plan('900001', '12'), parent_key='buy')
    ledger = MockFillLedger(tmp_path/'fills.db', account_tag=tag)
    notice = order_notice(event(**{'913':notice_status}), expected_account=ACCOUNT,
        profile_id=client.profile_id, session=SESSION, session_started_at=START,
        received_at=NOW, auth_generation=client.auth_generation)
    clock = [NOW+timedelta(seconds=1)]
    client._clock = lambda:clock[0]
    pass_one = wire()
    if remaining:
        pass_one[-1]['oso'] = [dict(ord_no='12', orig_ord_no='0000000', stk_cd='900001', ord_qty='3', oso_qty=str(remaining))]
    # One observed partial fill; the canceled request and no remaining order
    # must not be misrepresented as a fully filled root order.
    pass_one[2]['acnt_ord_cntr_prst_array'] = pass_one[2]['acnt_ord_cntr_prst_array'][:1]
    identity = dict(return_code=0, acctNo=ACCOUNT)
    http.responses.extend(Response(item, headers={'cont-yn':'N'}) for item in
                          [identity]+pass_one+pass_one+[identity])
    original_post = http.post
    def timed_post(url, **kwargs):
        response = original_post(url, **kwargs)
        clock[0] += timedelta(seconds=1)
        return response
    http.post = timed_post
    return client,http,journal,ledger,MockAccountReviewer(client),notice,clock,request


def review(parts, notice=None):
    client,http,journal,ledger,reviewer,original,clock,request = parts
    return review_after_notice(journal,ledger,reviewer,'buy',notice or original,
        expected_account=ACCOUNT,session=SESSION,known_orders={'0000012':request})


@pytest.mark.parametrize('remaining', [0, 1])
def test_fresh_recheck_is_read_only_and_audited_after_restart(tmp_path, remaining):
    parts = rig(tmp_path, remaining=remaining)
    client,http,journal,ledger,reviewer,notice,clock,request = parts
    before_orders = sum('/ordr' in url for url,_ in http.calls)
    result = review(parts)
    assert result.remaining_quantity == remaining
    assert result.filled_quantity == 2 and result.filled_value == 1980
    assert result.title == ('남은 주문이 있어요' if remaining else '조회된 남은 주문은 없어요')
    assert not result.final_cancellation_proven and not result.ready_for_orders
    assert sum('/ordr' in url for url,_ in http.calls) == before_orders
    assert ACCOUNT not in repr(result) and notice.auth_generation not in repr(result)
    restored = MockFillLedger(ledger.path, account_tag=notice.account_tag)
    with connect(restored.path) as db:
        row = db.execute('SELECT * FROM cancel_review_events').fetchone()
        assert row['review_id'] == result.review_id and row['remaining_quantity'] == remaining
        assert db.execute('SELECT count(*) FROM fill_events').fetchone()[0] == 1
    assert journal.get('cancel')['state'] == 'ACKNOWLEDGED'
    restored.record_cancellation_review('buy', result)
    with connect(restored.path) as db:
        assert db.execute('SELECT count(*) FROM cancel_review_events').fetchone()[0] == 1


@pytest.mark.parametrize('change', ['auth','socket','account','age','missing-auth'])
def test_wrong_or_old_notice_is_rejected_before_any_http(tmp_path, change):
    parts = rig(tmp_path)
    notice = parts[5]
    bad = {'auth': replace(notice, auth_generation='e'*32),
           'socket': replace(notice, session='e'*32),
           'account': replace(notice, account_tag='e'*64),
           'age': replace(notice, received_at=notice.received_at-timedelta(seconds=121)),
           'missing-auth': replace(notice, auth_generation=None)}[change]
    before = len(parts[1].calls)
    with pytest.raises(UncertainResponse):
        review(parts, bad)
    assert len(parts[1].calls) == before


def test_query_failure_never_creates_a_success_audit(tmp_path):
    parts = rig(tmp_path)
    parts[1].responses[1] = Response({}, status=500)
    with pytest.raises(UncertainResponse):
        review(parts)
    with connect(parts[3].path) as db:
        assert db.execute('SELECT count(*) FROM cancel_review_events').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fill_events').fetchone()[0] == 0


def test_captured_snapshot_must_start_after_notice_and_finish_recently(tmp_path):
    from app.kiwoom_mock_chain import project_chain
    from test_kiwoom_mock_ledger import snapshot
    parts = rig(tmp_path)
    client,http,journal,ledger,reviewer,notice,clock,request = parts
    chain = project_chain(journal,ledger,'buy')
    observed = replace(snapshot(), account_tag=notice.account_tag)
    good = CapturedObservations(observed, client.profile_id, client.auth_generation,
                                NOW+timedelta(seconds=1),NOW+timedelta(seconds=5))
    assert validate_pair(chain,notice,good,session=SESSION,now=NOW+timedelta(seconds=6),
                         code='900001',side='BUY').remaining_quantity == 0
    for bad in (replace(good,started_at=NOW),
                replace(good,finished_at=NOW-timedelta(seconds=1)),
                replace(good,auth_generation='e'*32),
                replace(good,profile_id='e'*32),
                replace(good,started_at=NOW+timedelta(days=1)),
                replace(good,finished_at=NOW.replace(tzinfo=None))):
        with pytest.raises(UncertainResponse):
            validate_pair(chain,notice,bad,session=SESSION,now=NOW+timedelta(seconds=6),code='900001',side='BUY')


def test_explicit_refusal_is_shown_without_changing_journal(tmp_path):
    parts = rig(tmp_path, notice_status='거부', remaining=1)
    result = review(parts)
    assert result.title == '취소 요청이 거절됐어요' and result.remaining_quantity == 1
    assert parts[2].get('cancel')['state'] == 'ACKNOWLEDGED'


def test_login_change_after_capture_cannot_save_a_success_review(tmp_path):
    parts = rig(tmp_path)
    client,_,_,ledger,reviewer,_,_,_ = parts
    original = reviewer.capture
    def changed(**kwargs):
        captured = original(**kwargs)
        client._auth_generation = 'e'*32
        return captured
    reviewer.capture = changed
    with pytest.raises(UncertainResponse):
        review(parts)
    with connect(ledger.path) as db:
        assert db.execute('SELECT count(*) FROM cancel_review_events').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM fill_events').fetchone()[0] == 0


def test_review_audit_cannot_be_rebound_or_overwritten(tmp_path):
    from app.kiwoom_mock_contract import ContractError
    parts = rig(tmp_path)
    result = review(parts)
    for root, changed in [('different-root',result), ('buy',replace(result,remaining_quantity=99)),
                           ('buy',replace(result,ready_for_orders=True))]:
        with pytest.raises(ContractError):
            parts[3].record_cancellation_review(root,changed)
    with connect(parts[3].path) as db:
        assert db.execute('SELECT remaining_quantity FROM cancel_review_events').fetchone()[0] == 0
