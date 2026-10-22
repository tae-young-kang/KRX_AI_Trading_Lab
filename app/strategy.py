from dataclasses import dataclass
from datetime import datetime
from app.practice_quotes import fresh_practice_price
from app.trading_clock import KST


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
        if not cfg.get('include_etf', True) and getattr(st, 'is_etf', False):
            failures.append('ETF 제외')
        if st.price <= 0:
            failures.append('유효 시세 없음')
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
    latest = {s.code: s for s in stocks if fresh_practice_price(s)}
    emitted = []
    for trade in db.open_trades():
        st = latest.get(trade['code'])
        if not st:
            continue  # Never sell on stale price.
        open_time = datetime.fromisoformat(trade['opened_at']).astimezone(KST)
        age_days = (datetime.now(KST).date() - open_time.date()).days
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
    """Compare actually tried conditions. No invented thresholds or auto-apply."""
    from app.trade_review import FILTER_KEYS, EXIT_KEYS, COST_KEYS
    min_n = max(20, int(cfg['min_strategy_trades']))
    reviews = db.condition_reviews(min_n)
    candidates = 0
    for r in reviews:
        old = r['settings']
        if not r['eligible'] or any(old[k] != cfg[k] for k in ('data_mode', 'kiwoom_env') + COST_KEYS):
            continue
        if any(r['exit_costs'][k] != cfg[k] for k in COST_KEYS):
            continue
        patch = {k: old[k] for k in FILTER_KEYS + EXIT_KEYS if old[k] != cfg[k]}
        if not patch:
            continue
        candidates += int(db.propose('기록에서 찾은 조건 비교 후보',
            f"{r['mode']} · {r['label']} / 비용 차감 후 앞 기록 {r['train']['count']}건 "
            f"평균 {r['train']['mean_pct']:+.2f}%, 뒤 기록 {r['test']['count']}건 "
            f"평균 {r['test']['mean_pct']:+.2f}%. 사용자 선택 기록의 탐색 비교이며 "
            '미래 수익을 보장하지 않습니다. 추가 관찰 후 적용 여부를 결정하세요.', patch,base_settings=cfg))
    return (f'{len(reviews)}개 조건 비교 · 새 후보 {candidates}개. 조건별 앞 기록 {min_n}건·5일 이상, '
            '뒤 기록 10건·3일 이상 및 양쪽 수익이 필요합니다. 데이터 축적·추가 검증 중이며 자동 적용하지 않습니다.')
