"""Headless widget tests with isolated data and fake credentials/market."""
import os
import time

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication, QMessageBox
from app import gui
from app.market import DemoMarket


@pytest.fixture
def window(tmp_path, monkeypatch):
    monkeypatch.setenv('KOSPI_AI_DATA_DIR', str(tmp_path))
    class NoCredentials:
        def __init__(self):
            self.values = {}
        def get(self, key):
            return self.values.get(key, '')
        def save(self, key, value, persist=True):
            self.values[key] = value
    monkeypatch.setattr(gui, 'Credentials', NoCredentials)
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)
    monkeypatch.setattr(QMessageBox, 'information', lambda *args: None)
    app = QApplication.instance() or QApplication([])
    win = gui.MainWindow()
    win.daily_research_timer.stop()
    win.maybe_daily_jobs = lambda: None
    yield app, win
    if win.work:
        win.work.requestInterruption()
        assert win.work.wait(5000)
    win.close()
    win.deleteLater()
    app.processEvents()


def finish(app, win):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        app.processEvents()
        if not win.scanning and win.scan_button.isEnabled():
            return
        time.sleep(.01)
    pytest.fail('scan worker did not complete')


def test_demo_scans_all_examples_and_updates_tables(window):
    app, win = window
    win.scan()
    finish(app, win)
    assert len(win.results) == len(DemoMarket.BASE)
    assert win.screen_table.rowCount() == len(DemoMarket.BASE)
    assert '합성 예제' in win.count_info.text()
    assert 'candidate_limit' not in win.fields
    assert not win.cancel_scan_button.isEnabled()


def test_failed_full_scan_clears_previous_recommendations(window, monkeypatch):
    app, win = window
    win.scan()
    finish(app, win)
    assert win.results

    class BrokenMarket:
        session = type('Session', (), {'close': lambda self: None})()

        def scan(self, market, progress, cancelled, include_etf=True):
            progress('전체 대상 100개 · 시세 조회 2개')
            raise RuntimeError('시세 누락')

    monkeypatch.setattr(gui, 'KiwoomMarket', lambda *args: BrokenMarket())
    win.cfg['data_mode'] = 'KIWOOM'
    win.scan()
    finish(app, win)
    assert not win.stocks and not win.results
    assert win.screen_table.rowCount() == win.dashboard_table.rowCount() == 0
    assert '전체 조회 미완료' in win.count_info.text()


def test_cancel_stops_auto_scan_and_does_not_publish(window, monkeypatch):
    app, win = window

    class WaitingMarket:
        session = type('Session', (), {'close': lambda self: None})()

        def scan(self, market, progress, cancelled, include_etf=True):
            while not cancelled():
                time.sleep(.01)
            raise RuntimeError('전체 조회 취소')

    monkeypatch.setattr(gui, 'KiwoomMarket', lambda *args: WaitingMarket())
    win.cfg['data_mode'] = 'KIWOOM'
    win.timer.start()
    win.scan()
    win.cancel_scan()
    finish(app, win)
    assert not win.timer.isActive()
    assert not win.stocks and not win.results
    assert '취소' in win.count_info.text()


def test_etf_checkbox_persists_and_clears_stale_recommendations(window):
    from app.config import Settings
    app, win = window
    win.scan()
    finish(app, win)
    assert win.results
    win.fields['include_etf'].setChecked(False)
    win.save_strategy()
    assert Settings().get('include_etf') is False
    assert not win.results and not win.stocks
    assert win.screen_table.rowCount() == 0
    win.fields['include_etf'].setChecked(True)
    win.save_strategy()
    assert Settings().get('include_etf') is True


def test_etf_settings_cannot_change_during_an_active_scan(window):
    app, win = window
    win.scanning = True
    win.fields['include_etf'].setChecked(False)
    win.save_strategy()
    assert win.cfg['include_etf'] is True
    win.scanning = False


def test_basic_navigation_and_advanced_toggle(window):
    app, win = window
    assert [win.nav.item(i).text() for i in range(win.nav.count()) if not win.nav.item(i).isHidden()] == [
        '홈', '종목 찾기', '연습 기록', '고르는 조건', '연결 설정']
    assert win.stack.currentIndex() == 0
    assert win.risk_panel.isHidden()
    win.advanced_toggle.setChecked(True)
    assert not win.nav.item(5).isHidden()
    assert not win.risk_panel.isHidden()
    win.nav.setCurrentRow(5)
    win.advanced_toggle.setChecked(False)
    assert win.stack.currentIndex() == 0


def test_modes_separate_credentials_records_and_money_labels(window):
    from app.modes import credential_names
    from app.market import Stock
    app, win = window
    win.mode_select.setCurrentIndex(1)
    win.secrets['kiwoom_appkey'].setText('mock')
    win.secrets['kiwoom_secret'].setText('mock')
    win.save_integrations()
    mock_db = win.db.path
    stock = Stock('900001', 'FAKE', 'KOSPI', 10000, 1, 200, 200, 5000)
    win.db.paper_open(stock, 1, 2, 4, 2, .015, .1, win.cfg, 3)
    win.mode_select.setCurrentIndex(2)
    assert not win.secrets['kiwoom_appkey'].text()
    win.secrets['kiwoom_appkey'].setText('real')
    win.secrets['kiwoom_secret'].setText('real')
    win.save_integrations()
    assert win.db.path != mock_db
    assert win.db.stats()['open_count'] == 0
    assert '실제 주문 잠금' in win.connection.text()
    assert '연습' in win.home_mode.text()
    assert win.creds.get(credential_names('MOCK')[0]) == 'mock'
    assert win.creds.get(credential_names('REAL')[0]) == 'real'
    win.mode_select.setCurrentIndex(1)
    assert win.secrets['kiwoom_appkey'].text() == 'mock'
    win.save_integrations()
    assert win.db.stats()['open_count'] == 1
    assert win.db.path == mock_db


def test_legacy_credentials_only_used_for_original_server(window):
    app, win = window
    win.cfg['legacy_kiwoom_env'] = 'REAL'
    win.creds.values.update(kiwoom_appkey='old', kiwoom_secret='old')
    assert win.broker_keys('REAL') == ('old', 'old')
    assert win.broker_keys('MOCK') == ('', '')


def test_old_database_is_only_read_and_not_merged(window, monkeypatch):
    import hashlib
    from PySide6.QtWidgets import QDialog
    from app.storage import Database
    from app.config import data_dir
    app, win = window
    legacy_path = data_dir() / 'trading.db'
    Database(legacy_path)
    before = hashlib.sha256(legacy_path.read_bytes()).digest()
    monkeypatch.setattr(QDialog, 'exec', lambda self: 0)
    win.show_legacy_trades()
    assert hashlib.sha256(legacy_path.read_bytes()).digest() == before
    assert win.db.path != legacy_path


def test_old_key_environment_survives_mode_changes(tmp_path):
    import json
    from app.config import Settings
    (tmp_path / 'settings.json').write_text(json.dumps({'data_mode':'KIWOOM', 'kiwoom_env':'REAL'}))
    settings = Settings(tmp_path)
    assert settings.get('legacy_kiwoom_env') == 'REAL'
    settings.set('kiwoom_env', 'MOCK')
    settings.save()
    assert Settings(tmp_path).get('legacy_kiwoom_env') == 'REAL'
