from dataclasses import dataclass
from datetime import datetime


@dataclass
class ScreeningResult:
    stock: object
    score: float
    passed: bool
    reason: str


def evaluate(stocks, cfg):
    results = []
    for st in stocks:
        failures = []
        if cfg['on_price'] and not cfg['min_price'] <= st.price <= cfg['max_price']:
            failures.append('주가 범위')
        if cfg['on_cap'] and (st.cap_eok is None or not cfg['min_cap_eok'] <= st.cap_eok <= cfg['max_cap_eok']):
            failures.append('시총 미수집' if st.cap_eok is None else '시총 범위')
        if cfg['on_value'] and st.trade_value_eok < cfg['min_value_eok']:
            failures.append('거래대금')
        if cfg['on_volratio'] and (st.vol_ratio_pct is None or st.vol_ratio_pct < cfg['min_vol_ratio']):
            failures.append('전일 거래량비 미수집' if st.vol_ratio_pct is None else '전일 거래량비')
        if cfg['on_change'] and not cfg['min_change_pct'] <= st.change_pct <= cfg['max_change_pct']:
            failures.append('등락률')
        # Heuristic display score, NOT predictive win probability.
        volume_part = min(1, max(0, ((st.vol_ratio_pct or 0) - 70) / 300)) * 35
        value_part = min(1, max(0, st.trade_value_eok / 450)) * 30
        mom_part = max(0, 1 - abs(st.change_pct - 3.5) / 8) * 25
        cap_part = (10 if st.cap_eok is not None and 3000 <= st.cap_eok <= 300000 else 0)
        score = round(volume_part + value_part + mom_part + cap_part, 1)
        results.append(ScreeningResult(st, score, not failures,
                                       '통과' if not failures else ', '.join(failures)))
    return sorted(results, key=lambda r: (r.passed, r.score), reverse=True)


def auto_paper_exit(db, stocks, cfg):
    """Simplified paper exit on observed snapshots, NOT exchange stop order."""
    latest = {s.code: s for s in stocks if s.price > 0}
    emitted = []
    for trade in db.open_trades():
        st = latest.get(trade['code'])
        if not st:
            continue  # Never sell on stale price.
        from datetime import datetime
        open_time = datetime.fromisoformat(trade['opened_at'])
        age_days = (datetime.now().date() - open_time.date()).days
        move = (st.price / trade['buy_fill'] - 1) * 100
        reason = None
        if move <= -trade['stop_pct']:
            reason = '가상 손절'
        elif move >= trade['take_pct']:
            reason = '가상 익절'
        elif age_days >= trade['max_days']:
            reason = '가상 보유기간 청산'
        if reason:
            pnl = db.paper_close(trade['id'], st.price, reason, cfg['paper_fee_pct'],
                                 cfg['paper_sell_tax_pct'], cfg['paper_slippage_pct'])
            emitted.append((trade['name'], reason, pnl))
    return emitted


def create_proposals(db, cfg):
    """Conservative descriptive experiment proposal; never modifies live settings."""
    closed = db.closed_trades()
    min_n = max(20, int(cfg['min_strategy_trades']))
    if len(closed) < min_n:
        return f'완료된 가상거래 {len(closed)}건 / 제안 최소 {min_n}건. 데이터 축적 중입니다.'
    # Analyze only realized paper trades; no spurious 'machine learning' claims.
    high_loss = [t for t in closed if t['pnl_pct'] < -2]
    if len(high_loss) / len(closed) >= 0.40 and cfg['exit_stop_pct'] > 1:
        patch = {'exit_stop_pct': round(max(1.0, cfg['exit_stop_pct'] - 0.5), 1)}
        db.propose('손절폭 축소 실험',
            f'{len(closed)}건 중 손실 -2% 초과 {len(high_loss)}건. '
            '다음 가상전략에서 손절폭을 좁혀 비교해볼 후보입니다. 개선 효과 미검증.', patch)
    pf = db.stats()['profit_factor']
    if pf < 1 and cfg['min_vol_ratio'] < 350:
        patch = {'min_vol_ratio': round(cfg['min_vol_ratio'] + 25, 1)}
        db.propose('거래량 조건 강화 실험',
            f'가상매매 {len(closed)}건 기준 Profit Factor {pf:.2f}. '
            '거래량 최소 기준을 올린 별도 실험을 제안합니다. 인과관계나 개선 보장은 없습니다.', patch)
    return f'{len(closed)}건을 분석해 실험 후보를 갱신했습니다. 승인 전에 독립 기간 재검증이 필요합니다.'
