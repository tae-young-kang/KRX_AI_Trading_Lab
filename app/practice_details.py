"""Plain-text, read-only presentation of synthetic sale history."""
from math import isfinite


def _money(value):
    if type(value) not in (int,float) or not isfinite(value):
        return '확인 필요'
    return f'{value:,.2f}원'


def sale_details(trade, sales):
    lines=[f'{trade["name"]} · 앱 안의 연습 기록',
           f'남은 수량 {trade["remaining_qty"]}주 / 처음 {trade["qty"]}주',
           '실제 증권사 체결이 아니며 비용과 체결가는 가정값입니다.',
           '남은 수량을 모두 판 뒤에만 한 번의 승·패·본전을 정합니다.']
    if not sales:
        lines.append('매도별 기록이 없습니다. 아직 팔지 않았거나 이전 버전의 완료 기록일 수 있어요.')
    for index,sale in enumerate(sales,1):
        lines.extend(['',f'{index}번째 매도 · {sale["sold_at"]}',
                      f'판 수량 {sale["qty"]}주 · {sale["reason"]}',
                      f'관측 가격 {_money(sale["reference"])} → 가정 체결가 {_money(sale["fill"])}',
                      f'이 수량의 매수 원가 {_money(sale["allocated_cost"])} (매수 수수료 포함)',
                      f'매도 수수료 {_money(sale["sell_fee"])} · 세금 {_money(sale["sell_tax"])}',
                      f'이번 매도의 비용 차감 후 손익 {_money(sale["pnl"])}'])
    lines.extend(['',f'누적 실현손익 {_money(trade["pnl"] if trade["closed_at"] else trade["realized_pnl"])}',
                  '이 화면은 기록 열람이며 장부 정합성 감사 결과가 아닙니다.'])
    return '\n'.join(lines)
