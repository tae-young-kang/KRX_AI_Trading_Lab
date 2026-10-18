"""Condition-at-entry reviews of realized paper trades; never places orders."""
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))


def _record_time(value):
    """Legacy offset-free records use PC local time, as in the daily ledger."""
    if not isinstance(value, str) or 'T' not in value:
        raise ValueError('invalid record time')
    return datetime.fromisoformat(value).astimezone(KST)

FILTER_KEYS = ('market', 'include_etf', 'recommend_count', 'on_price', 'min_price',
               'max_price', 'on_cap', 'min_cap_eok', 'max_cap_eok', 'on_value',
               'min_value_eok', 'on_volratio', 'min_vol_ratio', 'on_change',
               'min_change_pct', 'max_change_pct')
EXIT_KEYS = ('exit_stop_pct', 'exit_take_pct', 'exit_max_days')
COST_KEYS = ('paper_fee_pct', 'paper_sell_tax_pct', 'paper_slippage_pct')
SNAPSHOT_KEYS = FILTER_KEYS + EXIT_KEYS + COST_KEYS + ('data_mode', 'kiwoom_env', 'strategy_version')


def settings_snapshot(settings):
    """Explicit whitelist: no account/chat identifiers or credentials."""
    return {k: settings[k] for k in SNAPSHOT_KEYS if k in settings}


def outcome(trade):
    if not trade.get('closed_at'):
        return '보유 중'
    value = trade.get('pnl')
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return '확인 필요'
    return '본전' if abs(value) < 1e-8 else ('승' if value > 0 else '패')


def condition_label(cfg):
    parts = [str(cfg.get('market', '시장 미상')), 'ETF 포함' if cfg.get('include_etf') else 'ETF 제외']
    if cfg.get('on_price'):
        parts.append(f"주가 {cfg.get('min_price', 0):g}~{cfg.get('max_price', 0):g}원")
    if cfg.get('on_cap'):
        parts.append(f"시총 {cfg.get('min_cap_eok', 0):g}~{cfg.get('max_cap_eok', 0):g}억")
    if cfg.get('on_value'):
        parts.append(f"거래대금 {cfg.get('min_value_eok', 0):g}억 이상")
    if cfg.get('on_volratio'):
        parts.append(f"전일 거래량비 {cfg.get('min_vol_ratio', 0):g}% 이상")
    if cfg.get('on_change'):
        parts.append(f"등락 {cfg.get('min_change_pct', 0):g}~{cfg.get('max_change_pct', 0):g}%")
    return ' · '.join(parts)


def summarize(trades):
    closed = [t for t in trades if outcome(t) in ('승', '패', '본전')]
    wins = sum(outcome(t) == '승' for t in closed)
    losses = sum(outcome(t) == '패' for t in closed)
    return dict(count=len(closed), wins=wins, losses=losses, draws=len(closed)-wins-losses,
                winrate=wins/len(closed)*100 if closed else 0,
                pnl=sum(t['pnl'] for t in closed),
                mean_pct=sum(t['pnl_pct'] for t in closed)/len(closed) if closed else 0)


def review_conditions(trades, min_train=20):
    groups = defaultdict(list)
    metadata = {}
    times = {}
    for trade in trades:
        try:
            opened = _record_time(trade['opened_at'])
            closed_at = _record_time(trade['closed_at']) if trade.get('closed_at') else None
            if closed_at is not None and closed_at < opened:
                continue
            cfg = settings_snapshot(json.loads(trade['strat_json']))
            # Unknown legacy provenance/partial settings stay outside comparisons.
            if any(k not in cfg for k in SNAPSHOT_KEYS if k != 'strategy_version'):
                continue
            if cfg['data_mode'] not in ('DEMO', 'KIWOOM') or cfg['kiwoom_env'] not in ('MOCK', 'REAL'):
                continue
            numeric = [k for k in FILTER_KEYS + EXIT_KEYS + COST_KEYS if k not in
                       ('market', 'include_etf') and not k.startswith('on_')]
            if any(not isinstance(cfg[k], (int, float)) or not math.isfinite(cfg[k]) for k in numeric):
                continue
            if any(not isinstance(cfg[k], bool) for k in FILTER_KEYS if k.startswith('on_') or k == 'include_etf'):
                continue
            max_days = float(trade['max_days'])
            if not math.isfinite(max_days) or not max_days.is_integer() or max_days < 1:
                continue
            cfg.update(exit_stop_pct=float(trade['stop_pct']), exit_take_pct=float(trade['take_pct']),
                       exit_max_days=int(max_days))
            if (cfg['data_mode'] == 'DEMO') != str(trade['source']).startswith('DEMO'):
                continue
            if cfg['data_mode'] == 'KIWOOM' and trade['source'] != 'KIWOOM':
                continue
            if trade.get('closed_at') and (outcome(trade) == '확인 필요' or
                    not isinstance(trade.get('pnl_pct'), (int, float)) or not math.isfinite(trade['pnl_pct'])):
                continue
            canonical = dict(cfg)
            canonical.pop('strategy_version', None)
            for k in numeric:
                canonical[k] = float(canonical[k])
            for flag, keys in (('on_price', ('min_price', 'max_price')),
                               ('on_cap', ('min_cap_eok', 'max_cap_eok')),
                               ('on_value', ('min_value_eok',)),
                               ('on_volratio', ('min_vol_ratio',)),
                               ('on_change', ('min_change_pct', 'max_change_pct'))):
                if not canonical[flag]:
                    for k in keys: canonical.pop(k, None)
            canonical['source'] = trade['source']
            # Actual entry exit-policy arguments and exit costs distinguish groups.
            for field in ('stop_pct', 'take_pct', 'max_days'):
                canonical[field] = float(trade[field])
            costs = (json.loads(trade['exit_settings_json']) if trade.get('exit_settings_json')
                     else ({k: cfg[k] for k in COST_KEYS} if not trade.get('closed_at') else None))
            cost_status='MISSING' if costs is None else 'KNOWN'
            if costs is not None:
                if not isinstance(costs,dict): raise ValueError('invalid exit costs')
                parts=costs.get('segments') if costs.get('mixed') is True else [costs]
                if not isinstance(parts,list) or not parts:
                    raise ValueError('invalid split exit costs')
                if any(not isinstance(part,dict) or any(type(part.get(k)) not in (int,float)
                    or not math.isfinite(part[k]) or not 0<=part[k]<100 for k in COST_KEYS)
                    or part['paper_fee_pct']+part['paper_sell_tax_pct']>=100 for part in parts):
                    raise ValueError('invalid exit costs')
                if costs.get('mixed') is True:
                    cost_status='MIXED'
                    costs=None
            canonical['exit_costs'] = {k: float(costs[k]) for k in COST_KEYS} if costs is not None else None
            canonical['exit_cost_status']=cost_status
            key = hashlib.sha256(json.dumps(canonical, sort_keys=True, allow_nan=False).encode()).hexdigest()
            groups[key].append(trade)
            times[id(trade)] = (opened, closed_at)
            metadata[key] = (cfg, canonical['exit_costs'],cost_status)
        except (ValueError, TypeError, KeyError):
            continue
    reviews = []
    for key, records in groups.items():
        cfg, exit_costs, cost_status = metadata[key]
        closed = sorted((t for t in records if outcome(t) in ('승', '패', '본전')),
                        key=lambda t: (times[id(t)][0], t['id']))
        day = lambda t: times[id(t)][0].date()
        dates = sorted({day(t) for t in closed})
        cut = dates[max(0, int(len(dates)*.7)-1)] if dates else ''
        test = [t for t in closed if day(t) > cut]
        first_test = min((times[id(t)][0] for t in test), default=None)
        train = [t for t in closed if day(t) <= cut and first_test is not None
                 and times[id(t)][1] < first_test]
        train_stats, test_stats = summarize(train), summarize(test)
        enough = (train_stats['count'] >= max(20, min_train) and test_stats['count'] >= 10
                  and len({day(t) for t in train}) >= 5
                  and len({day(t) for t in test}) >= 3)
        positive = (enough and exit_costs is not None and train_stats['mean_pct'] > 0 and train_stats['pnl'] > 0
                    and test_stats['mean_pct'] > 0 and test_stats['pnl'] > 0)
        mode = '가상 시세로 연습' if cfg['data_mode'] == 'DEMO' else (
            '모의 시세로 연습' if cfg['kiwoom_env'] == 'MOCK' else '실제 시세로 연습')
        reviews.append(dict(key=key, settings=cfg, exit_costs=exit_costs, cost_status=cost_status, mode=mode, label=condition_label(cfg),
                            total=summarize(closed), open_count=sum(not t.get('closed_at') for t in records),
                            train=train_stats, test=test_stats, eligible=positive,
                            state=('분할매도 비용 가정 다름' if cost_status=='MIXED' and closed else
                                   '청산 비용 기록 미확인' if exit_costs is None and closed else
                                   '추가 검증 후보' if positive else ('수익 기준 미달' if enough else '기록 모으는 중'))))
    return sorted(reviews, key=lambda r: (r['eligible'], r['total']['count']), reverse=True)
