"""Daily synthetic cash-flow observations, never brokerage settlement."""
from datetime import datetime
from math import fsum,isfinite
from app.recommendation_outcomes import session_day
from app.trading_clock import KST
from app.trade_review import outcome


def summarize(db, day):
    target=session_day(day)
    def on_day(stamp):
        parsed=datetime.fromisoformat(stamp)
        # Historical app records used PC-local wall time. Label this convention.
        return parsed.astimezone(KST).date()==target
    def total(values):
        try:
            values=list(values)
            if any(type(v) not in (int,float) or not isfinite(v) for v in values):
                raise ValueError()
            result=fsum(values)
            if not isfinite(result):raise ValueError()
            return result
        except (TypeError,ValueError,OverflowError):
            raise ValueError('연습 결산 금액 기록을 확인해 주세요.') from None
    db.execute('BEGIN')
    trades=[dict(row) for row in db.execute('SELECT * FROM paper_trades')]
    sales=[dict(row) for row in db.execute('SELECT * FROM paper_exits')]
    buys=[row for row in trades if on_day(row['opened_at'])]
    daily_sales=[row for row in sales if on_day(row['sold_at'])]
    recorded_ids={row['trade_id'] for row in sales}
    completed=[row for row in trades if row['closed_at'] and on_day(row['closed_at'])]
    legacy=[row for row in completed if row['id'] not in recorded_ids]
    completed=[row for row in completed if row['id'] in recorded_ids]
    outcomes=[outcome(row) for row in completed]
    wins,losses,draws=(outcomes.count(label) for label in ('승','패','본전'))
    return dict(day=day,buy_count=len(buys),sale_count=len(daily_sales),
                buy_gross=total(row['buy_fill']*row['qty'] for row in buys),buy_fees=total(row['buy_fee'] for row in buys),
                sell_gross=total(row['fill']*row['qty'] for row in daily_sales),
                sell_fees=total(row['sell_fee'] for row in daily_sales),sell_tax=total(row['sell_tax'] for row in daily_sales),
                realized=total(row['pnl'] for row in daily_sales),wins=wins,losses=losses,draws=draws,
                unknown=outcomes.count('확인 필요'),legacy=len(legacy),
                winrate=wins/(wins+losses+draws)*100 if wins+losses+draws else None)


def render(result):
    winrate='미확인' if result['winrate'] is None else f'{result["winrate"]:.1f}%'
    return (f'[앱 안의 연습 하루 결산] {result["day"]} · 한국 날짜\n'
            '실제 계좌·키움 모의 체결·증권사 결제 내역이 아닙니다.\n'
            f'매수 {result["buy_count"]}건 · 매수금액 {result["buy_gross"]:,.2f}원 · 매수 수수료 {result["buy_fees"]:,.2f}원\n'
            f'매도 기록 {result["sale_count"]}건 · 매도금액 {result["sell_gross"]:,.2f}원\n'
            f'매도 수수료 {result["sell_fees"]:,.2f}원 · 세금 {result["sell_tax"]:,.2f}원\n'
            f'당일 매도분 실현손익 {result["realized"]:+,.2f}원 (배분한 매수 비용 포함)\n'
            f'오늘 전량 매도 완료: 승 {result["wins"]} / 패 {result["losses"]} / 본전 {result["draws"]} · 승률 {winrate}\n'
            f'완료 손익 미확인 {result["unknown"]}건 · 매도별 근거 없는 이전 완료 기록 {result["legacy"]}건 제외\n'
            '분할매도 손익은 판 날에, 승패는 전량 매도 완료일에 한 번 셉니다. 둘은 다른 집계입니다.\n'
            '시각 offset이 없는 이전 기록은 PC 지역 시각으로 해석합니다. 비용은 가정값입니다.')
