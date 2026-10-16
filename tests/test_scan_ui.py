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
    monkeypatch.setattr(gui, 'Credentials', lambda: type('NoCredentials', (), {'get': lambda self, key: ''})())
    monkeypatch.setattr(QMessageBox, 'warning', lambda *args: None)
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

        def scan(self, market, progress, cancelled):
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

        def scan(self, market, progress, cancelled):
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
