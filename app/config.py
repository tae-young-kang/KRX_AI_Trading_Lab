import json
import os
from copy import deepcopy
from pathlib import Path

APP_NAME = 'KOSPI_AI_Trading'


def data_dir():
    override = os.getenv('KOSPI_AI_DATA_DIR')
    if override:
        path = Path(override).expanduser()
    elif os.name == 'nt':
        path = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / APP_NAME
    else:
        path = Path.home() / '.local' / 'share' / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


DEFAULTS = {
    'market': '전체',
    'include_etf': True,
    'data_mode': 'DEMO',
    'kiwoom_env': 'MOCK',
    'legacy_kiwoom_env': '',
    'show_advanced': False,
    'scan_interval_sec': 60,
    'recommend_count': 15,
    'min_price': 2000.0,
    'max_price': 200000.0,
    'min_cap_eok': 3000.0,
    'max_cap_eok': 1000000.0,
    'min_value_eok': 100.0,
    'min_vol_ratio': 150.0,
    'min_change_pct': 0.0,
    'max_change_pct': 8.0,
    'on_price': True,
    'on_cap': True,
    'on_value': True,
    'on_volratio': True,
    'on_change': True,
    'exit_stop_pct': 2.0,
    'exit_take_pct': 4.0,
    'exit_max_days': 2,
    'paper_fee_pct': 0.015,
    'paper_sell_tax_pct': 0.20,
    'paper_slippage_pct': 0.10,
    'max_paper_positions': 3,
    'min_strategy_trades': 20,
    # Historical OHLCV CSV backtest. These are NOT Kiwoom prev-day ratios.
    'backtest_volume_ratio': 200.0,
    'backtest_volume_days': 20,
    'backtest_gap_limit': 3.0,
    'backtest_entry_delay': 1,
    'backtest_use_ma': True,
    'backtest_initial_cash': 1000000.0,
    'backtest_qty': 1,
    'last_backtest_csv': '',
    'strategy_version': 1,
    'telegram_user_id': '',
    'telegram_chat_id': '',
    'telegram_auto_send': False,
    'telegram_send_hour': 16,
    'last_notification_day': '',
    'last_optimize_day': '',
    'research_auto_daily': False,
    'recommendation_auto_evaluate': False,
    'recommendation_calendar_sources': {},
    'research_last_update': '',
}


class Settings:
    def __init__(self, root=None):
        self.root = Path(root) if root else data_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'settings.json'
        self.values = deepcopy(DEFAULTS)
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    self.values.update({k: v for k, v in loaded.items() if k in DEFAULTS})
            except (OSError, ValueError):
                pass
        # Freeze the provenance of pre-separation keys before switching servers.
        if not self.values['legacy_kiwoom_env']:
            self.values['legacy_kiwoom_env'] = self.values['kiwoom_env']

    def get(self, key):
        return self.values[key]

    def set(self, key, value):
        if key not in DEFAULTS:
            raise KeyError(key)
        self.values[key] = value

    def save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, self.path)


class Credentials:
    """Never persist secrets in JSON/SQLite; keyring is required to save credentials."""
    def __init__(self):
        self.session_secrets = {}
        try:
            import keyring
            self.kr = keyring
        except ImportError:
            self.kr = None

    def get(self, kind):
        if kind in self.session_secrets:
            return self.session_secrets[kind]
        if self.kr:
            try:
                return self.kr.get_password(APP_NAME, kind) or ''
            except Exception:
                return ''
        return ''

    def save(self, kind, value, persist=True):
        self.session_secrets[kind] = value
        if persist:
            if self.kr is None:
                raise RuntimeError('보안 저장을 위한 keyring 패키지가 설치되지 않았습니다.')
            try:
                if value:
                    self.kr.set_password(APP_NAME, kind, value)
                else:
                    try:
                        self.kr.delete_password(APP_NAME, kind)
                    except Exception:
                        pass
            except Exception as exc:
                raise RuntimeError('OS 보안 저장소에 접근할 수 없습니다.') from exc

    def delete(self, kind):
        self.session_secrets.pop(kind, None)
        if self.kr:
            try:
                self.kr.delete_password(APP_NAME, kind)
            except Exception:
                pass
