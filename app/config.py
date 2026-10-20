import json
import os
import sys
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
            self.kr = secure_storage()
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
        if persist:
            if self.kr is None:
                raise RuntimeError(storage_install_help())
            try:
                if value:
                    self.kr.set_password(APP_NAME, kind, value)
                else:
                    try:
                        self.kr.delete_password(APP_NAME, kind)
                    except Exception:
                        pass
            except Exception:
                raise RuntimeError('Windows 보안 보관함에 저장하지 못했어요.\n'
                                   '입력한 키의 오류는 아닙니다. PC의 Windows 사용자 계정으로 로그인한 뒤 다시 시도해 주세요.') from None
        self.session_secrets[kind] = value

    def delete(self, kind):
        self.session_secrets.pop(kind, None)
        if self.kr:
            try:
                self.kr.delete_password(APP_NAME, kind)
            except Exception:
                pass


def secure_storage():
    """Use Windows Credential Manager explicitly, including packaged builds.

    Constructing the backend does not read, write or enumerate credentials.
    """
    if os.name == 'nt':
        from keyring.backends.Windows import WinVaultKeyring
        return WinVaultKeyring()
    import keyring
    return keyring


def storage_install_help():
    if getattr(sys, 'frozen', False):
        return ('이 실행 파일에 연결 키를 안전하게 저장하는 기능이 빠져 있어요.\n'
                '키를 다시 발급받을 필요는 없어요. 수정된 최신 실행 파일을 받아 다시 실행해 주세요.\n'
                '입력한 키는 저장되지 않았어요.')
    return ('연결 키를 안전하게 저장하는 필수 구성요소가 없어요.\n'
            '프로그램을 닫고 같은 폴더의 run_windows.bat를 더블클릭해 주세요.\n'
            '필요한 구성요소를 설치한 뒤 다시 열립니다. 입력한 키는 저장되지 않았어요.')
