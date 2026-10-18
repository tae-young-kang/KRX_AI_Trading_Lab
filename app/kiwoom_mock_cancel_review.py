"""Read-only cancellation notice -> NEW balance/fill capture -> durable audit.

Never resend an order, clear UNKNOWN, release engine reservations, or declare
final cancellation/settlement. Receipt time is not a broker sequence number.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime
import hashlib
import json

from app.kiwoom_mock_chain import project_chain
from app.kiwoom_mock_contract import ContractError, UncertainResponse
from app.kiwoom_mock_review import CapturedObservations, MockAccountReviewer
from app.kiwoom_mock_status import OrderNotice, cancellation_status, money_view, MoneyView
from app.trading_clock import KST


@dataclass(frozen=True)
class CancellationReview:
    review_id: str
    day: str
    checked_at: str
    account_tag: str = field(repr=False)
    title: str
    detail: str
    remaining_quantity: int
    filled_quantity: int
    filled_value: int
    money: MoneyView
    final_cancellation_proven: bool = False
    ready_for_orders: bool = False


def validate_pair(chain, notice, capture, *, session, now, code, side):
    if type(notice) is not OrderNotice or type(capture) is not CapturedObservations:
        raise ContractError('취소 알림과 새로 조회한 기록이 필요합니다.')
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ContractError('대조 시간을 확인하세요.')
    for moment in (capture.started_at, capture.finished_at, notice.received_at):
        if not isinstance(moment, datetime) or moment.tzinfo is None or moment.utcoffset() is None:
            raise UncertainResponse('대조할 기록의 시간이 불완전합니다.')
    if (not notice.auth_generation or notice.auth_generation != capture.auth_generation
            or notice.profile_id != capture.profile_id
            or capture.started_at <= notice.received_at
            or capture.finished_at < capture.started_at
            or not 0 <= (now - capture.finished_at).total_seconds() <= 120
            or (capture.finished_at - capture.started_at).total_seconds() > 120
            or any(moment.astimezone(KST).date() != capture.observations.day for moment in
                   (capture.started_at, capture.finished_at, notice.received_at, now))):
        raise UncertainResponse('같은 연결에서 알림을 받은 뒤 새로 조회해야 합니다.')
    return cancellation_status(chain, capture.observations, notice, session=session,
                               now=now, code=code, side=side)


def review_after_notice(journal, ledger, reviewer, root_key, notice, *, expected_account,
                        session, known_orders, expected_deposit=None):
    """Read-only coordinator. Caller owns the current verified MOCK session.

    Uses reviewer.capture rather than a cached snapshot. Sources remain separate:
    journal ACKs, realtime observation, stable HTTP observations, fill ledger.
    """
    if type(reviewer) is not MockAccountReviewer or type(notice) is not OrderNotice:
        raise ContractError('모의 조회 연결과 취소 알림이 필요합니다.')
    transport = reviewer.transport
    before = project_chain(journal, ledger, root_key)
    tag, rows = journal.read_chain(root_key)
    root = next(row for row in rows if row['request_key'] == root_key)
    code = json.loads(root['payload'])['stk_cd']
    side = 'BUY' if root['api_id'] == 'kt10000' else 'SELL'
    now = transport._clock()
    # Reject stale/wrong-session notices BEFORE requesting account information.
    if (not isinstance(notice.received_at, datetime) or notice.received_at.tzinfo is None
            or notice.received_at.utcoffset() is None or not isinstance(now,datetime) or now.tzinfo is None
            or notice.auth_generation != transport.auth_generation or notice.profile_id != transport.profile_id
            or notice.session != session
            or notice.account_tag != tag or notice.action != 'CANCEL'
            or notice.order_no != before.order_nos[-1] or notice.code != code or notice.side != side
            or not before.cancel_request_acknowledged
            or notice.original_no not in before.order_nos[:-1]
            or now.tzinfo is None or not 0 <= (now-notice.received_at).total_seconds() <= 120):
        raise UncertainResponse('현재 연결의 최근 취소 알림을 확인해 주세요.')
    capture = reviewer.capture(expected_account=expected_account, trade_date=before.day)
    now = transport._clock()
    if capture.auth_generation != transport.auth_generation or capture.profile_id != transport.profile_id:
        raise UncertainResponse('조회 후 연결이 달라졌습니다. 다시 확인해 주세요.')
    validate_pair(before, notice, capture, session=session, now=now, code=code, side=side)
    # Store actual query evidence even if a later mapping comparison fails.
    ledger.ingest(capture.observations)
    after = project_chain(journal, ledger, root_key)
    if after.order_nos != before.order_nos:
        raise UncertainResponse('조회 중 주문 기록이 달라졌습니다. 다시 확인해 주세요.')
    now = transport._clock()
    status = validate_pair(after, notice, capture, session=session, now=now, code=code, side=side)
    funds = money_view(capture.observations, known_orders, expected_deposit=expected_deposit)
    if notice.status == '취소':
        title = '남은 주문이 있어요' if status.remaining_quantity else '조회된 남은 주문은 없어요'
    elif notice.status == '거부':
        title = '취소 요청이 거절됐어요'
    else:
        title = '취소 확인 중'
    detail = (f'알림을 받은 뒤 다시 조회했어요. 체결된 수량은 {after.filled_qty:,}주, '
              f'조회된 남은 주문은 {status.remaining_quantity:,}주예요.\n'
              '최종 취소 완료·결제 완료를 확정한 것은 아니에요. 주문을 새로 보내지 않았어요.')
    evidence = dict(notice=asdict(notice), capture=asdict(capture), orders=after.order_nos)
    digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()
    result = CancellationReview(digest, after.day.isoformat(), capture.finished_at.isoformat(), tag,
        title, detail, status.remaining_quantity, after.filled_qty, after.filled_value, funds)
    transport.ready()
    if capture.auth_generation != transport.auth_generation or capture.profile_id != transport.profile_id:
        raise UncertainResponse('대조 중 연결이 달라졌습니다. 다시 확인해 주세요.')
    ledger.record_cancellation_review(root_key, result)
    return result
