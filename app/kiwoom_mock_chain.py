"""Read-only dispatch lineage -> persistent fills. Never an order adapter."""
from dataclasses import dataclass
from datetime import date
import json

from app.order_engine import connect
from app.kiwoom_mock_contract import ContractError
from app.kiwoom_mock_journal import MockDispatchJournal
from app.kiwoom_mock_ledger import MockFillLedger
from app.kiwoom_mock_review import order_number


@dataclass(frozen=True)
class ChainExecutionView:
    day: date
    root_key: str
    order_nos: tuple[str, ...]
    filled_qty: int
    filled_value: int
    commission: int | None
    tax: int | None
    net_cash_flow: int | None
    cost_source: str | None
    cancel_request_acknowledged: bool
    # Neither absence of unfilled nor a cancellation ACK proves finality.
    state: str = 'CHAIN_OBSERVATIONS_ONLY'
    ready_for_orders: bool = False


def project_chain(journal, ledger, root_key):
    if type(journal) is not MockDispatchJournal or type(ledger) is not MockFillLedger:
        raise ContractError('모의 전용 접수 기록과 체결 장부가 필요합니다.')
    tag, rows = journal.read_chain(root_key)
    if tag != ledger._account_tag:
        raise ContractError('접수 기록과 체결 장부의 모의 계좌가 다릅니다.')
    by_key = {row['request_key']: row for row in rows}
    root = by_key.get(root_key)
    if (not root or root['api_id'] not in ('kt10000', 'kt10001') or root['parent_key'] is not None
            or not root['trade_day'] or root['state'] != 'ACKNOWLEDGED'):
        raise ContractError('거래일과 접수가 확인된 최초 주문이 필요합니다.')
    day = date.fromisoformat(root['trade_day'])
    body = json.loads(root['payload'])
    side = 'BUY' if root['api_id'] == 'kt10000' else 'SELL'
    active, numbers, children = {}, set(), {}
    for row in rows:
        if row['trade_day'] != root['trade_day'] or row['state'] in ('SENDING', 'UNKNOWN'):
            raise ContractError('계보에 거래일 불일치·미확인 요청이 있습니다.')
        if row['state'] == 'REJECTED':
            continue
        if row['state'] != 'ACKNOWLEDGED' or not row['order_no']:
            raise ContractError('접수 계보가 불완전합니다.')
        number = order_number(row['order_no'])
        if number in numbers or json.loads(row['payload'])['stk_cd'] != body['stk_cd']:
            raise ContractError('계보의 주문번호·종목이 일치하지 않습니다.')
        numbers.add(number)
        active[row['request_key']] = row
        if row['request_key'] != root_key:
            parent = by_key.get(row['parent_key'])
            if (not parent or parent['state'] != 'ACKNOWLEDGED' or parent['api_id'] == 'kt10003'
                    or row['api_id'] not in ('kt10002', 'kt10003')
                    or row['parent_key'] in children
                    or order_number(row['base_order_no']) != order_number(root['order_no'])
                    or order_number(json.loads(row['payload'])['orig_ord_no']) != order_number(parent['order_no'])):
                raise ContractError('정정·취소의 원주문 연결이 불완전합니다.')
            children[row['parent_key']] = row['request_key']
    # Traverse the entire chain: reject disconnected cycles/branches.
    sequence, key = [], root_key
    while key is not None and key not in sequence:
        sequence.append(key)
        key = children.get(key)
    if key is not None or set(sequence) != set(active):
        raise ContractError('연결되지 않은 주문 계보입니다.')
    # Conservative ceiling only: a zero-quantity amendment cannot itself add
    # shares. Exact remaining/target size needs fill timing and broker status.
    ceiling = int(body['ord_qty']) + sum(int(json.loads(row['payload'])['mdfy_qty'])
        for row in active.values() if row['api_id'] == 'kt10002')
    total_qty, total_gross, commissions, taxes, sources = 0, 0, [], [], set()
    missing_costs, fill_ids = False, set()
    with connect(ledger.path) as db:
        db.execute('BEGIN')
        # A fill naming this lineage but absent from the dispatch chain must not
        # be silently excluded from the aggregate.
        for row in db.execute('SELECT payload FROM fill_events WHERE day=?', (day.isoformat(),)):
            fill = json.loads(row[0])
            if fill['order_no'] not in numbers and fill['original_no'] in numbers:
                raise ContractError('접수 기록에 없는 후속 주문 체결이 있습니다.')
        for key in sequence:
            row = active[key]
            number = order_number(row['order_no'])
            fills, digest = ledger._fills(db, day.isoformat(), number)
            plan = json.loads(row['payload'])
            if row['api_id'] == 'kt10003':
                if fills:
                    raise ContractError('취소 접수 번호에 체결이 있습니다. 원자료 대조가 필요합니다.')
                continue
            limit = int(plan['ord_uv'] if key == root_key else plan['mdfy_uv'])
            cap = int(plan['ord_qty'] if key == root_key else plan['mdfy_qty'])
            subtotal = 0
            for fill in fills:
                allowed_parents = {order_number(root['order_no'])}
                if row['parent_key']:
                    allowed_parents.add(order_number(active[row['parent_key']]['order_no']))
                if (fill['code'] != body['stk_cd'] or fill['side'] != side
                        or (key == root_key and fill['original_no'] is not None)
                        or (key != root_key and fill['original_no'] not in allowed_parents)
                        or (side == 'BUY' and fill['price'] > limit)
                        or (side == 'SELL' and fill['price'] < limit)
                        or fill['fill_no'] in fill_ids):
                    raise ContractError('계보의 체결번호·종목·방향·원주문·지정가를 확인하세요.')
                fill_ids.add(fill['fill_no'])
                subtotal += fill['quantity']
                total_gross += fill['quantity'] * fill['price']
            if cap and subtotal > cap:
                raise ContractError('해당 주문번호의 체결량이 요청 수량을 초과합니다.')
            total_qty += subtotal
            if fills:
                cost = db.execute('SELECT * FROM fill_costs WHERE day=? AND order_no=? AND fill_digest=?',
                                  (day.isoformat(), number, digest)).fetchone()
                if cost:
                    commissions.append(cost['commission'])
                    taxes.append(cost['tax'])
                    sources.add(cost['source'])
                else:
                    missing_costs = True
    if total_qty > ceiling:
        raise ContractError('전체 체결량이 계보 요청으로 가능한 상한을 초과합니다.')
    commission, tax, net, source = None, None, None, None
    if total_qty and not missing_costs and len(sources) == 1:
        commission, tax, source = sum(commissions), sum(taxes), next(iter(sources))
        net = (total_gross if side == 'SELL' else -total_gross) - commission - tax
    return ChainExecutionView(day, root_key, tuple(order_number(active[key]['order_no']) for key in sequence),
        total_qty, total_gross, commission, tax, net, source,
        any(active[key]['api_id'] == 'kt10003' for key in sequence))
