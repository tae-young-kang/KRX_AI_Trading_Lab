"""PySide6 desktop GUI - research / paper trading only (NO LIVE ORDER CODE)."""
from __future__ import annotations
import json
import hashlib
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QFrame, QStackedWidget, QListWidget, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QComboBox, QSpinBox,
    QDoubleSpinBox, QCheckBox, QFormLayout, QLineEdit, QScrollArea, QFileDialog,
    QInputDialog, QAbstractItemView, QTextEdit, QDialog, QTabWidget
)

from app.config import Settings, Credentials, data_dir
from app.storage import Database
from app.market import DemoMarket, KiwoomMarket, Telegram, recommendation_text
from app.strategy import evaluate, auto_paper_exit, create_proposals
from app.practice_quotes import fresh_practice_price
from app.recommendation_request import RecommendationRequest
from app.quote_batch_profile import QuoteBatchProfile
from app.recommendation_schedule import summary as recommendation_schedule_summary
from app.practice_details import sale_details
from app.theme_store import latest_report as theme_latest_report
from app.theme_input import prepare_selection as prepare_theme_selection
from app.theme_input import selection_from_days, prepare_days as prepare_theme_days
from app.theme_input import prepare_confirmed
from app.theme_request import ThemeRequest
from app.theme_collection import collect_and_prepare
from app.theme_delivery import delivery_summary as theme_delivery_summary
from app.theme_delivery import latest_snapshot as theme_snapshot, deliver_run as theme_deliver_run
from app.theme_budget import preview_latest as theme_budget_preview, preview_text as theme_budget_text
from app.nxt_evidence import record as record_nxt_evidence
from app.shadow import ShadowEngine
from app.backtest import load_ohlcv_csv, run_experiments, export_results
from app.krx_history import HistoryStore, KRXDailyClient
from app.research import evaluate_research, export_report
from app.modes import (MODES, current_mode, practice_db_name, credential_names,
                       telegram_start_guidance, MOCK_REFRESH_GUIDANCE, MOCK_LISTEN_GUIDANCE)
from app.trading_clock import PRICE_BASIS_NOTICE, KST
from app.trade_review import outcome, condition_label, settings_snapshot
from app.telegram_worker import TelegramOrderWorker
from app.telegram_conditions import ConditionCommands
from app.kiwoom_mock_watch import require_mock_mode
from app.kiwoom_mock_watch_worker import MockWatchWorker
from app.mock_watch_status import MockWatchStatus

STYLE = '''
QMainWindow,QWidget {background:#0b1220;color:#e6edf7;font:10pt 'Malgun Gothic';}
QFrame#Sidebar{background:#111e31;border-right:1px solid #293448;}
QFrame#Card{background:#152237;border:1px solid #2a3c56;border-radius:11px;}
QFrame#Panel{background:#121f33;border:1px solid #293b55;border-radius:10px;}
QLabel#H1{font-size:21pt;font-weight:700;color:#f4f8ff;}
QLabel#H2{font-size:13pt;font-weight:700;color:#dceaff;}
QLabel#Metric{font-size:23pt;font-weight:700;color:#5fd5c4;}
QLabel#Muted{color:#9bacc4;}
QListWidget{background:transparent;border:0;padding:12px 4px;outline:0;}
QListWidget::item{height:46px;border-radius:7px;padding:7px 13px;color:#b7c8df;}
QListWidget::item:selected{background:#173d56;color:#78e3d1;font-weight:700;}
QPushButton{background:#263a53;color:#f4f7fb;border:1px solid #3a526c;border-radius:7px;padding:9px 13px;font-weight:600;}
QPushButton:hover{background:#36506a;}
QPushButton#Primary{background:#087b76;border-color:#11a49a;color:white;}
QPushButton#Primary:hover{background:#0f9589;}
QPushButton#Danger{background:#6d2935;border-color:#b04b59;}
QTableWidget{background:#101d2f;alternate-background-color:#14243a;border:1px solid #293e58;gridline-color:#29394f;
 selection-background-color:#1a6574;selection-color:#ffffff;}
QHeaderView::section{background:#1c304b;color:#bdcee6;font-weight:700;padding:10px;border:0;border-right:1px solid #2b3b50;}
QTableWidget::item{padding:6px;}
QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QTextEdit{
 background:#192b43;color:white;border:1px solid #36506c;border-radius:6px;padding:7px;min-height:23px;}
QComboBox QAbstractItemView{background:#1b2d47;selection-background-color:#1a6774;}
QCheckBox{spacing:8px;color:#d5e3f3;}
QScrollArea{border:0;}
QStatusBar{background:#111e31;color:#b1c6df;}
'''


class Work(QThread):
    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    def __init__(self, func, with_progress=False):
        super().__init__()
        self.func = func
        self.with_progress = with_progress
    def run(self):
        try:
            result = (self.func(self.progress.emit, self.isInterruptionRequested)
                      if self.with_progress else self.func())
            self.succeeded.emit(result)
        except Exception as ex:
            self.failed.emit(str(ex))


def lbl(text, style=None):
    x = QLabel(text)
    if style:
        x.setObjectName(style)
    return x


def make_table(cols):
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setAlternatingRowColors(True)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setStretchLastSection(False)
    t.horizontalHeader().setMinimumSectionSize(70)
    t.horizontalHeader().setDefaultSectionSize(130)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    t.setSortingEnabled(False)
    return t


def cell(value, align=Qt.AlignmentFlag.AlignCenter, positive=None):
    x = QTableWidgetItem(str(value))
    x.setTextAlignment(align)
    if positive is not None:
        from PySide6.QtGui import QColor
        x.setForeground(QColor('#6de2bc' if positive >= 0 else '#ff8290'))
    return x


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings()
        self.cfg = self.settings.values
        self.condition_commands = ConditionCommands(data_dir()/'telegram_conditions.db', self.cfg)
        self.creds = Credentials()
        self.db = Database(data_dir() / practice_db_name(self.cfg))
        self.demo = DemoMarket()
        self.quote_batch_profile=QuoteBatchProfile()
        self.shadow = ShadowEngine(self.db)
        self.history = HistoryStore(data_dir() / 'research.db')
        self.research_report = None
        self.research_run_id = None
        self.shadow.initialize_defaults(dict(self.cfg), self.cfg['data_mode'])
        self.backtest_runs = []
        self.backtest_file = ''
        self.last_backtest_dir = None
        self.results = []
        self.stocks = []
        self.work = None
        self.telegram_order_worker = None
        self.mock_watch_worker = None
        self.mock_watch_status = MockWatchStatus(history_path=data_dir()/'kiwoom_mock_watch'/'observations.db')
        self.scanning = False
        self.remote_scan = None
        self.remote_scan_active = False
        self.remote_theme = None
        self.remote_theme_active = False
        self.setWindowTitle('KRX 주식 도우미 · 실제 주문 잠금')
        self.resize(1390, 860)
        self.setMinimumSize(1100, 690)
        self.setStyleSheet(STYLE)
        self.build()
        self.timer = QTimer(self)
        self.timer.setInterval(max(10, int(self.cfg['scan_interval_sec'])) * 1000)
        self.timer.timeout.connect(self.scan)
        self.condition_timer = QTimer(self)
        self.condition_timer.setInterval(1000)
        self.condition_timer.timeout.connect(self.sync_telegram_conditions)
        self.condition_timer.start()
        self.remote_scan_timer = QTimer(self)
        self.remote_scan_timer.setInterval(1000)
        self.remote_scan_timer.timeout.connect(self.poll_remote_scan)
        self.remote_scan_timer.start()
        self.refresh_stats()
        self.refresh_trades()
        self.refresh_proposals()
        self.refresh_shadow()
        self.daily_research_timer=QTimer(self)
        self.daily_research_timer.setInterval(10*60*1000)
        self.daily_research_timer.timeout.connect(self.maybe_daily_research)
        self.daily_research_timer.timeout.connect(self.maybe_daily_recommendation_evaluation)
        self.daily_research_timer.start()
        QTimer.singleShot(25000,self.maybe_daily_research)
        QTimer.singleShot(30000,self.maybe_daily_recommendation_evaluation)
        self.db.log('START', 'GUI 실행 - 실계좌 주문 기능 없음')

    def build(self):
        main = QWidget()
        self.setCentralWidget(main)
        layout = QHBoxLayout(main)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(0)
        sidebar = QFrame(objectName='Sidebar')
        sidebar.setFixedWidth(218)
        left = QVBoxLayout(sidebar)
        left.setContentsMargins(13,25,13,18)
        left.addWidget(lbl('  ◉  KRX AI LAB', 'H2'))
        left.addWidget(lbl('    TRADING LAB  /  v0.3', 'Muted'))
        left.addSpacing(30)
        self.nav = QListWidget()
        for item in ['홈','종목 찾기','연습 기록',
                     '전략 비교 · 고급','과거 자료로 시험 · 고급',
                     '종목 선정 연구 · 고급','고르는 조건','조건 개선안 · 고급','연결 설정']:
            self.nav.addItem(item)
        left.addWidget(self.nav,1)
        self.advanced_toggle = QCheckBox('연구·고급 메뉴 보기')
        self.advanced_toggle.setChecked(bool(self.cfg['show_advanced']))
        self.advanced_toggle.toggled.connect(self.set_advanced)
        left.addWidget(self.advanced_toggle)
        self.demo_badge = lbl('실제 돈의 주문: 잠금', 'Muted')
        self.demo_badge.setWordWrap(True)
        left.addWidget(self.demo_badge)
        layout.addWidget(sidebar)
        outer = QVBoxLayout()
        outer.setContentsMargins(23,18,23,15)
        outer.setSpacing(13)
        bar = QHBoxLayout()
        self.title = lbl('대시보드', 'H1')
        bar.addWidget(self.title)
        bar.addStretch()
        self.connection = lbl('● DEMO', 'Muted')
        bar.addWidget(self.connection)
        self.scan_button = QPushButton('종목 찾기',objectName='Primary')
        self.scan_button.clicked.connect(self.scan)
        bar.addWidget(self.scan_button)
        self.cancel_scan_button = QPushButton('조회 취소')
        self.cancel_scan_button.setEnabled(False)
        self.cancel_scan_button.clicked.connect(self.cancel_scan)
        bar.addWidget(self.cancel_scan_button)
        self.auto_button = QPushButton('자동 검색 시작')
        self.auto_button.clicked.connect(self.toggle_auto)
        bar.addWidget(self.auto_button)
        outer.addLayout(bar)
        self.notice = lbl('데모 모드의 종목·가격은 생성된 가상 데이터입니다. 이 프로그램은 실주문을 수행하지 않습니다.','Muted')
        self.notice.setWordWrap(True)
        outer.addWidget(self.notice)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack,1)
        for fn in (self.make_dashboard, self.make_screener, self.make_trades,
                   self.make_shadow, self.make_backtest, self.make_research, self.make_strategy, self.make_optimizer, self.make_integrations):
            self.stack.addWidget(fn())
        layout.addLayout(outer,1)
        self.statusBar().showMessage(f'데이터 저장 위치: {data_dir()}')
        self.nav.currentRowChanged.connect(self.move_tab)
        self.nav.setCurrentRow(0)
        self.update_connection()
        self.set_advanced(self.cfg['show_advanced'], persist=False)

    def move_tab(self, row):
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        self.title.setText(['홈','종목 찾기','연습으로 사고판 기록','전략별 연습 결과','과거 자료로 시험', '종목 선정 연구','어떤 종목을 고를까요?',
                            '조건 개선안','연결과 사용 모드'][row])
        if row == 2:
            self.refresh_trades()
        if row == 3:
            self.refresh_shadow()
        if row == 5:
            self.refresh_research()
        if row == 7:
            self.refresh_proposals()

    def set_advanced(self, enabled, persist=True):
        for row in (3, 4, 5, 7):
            self.nav.item(row).setHidden(not enabled)
        if not enabled and self.nav.currentRow() in (3, 4, 5, 7):
            self.nav.setCurrentRow(0)
        for col in (6, 7, 8, 9, 12):
            self.screen_table.setColumnHidden(col, not enabled)
        for label, widget in self.advanced_fields:
            label.setVisible(enabled)
            widget.setVisible(enabled)
        self.risk_panel.setVisible(enabled)
        self.order_demo_button.setVisible(enabled)
        if persist:
            self.cfg['show_advanced'] = bool(enabled)
            self.settings.save()

    def card(self, heading, name):
        frame = QFrame(objectName='Card')
        v = QVBoxLayout(frame)
        v.setContentsMargins(19,14,19,14)
        v.addWidget(lbl(heading,'Muted'))
        metric = lbl('—','Metric')
        metric.setObjectName('Metric')
        metric.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(metric)
        setattr(self, name, metric)
        return frame

    def make_dashboard(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(15)
        self.home_mode = lbl('', 'H2')
        self.home_mode.setWordWrap(True)
        v.addWidget(self.home_mode)
        guide = lbl('처음이라면 ① 연결 설정에서 모드 선택 → ② 고르는 조건에서 시장·ETF 선택 → ③ 종목 찾기 → ④ 텔레그램으로 보내기', 'Muted')
        guide.setWordWrap(True)
        v.addWidget(guide)
        shortcuts = QHBoxLayout()
        for text, row in [('1. 연결 설정', 8), ('2. 고르는 조건', 6), ('3. 종목 찾기', 1)]:
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, target=row: self.nav.setCurrentRow(target))
            shortcuts.addWidget(button)
        live = QPushButton('실제 투자 · 준비 중')
        live.setEnabled(False)
        shortcuts.addWidget(live)
        v.addLayout(shortcuts)
        history_button = QPushButton('지난 추천 확인하기 · 기록과 이후 가격')
        history_button.clicked.connect(self.show_recommendation_history)
        v.addWidget(history_button)
        theme_button=QPushButton('테마1 · 저장된 감지 종목 보기')
        theme_button.clicked.connect(self.show_theme_history)
        v.addWidget(theme_button)
        cards = QHBoxLayout()
        cards.addWidget(self.card('연습으로 번 돈 / 잃은 돈', 'kpi_pnl'))
        cards.addWidget(self.card('이익을 낸 연습 비율', 'kpi_win'))
        cards.addWidget(self.card('끝난 연습 거래', 'kpi_count'))
        cards.addWidget(self.card('연습 중인 종목', 'kpi_open'))
        v.addLayout(cards)
        v.addWidget(lbl('최근 검색 종목 · 추천 상위', 'H2'))
        self.dashboard_table = make_table(['종목','시장','현재가','등락률','점수','선정'])
        v.addWidget(self.dashboard_table,2)
        bottom = QFrame(objectName='Panel')
        b = QHBoxLayout(bottom)
        b.addWidget(lbl('실험 메모  |  수익률·손절·승률은 가상체결 기준이며 실제 체결이나 미래 수익을 보장하지 않습니다.','Muted'))
        v.addWidget(bottom)
        return page

    def show_theme_history(self):
        mode=current_mode(self.cfg)
        dialog=QDialog(self)
        dialog.setWindowTitle(mode.title+' · 테마1 저장 결과')
        dialog.resize(800,600)
        layout=QVBoxLayout(dialog)
        guide=lbl('저장된 감지 결과예요. 새 판정과 알림 발송은 아래 버튼에서 각각 선택하세요. 텔레그램 조회: /테마1','Muted')
        guide.setWordWrap(True)
        layout.addWidget(guide)
        prepare_button=QPushButton('새 테마1 판정 만들기 · 자료 선택')
        prepare_button.clicked.connect(self.prepare_theme_guided)
        layout.addWidget(prepare_button)
        view=QTextEdit()
        view.setReadOnly(True)
        layout.addWidget(view)
        page=QSpinBox()
        page.setPrefix('페이지 ')
        page.setRange(1,999999)
        layout.addWidget(page)
        def load(number):
            try:
                text=theme_latest_report(self.history,mode=mode.key,page=number)
            except Exception:
                text='테마1 기록을 읽지 못했어요. 저장 공간과 기록 파일을 확인해 주세요.'
            view.setPlainText(text)
        page.valueChanged.connect(load)
        load(1)
        send_button=QPushButton('저장된 최신 테마1 종목을 텔레그램으로 보내기')
        send_button.clicked.connect(self.send_theme_alerts)
        layout.addWidget(send_button)
        continue_button=QPushButton('미시도 알림만 이어 보내기')
        continue_button.clicked.connect(lambda:self.send_theme_alerts(continue_unattempted=True))
        layout.addWidget(continue_button)
        budget_button=QPushButton('총예산으로 예상 주수 보기 · 주문 안 함')
        budget_button.clicked.connect(self.show_theme_budget)
        layout.addWidget(budget_button)
        close=QPushButton('닫기')
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        dialog.exec()

    def show_theme_budget(self):
        total,ok=QInputDialog.getInt(self,'1/3 · 총예산','전체 종목에 나눌 총예산 (원)',1000000,1,2000000000,10000)
        if not ok:return
        fee,ok=QInputDialog.getDouble(self,'2/3 · 매수 비용 가정','매수 수수료 가정 (%) · 실제 증권사 비용과 다를 수 있어요',0,0,100,2)
        if not ok:return
        buffer,ok=QInputDialog.getDouble(self,'3/3 · 가격 여유','가격 상승에 대비해 남겨둘 여유 (%)',0,0,100,2)
        if not ok:return
        try:
            text=theme_budget_text(theme_budget_preview(self.history,mode=current_mode(self.cfg).key,total_won=total,
                                                       fee_bps=round(fee*100),buffer_bps=round(buffer*100)))
        except ValueError as error:
            QMessageBox.information(self,'예산 미리보기 확인',str(error));return
        dialog=QDialog(self)
        dialog.setWindowTitle('테마1 예산 미리보기 · 주문 안 함')
        dialog.resize(800,600)
        layout=QVBoxLayout(dialog)
        view=QTextEdit();view.setReadOnly(True);view.setPlainText(text);layout.addWidget(view)
        close=QPushButton('닫기');close.clicked.connect(dialog.accept);layout.addWidget(close)
        dialog.exec()

    def send_theme_alerts(self, checked=False, *, continue_unattempted=False):
        mode=current_mode(self.cfg).key
        try:
            snapshot=theme_snapshot(self.history,mode)
            chat_id=int(self.cfg['telegram_chat_id'])
            token=self.creds.get('telegram_token')
            if chat_id<=0 or not token or snapshot['count']==0:
                raise ValueError('감지 종목과 저장된 텔레그램 연결 정보를 확인해 주세요.')
        except (ValueError,TypeError) as error:
            QMessageBox.information(self,'테마1 알림 확인',str(error))
            return
        if QMessageBox.question(self,'테마1 알림 보내기',
                f'{snapshot["day"]} · {snapshot["source"]} · {snapshot["count"]}종목\n'
                '저장된 개인 대화방으로 종목별 알림을 보냅니다. 주문은 하지 않아요.\n'
                +('기존 미확인 알림은 건너뛰고 미시도 알림만 보냅니다.\n' if continue_unattempted else '')+
                '이미 시도한 알림은 다시 보내지 않으며 새 미확인 응답에서 중단합니다. 진행할까요?')!=QMessageBox.StandardButton.Yes:
            return
        def send():
            from app.telegram_receiver import TelegramTransport
            client=TelegramTransport(token)
            try:
                return theme_deliver_run(self.history,mode=mode,run_id=snapshot['run_id'],transport=client,chat_id=chat_id,
                                         continue_unattempted=continue_unattempted)
            finally:
                client.close()
        self.run_task(send,lambda result:QMessageBox.information(self,'테마1 알림 결과',
            f'발송 응답 확인(기존 포함) {result["SENT"]}개 · 미확인 {result["UNKNOWN"]}개 · 남은 {result["remaining"]}개\n'
            '자세한 기록은 /테마1알림상태에서 확인하세요.'),'테마1 알림 처리 완료')

    def show_recommendation_history(self):
        mode = current_mode(self.cfg)
        try:
            reports = [('추천 기록',self.db.recommendation_summary(mode.key)),
                       ('이후 가격',self.db.recommendation_summary(mode.key,performance=True)),
                       ('조건별 요약',self.db.recommendation_overview(mode.key))]
            page_count = self.db.recommendation_page_count(mode.key)
        except Exception:
            QMessageBox.information(self,'추천 기록 확인','기록을 읽지 못했어요. 집 PC의 저장 공간과 기록 파일 상태를 확인해 주세요.')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(mode.title+' · 지난 추천 확인하기')
        layout = QVBoxLayout(dialog)
        guide = lbl('① 추천 기록 → ② 이후 가격 → ③ 조건별 요약 순서로 확인하세요.\n'
                    '자료가 없으면 아직 평가할 수 없어요. 실제 매매 수익률과는 달라요.','Muted')
        guide.setWordWrap(True)
        layout.addWidget(guide)
        tabs = QTabWidget()
        views = []
        for title,text in reports:
            view = QTextEdit()
            view.setReadOnly(True)
            view.setPlainText(text)
            tabs.addTab(view,title)
            views.append(view)
        layout.addWidget(tabs)
        navigation = QHBoxLayout()
        navigation.addWidget(lbl('추천 기록·이후 가격 페이지'))
        page_number = QSpinBox()
        page_number.setRange(1,min(page_count,999999))
        navigation.addWidget(page_number)
        navigation.addWidget(lbl(f'전체 {page_count:,}페이지 · 5개씩 표시','Muted'))
        navigation.addStretch()
        layout.addLayout(navigation)
        def load_page(number):
            try:
                texts = [self.db.recommendation_summary(mode.key,page=number),
                         self.db.recommendation_summary(mode.key,performance=True,page=number)]
            except Exception:
                texts = ['기록을 읽지 못했어요. 창을 닫고 기록 파일 상태를 확인해 주세요.']*2
            for view,text in zip(views,texts):
                view.setPlainText(text)
        page_number.valueChanged.connect(load_page)
        note = lbl('새 검색부터 추천을 보관해요. 확인한 가격 자료는 고급 연구 화면에서 넣을 수 있어요.\n'
                   '텔레그램에서는 /추천기록 · /추천성적 · /추천요약을 보내세요.','Muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        close_button = QPushButton('닫기')
        close_button.clicked.connect(dialog.accept)
        layout.addWidget(close_button)
        dialog.resize(900,650)
        dialog.exec()

    def make_screener(self):
        page = QWidget()
        v = QVBoxLayout(page)
        bar = QHBoxLayout()
        bar.addWidget(lbl('조건에 맞는 종목이 위에 나옵니다. 순위는 정해 둔 계산식이며 수익을 보장하지 않습니다.','Muted'))
        bar.addStretch()
        self.count_info = lbl('검색 전','Muted')
        bar.addWidget(self.count_info)
        v.addLayout(bar)
        self.screen_table = make_table(['순위','종목코드','종목명','시장','현재가','등락률',
                                        '거래대금(억)','전일 거래량비(%)','시총(억)','점수','필터','시세 기준','조회 시각'])
        v.addWidget(self.screen_table)
        control = QHBoxLayout()
        control.addWidget(lbl('※ 선택 시장 전체 종목을 조회한 뒤 필터링합니다. 순차 조회이며 실시간 동시 시세가 아닙니다.','Muted'))
        control.addStretch()
        btn = QPushButton('연습으로 1주 사기 · 실제 주문 아님', objectName='Primary')
        btn.clicked.connect(self.paper_buy)
        control.addWidget(btn)
        telegram = QPushButton('추천 목록 텔레그램 발송')
        telegram.clicked.connect(self.telegram_recommend)
        control.addWidget(telegram)
        v.addLayout(control)
        return page

    def make_trades(self):
        page = QWidget()
        v = QVBoxLayout(page)
        row = QHBoxLayout()
        self.stats_line = lbl('거래내역 없음','H2')
        self.stats_line.setWordWrap(True)
        v.addWidget(self.stats_line)
        row.addStretch()
        close_btn = QPushButton('선택한 연습 종목 팔기')
        close_btn.clicked.connect(self.paper_manual_close)
        row.addWidget(close_btn)
        sales_btn = QPushButton('선택 종목의 매도 내역')
        sales_btn.clicked.connect(self.show_practice_sales)
        row.addWidget(sales_btn)
        daily_btn=QPushButton('연습 하루 결산 보기')
        daily_btn.clicked.connect(self.show_practice_daily)
        row.addWidget(daily_btn)
        v.addLayout(row)
        row = QHBoxLayout()
        row.addStretch()
        exp = QPushButton('매매기록 CSV 내보내기')
        exp.clicked.connect(self.export_trades)
        row.addWidget(exp)
        exits_export = QPushButton('나눠 판 내역 CSV')
        exits_export.clicked.connect(self.export_practice_exits)
        row.addWidget(exits_export)
        backup_button=QPushButton('현재 모드 기록 백업하기')
        backup_button.clicked.connect(self.backup_current_records)
        row.addWidget(backup_button)
        verify_button=QPushButton('저장한 백업 확인하기')
        verify_button.clicked.connect(self.inspect_saved_backup)
        row.addWidget(verify_button)
        detail = QPushButton('선택 매매의 당시 조건 보기')
        detail.clicked.connect(self.show_trade_conditions)
        row.addWidget(detail)
        v.addLayout(row)
        self.trade_table = make_table(['ID','종목','구분','남은/처음 수량','매수가','평균 매도가','실현손익(원)',
                                       '손익률','매수일','청산일','사유','승패'])
        v.addWidget(self.trade_table)
        v.addWidget(lbl('앱 안의 연습 계산입니다. 손익은 일부 매도를 포함하며, 손익률·승패는 남은 수량을 모두 판 뒤 정해요. 비용은 가정값입니다.','Muted'))
        v.addWidget(lbl('내가 쓴 조건의 성적표', 'H2'))
        note = lbl('매수 당시 조건을 보관하고 매도 후 비용을 뺀 결과로 승·패·본전을 나눕니다. '
                   '일부만 판 종목도 남은 수량을 모두 팔기 전에는 승률에서 제외합니다. 비용 가정이 다른 분할매도는 별도로 표시합니다. '
                   '조건 기록이 없는 이전 매매는 성적표에서 제외합니다. '
                   '아래 기록은 모두 연습이며 실제 체결 기록이 아닙니다.', 'Muted')
        note.setWordWrap(True)
        v.addWidget(note)
        self.condition_table = make_table(['매수 당시 조건','완료 / 보유','승 / 패 / 본전',
                                          '승률','평균 손익률','기록 종류','평가'])
        v.addWidget(self.condition_table)
        compare = QPushButton('이 기록으로 조건 비교하기')
        compare.clicked.connect(self.create_optimizer)
        v.addWidget(compare)
        return page

    def make_shadow(self):
        page=QWidget(); v=QVBoxLayout(page)
        intro=lbl('미적용 조건도 다음 관측일 첫 스캔 시세로 독립 가상매매 · 사용자 승인 전 전략 자동적용 금지', 'Muted')
        intro.setWordWrap(True);v.addWidget(intro)
        self.shadow_summary=lbl('시장 데이터 수집 전','H2');v.addWidget(self.shadow_summary)
        self.shadow_table=make_table(['ID','실험 전략','데이터','상태','누적 수익률','실현손익','승률','거래','보유','MDD','마지막 기록'])
        self.shadow_table.itemSelectionChanged.connect(self.refresh_shadow_detail)
        v.addWidget(self.shadow_table,2)
        bar=QHBoxLayout()
        refresh=QPushButton('성과 새로고침');refresh.clicked.connect(self.refresh_shadow);bar.addWidget(refresh)
        export=QPushButton('섀도 전략 4종 CSV 내보내기',objectName='Primary');export.clicked.connect(self.export_shadow);bar.addWidget(export)
        bar.addStretch();v.addLayout(bar)
        v.addWidget(lbl('선택 전략의 일별 자산 변동 및 미체결/가격제한 사유', 'H2'))
        self.shadow_history=make_table(['날짜','자산 평가액','누적 실현','미실현','청산 거래','보유','시세 누락'])
        v.addWidget(self.shadow_history,1)
        self.shadow_pending=make_table(['추천일','종목','추천가','진입상태','평가일','사유'])
        v.addWidget(self.shadow_pending,1)
        v.addWidget(lbl('주의: DEMO는 합성시세입니다. 실제 키움 순위 데이터 역시 전일 거래량비이며, 첫 스캔 가격은 오전 8시 시가가 아닙니다. 호가·부분체결 미반영.','Muted'))
        return page

    def refresh_shadow(self):
        if not hasattr(self,'shadow_table'):return
        data=self.shadow.standings()
        sid=None
        if self.shadow_table.currentRow()>=0 and self.shadow_table.item(self.shadow_table.currentRow(),0):
            sid=int(self.shadow_table.item(self.shadow_table.currentRow(),0).text())
        self.shadow_table.blockSignals(True)
        self.shadow_table.setRowCount(len(data))
        for i,s in enumerate(data):
            val=('—' if not s['closed'] else f'{s["winrate"]:.1f}%')
            row=[s['id'],s['name'],s['data_source'],s['status'],f'{s["return_pct"]:+.2f}%',
                 f'{s["realized"]:+,.0f}',val,s['closed'],s['open'],f'{s["mdd"]:.2f}%',s['last_day']]
            for j,x in enumerate(row):
                self.shadow_table.setItem(i,j,cell(x,positive=s['return_pct'] if j in (4,5) else None))
            if sid==s['id']: self.shadow_table.selectRow(i)
        self.shadow_table.blockSignals(False)
        self.shadow_summary.setText(f'독립 가상전략 {len(data)}개 · {self.shadow.summarize_comparison(5,10)}')
        self.refresh_shadow_detail()

    def refresh_shadow_detail(self):
        if not hasattr(self,'shadow_table'):return
        idx=self.shadow_table.currentRow()
        sid=(int(self.shadow_table.item(idx,0).text())
             if idx>=0 and self.shadow_table.item(idx,0) else None)
        history=self.shadow.daily_history(sid) if sid is not None else []
        self.shadow_history.setRowCount(len(history))
        for i,r in enumerate(history):
            cols=[r['day'],f'{r["equity"]:,.0f}',f'{r["realized_pnl"]:+,.0f}',
                  f'{r["unrealized_pnl"]:+,.0f}',r['closed_count'],r['open_count'],r['stale_marks']]
            for j,x in enumerate(cols):self.shadow_history.setItem(i,j,cell(x))
        pending=self.shadow.pending_history(sid) if sid is not None else []
        self.shadow_pending.setRowCount(min(150,len(pending)))
        for i,r in enumerate(pending[:150]):
            cols=[r['signal_day'],r['name'],f'{r["signal_price"]:,.0f}',
                  r['status'],r['resolved_day'] or '—',r['reason'] or '—']
            for j,x in enumerate(cols):self.shadow_pending.setItem(i,j,cell(x))

    def export_shadow(self):
        path=QFileDialog.getExistingDirectory(self,'섀도 전략 CSV 내보낼 폴더',str(Path.home()))
        if path:
            files=self.shadow.export(path)
            QMessageBox.information(self,'내보내기',f'{len(files)}개 CSV 저장 완료\n{path}')

    def make_backtest(self):
        page=QWidget();v=QVBoxLayout(page)
        note=lbl('실제 OHLCV CSV를 직접 불러와 과거 20거래일 평균 거래량비 → 다음 거래일 정규장 시가 진입을 검증합니다. NXT 프리마켓 재현 아님.','Muted')
        note.setWordWrap(True);v.addWidget(note)
        top=QHBoxLayout()
        sample=str(Path(__file__).resolve().parent.parent/'sample_data'/'DEMO_synthetic_ohlcv.csv')
        saved=self.cfg.get('last_backtest_csv','')
        self.bt_path=QLineEdit(saved if saved and Path(saved).is_file() else sample)
        self.bt_path.setPlaceholderText('OHLCV CSV: date,code,market,open,high,low,close,volume,trade_value_eok,cap_eok')
        top.addWidget(self.bt_path,1)
        select=QPushButton('CSV 선택');select.clicked.connect(self.choose_backtest_csv);top.addWidget(select)
        start=QPushButton('조건별 백테스트 실행',objectName='Primary');start.clicked.connect(self.do_backtest);top.addWidget(start)
        v.addLayout(top)
        self.bt_status=lbl('CSV 파일을 선택하세요 · 14개 사전등록 실험 조합을 동일한 데이터에 적용합니다.','H2')
        v.addWidget(self.bt_status)
        self.bt_table=make_table(['실험','분류','매수신호','진입건','갭 제한 탈락','승률','실현 순손익','평가 수익률','MDD'])
        self.bt_table.itemSelectionChanged.connect(self.show_backtest_detail)
        v.addWidget(self.bt_table,2)
        v.addWidget(lbl('선택 전략의 실제로 가정한 매수·매도 기록 / 탈락 이유', 'H2'))
        self.bt_trades=make_table(['종목','신호일','매수일','매수가','매도일','매도가','매도사유','순손익','순수익률'])
        v.addWidget(self.bt_trades,1)
        self.bt_note=lbl('데이터 부족, 시가총액 미제공, 거래량 기준 불충족, 갭 제한 등 탈락 사유를 CSV로 내보낼 수 있습니다.','Muted')
        self.bt_note.setWordWrap(True);v.addWidget(self.bt_note)
        save=QPushButton('모든 실험·신호·매매·일별 자산 4종 CSV 내보내기');save.clicked.connect(self.export_backtest)
        v.addWidget(save)
        return page

    def choose_backtest_csv(self):
        path,_=QFileDialog.getOpenFileName(self,'일별 OHLCV CSV 선택',str(Path.home()),'CSV (*.csv)')
        if path:self.bt_path.setText(path)

    def do_backtest(self):
        path=self.bt_path.text().strip()
        if not Path(path).is_file():
            QMessageBox.warning(self,'파일 오류','존재하는 CSV를 선택하세요.');return
        cfg=dict(self.cfg)
        def runner():
            rows=load_ohlcv_csv(path)
            runs=run_experiments(rows,cfg)
            folder=(data_dir()/'backtest_history'/
                    (datetime.now().strftime('%Y%m%d_%H%M%S_')+uuid.uuid4().hex[:7]))
            export_results(folder,runs)
            digest=hashlib.sha256(Path(path).read_bytes()).hexdigest()
            (folder/'manifest.json').write_text(json.dumps({
                'generated_at':datetime.now().isoformat(timespec='seconds'),
                'input_file':Path(path).name,'input_sha256':digest,
                'bar_count':len(rows),'strategies':len(runs),
                'parameters':cfg,'data_kind':'USER CSV / HISTORICAL PAPER',
                'execution_model':'NEXT SESSION OPEN / OHLC STOP FIRST'
            },ensure_ascii=False,indent=2),encoding='utf-8')
            return len(rows),runs,folder
        self.bt_status.setText('계산 중… · 과거 자료를 읽고 있습니다.')
        self.run_task(runner,self.on_backtest,'과거 데이터 실험 완료')

    def on_backtest(self,result):
        num,runs,folder=result
        self.backtest_runs=runs
        self.last_backtest_dir=folder
        self.bt_table.setRowCount(len(runs))
        for i,r in enumerate(runs):
            s=r['summary']
            cols=[r['name'],r['category'],s['signals'],s['bought'],s['skip_gap'],
                  f'{s["winrate"]:.1f}%' if s['winrate'] is not None else '표본 없음',
                  f'{s["realized_pnl"]:+,.0f}원',f'{s["return_pct"]:+.2f}%',f'{s["mdd"]:.2f}%']
            for j,x in enumerate(cols):self.bt_table.setItem(i,j,cell(x,positive=s['realized_pnl'] if j in (6,7) else None))
        self.bt_status.setText(f'{num:,}개 OHLCV 행 · 전략 {len(runs)}개 · 자동 저장: {folder} · 실제 체결 아님.')
        self.cfg['last_backtest_csv']=self.bt_path.text().strip()
        self.settings.save()
        self.db.log('BACKTEST',f'과거 CSV 실험 {num}행, {len(runs)}전략 (데이터 원본 로컬 경로 미기록)')
        self.bt_table.selectRow(0)

    def show_backtest_detail(self):
        idx=self.bt_table.currentRow()
        if idx<0 or idx>=len(self.backtest_runs):return
        r=self.backtest_runs[idx]
        trades=r['trades']
        self.bt_trades.setRowCount(len(trades))
        for i,t in enumerate(trades):
            cols=[t['name'],t['signal_day'],t['entry_day'],f'{t["entry_fill"]:,.0f}',
                  t['exit_day'],f'{t["exit_fill"]:,.0f}',t['exit_reason'],
                  f'{t["pnl"]:+,.0f}',f'{t["pnl_pct"]:+.2f}%']
            for j,x in enumerate(cols):self.bt_trades.setItem(i,j,cell(x,positive=t['pnl'] if j in (7,8) else None))
        s=r['summary']
        self.bt_note.setText(f'추천 신호 {s["signals"]}건 / 매수 {s["bought"]}건 / 갭상승 거절 {s["skip_gap"]}건 / '
                             f'보유종목 제한 {s["skip_capacity"]}건 / 자금 부족 {s["skip_cash"]}건 / '
                             '손절·익절 동시 터치 시 손절 우선 · 체결가는 비용을 반영한 가정값입니다.')

    def export_backtest(self):
        if not self.backtest_runs:
            QMessageBox.information(self,'기록 없음','먼저 백테스트를 실행하세요.');return
        folder=QFileDialog.getExistingDirectory(self,'결과 CSV 내보낼 폴더',str(Path.home()))
        if folder:
            files=export_results(folder,self.backtest_runs)
            QMessageBox.information(self,'내보내기',f'{len(files)}개 CSV 저장\n{folder}')

    def make_research(self):
        page=QWidget(); v=QVBoxLayout(page);v.setSpacing(12)
        title=lbl('종목을 얼마나 잘 고르는지 검증하는 연구실','H2');v.addWidget(title)
        note=lbl('KRX 시세·시가총액의 일봉 기준입니다. 매일 종가에 선정 → 다음 거래일 시가 가상진입, 1·3·5일 보유 수익률 검증.\n'
                 '실제 자동주문·AI 예측확률이 아닙니다. 미적용 조건은 독립 비교되며 사용자 승인 없이 실전 적용되지 않습니다.','Muted')
        note.setWordWrap(True);v.addWidget(note)
        recommendation_import = QPushButton('저장된 추천의 가격 자료 넣기 (고급)')
        recommendation_import.setToolTip('직접 확인한 거래일·종가 자료 파일을 저장된 추천과 대조해요. 실제 수익률이나 공식 검증 완료로 처리하지 않아요.')
        recommendation_import.clicked.connect(self.import_recommendation_prices)
        v.addWidget(recommendation_import)
        recommendation_cached = QPushButton('저장된 일별 시세로 추천 성적 계산하기 (고급)')
        recommendation_cached.clicked.connect(self.evaluate_cached_recommendations)
        v.addWidget(recommendation_cached)
        recommendation_repeat = QPushButton('저장한 거래일로 추천 성적 다시 계산하기 (고급)')
        recommendation_repeat.clicked.connect(self.repeat_cached_recommendations)
        v.addWidget(recommendation_repeat)
        recommendation_auto = QCheckBox('PC가 켜져 있으면 매일 저녁 추천 성적 갱신 (저장 자료만 사용)')
        recommendation_auto.setChecked(self.cfg.get('recommendation_auto_evaluate') is True)
        recommendation_auto.toggled.connect(self.save_auto_recommendation_evaluation)
        v.addWidget(recommendation_auto)
        self.recommendation_auto_status=lbl('20:30 이후 약 10분 간격 확인 · 확인한 거래일 범위만 평가 · 자동 보고 발송/주문 아님','Muted')
        self.recommendation_auto_status.setWordWrap(True)
        v.addWidget(self.recommendation_auto_status)
        theme_prepare=QPushButton('테마1 · 저장 시세와 확인한 거래일로 판정하기 (고급)')
        theme_prepare.clicked.connect(self.prepare_theme_one)
        v.addWidget(theme_prepare)
        box=QFrame(objectName='Panel');form=QGridLayout(box)
        self.research_source=QComboBox();self.research_source.addItems(['CSV_USER','KRX','DEMO_SYNTHETIC'])
        form.addWidget(lbl('연구 데이터'),0,0);form.addWidget(self.research_source,0,1)
        self.research_source.currentTextChanged.connect(self.research_source_changed)
        self.research_k=QSpinBox();self.research_k.setRange(1,50);self.research_k.setValue(5)
        form.addWidget(lbl('일별 추천 Top-K'),0,2);form.addWidget(self.research_k,0,3)
        self.research_start=QLineEdit((datetime.now()-timedelta(days=180)).strftime('%Y-%m-%d'))
        form.addWidget(lbl('KRX 수집 시작일'),1,0);form.addWidget(self.research_start,1,1)
        self.research_end=QLineEdit(datetime.now().strftime('%Y-%m-%d'))
        form.addWidget(lbl('KRX 수집 종료일'),1,2);form.addWidget(self.research_end,1,3)
        self.research_auto=QCheckBox('KRX 키 등록 후, GUI 실행 중 매 거래일 17시 이후 자동 수집·조건 비교')
        self.research_auto.setChecked(bool(self.cfg.get('research_auto_daily',False)))
        self.research_auto.toggled.connect(self.save_auto_research)
        v.addWidget(box)
        v.addWidget(self.research_auto)
        buttons=QHBoxLayout()
        for label,fn in [('CSV 불러오기',self.import_research_csv),('합성 예제 가져오기',self.import_research_demo),
                         ('KRX 과거 시세 수집',self.collect_krx),('48개 조건 검증',self.run_research),
                         ('결과 CSV 내보내기',self.export_research)]:
            b=QPushButton(label,objectName='Primary' if label=='48개 조건 검증' else '')
            b.clicked.connect(fn);buttons.addWidget(b)
        v.addLayout(buttons)
        check_coverage=QPushButton('저장 일봉과 외부 거래일·최소 종목 수 대조 (고급)')
        check_coverage.setToolTip('별도로 확인한 market,day,min_symbols CSV와 선택한 연구 DB를 읽기 전용으로 비교해요.')
        check_coverage.clicked.connect(self.audit_research_market_coverage)
        v.addWidget(check_coverage)
        decision=QHBoxLayout()
        a=QPushButton('선택 조건 연구 프리셋 승인');a.clicked.connect(lambda:self.review_research(True));decision.addWidget(a)
        b=QPushButton('선택 조건 거부');b.clicked.connect(lambda:self.review_research(False));decision.addWidget(b)
        decision.addStretch();v.addLayout(decision)
        self.research_status=lbl('시세를 준비한 후 조건 검증을 시작하세요.','Muted')
        self.research_status.setWordWrap(True);v.addWidget(self.research_status)
        self.research_result=lbl('학습 구간에서만 선정한 후보 전략을 검증 구간에서 확인합니다.','H2')
        self.research_result.setWordWrap(True);v.addWidget(self.research_result)
        self.research_table=make_table(['실험 조건','시장','학습 거래일','학습 초과수익 3일','검증 거래일',
                                        '검증 초과수익 3일','검증 Rank IC','학습→검증 차이(%p)',
                                        'TEST 초반→후반','자료/결과 해석','승인 상태'])
        self.research_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
        self.research_table.itemSelectionChanged.connect(self.research_detail)
        v.addWidget(self.research_table,4)
        self.research_detail_label=lbl('선택한 조건의 세부 검증 결과가 표시됩니다.','Muted')
        self.research_detail_label.setWordWrap(True);v.addWidget(self.research_detail_label)
        v.addWidget(lbl('※ 3일 수익률은 다음 날 시가→3번째 거래일 종가, 왕복 비용 0.25%p 가정. '
                        '일별 추천이 겹치므로 수익률을 누적 실전계좌 수익처럼 해석하면 안 됩니다.','Muted'))
        return page

    def research_source_changed(self):
        self.research_report=None
        self.research_run_id=None
        # The external evidence applies ONLY to the source the user audited.
        self.research_reference=None
        self.refresh_research()

    def save_auto_research(self,checked):
        self.cfg['research_auto_daily']=bool(checked)
        self.settings.save()

    def save_auto_recommendation_evaluation(self,checked):
        self.cfg['recommendation_auto_evaluate']=bool(checked)
        self.settings.save()

    def maybe_daily_recommendation_evaluation(self):
        from app.recommendation_schedule import run_due
        if self.cfg.get('recommendation_auto_evaluate') is not True or (self.work and self.work.isRunning()):
            return
        db,history,mode=self.db,self.history,current_mode(self.cfg).key
        def finish(result):
            if result['state']=='SUCCEEDED':
                self.recommendation_auto_status.setText('저장 자료로 추천 성적 갱신 완료 · 확인한 기간 끝 '+result['result']['available_through'])
            elif result['state']=='FAILED':
                self.recommendation_auto_status.setText('자동 갱신 미완료 · 거래일/저장 자료를 확인해 수동 재평가하세요. 오늘 자동 재시도하지 않아요.')
            elif result['state']=='ALREADY_ATTEMPTED':
                labels={'SUCCEEDED':'완료','FAILED':'실패 · 수동 재평가 필요','RUNNING':'종료 미확인 · 수동 확인 필요'}
                self.recommendation_auto_status.setText('오늘 자동 평가 기록: '+labels.get(result['previous'],'확인 필요'))
        self.run_task(lambda:run_due(db,history,mode,enabled=True,now=datetime.now(KST)),finish,'추천 성적 자동 갱신 점검 완료')

    def maybe_daily_research(self):
        # Runs ONLY while GUI is alive; no hidden service. Does not send real orders.
        now=datetime.now()
        key=self.creds.get('krx_api_key')
        today=now.date().isoformat()
        if (not self.cfg.get('research_auto_daily') or now.hour<17 or now.weekday()>4
            or self.cfg.get('research_last_update')==today or not key
            or (self.work is not None and self.work.isRunning())):return
        start=(now-timedelta(days=7)).date().isoformat()
        def run():
            result=self.history.fetch_range(KRXDailyClient(key),start,today)
            bars=self.history.load_bars('KRX')
            if len(bars)<100:return result,None
            from app.research_preflight import check_research_coverage, auto_research_blocked
            selected_reference=getattr(self,'research_reference',None)
            reference_path=(selected_reference[1] if selected_reference and selected_reference[0]=='KRX' else None)
            quality=check_research_coverage(self.history,'KRX',bars,reference_path=reference_path)
            if auto_research_blocked(quality):
                # Automatic research must never silently promote suspect evidence.
                return result,('COVERAGE_ALERT',quality)
            report=evaluate_research(bars,topk=5)
            report['coverage_quality']=quality
            rid=self.history.save_run('KRX',{'source':'KRX','topk':5,'auto':True},report)
            return result,(rid,report)
        def finish(output):
            self.cfg['research_last_update']=today
            self.settings.save()
            if output[1] is not None and output[1][0]=='COVERAGE_ALERT':
                from app.research_preflight import preflight_notice
                self.statusBar().showMessage('자동 조건 연구 보류 · '+preflight_notice(output[1][1]))
                self.research_status.setText('자동 연구 보류: '+preflight_notice(output[1][1]))
                return
            if output[1] is not None:
                self.research_source.setCurrentText('KRX')
                self.research_run_id=output[1][0]
                self.show_research(output[1][1],show_message=False)
            self.statusBar().showMessage('KRX 일간 데이터·조건 평가 갱신 (승인 전 미적용)')
        self.run_task(run,finish,'KRX 자동 조건 평가 완료')

    def refresh_research(self):
        source=self.research_source.currentText()
        n=self.history.count(source)
        self.research_status.setText(f'[{source}] 저장 {n[0]:,}행 / {n[1]}거래일 / {n[2]}종목 / {n[3] or "—"} ~ {n[4] or "—"}  ·  연구 데이터 DB: research.db')
        self.research_status.setText(self.research_status.text()+'\n'+self.history.ingest_summary(source))
        latest=self.history.last_run(source)
        if latest and self.research_report is None:
            self.research_run_id=latest[0]
            self.research_report=latest[2]
            self.show_research(latest[2],show_message=False)

    def audit_research_market_coverage(self):
        if self.work is not None and self.work.isRunning():
            self.statusBar().showMessage('진행 중인 작업이 끝난 뒤 자료를 확인해 주세요.')
            return
        path,_=QFileDialog.getOpenFileName(
            self,'외부에서 확인한 거래일·최소 종목 수 CSV 선택',str(Path.home()),'CSV (*.csv)')
        if not path:
            return
        from app.market_coverage import audit_coverage, render_audit
        source=self.research_source.currentText()
        history=self.history
        def done(message):
            if self.research_source.currentText()==source:
                # Session-only selection. Do not persist local paths/contents.
                # The CSV and database are both checked again before research.
                self.research_reference=(source,path)
            QMessageBox.information(
                self,'저장 시세 누락 대조',
                message+'\n\n이 출처의 다음 48개 조건 검증에도 이 대조 자료를 다시 적용해요.')
        self.run_task(
            lambda: render_audit(audit_coverage(history,source,path)),
            done,
            '외부 입력과 저장 일봉의 누락 대조 완료')

    def import_research_csv(self):
        path,_=QFileDialog.getOpenFileName(self,'실제 OHLCV CSV 불러오기',str(Path.home()),'CSV (*.csv)')
        if not path:return
        self.run_task(lambda:self.history.import_csv(path,'CSV_USER'),
                      lambda n:(self.research_source.setCurrentText('CSV_USER'),self.refresh_research(),
                                QMessageBox.information(self,'CSV 저장',f'{n:,}행을 CSV_USER 데이터베이스에 등록했습니다.')),
                      'CSV 수집 완료')

    def import_research_demo(self):
        path=Path(__file__).resolve().parent.parent/'sample_data'/'DEMO_research_ohlcv.csv'
        if not path.exists():
            QMessageBox.warning(self,'예제 없음','예제 CSV를 찾지 못했습니다.');return
        self.run_task(lambda:self.history.import_csv(path,'DEMO_SYNTHETIC'),
                      lambda n:(self.research_source.setCurrentText('DEMO_SYNTHETIC'),self.refresh_research(),
                                QMessageBox.information(self,'합성 데이터',f'{n:,}행을 가져왔습니다. 실제 주가가 아닙니다.')),
                      '합성 예제 준비 완료')

    def collect_krx(self):
        key=self.secrets['krx_api_key'].text().strip() or self.creds.get('krx_api_key')
        if not key:
            QMessageBox.information(self,'KRX 인증키 필요','[API · 텔레그램]에서 별도의 KRX OPEN API 인증키를 먼저 등록하세요.');return
        start=self.research_start.text().strip();end=self.research_end.text().strip()
        if QMessageBox.question(self,'KRX 과거 시세 수집',
              f'{start} ~ {end}의 KOSPI·KOSDAQ 일봉을 KRX에 요청합니다.\n'
              'KRX API의 각 서비스 이용 승인 및 데이터 사용 약관을 확인했나요?') != QMessageBox.StandardButton.Yes:return
        self.run_task(lambda:self.history.fetch_range(KRXDailyClient(key),start,end),
                      lambda r:(self.research_source.setCurrentText('KRX'),self.refresh_research(),
                                QMessageBox.information(self,'KRX 저장',f'신규 응답 {r["requests"]}회 / {r["rows"]:,}행 저장')),
                      'KRX 수집 완료')

    def prepare_theme_guided(self):
        mode=current_mode(self.cfg).key
        sources=['DEMO'] if mode=='DEMO' else ['KRX 저장 자료','KRX 수집 후 판정','CSV_USER']
        choice,ok=QInputDialog.getItem(self,'1/2 · 테마1 자료 선택',
            '저장 자료로 판정하거나, KRX 일봉을 수집한 뒤 판정할 수 있어요. KRX 수집은 별도 인증키와 서비스 승인이 필요해요.',sources,0,False)
        if not ok:return
        collect=choice=='KRX 수집 후 판정'
        source='KRX' if choice.startswith('KRX') else choice
        dates,ok=QInputDialog.getMultiLineText(self,'2/2 · 확인한 거래일',
            '휴장일을 뺀 실제 거래일을 오래된 순서대로 한 줄에 하나씩 입력하세요 (YYYY-MM-DD).\n'
            '마지막 날짜가 감지 기준일이에요. 최근 6거래일과 전일 종가용 1일, 총 7거래일 이상이 필요해요.\n'
            '저장된 날짜를 거래일로 자동 간주하지 않아요. 거래소 달력에서 빠진 거래일이 없는지 확인하세요.')
        if not ok:return
        try:
            data,_=selection_from_days(dates,mode,source)
        except ValueError as error:
            QMessageBox.information(self,'거래일 확인',str(error));return
        key=None
        if collect:
            key=self.secrets['krx_api_key'].text().strip() or self.creds.get('krx_api_key')
            if not key:
                QMessageBox.information(self,'KRX 인증키 필요','API · 텔레그램에서 별도의 KRX OPEN API 인증키를 등록해 주세요.');return
        action=('최근 7거래일의 코스피·코스닥 일봉을 KRX에 요청합니다.\n'
                '두 시장의 서비스 이용 승인과 데이터 사용 약관을 확인했나요?\n'
                '오늘 자료는 잠정치이며 다음 날 다시 확인해야 해요.\n' if collect else '저장 시세로만 판정합니다.\n')
        if QMessageBox.question(self,'테마1 판정 시작',
            f'자료 {source} · 확인한 거래일 {len(data["calendar"]["days"])}일 · 기준일 {data["through"]}\n'
            '입력 범위의 모든 거래일을 빠짐없이 확인했나요?\n'
            +action+'전체 시장 수집 완료를 보증하지 않아요.\n'
            '완료 후 이 창을 닫고 테마1 결과를 다시 열어 주세요.') != QMessageBox.StandardButton.Yes:return
        if collect:
            self.run_task(lambda progress,cancelled:collect_and_prepare(self.history,KRXDailyClient(key),data,mode,
                          progress=progress,cancelled=cancelled),self.theme_prepared,
                          '테마1 수집 및 판정 완료',with_progress=True)
        else:
            self.run_task(lambda:prepare_theme_days(self.history,dates,mode,source),
                          self.theme_prepared,'테마1 저장 시세 판정')

    def theme_prepared(self,result):
        collection=result.get('collection')
        collected=(f'KRX 신규 응답 {collection["requests"]}회 · 저장 {collection["rows"]:,}행\n'
                   +('오늘 자료 포함 · 잠정치예요.\n' if collection['provisional'] else '') if collection else '')
        QMessageBox.information(self,'테마1 판정 완료',
            collected+
            f'저장된 종목 {result["symbols"]}개 · 감지 {result["matched"]}개\n'
            f'자료 부족 종목 {result["states"].get("PRICE_MISSING",0)}개\n'
            f'시장별 일별 자료가 없는 구간 {len(result["missing_market_days"])}개\n'
            '전체 시장 자료 완료는 미확인입니다. 홈의 테마1 결과 또는 /테마1에서 확인하세요.\n'
            '실제 발송·주문은 하지 않았어요.')

    def prepare_theme_one(self):
        mode=current_mode(self.cfg).key
        path,_=QFileDialog.getOpenFileName(self,'테마1 거래일·자료 출처 선택','','테마1 자료 선택 (*.json)')
        if not path:
            return
        self.run_task(lambda:prepare_theme_selection(self.history,path,mode),
                      self.theme_prepared,'테마1 저장 시세 판정')

    def import_recommendation_prices(self):
        if self.work is not None and self.work.isRunning():
            self.statusBar().showMessage('진행 중인 작업이 끝난 뒤 자료를 넣어 주세요.')
            return
        path,_ = QFileDialog.getOpenFileName(self,'확인한 거래일·종가 자료 넣기','','추천 가격 자료 (*.json)')
        if not path:
            return
        from app.recommendation_import import import_package
        db,mode = self.db,current_mode(self.cfg).key
        self.run_task(lambda:import_package(db,path,mode),
            lambda result:QMessageBox.information(self,'추천 가격 자료 반영',
                f"추천 {result['recommendations']}개 대조 · 새 평가 {result['added']}개\n"
                f"자료 확인 완료일 {result['available_through']}\n"
                '텔레그램 /추천성적에서 확인할 수 있어요. 실제 거래 수익률이 아니에요.'),
            '추천 가격 자료 대조 완료')

    def evaluate_cached_recommendations(self):
        if self.work is not None and self.work.isRunning():
            self.statusBar().showMessage('진행 중인 작업이 끝난 뒤 평가해 주세요.')
            return
        path,_ = QFileDialog.getOpenFileName(self,'저장 시세 출처·확인한 거래일 선택','','자료 선택 (*.json)')
        if not path:
            return
        from app.recommendation_cache import evaluate_selection
        db,history,mode = self.db,self.history,current_mode(self.cfg).key
        self.run_task(lambda:evaluate_selection(db,history,path,mode),
            lambda result:QMessageBox.information(self,'저장 시세로 추천 평가',
                f"추천 {result['recommendations']}개 대조 · 새 평가 {result['added']}개\n"
                f"자료 확인 완료일 {result['available_through']}\n"
                '빠진 가격은 자료 없음으로 표시해요. /추천성적 에서 확인하세요.\n실제 거래 수익률이 아니에요.'),
            '저장 시세 추천 평가 완료')

    def repeat_cached_recommendations(self):
        if self.work is not None and self.work.isRunning():
            self.statusBar().showMessage('진행 중인 작업이 끝난 뒤 평가해 주세요.')
            return
        from app.recommendation_cache import evaluate_saved
        db,history,mode=self.db,self.history,current_mode(self.cfg).key
        self.run_task(lambda:evaluate_saved(db,history,mode),
            lambda result:QMessageBox.information(self,'저장한 거래일로 다시 평가',
                f"추천 {result['recommendations']}개 대조 · 새 평가 {result['added']}개\n"
                f"확인한 기간 끝 {result['available_through']} · 이후 날짜는 자동으로 늘리지 않아요.\n"
                '/추천성적 에서 확인하세요. 실제 거래 수익률이 아니에요.'),
            '저장한 거래일 추천 평가 완료')

    def run_research(self):
        source=self.research_source.currentText()
        self.research_report=None
        topk=self.research_k.value()
        selected_reference=getattr(self,'research_reference',None)
        reference_path=(selected_reference[1] if selected_reference and selected_reference[0]==source else None)
        def run():
            from app.research_preflight import check_research_coverage
            bars=self.history.load_bars(source)
            if len(bars)<100:raise ValueError('자료가 부족합니다. CSV 또는 KRX 데이터를 먼저 수집하세요.')
            # Check BEFORE research, and carry the result into the saved report.
            quality=check_research_coverage(self.history,source,bars,reference_path=reference_path)
            report=evaluate_research(bars,topk=topk)
            report['coverage_quality']=quality
            run_id=self.history.save_run(source,{'topk':topk,'source':source},report)
            return run_id,report
        def done(output):
            self.research_run_id=output[0]
            self.show_research(output[1])
        self.run_task(run,done,'종목 선정 조건 검증 완료')

    def show_research(self,report,show_message=True):
        from app.research_comparison import compare_variants, comparison_notice
        self.research_report=report
        self.research_comparisons=compare_variants(report)
        self.research_table.setRowCount(0)
        states=self.history.research_states(self.research_source.currentText())
        for r in self.research_comparisons:
            i=self.research_table.rowCount();self.research_table.insertRow(i)
            def fmt(x):return '—' if x is None else f'{x:+.3f}'
            values=[r['name'],r['market'],str(r['train_days']),fmt(r['train_excess3']),
                    str(r['test_days']),fmt(r['test_excess3']),fmt(r['test_ic']),
                    fmt(r['holdout_gap3']),
                    fmt(r.get('test_early_excess3'))+' → '+fmt(r.get('test_late_excess3')),
                    r['data_quality_label']+' · '+r['comparison_note'],
                    states.get(r['id'], ('표본 부족' if r['train_days']<12 or r['test_days']<5 else '관찰 중'))]
            for j,t in enumerate(values):self.research_table.setItem(i,j,cell(t))
        chosen=next((x for x in report['variants'] if x['id']==report['chosen_on_train_only']),None)
        from app.research_preflight import preflight_notice
        self.research_result.setText(
            '학습 구간 우선 후보: '+(chosen['name'] if chosen else '표본 부족으로 선정하지 않음')
            +'\n'+preflight_notice(report.get('coverage_quality'))
            +'\n'+comparison_notice(report))
        self.research_status.setText(f'{report["data_begin"]} ~ {report["data_end"]} | '
                     f'{report["symbols"]}종목, {report["data_rows"]:,}행, {len(report["variants"])}개 조건 | '
                     'TRAIN으로 조건 순위 선정 / TEST는 미사용 기간 평가')
        if show_message:
            QMessageBox.information(self,'조건 검증 완료',
                '조건별 일별 추천·검증 수익을 DB에 기록했습니다.\n'
                +preflight_notice(report.get('coverage_quality'))+'\n'
                '이 결과는 연구용이며 적용이나 실제 주문으로 이어지지 않습니다.')

    def research_detail(self):
        i=self.research_table.currentRow()
        if not self.research_report or i<0 or i>=len(self.research_report['variants']):return
        from app.research_comparison import compare_variants
        rows=getattr(self,'research_comparisons',None)
        if rows is None or len(rows)!=len(self.research_report['variants']):
            rows=compare_variants(self.research_report)
        r=rows[i]
        def f(k):return '자료 없음' if r.get(k) is None else f'{r[k]:+.3f}'
        self.research_detail_label.setText(
            f'학습(이전 기간): {r["train_days"]}일 / {r["train_signals"]}추천 · 평균 순수익 {f("train_return3")}% · '
            f'초과수익 {f("train_excess3")}%p  |  '
            f'검증(미사용 기간): {r["test_days"]}일 / {r["test_signals"]}추천 · '
            f'평균 순수익 {f("test_return3")}% · 초과수익 {f("test_excess3")}%p · Rank IC {f("test_ic")} · 종목별 수익 비율 {f("test_signal_win_pct")}%\n'
            f'학습→검증 초과수익 차이 {f("holdout_gap3")}%p · '
            f'자료 품질 {r["data_quality_label"]} · 해석 {r["comparison_note"]}\n'
            f'TEST 초반 시장대비 초과수익 {f("test_early_excess3")}%p'
            f' ({r.get("test_early_days",0)}일) → 후반 {f("test_late_excess3")}%p'
            f' ({r.get("test_late_days",0)}일) · 후반 변화 {f("test_late_gap3")}%p'
            f' · {r.get("test_stability_note","구간 분리 기록 없음")}\n'
            +(f"3거래일 추천 가격 관측: {r['test_r3_observed']}/{r['test_r3_selected']}종목 · "
              f"가격 누락 {r['test_r3_missing']} · 자료 끝쪽 {r['test_r3_tail']}\n"
              f"3거래일 경과한 추천일: 완전 {r['test_r3_complete_days']} / 일부 {r['test_r3_partial_days']} / "
              f"전체 누락 {r['test_r3_missing_days']}일\n" if 'test_r3_selected' in r else '추천별 가격 확보 집계 기록 없음\n')
            +
            '같은 검증 구간을 앞/뒤로 나눈 진단으로 독립적인 추가 검증이나 실제 투자수익은 아닙니다.')

    def review_research(self,approve):
        ix=self.research_table.currentRow()
        if not self.research_report or ix<0 or self.research_run_id is None:return
        v=self.research_report['variants'][ix]
        label='연구 프리셋 승인' if approve else '연구 조건 거부'
        if QMessageBox.question(self,label,f'{v["name"]}\n\n'
              '실제 매수·매도 또는 키움 실시간 필터로 연결되지 않습니다.\n'
              '향후 데이터로 검증할 연구 전략 상태만 저장합니다. 계속할까요?') != QMessageBox.StandardButton.Yes:return
        self.history.decide_research(self.research_source.currentText(),self.research_run_id,v,approve)
        self.show_research(self.research_report,show_message=False)

    def export_research(self):
        if not self.research_report:
            QMessageBox.information(self,'결과 없음','먼저 조건 검증을 실행하세요.');return
        folder=QFileDialog.getExistingDirectory(self,'결과를 저장할 폴더',str(Path.home()))
        if not folder:return
        dest=Path(folder)/('KRX_research_'+datetime.now().strftime('%Y%m%d_%H%M%S'))
        export_report(self.research_report,dest)
        QMessageBox.information(self,'저장 완료',f'{dest}\nsummary.csv / signals.csv / manifest.json')

    def make_strategy(self):
        page = QWidget()
        v = QVBoxLayout(page)
        guide = lbl('시장·ETF·추천 개수부터 고르세요. 가격·회사 크기·거래량 등의 저장된 조건도 함께 적용됩니다.\n자세한 조건과 연습 매매 설정은 왼쪽의 「연구·고급 메뉴 보기」를 켜면 바꿀 수 있습니다.', 'Muted')
        guide.setWordWrap(True)
        v.addWidget(guide)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setColumnStretch(0,1)
        grid.setColumnStretch(1,1)
        self.fields = {}
        self.advanced_fields = []
        filter_items = [
            ('market','어느 시장에서 찾을까요?','choice',['전체','KOSPI','KOSDAQ']),
            ('include_etf','ETF 포함','bool'),
            ('recommend_count','추천을 몇 개까지 볼까요?','int',1,50),
            ('on_price','주가 범위 적용','bool'),
            ('min_price','최소 주가 (원)','double',0,1000000),
            ('max_price','최대 주가 (원)','double',100,10000000),
            ('on_cap','시가총액 범위 적용','bool'),
            ('min_cap_eok','최소 시총 (억 원)','double',0,10000000),
            ('max_cap_eok','최대 시총 (억 원)','double',0,10000000),
            ('on_value','거래대금 하한 적용','bool'),
            ('min_value_eok','최소 거래대금 (억 원)','double',0,100000),
            ('on_volratio','전일 거래량비 조건','bool'),
            ('min_vol_ratio','거래량 전일비 최소 (%)','double',0,5000),
            ('on_change','등락률 범위 적용','bool'),
            ('min_change_pct','최소 등락률 (%)','double',-30,30),
            ('max_change_pct','최대 등락률 (%)','double',-30,30),
        ]
        risk_items = [
            ('exit_stop_pct','가상 손절 (%)','double',0.1,30),
            ('exit_take_pct','가상 익절 (%)','double',0.1,50),
            ('exit_max_days','가상 보유 제한 (달력일)','int',1,30),
            ('max_paper_positions','동시 가상 보유종목','int',1,20),
            ('paper_fee_pct','편도 가상 수수료 (%)','double',0,1),
            ('paper_sell_tax_pct','가상 매도세금 (%)','double',0,1),
            ('paper_slippage_pct','편도 가상 슬리피지 (%)','double',0,3),
            ('scan_interval_sec','검색 간격 (초)','int',10,600),
            ('min_strategy_trades','개선안 검토 최소 거래건수','int',20,2000),
            ('backtest_gap_limit','다음 날 가상매수 갭상승 상한 (%)','double',0,20),
        ]
        for col, title, options in [(0,'종목 검색·필터',filter_items),(1,'가상매매·위험관리',risk_items)]:
            panel = QFrame(objectName='Panel')
            p = QVBoxLayout(panel)
            p.addWidget(lbl(title,'H2'))
            form = QFormLayout()
            form.setSpacing(12)
            for item in options:
                key, name, kind, *extra = item
                if kind == 'bool':
                    w = QCheckBox('사용')
                    w.setChecked(bool(self.cfg[key]))
                elif kind == 'choice':
                    w = QComboBox()
                    for value in extra[0]:
                        w.addItem({'전체': '코스피 + 코스닥', 'KOSPI': '코스피', 'KOSDAQ': '코스닥'}.get(value, value), value)
                    w.setCurrentIndex(w.findData(self.cfg[key]))
                elif kind == 'int':
                    w = QSpinBox()
                    w.setRange(*extra)
                    w.setValue(int(self.cfg[key]))
                else:
                    w = QDoubleSpinBox()
                    w.setRange(*extra)
                    w.setDecimals(2)
                    w.setSingleStep(0.5)
                    w.setValue(float(self.cfg[key]))
                    w.setGroupSeparatorShown(True)
                form.addRow(name, w)
                if key not in ('market', 'include_etf', 'recommend_count'):
                    self.advanced_fields.append((form.labelForField(w), w))
                self.fields[key] = w
            p.addLayout(form)
            p.addStretch()
            grid.addWidget(panel,0,col)
            if col == 1:
                self.risk_panel = panel
        scroll.setWidget(body)
        v.addWidget(scroll)
        controls = QHBoxLayout()
        controls.addWidget(lbl('모든 수정은 저장 후 신규 스캔·신규 가상매수부터 적용됩니다. 기존 포지션의 손절/익절은 매수 당시 값 유지.','Muted'))
        controls.addStretch()
        save = QPushButton('조건 저장',objectName='Primary')
        save.clicked.connect(self.save_strategy)
        controls.addWidget(save)
        v.addLayout(controls)
        return page

    def make_optimizer(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.addWidget(lbl('전략 변경은 자동 적용되지 않습니다. 누적 거래 기반의 탐색적 가설만 제안합니다.','Muted'))
        self.opt_status = lbl('데이터 축적 중','H2')
        v.addWidget(self.opt_status)
        self.proposal_table = make_table(['번호','생성일','실험 제안','근거','상태'])
        v.addWidget(self.proposal_table)
        buttons = QHBoxLayout()
        create = QPushButton('지금 개선안 분석')
        create.clicked.connect(self.create_optimizer)
        buttons.addWidget(create)
        accept = QPushButton('선택 개선안 승인 · 조건 적용',objectName='Primary')
        accept.clicked.connect(lambda: self.review_proposal(True))
        buttons.addWidget(accept)
        decline = QPushButton('선택 개선안 거부')
        decline.clicked.connect(lambda: self.review_proposal(False))
        buttons.addWidget(decline)
        buttons.addStretch()
        v.addLayout(buttons)
        v.addWidget(lbl('주의: 현재는 기술통계 기반 비교 기능입니다. 머신러닝, 워크포워드 백테스트, 자동 학습은 후속 버전 예정입니다.','Muted'))
        return page

    def make_integrations(self):
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(15)
        self.secrets = {}
        box = QFrame(objectName='Panel')
        formbox = QVBoxLayout(box)
        formbox.addWidget(lbl('1. 어떻게 사용하실 건가요?','H2'))
        f = QFormLayout()
        self.mode_select = QComboBox()
        for mode in MODES:
            self.mode_select.addItem(mode.title, mode.key)
        self.mode_select.addItem('실제 투자 · 준비 중 (주문 잠금)', 'LOCKED')
        self.mode_select.model().item(3).setEnabled(False)
        self.mode_select.setCurrentIndex(MODES.index(current_mode(self.cfg)))
        f.addRow('사용 모드', self.mode_select)
        self.mode_help = lbl(current_mode(self.cfg).description, 'Muted')
        self.mode_help.setWordWrap(True)
        f.addRow(self.mode_help)
        self.key_scope = lbl('', 'Muted')
        self.key_scope.setWordWrap(True)
        f.addRow(self.key_scope)
        self._credential_env = current_mode(self.cfg).environment
        self._key_drafts = {}
        self.data_mode = QComboBox()
        self.data_mode.addItems(['DEMO','KIWOOM'])
        self.data_mode.setCurrentText(self.cfg['data_mode'])
        self.data_mode.hide()
        self.kiwoom_env = QComboBox()
        self.kiwoom_env.addItems(['MOCK','REAL'])
        self.kiwoom_env.setCurrentText(self.cfg['kiwoom_env'])
        self.kiwoom_env.hide()
        initial_keys = self.broker_keys(self._credential_env)
        for key, title in [('kiwoom_appkey','App Key'),('kiwoom_secret','App Secret')]:
            line = QLineEdit()
            line.setEchoMode(QLineEdit.EchoMode.Password)
            line.setPlaceholderText('OS 보안 저장소에 별도 보관')
            line.setText(initial_keys[0 if key == 'kiwoom_appkey' else 1])
            f.addRow('키움 연결 키' if key == 'kiwoom_appkey' else '키움 비밀 키',line)
            self.secrets[key] = line
        krx_line=QLineEdit()
        krx_line.setEchoMode(QLineEdit.EchoMode.Password)
        krx_line.setPlaceholderText('KRX 별도 인증키 (일봉 연구용, 선택)')
        krx_line.setText(self.creds.get('krx_api_key'))
        self.secrets['krx_api_key']=krx_line
        f.addRow('과거 자료 연구용 키 (선택)',krx_line)
        self.mode_select.currentIndexChanged.connect(self.preview_mode)
        self.preview_mode(self.mode_select.currentIndex())
        formbox.addLayout(f)
        v.addWidget(box)
        tg = QFrame(objectName='Panel')
        t = QVBoxLayout(tg)
        t.addWidget(lbl('2. 추천을 받을 텔레그램 연결','H2'))
        tf = QFormLayout()
        token = QLineEdit()
        token.setEchoMode(QLineEdit.EchoMode.Password)
        token.setText(self.creds.get('telegram_token'))
        self.secrets['telegram_token'] = token
        tf.addRow('봇 연결 키 (Bot Token)',token)
        self.chat = QLineEdit(self.cfg['telegram_chat_id'])
        tf.addRow('알림 받을 방 번호 (Chat ID)',self.chat)
        self.telegram_user = QLineEdit(self.cfg['telegram_user_id'])
        tf.addRow('명령을 보낼 내 사용자 번호 (User ID)', self.telegram_user)
        self.telegram_auto = QCheckBox('프로그램이 실행 중일 때 매일 16시 이후 첫 검색 결과를 자동 발송')
        self.telegram_auto.setChecked(bool(self.cfg['telegram_auto_send']))
        tf.addRow('자동 알림',self.telegram_auto)
        t.addLayout(tf)
        v.addWidget(tg)
        buttons = QHBoxLayout()
        save = QPushButton('이 모드와 연결 정보 저장',objectName='Primary')
        save.clicked.connect(self.save_integrations)
        buttons.addWidget(save)
        testki = QPushButton('키움 연결 확인 · 주문 안 함')
        self.test_ki_button = testki
        testki.setEnabled(self.mode_select.currentIndex() != 0)
        testki.clicked.connect(self.test_kiwoom)
        buttons.addWidget(testki)
        testtg = QPushButton('텔레그램 테스트 메시지')
        testtg.clicked.connect(self.test_telegram)
        buttons.addWidget(testtg)
        buttons.addStretch()
        v.addLayout(buttons)
        v.addWidget(lbl('API 키와 Bot Token은 JSON에 저장하지 않습니다. Windows Credential Manager(keyring)로 보관됩니다.','Muted'))
        warning = lbl('모의투자와 실제 시세의 연결 키·연습 기록은 따로 보관합니다. 실제 투자 기능은 아직 잠겨 있습니다.', 'Muted')
        warning.setWordWrap(True)
        v.addWidget(warning)
        legacy = QPushButton('이전 버전의 연습 기록 보기 (모드 구분 전)')
        legacy.clicked.connect(self.show_legacy_trades)
        v.addWidget(legacy)
        self.order_demo_button = QPushButton('가짜 서버로 주문 흐름 시험 · 실제 돈 사용 안 함')
        self.order_demo_button.clicked.connect(self.test_order_flow)
        v.addWidget(self.order_demo_button)
        v.addWidget(lbl('3. 텔레그램으로 추천 조건 보기·바꾸기', 'H2'))
        help_text = lbl('/조건 → 현재 기준 확인 · /조건변경 거래대금=100 → 내용 확인 → /조건확인 코드\n'
                        '현재 저장한 모드의 추천 기준만 바꿉니다. 프로그램을 켜고 직접 시작해야 합니다.\n'
                        '/매수 900001 1 1000 → 내용 확인 → /확인 코드\n'
                        '수량·지정가 필수 · 2분 안에 확인 · 접수와 체결은 다릅니다. 키움 모의·실제 주문은 연결하지 않습니다.', 'Muted')
        help_text.setWordWrap(True)
        v.addWidget(help_text)
        self.telegram_order_status = lbl('수신 중지 · 자동으로 시작하지 않습니다.', 'Muted')
        self.telegram_order_status.setWordWrap(True)
        v.addWidget(self.telegram_order_status)
        tg_controls = QHBoxLayout()
        self.telegram_order_start = QPushButton('텔레그램 명령 받기 시작')
        self.telegram_order_start.clicked.connect(self.start_telegram_orders)
        self.telegram_order_stop = QPushButton('텔레그램 명령 받기 중지')
        self.telegram_order_stop.setEnabled(False)
        self.telegram_order_stop.clicked.connect(self.stop_telegram_orders)
        self.telegram_emergency = QPushButton('긴급 중지 · 미체결 취소 시도')
        self.telegram_emergency.setEnabled(False)
        self.telegram_emergency.clicked.connect(self.emergency_telegram_orders)
        self.telegram_resume = QPushButton('복구 확인 후 연습 재개')
        self.telegram_resume.setEnabled(False)
        self.telegram_resume.clicked.connect(self.resume_telegram_orders)
        tg_controls.addWidget(self.telegram_order_start)
        tg_controls.addWidget(self.telegram_order_stop)
        v.addLayout(tg_controls)
        safety_controls = QHBoxLayout()
        safety_controls.addWidget(self.telegram_emergency)
        safety_controls.addWidget(self.telegram_resume)
        v.addLayout(safety_controls)
        limits_note = lbl('연습 한도: 한 주문 100주·100만원 / 한국 날짜 기준 하루 확인 20회·누적 200만원. '
                          '매수·매도 모두 합산하며 미접수·취소도 사용한 한도를 돌려주지 않습니다.', 'Muted')
        limits_note.setWordWrap(True)
        v.addWidget(limits_note)
        panel = QFrame(objectName='Panel')
        watch = QVBoxLayout(panel)
        watch.addWidget(lbl('4. 키움 모의투자 연결', 'H2'))
        note = lbl('키움 모의투자 모드에서만 사용해요. 기본은 조회·알림만 받아요. 실제 돈 주문은 잠겨 있어요.\n'
                   '처음 연결만 이 화면에서 해요. 이후 텔레그램 /프로그램상태로 확인할 수 있어요.\n'
                   '1번으로 계좌를 확인한 뒤 2분 안에 2번을 눌러야 알림을 받기 시작해요.', 'Muted')
        note.setWordWrap(True)
        watch.addWidget(note)
        self.mock_account = QLineEdit()
        self.mock_account.setEchoMode(QLineEdit.EchoMode.Password)
        self.mock_account.setMaxLength(10)
        self.mock_account.setPlaceholderText('확인할 키움 모의 계좌번호 10자리 · 저장하지 않아요')
        watch.addWidget(self.mock_account)
        self.mock_recovery = QCheckBox('오늘 텔레그램 재연결 허용 · 재연결 뒤 주문은 잠금')
        self.mock_recovery.setToolTip('켜면 오늘 이 프로그램에서만 복구용 모의 키·계좌를 메모리에 보관해요. 중지·종료하면 지워요.')
        watch.addWidget(self.mock_recovery)
        self.mock_orders_allowed = QCheckBox('오늘 텔레그램 키움 모의 주문 허용 · 실제 돈 사용 안 함')
        self.mock_day_confirmed = QCheckBox('모의 HTS에서 오늘 거래 가능한 날임을 확인했어요')
        self.mock_orders_allowed.setToolTip('모의 시험 한도: 한 주문 10주·10만원, 하루 매수·매도·정정 합산 3회·20만원. 취소는 별도 10회예요. 끊기면 허용이 해제돼요.')
        watch.addWidget(self.mock_orders_allowed)
        watch.addWidget(self.mock_day_confirmed)
        trial_note = lbl('모의 주문 시험 한도: 한 주문 10주·10만원, 하루 매수·매도·정정 합산 3회·20만원. 취소는 별도 10회예요.\n'
                         '모의 주문은 텔레그램에서 내용을 읽고 별도로 확인해야 해요. 끊기면 주문 허용이 해제돼요.', 'Muted')
        trial_note.setWordWrap(True)
        watch.addWidget(trial_note)
        controls = QHBoxLayout()
        self.mock_verify = QPushButton('1. 모의 계좌 확인',objectName='Primary')
        self.mock_listen = QPushButton('2. 알림 받기 시작')
        self.mock_listen.setEnabled(False)
        self.mock_stop = QPushButton('알림 받기 중지')
        self.mock_stop.setEnabled(False)
        self.mock_verify.clicked.connect(self.verify_mock_account)
        self.mock_listen.clicked.connect(self.start_mock_notices)
        self.mock_stop.clicked.connect(self.stop_mock_notices)
        for button in (self.mock_verify,self.mock_listen,self.mock_stop):
            controls.addWidget(button)
        watch.addLayout(controls)
        self.mock_status = lbl('3. 상태 확인 · 아직 연결하지 않았어요. 주문 기능은 잠겨 있어요.', 'Muted')
        self.mock_balance = lbl('잔고는 모의 계좌를 확인한 시점의 값이에요.', 'Muted')
        self.mock_last_notice = lbl('아직 받은 알림이 없어요.', 'Muted')
        for label in (self.mock_status,self.mock_balance,self.mock_last_notice):
            label.setWordWrap(True)
            watch.addWidget(label)
        v.addWidget(panel)
        v.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def broker_keys(self, environment):
        names = credential_names(environment)
        scoped = tuple(self.creds.get(name) for name in names)
        if not any(scoped) and environment == self.cfg['legacy_kiwoom_env']:
            return self.creds.get('kiwoom_appkey'), self.creds.get('kiwoom_secret')
        return scoped

    def preview_mode(self, index):
        if index not in range(len(MODES)):
            return
        mode = MODES[index]
        previous = self._credential_env
        self._key_drafts[previous] = tuple(self.secrets[key].text() for key in ('kiwoom_appkey', 'kiwoom_secret'))
        self._credential_env = mode.environment
        values = self._key_drafts.get(mode.environment, self.broker_keys(mode.environment))
        for key, value in zip(('kiwoom_appkey', 'kiwoom_secret'), values):
            self.secrets[key].setText(value)
            self.secrets[key].setEnabled(mode.key != 'DEMO')
        self.data_mode.setCurrentText(mode.data_mode)
        self.kiwoom_env.setCurrentText(mode.environment)
        self.mode_help.setText(mode.description + '\n아래 저장 버튼을 눌러야 바뀝니다.')
        self.key_scope.setText('가상 연습에는 키움 연결 키가 필요 없습니다.' if mode.key == 'DEMO' else
            '연결 키 보관함: ' + ('키움 모의 서버 전용' if mode.environment == 'MOCK' else '키움 실제 서버 전용'))
        if hasattr(self, 'test_ki_button'):
            self.test_ki_button.setEnabled(mode.key != 'DEMO')

    def verify_mock_account(self):
        if self.mock_watch_worker and self.mock_watch_worker.isRunning():
            return
        try:
            require_mock_mode(self.cfg)
            if self.mode_select.currentData() != 'MOCK':
                raise ValueError('키움 모의투자 모드를 선택하고 저장해 주세요.')
            account = self.mock_account.text().strip()
            if not (len(account)==10 and account.isascii() and account.isdigit()):
                raise ValueError('확인할 모의 계좌번호 10자리를 입력해 주세요.')
            key_name, secret_name = credential_names('MOCK')
            key, secret = self.creds.get(key_name), self.creds.get(secret_name)
            if not key or not secret:
                raise ValueError('모의 연결 키와 비밀 키를 먼저 저장해 주세요.')
        except ValueError as error:
            QMessageBox.information(self,'모의 계좌 확인',str(error))
            return
        allow_orders = self.mock_orders_allowed.isChecked()
        if allow_orders and not self.mock_day_confirmed.isChecked():
            QMessageBox.information(self,'모의 거래일 확인','모의 HTS에서 오늘 거래 가능한 날인지 먼저 확인해 주세요.')
            return
        self.mock_account.clear()
        worker = MockWatchWorker(data_dir()/'kiwoom_mock_watch',key,secret,account,self.mock_watch_status,
                                 allow_recovery=self.mock_recovery.isChecked(),allow_orders=allow_orders,
                                 confirmed_day=datetime.now(KST).date() if allow_orders else None)
        self.mock_watch_worker = worker
        worker.verified.connect(self.mock_account_verified)
        worker.status.connect(self.mock_status.setText)
        worker.notice.connect(self.mock_notice_received)
        worker.finished.connect(self.mock_watch_finished)
        self.mock_verify.setEnabled(False)
        self.mock_account.setEnabled(False)
        self.mock_recovery.setEnabled(False)
        self.mock_orders_allowed.setEnabled(False)
        self.mock_day_confirmed.setEnabled(False)
        self.mock_listen.setEnabled(False)
        self.mock_stop.setEnabled(True)
        self.mode_select.setEnabled(False)
        for name in ('kiwoom_appkey','kiwoom_secret'):
            self.secrets[name].setEnabled(False)
        self.mock_status.setText('모의 계좌와 잔고를 확인하고 있어요. 주문은 보내지 않아요.')
        self.mock_balance.setText('확인 중이에요. 이전 잔고는 사용하지 않아요.')
        self.mock_last_notice.setText('아직 알림 받기를 시작하지 않았어요.')
        worker.start()

    def mock_account_verified(self, report):
        worker = self.mock_watch_worker
        if not worker or worker.isInterruptionRequested() or not worker.isRunning():
            return
        money = '다시 확인 필요' if report['order_money'] is None else f"{report['order_money']:,}원"
        self.mock_balance.setText(f"{report['checked_at']} 확인 · 주문에 쓸 수 있는 돈(조회값): {money}\n"
                                  f"보유 {report['held']:,}주 · 조회된 남은 주문 {report['pending']:,}주\n"
                                  '이 화면에서는 매수·매도 주문을 할 수 없어요.')
        self.mock_listen.setEnabled(not worker._start_requested.is_set())

    def start_mock_notices(self):
        worker = self.mock_watch_worker
        if worker and worker.isRunning() and not worker.isInterruptionRequested():
            self.mock_listen.setEnabled(False)
            self.mock_status.setText(MOCK_LISTEN_GUIDANCE)
            worker.start_listening()

    def mock_notice_received(self, text):
        self.mock_last_notice.setText(text)
        self.mock_balance.setText(MOCK_REFRESH_GUIDANCE)

    def stop_mock_notices(self):
        if self.mock_watch_worker:
            self.mock_watch_worker.stop()
        self.mock_stop.setEnabled(False)
        self.mock_listen.setEnabled(False)
        self.mock_status.setText('모의 알림 연결을 닫고 있어요. 현재 요청이 끝나면 중지해요.')

    def mock_watch_finished(self):
        self.mock_listen.setEnabled(False)
        self.mock_stop.setEnabled(False)
        self.mock_verify.setEnabled(True)
        self.mock_account.setEnabled(True)
        self.mock_recovery.setEnabled(True)
        self.mock_recovery.setChecked(False)
        self.mock_orders_allowed.setEnabled(True)
        self.mock_orders_allowed.setChecked(False)
        self.mock_day_confirmed.setEnabled(True)
        self.mock_day_confirmed.setChecked(False)
        self.mode_select.setEnabled(True)
        for name in ('kiwoom_appkey','kiwoom_secret'):
            self.secrets[name].setEnabled(self.mode_select.currentData()!='DEMO')
        self.mock_balance.setText('연결이 끝났어요. 잔고는 1번으로 다시 확인해 주세요.')

    def show_legacy_trades(self):
        import sqlite3
        from PySide6.QtWidgets import QDialog
        path = data_dir() / 'trading.db'
        if not path.exists():
            QMessageBox.information(self, '이전 기록', '이전 버전의 연습 기록이 없습니다.')
            return
        from contextlib import closing
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                rows = db.execute('SELECT name,qty,buy_fill,pnl,source FROM paper_trades ORDER BY id DESC LIMIT 1000').fetchall()
        except sqlite3.Error:
            QMessageBox.warning(self, '이전 기록', '이전 기록을 읽지 못했습니다. 원본 파일은 변경하지 않았습니다.')
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('이전 연습 기록 · 모의/실제 서버 구분 불명 · 실제 체결 내역 아님')
        layout = QVBoxLayout(dialog)
        layout.addWidget(lbl('기존 기록은 보존했습니다. 모드를 구분할 수 없어 새 기록과 합치지 않습니다.', 'Muted'))
        table = make_table(['종목', '연습 수량', '연습 매수가', '연습 손익', '이전 출처'])
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                table.setItem(i, j, cell('—' if value is None else value))
        layout.addWidget(table)
        dialog.resize(900, 500)
        dialog.exec()

    def update_connection(self):
        mode = current_mode(self.cfg)
        self.connection.setText(mode.title + ' · 실제 주문 잠금')
        self.connection.setStyleSheet(f'color:{mode.color};font-weight:700;')
        self.notice.setText(mode.description + ('\n' + PRICE_BASIS_NOTICE if mode.key != 'DEMO' else ''))
        self.notice.setToolTip(PRICE_BASIS_NOTICE)
        self.home_mode.setText('현재: ' + mode.title + '\n' + mode.description)
        self.home_mode.setStyleSheet(f'color:{mode.color};')
        self.demo_badge.setText('실제 돈의 주문: 잠금\n현재 기록은 모두 연습입니다.')

    def test_order_flow(self):
        from app.order_demo import run_order_demo
        def done(report):
            QMessageBox.information(self, '가짜 서버 시험 완료',
                                    '\n\n'.join(report['steps']) + '\n\n' + report['notice'])
        self.run_task(run_order_demo, done, '오프라인 가짜 주문 흐름 확인 완료')

    def start_telegram_orders(self):
        if self.telegram_order_worker and self.telegram_order_worker.isRunning():
            return
        if self.mode_select.currentData() != current_mode(self.cfg).key:
            QMessageBox.information(self, '모드 저장 필요', '선택한 모드와 연결 정보를 먼저 저장해 주세요.')
            return
        try:
            user_id, chat_id = int(self.cfg['telegram_user_id']), int(self.cfg['telegram_chat_id'])
            if min(user_id, chat_id) <= 0:
                raise ValueError()
            token = self.creds.get('telegram_token')
            if not token:
                raise ValueError()
        except (ValueError, TypeError):
            QMessageBox.information(self, '연결 정보 필요', '봇 연결 키·허용 User ID·개인 대화 Chat ID를 입력하고 저장하세요.')
            return
        orders_enabled = self.cfg['data_mode'] == 'DEMO'
        if QMessageBox.question(self, '텔레그램 명령 받기',
                telegram_start_guidance(self.cfg) + '\n텔레그램 명령 받기를 시작할까요?') != QMessageBox.StandardButton.Yes:
            return
        catalog = {s.code:s.name for s in self.stocks if s.source == 'DEMO'}
        self.condition_commands.sync(self.cfg, self.settings.save, now=datetime.now().timestamp(), busy=True)
        if self.remote_scan_active or self.remote_theme_active:
            return
        self.remote_scan = RecommendationRequest(current_mode(self.cfg).key)
        self.remote_theme = ThemeRequest(self.history,current_mode(self.cfg).key)
        worker = TelegramOrderWorker(data_dir()/'telegram_offline', token, user_id, chat_id, catalog,
                                     conditions=self.condition_commands, orders_enabled=orders_enabled,
                                     recommendation_request=self.remote_scan,
                                     theme_request=self.remote_theme,
                                     recommendation_status=lambda db=self.db, mode=current_mode(self.cfg).key: recommendation_schedule_summary(db,mode,enabled=self.cfg.get('recommendation_auto_evaluate'),now=datetime.now(KST)),
                                     watch_status=self.mock_watch_status.summary,
                                     watch_refresh=self.mock_watch_status.request_refresh,
                                     watch_result=self.mock_watch_status.refresh_result,
                                     watch_reconnect=self.mock_watch_status.request_reconnect,
                                     watch_validation=self.mock_watch_status.validation_summary,
                                     watch_history=self.mock_watch_status.history_summary,
                                     watch_funds=self.mock_watch_status.funds_summary,
                                     recommendation_report=lambda performance, page=1, scan_id=None, db=self.db, mode=current_mode(self.cfg).key:
                                         (db.recommendation_search_result(mode,scan_id,page=page) if scan_id is not None else
                                          db.recommendation_summary(mode,performance=performance,page=page)),
                                     theme_budget=lambda total, fee, buffer, page, take_bps=200, stop_bps=300, history=self.history, mode=current_mode(self.cfg).key: theme_budget_text(theme_budget_preview(history,mode=mode,total_won=total,fee_bps=fee,buffer_bps=buffer,take_bps=take_bps,stop_bps=stop_bps),page=page),
                                     theme_status=lambda destination, history=self.history, mode=current_mode(self.cfg).key: theme_delivery_summary(history,mode=mode,destination=destination),
                                     theme_report=lambda page=1, history=self.history, mode=current_mode(self.cfg).key: theme_latest_report(history,mode=mode,page=page),
                                     practice_daily=lambda day, page=1, db=self.db: db.practice_daily_report(day,page=page),
                                     practice_sales=lambda trade_id=None, page=1, db=self.db: db.practice_sale_report(trade_id,page),
                                     recommendation_overview=lambda db=self.db, mode=current_mode(self.cfg).key:
                                         db.recommendation_overview(mode),
                                     watch_orders=self.mock_watch_status.handle_orders,
                                     watch_cancel_orders=self.mock_watch_status.cancel_orders)
        self.telegram_order_worker = worker
        worker.status.connect(self.telegram_orders_status)
        worker.finished.connect(self.telegram_orders_finished)
        self.telegram_order_status.setText('연결 확인 중 · '+current_mode(self.cfg).title)
        self.telegram_order_start.setEnabled(False)
        self.telegram_order_stop.setEnabled(True)
        worker.start()

    def stop_telegram_orders(self):
        if self.telegram_order_worker:
            self.telegram_order_worker.stop()
        self.telegram_order_stop.setEnabled(False)
        self.telegram_emergency.setEnabled(False)
        self.telegram_resume.setEnabled(False)
        self.telegram_order_status.setText('수신 중지 요청 · 확인 대기 철회 · 이미 접수한 주문 취소는 별도입니다.')

    def telegram_orders_status(self, text):
        self.telegram_order_status.setText(text)
        worker = self.telegram_order_worker
        ready = bool(worker and worker.service is not None and worker.isRunning()
                     and not worker.isInterruptionRequested() and text.startswith('수신 중 ·'))
        self.telegram_emergency.setEnabled(ready and worker.orders_enabled)
        self.telegram_resume.setEnabled(ready and worker.orders_enabled)

    def emergency_telegram_orders(self):
        worker = self.telegram_order_worker
        if worker is None or not worker.isRunning():
            return
        try:
            report = worker.emergency_stop()
            self.telegram_order_status.setText(report)
            QMessageBox.information(self, '긴급 중지 · 가짜 서버만 연결', report)
        except Exception:
            self.telegram_order_status.setText('긴급 중지 기록·주문 상태 확인이 필요합니다. 수신을 중지하고 재시작해 복구 확인하세요.')

    def resume_telegram_orders(self):
        worker = self.telegram_order_worker
        if worker is None or not worker.isRunning():
            return
        if QMessageBox.question(self, '복구 확인 후 연습 재개',
                '미확인·미체결 주문과 잔액을 대조하고 문제가 없을 때만 새 연습 주문을 허용합니다.\n'
                '실제 계좌에 주문하지 않습니다. 계속할까요?') != QMessageBox.StandardButton.Yes:
            return
        try:
            self.telegram_order_status.setText(worker.resume_after_review())
        except ValueError as exc:
            QMessageBox.information(self, '재개하지 않음', str(exc))
        except Exception:
            QMessageBox.information(self, '재개하지 않음', '복구 기록을 확인하지 못했습니다. 수신을 중지하세요.')

    def telegram_orders_finished(self):
        self.telegram_order_start.setEnabled(True)
        self.telegram_order_stop.setEnabled(False)
        self.telegram_emergency.setEnabled(False)
        self.telegram_resume.setEnabled(False)
        if ('확인하세요' not in self.telegram_order_status.text()
                and not self.telegram_order_status.text().startswith('수신 중지 ·')):
            self.telegram_order_status.setText('수신 중지 · 자동으로 다시 시작하지 않습니다.')

    def run_task(self, fn, ok, success_text='완료', with_progress=False):
        if self.work is not None and self.work.isRunning():
            self.statusBar().showMessage('요청 작업이 진행 중입니다.')
            return
        worker = Work(fn, with_progress)
        self.work = worker
        self.scan_button.setEnabled(False)
        self.cancel_scan_button.setEnabled(with_progress)
        worker.progress.connect(self.show_scan_progress)
        worker.succeeded.connect(lambda result: self.complete_task(ok, result, success_text))
        worker.failed.connect(self.task_failed)
        worker.finished.connect(lambda: self.scan_button.setEnabled(True))
        worker.finished.connect(lambda: self.cancel_scan_button.setEnabled(False))
        worker.start()

    def complete_task(self, callback, result, text):
        try:
            callback(result)
            self.statusBar().showMessage(text)
        except Exception as ex:
            self.task_failed(str(ex))

    def task_failed(self, message):
        if self.remote_theme_active:
            self.remote_theme.finish(success=False)
            self.remote_theme_active=False
            self.statusBar().showMessage('테마1 판정 미완료 · 저장 자료를 확인하세요.')
            return
        if self.remote_scan_active:
            self.remote_scan.finish(success=False)
            self.remote_scan_active = False
            self.scanning = False
            self.count_info.setText('원격 추천 검색 미완료 · 연결 상태를 확인하세요.')
            self.db.log('ERROR', '원격 추천 검색 미완료')
            return
        if self.scanning:
            self.count_info.setText('전체 조회 미완료 · 추천 없음 · ' + message)
        self.scanning = False
        self.db.log('ERROR', message)
        self.statusBar().showMessage('오류: ' + message)
        QMessageBox.warning(self, '작업 실패', message)

    def poll_remote_scan(self):
        worker = self.telegram_order_worker
        if not self.remote_scan:
            return
        if self.remote_scan_active and self.remote_scan.cancellation_requested():
            if self.work and self.work.isRunning():
                self.work.requestInterruption()
            return
        active = bool(worker and worker.isRunning() and worker.service is not None
                      and not worker.isInterruptionRequested())
        if active:
            active = worker.service.accepts_search_requests()
        if self.remote_theme is not None:
            data=self.remote_theme.take(current_mode(self.cfg).key,
                busy=bool(self.work and self.work.isRunning()),active=active)
            if data is not None:
                self.remote_theme_active=True
                self.run_task(lambda:prepare_confirmed(self.history,data,self.remote_theme.mode,remember=False),
                              self.remote_theme_done,'테마1 판정 저장 완료')
                return
        if self.remote_scan.take(current_mode(self.cfg).key,
                                 busy=bool(self.work and self.work.isRunning()), active=active):
            self.scan(remote=True)

    def remote_theme_done(self,result):
        self.remote_theme.finish(success=True,result=result)
        self.remote_theme_active=False

    def scan(self, remote=False):
        if self.work and self.work.isRunning():
            return
        cfg = dict(self.cfg)
        self.remote_scan_active = remote is True
        mode = cfg['data_mode']
        self.scanning = True
        self.stocks = []
        self.results = []
        self.fill_stock_tables()
        self.count_info.setText('전체 종목 조회 준비 중 · 이전 추천 초기화')
        self.statusBar().showMessage(f'조회 중: {mode}')
        def fetch(progress, cancelled):
            if mode == 'DEMO':
                return self.demo.scan(cfg['market']),None
            key, secret = self.broker_keys(cfg['kiwoom_env'])
            client = KiwoomMarket(key, secret, cfg['kiwoom_env'],batch_profile=self.quote_batch_profile)
            try:
                rows=client.scan(cfg['market'], progress=progress, cancelled=cancelled,
                                   include_etf=cfg['include_etf'])
                return rows,client.last_scan_metrics
            finally:
                client.session.close()
        self.run_task(fetch, lambda output: self.on_scan(output[0],cfg,mode,metrics=output[1]), '전체 대상 검색 및 DB 저장 완료', with_progress=True)

    def show_scan_progress(self, message):
        if self.remote_scan_active:
            self.remote_scan.progress(message)
        self.count_info.setText(message)
        self.statusBar().showMessage(message)

    def cancel_scan(self):
        if self.work and self.work.isRunning() and self.cancel_scan_button.isEnabled():
            self.work.requestInterruption()
            self.timer.stop()
            self.auto_button.setText('자동 검색 시작')
            self.cancel_scan_button.setEnabled(False)
            self.show_scan_progress('조회 취소 대기 중 · 현재 요청 종료 후 중지')

    def on_scan(self, stocks, cfg, mode, *, metrics=None):
        if self.remote_scan_active and self.remote_scan.cancellation_requested():
            raise RuntimeError('원격 검색을 취소하여 추천을 생성하지 않았습니다.')
        if self.work and self.work.isInterruptionRequested():
            raise RuntimeError('조회가 취소되어 추천을 생성하지 않았습니다.')
        self.scanning = False
        self.stocks = stocks
        self.results = evaluate(stocks, cfg)
        scan_id=self.db.save_scan(stocks, self.results, cfg, cfg['market'], mode,metrics=metrics)
        try:
            record_nxt_evidence(self.history,current_mode(cfg).key,stocks)
        except Exception:
            self.db.log('NXT_EVIDENCE_WARNING','NXT 날짜별 관측 저장 실패 · 테마 판정의 해당 정보는 확인 필요')
            if not self.remote_scan_active:
                QMessageBox.information(self,'NXT 관측 확인','NXT 관측을 저장하지 못했어요. 테마1의 NXT 정보는 확인이 필요해요.')
        if self.remote_scan_active:
            self.fill_stock_tables()
            self.count_info.setText(f'원격 추천 검색 저장 완료 · 조회 {len(stocks):,}개')
            self.remote_scan.finish(success=True, result=self.db.recommendation_search_result(current_mode(cfg).key,scan_id))
            self.remote_scan_active = False
            return
        self.shadow.initialize_defaults(cfg,mode)
        self.shadow.ensure_active_reference(cfg,mode)
        shadow_events=self.shadow.process_scan(scan_id,stocks)
        for ev in shadow_events:
            self.db.log('SHADOW',f'{ev.name} / {ev.code} / {ev.event}, pnl={ev.pnl}')
        self.refresh_shadow()
        events = auto_paper_exit(self.db, stocks, cfg)
        for name, reason, pnl in events:
            self.db.log('PAPER_EXIT', f'{name}: {reason}, 가상손익 {pnl:,.0f}원')
        self.fill_stock_tables()
        self.refresh_stats()
        self.refresh_trades()
        self.maybe_daily_jobs()
        label = '합성 예제' if mode == 'DEMO' else '선택 시장 전체 조회 완료'
        label += ' · ETF ' + ('포함' if cfg['include_etf'] else '제외')
        self.count_info.setText(f'{label} {len(stocks):,}개 · 필터 통과 {sum(r.passed for r in self.results):,}개')
        if events:
            QMessageBox.information(self,'가상매매 자동청산', '\n'.join(f'{name}: {reason}  {pnl:+,.0f}원' for name,reason,pnl in events))

    def fill_stock_tables(self):
        self.screen_table.setRowCount(len(self.results))
        filtered = [r for r in self.results if r.passed][:self.cfg['recommend_count']]
        for row_idx, r in enumerate(self.results):
            st = r.stock
            cols = [row_idx+1,st.code,st.name,st.market,f'{st.price:,.0f}', f'{st.change_pct:+.2f}%',
                    f'{st.trade_value_eok:,.1f}',f'{st.vol_ratio_pct:,.0f}' if st.vol_ratio_pct is not None else '자료 없음',
                    f'{st.cap_eok:,.0f}' if st.cap_eok is not None else '자료 없음',f'{r.score:.0f}',
                    ('✓ 통과 · ETF' if st.is_etf else '✓ 통과') if r.passed else r.reason,
                    st.quote_stamp or ('합성 예제' if st.source == 'DEMO' else '확인 불가'), st.stamp]
            for j,x in enumerate(cols):
                self.screen_table.setItem(row_idx,j,cell(x))
        self.dashboard_table.setRowCount(len(filtered))
        for i,r in enumerate(filtered):
            s = r.stock
            for j,x in enumerate([s.name,s.market,f'{s.price:,.0f}',f'{s.change_pct:+.2f}%',
                                  f'{r.score:.0f}', '조건 충족']):
                self.dashboard_table.setItem(i,j,cell(x))

    def paper_buy(self):
        row_idx = self.screen_table.currentRow()
        if row_idx < 0 or row_idx >= len(self.results):
            QMessageBox.information(self,'종목 선택','스크리너에서 먼저 종목을 선택하세요.')
            return
        result = self.results[row_idx]
        # A saved filter can change after the scan. Attribute the trade only to
        # the conditions actually checked at entry, never a stale passed flag.
        result = evaluate([result.stock], self.cfg)[0]
        if not result.passed:
            QMessageBox.warning(self,'선정 조건 미충족','필터를 통과한 종목만 가상매수할 수 있습니다.')
            return
        st = result.stock
        qty,ok = QInputDialog.getInt(self,'가상매수 수량',f'{st.name} · 가상체결 (주)',1,1,100,1)
        if not ok:
            return
        if QMessageBox.question(self, '가상매수 확인',
                f'{st.name} {qty}주 · 관측가 {st.price:,.0f}원\n가상매매이며 실제 증권계좌에 주문하지 않습니다.\n진행할까요?') != QMessageBox.StandardButton.Yes:
            return
        try:
            if not fresh_practice_price(st):
                raise ValueError('가격 조회가 오래됐어요. [지금 검색] 후 다시 시도하세요.')
            fill = self.db.paper_open(st,qty,self.cfg['exit_stop_pct'],self.cfg['exit_take_pct'],
                self.cfg['exit_max_days'],self.cfg['paper_fee_pct'],self.cfg['paper_slippage_pct'],
                dict(self.cfg),self.cfg['max_paper_positions'])
            self.db.log('PAPER_BUY',f'{st.code} {qty}주 가상매수 {fill:,.1f}원')
            self.refresh_stats()
            self.refresh_trades()
            self.statusBar().showMessage(f'가상매수: {st.name} × {qty}주')
        except Exception as ex:
            QMessageBox.warning(self,'가상매수 실패',str(ex))

    def paper_manual_close(self):
        idx = self.trade_table.currentRow()
        if idx < 0:
            return
        trade_id = int(self.trade_table.item(idx,0).text())
        trade = next((x for x in self.db.open_trades() if x['id']==trade_id),None)
        if not trade:
            QMessageBox.information(self,'선택 오류','이미 청산된 거래입니다.')
            return
        st = next((s for s in self.stocks if s.code == trade['code']),None)
        if not fresh_practice_price(st):
            QMessageBox.warning(self,'가격 갱신 필요','최신 가격이 없습니다. 먼저 [지금 검색]을 눌러주세요.')
            return
        remaining=trade['remaining_qty']
        if type(remaining) is not int or remaining<=0:
            QMessageBox.information(self,'연습 수량 확인','남은 수량 기록을 확인해야 해요. 매도를 기록하지 않았어요.')
            return
        quantity,ok=QInputDialog.getInt(self,'연습 매도 수량',f'{trade["name"]} · 남은 {remaining}주 중 몇 주를 팔까요?',remaining,1,remaining,1)
        if not ok:
            return
        if QMessageBox.question(self,'연습 매도 확인',f'{trade["name"]} {quantity}주 · 관측가 {st.price:,.0f}원\n'
                f'매도 후 남은 수량 {remaining-quantity}주\n앱 안의 연습 기록이며 증권계좌에 주문하지 않아요. 진행할까요?') != QMessageBox.StandardButton.Yes:
            return
        try:
            if not fresh_practice_price(st):
                raise ValueError('가격 조회가 오래됐어요. [지금 검색] 후 다시 시도하세요.')
            self.db.paper_sell(trade_id,quantity,st.price,'가상 수동청산' if quantity==remaining else '가상 분할매도',
                self.cfg['paper_fee_pct'],self.cfg['paper_sell_tax_pct'],self.cfg['paper_slippage_pct'],request_key=uuid.uuid4().hex)
        except ValueError as error:
            QMessageBox.information(self,'연습 매도 확인',str(error))
            return
        self.refresh_trades()
        self.refresh_stats()

    def export_practice_exits(self):
        path,_=QFileDialog.getSaveFileName(self,'연습 분할매도 내역 CSV 저장','practice_sales.csv','CSV (*.csv)')
        if path:
            self.db.export_paper_exits(path)
            QMessageBox.information(self,'연습 내역 저장','가정 비용을 포함한 연습 매도 내역을 저장했어요. 실제 증권사 체결 내역이 아니에요.')

    def show_practice_daily(self):
        from app.trading_clock import KST
        mode=current_mode(self.cfg)
        database=self.db
        day,ok=QInputDialog.getText(self,'연습 하루 결산','확인할 한국 날짜 (YYYY-MM-DD)',
                                   text=datetime.now(KST).date().isoformat())
        if not ok:return
        dialog=QDialog(self)
        dialog.setWindowTitle(mode.title+' · 연습 하루 결산')
        dialog.resize(850,650)
        layout=QVBoxLayout(dialog)
        note=lbl('저장된 연습 기록만 읽습니다. 실제 계좌 조회·발송·주문은 하지 않아요.','Muted')
        note.setWordWrap(True);layout.addWidget(note)
        view=QTextEdit();view.setReadOnly(True);layout.addWidget(view)
        page=QSpinBox();page.setRange(1,999999);page.setPrefix('페이지 ');layout.addWidget(page)
        def load(number):
            try: text=database.practice_daily_report(day,page=number)
            except Exception: text='날짜와 연습 결산 기록을 확인해야 해요. 저장 파일을 바꾸지 않았어요.'
            view.setPlainText(text)
        page.valueChanged.connect(load);load(1)
        close=QPushButton('닫기');close.clicked.connect(dialog.accept);layout.addWidget(close)
        dialog.exec()

    def show_practice_sales(self):
        selected=self.trade_table.currentRow()
        if selected<0:
            QMessageBox.information(self,'종목 선택','연습 기록에서 먼저 종목을 선택하세요.')
            return
        trade_id=int(self.trade_table.item(selected,0).text())
        trade=next((row for row in self.db.trades() if row['id']==trade_id),None)
        if trade is None:
            QMessageBox.information(self,'기록 확인','기록을 새로 불러온 뒤 다시 선택하세요.')
            return
        dialog=QDialog(self)
        dialog.setWindowTitle('선택 종목의 연습 매도 내역')
        dialog.resize(700,520)
        layout=QVBoxLayout(dialog)
        view=QTextEdit()
        view.setReadOnly(True)
        view.setPlainText(sale_details(trade,self.db.paper_exit_history(trade_id)))
        layout.addWidget(view)
        done=QPushButton('닫기')
        done.clicked.connect(dialog.accept)
        layout.addWidget(done)
        dialog.exec()

    def refresh_stats(self):
        st = self.db.stats()
        self.kpi_pnl.setText(f'{st["pnl"]:+,.0f}원')
        self.kpi_win.setText(f'{st["winrate"]:.1f}%')
        self.kpi_count.setText(f'{st["count"]}건')
        self.kpi_open.setText(f'{st["open_count"]}종목')
        self.stats_line.setText(f'연습 실현손익 {st["pnl"]:+,.0f}원 (일부 매도 포함)  |  모두 판 거래 {st["count"]}건 · 승률 {st["winrate"]:.1f}%'
                               +(f' · 금액 확인 필요 {st["unknown_count"]}건' if st['unknown_count'] else ''))

    def refresh_trades(self):
        trades = self.db.trades()
        self.trade_table.setRowCount(len(trades))
        for i,t in enumerate(trades):
            pnl = t['pnl'] if t['closed_at'] else t['realized_pnl']
            partial=t['remaining_qty'] is not None and 0<t['remaining_qty']<t['qty']
            data = [t['id'],t['name'], '일부 매도·보유 중' if partial else '보유 중' if t['closed_at'] is None else '모두 매도',
                    f"{t['remaining_qty'] if t['remaining_qty'] is not None else '미확인'} / {t['qty']}",
                    f'{t["buy_fill"]:,.0f}',f'{t["close_fill"]:,.0f}' if t['close_fill'] is not None else '—',
                    f'{pnl:+,.0f}' if pnl is not None else '확인 필요',
                    f'{t["pnl_pct"]:+.2f}%' if t['pnl_pct'] is not None else '—',
                    t['opened_at'][:16],t['closed_at'][:16] if t['closed_at'] else '—', t['close_reason'] or '—', outcome(t)]
            for j,x in enumerate(data):
                self.trade_table.setItem(i,j,cell(x, positive=pnl if j in (6,7) and pnl is not None else None))
        reviews = self.db.condition_reviews(max(20, int(self.cfg['min_strategy_trades'])))
        self.condition_table.setRowCount(len(reviews))
        for i, review in enumerate(reviews):
            st = review['total']
            policy = review['settings']
            label = (review['label'] + f" · 손절 {policy['exit_stop_pct']:g}% / "
                     f"익절 {policy['exit_take_pct']:g}% / 최대 {policy['exit_max_days']:g}일")
            values = [label, f"{st['count']} / {review['open_count']}",
                      f"{st['wins']} / {st['losses']} / {st['draws']}",
                      f"{st['winrate']:.1f}%" if st['count'] else '—',
                      f"{st['mean_pct']:+.2f}%" if st['count'] else '—', review['mode'], review['state']]
            for j, value in enumerate(values):
                item = cell(value)
                item.setToolTip(label + '\n수수료·세금·미끄러짐 가정이 다르면 별도로 집계합니다.\n'
                                '사용자가 선택한 매매 기록 비교이며 모든 추천 종목의 성과를 뜻하지 않습니다.')
                self.condition_table.setItem(i, j, item)

    def show_trade_conditions(self):
        row = self.trade_table.currentRow()
        if row < 0:
            return
        trade_id = int(self.trade_table.item(row, 0).text())
        trade = next((t for t in self.db.trades() if t['id'] == trade_id), None)
        if trade is None:
            return
        try:
            cfg = settings_snapshot(json.loads(trade['strat_json']))
            text = condition_label(cfg)
            text += (f"\n손절 {trade['stop_pct']:g}% · 익절 {trade['take_pct']:g}% · 최대 {trade['max_days']}일"
                     f"\n결과: {outcome(trade)}\n매수 수수료 {trade['buy_fee']:,.2f}원")
            if trade['closed_at']:
                text += f"\n매도 수수료 {trade['close_fee']:,.2f}원 · 세금 {trade['close_tax']:,.2f}원"
            else:
                text += '\n보유 중인 종목은 매도 후 승패가 정해집니다.'
            text += '\n\n연습 매매의 비용 가정으로 계산한 기록이며 실제 체결을 뜻하지 않습니다.'
        except (ValueError, TypeError):
            text = '이전 기록의 조건을 확인할 수 없습니다.'
        QMessageBox.information(self, '매수 당시 조건 · 연습 기록', text)

    def sync_telegram_conditions(self):
        busy = self.scanning or bool(self.work and self.work.isRunning())
        for key, widget in self.fields.items():
            value = (widget.isChecked() if isinstance(widget,QCheckBox) else
                     widget.currentData() if isinstance(widget,QComboBox) else widget.value())
            busy = busy or value != self.cfg[key]
        try:
            changed = self.condition_commands.sync(self.cfg, self.settings.save,
                now=datetime.now().timestamp(), busy=busy)
            if changed:
                self.stocks, self.results = [], []
                self.fill_stock_tables()
                self.refresh_strategy_fields()
                self.count_info.setText('텔레그램에서 조건을 바꿨어요. 새 조건으로 다시 검색해 주세요.')
                self.shadow.ensure_active_reference(dict(self.cfg),self.cfg['data_mode'])
                self.refresh_shadow()
        except Exception:
            self.condition_timer.stop()
            self.stocks, self.results = [], []
            self.fill_stock_tables()
            self.count_info.setText('추천 조건 저장을 확인해야 해요. 프로그램에서 조건을 다시 확인해 주세요.')

    def save_strategy(self):
        if self.scanning:
            QMessageBox.information(self, '조회 진행 중', '조회가 끝나거나 취소한 뒤 조건을 저장하세요.')
            return
        cfg = dict(self.cfg)
        for key,w in self.fields.items():
            if isinstance(w,QCheckBox):
                cfg[key] = w.isChecked()
            elif isinstance(w,QComboBox):
                cfg[key] = w.currentData()
            else:
                cfg[key] = w.value()
        if cfg['min_price'] > cfg['max_price'] or cfg['min_cap_eok'] > cfg['max_cap_eok'] or cfg['min_change_pct'] > cfg['max_change_pct']:
            QMessageBox.warning(self,'조건 오류','최소값이 최대값보다 클 수 없습니다.')
            return
        cfg['strategy_version'] += 1
        self.cfg.update(cfg)
        self.settings.save()
        # A previous scan may have excluded ETFs; it cannot satisfy new settings.
        self.stocks = []
        self.results = []
        self.fill_stock_tables()
        self.count_info.setText('조건 변경 · 새 설정으로 다시 검색하세요.')
        self.timer.setInterval(max(10, int(cfg['scan_interval_sec']))*1000)
        self.db.log('CONFIG','전략 설정 변경, 버전 '+str(cfg['strategy_version']))
        self.shadow.ensure_active_reference(dict(self.cfg),self.cfg['data_mode'])
        self.refresh_shadow()
        QMessageBox.information(self,'저장 완료','전략 조건을 저장했습니다. 다음 신규 검색부터 반영됩니다.')

    def refresh_strategy_fields(self):
        for k,w in self.fields.items():
            val = self.cfg[k]
            if isinstance(w,QCheckBox):
                w.setChecked(bool(val))
            elif isinstance(w,QComboBox):
                w.setCurrentIndex(w.findData(val))
            else:
                w.setValue(val)

    def create_optimizer(self):
        status = create_proposals(self.db, self.cfg)
        for proposal in self.db.proposals():
            if proposal['state']=='PENDING':
                candidate=dict(self.cfg)
                candidate.update(json.loads(proposal['patch_json']))
                self.shadow.add_proposal_variant(candidate,proposal['id'],self.cfg['data_mode'])
        self.refresh_shadow()
        self.refresh_proposals()
        QMessageBox.information(self,'전략 분석',status)

    def refresh_proposals(self):
        proposals = self.db.proposals()
        self.proposal_table.setRowCount(len(proposals))
        for i,p in enumerate(proposals):
            for j,x in enumerate([p['id'],p['created_at'][:16],p['title'],p['rationale'],p['state']]):
                self.proposal_table.setItem(i,j,cell(x))
        n = len(self.db.closed_trades())
        self.opt_status.setText(f'연습 매도 완료 {n}건 · 조건별 앞 기록 {max(20,self.cfg["min_strategy_trades"])}건 + 뒤 기록 10건부터 비교')

    def review_proposal(self, accept):
        row = self.proposal_table.currentRow()
        if row < 0:
            return
        pid = int(self.proposal_table.item(row,0).text())
        prop = next((p for p in self.db.proposals() if p['id']==pid),None)
        if not prop:
            return
        verb = '승인 · 적용' if accept else '거부'
        if QMessageBox.question(self,'전략 개선안 검토',f'{prop["title"]}\n\n{prop["rationale"]}\n\n{verb}할까요?') != QMessageBox.StandardButton.Yes:
            return
        try:
            patch = json.loads(prop['patch_json'])
            if accept and any(k not in self.cfg for k in patch):
                raise ValueError('알 수 없는 전략 변경값입니다.')
            patch = self.db.review(pid,accept)
            if accept:
                self.cfg.update(patch)
                self.cfg['strategy_version'] += 1
                self.settings.save()
                self.refresh_strategy_fields()
                self.shadow.ensure_active_reference(dict(self.cfg),self.cfg['data_mode'])
                self.refresh_shadow()
            self.db.log('PROPOSAL',f'{pid}: {verb}')
            self.refresh_proposals()
        except Exception as ex:
            QMessageBox.warning(self,'변경 실패',str(ex))

    def save_integrations(self):
        if self.mock_watch_worker and self.mock_watch_worker.isRunning():
            QMessageBox.information(self,'모의 알림 연결 중','모의 알림 받기를 먼저 중지한 뒤 모드·연결 정보를 저장해 주세요.')
            return
        if self.telegram_order_worker and self.telegram_order_worker.isRunning():
            QMessageBox.information(self, '텔레그램 명령 받는 중', '텔레그램 명령 받기를 먼저 중지한 뒤 연결 정보·모드를 저장하세요.')
            return
        if self.scanning or (self.work and self.work.isRunning()):
            QMessageBox.information(self, '작업 진행 중', '현재 작업이 끝나거나 조회를 취소한 뒤 모드를 바꾸세요.')
            return
        try:
            environment = self.kiwoom_env.currentText()
            for key,w in self.secrets.items():
                if self.data_mode.currentText() == 'DEMO' and key in ('kiwoom_appkey', 'kiwoom_secret'):
                    continue
                target = (credential_names(environment)[0 if key == 'kiwoom_appkey' else 1]
                          if key in ('kiwoom_appkey', 'kiwoom_secret') else key)
                self.creds.save(target,w.text().strip(),persist=True)
            self.cfg['data_mode'] = self.data_mode.currentText()
            self.cfg['kiwoom_env'] = self.kiwoom_env.currentText()
            self.cfg['telegram_chat_id'] = self.chat.text().strip()
            self.cfg['telegram_user_id'] = self.telegram_user.text().strip()
            self.cfg['telegram_auto_send'] = self.telegram_auto.isChecked()
            self.settings.save()
            self.timer.stop()
            self.auto_button.setText('자동 검색 시작')
            self.stocks = []
            self.results = []
            self.fill_stock_tables()
            self.count_info.setText('모드·연결 변경 · 다시 종목을 찾아주세요.')
            self.db = Database(data_dir() / practice_db_name(self.cfg))
            self.shadow = ShadowEngine(self.db)
            self.shadow.initialize_defaults(dict(self.cfg), self.cfg['data_mode'])
            self.shadow.ensure_active_reference(dict(self.cfg), self.cfg['data_mode'])
            self.refresh_stats()
            self.refresh_trades()
            self.refresh_proposals()
            self.refresh_shadow()
            self.update_connection()
            self.db.log('SETTINGS','연동 설정 갱신 (토큰/Secret 미기록)')
            QMessageBox.information(self,'저장 완료','인증정보를 OS 보안 저장소에 저장했습니다.')
        except Exception as ex:
            QMessageBox.warning(self,'연동 설정 저장 실패',str(ex))

    def test_kiwoom(self):
        key = self.secrets['kiwoom_appkey'].text().strip()
        secret = self.secrets['kiwoom_secret'].text().strip()
        env = self.kiwoom_env.currentText()
        self.run_task(lambda: KiwoomMarket(key,secret,env).login(),
                      lambda _: QMessageBox.information(self,'키움 API 연결','접근토큰 발급 성공. 실계좌 주문은 불가능합니다.'), '키움 인증 완료')

    def test_telegram(self):
        token = self.secrets['telegram_token'].text().strip()
        chat_id = self.chat.text().strip()
        self.run_task(lambda: Telegram(token,chat_id).send('✅ KRX AI Trading v0.3 연결 테스트 (주문 미지원)'),
                      lambda _: QMessageBox.information(self,'텔레그램','테스트 메시지 전송 성공'), '텔레그램 전송 완료')

    def telegram_recommend(self):
        filtered = [r for r in self.results if r.passed][:self.cfg['recommend_count']]
        if not self.stocks:
            QMessageBox.information(self,'추천 없음','먼저 종목 검색을 실행하세요.')
            return
        class Decor:
            def __init__(self,r):
                self.__dict__.update(r.stock.__dict__)
                self.score = r.score
        rows = [Decor(x) for x in filtered]
        self.send_recs(rows)

    def send_recs(self,rows, on_success=None):
        tok = self.creds.get('telegram_token')
        chat = self.cfg['telegram_chat_id']
        text = recommendation_text(rows, demo=self.cfg['data_mode']=='DEMO')
        def sent(_):
            self.db.log('TELEGRAM','추천 메시지 발송')
            if on_success:
                on_success()
        self.run_task(lambda: Telegram(tok,chat).send(text), sent,
                      '추천 목록을 텔레그램으로 전송했습니다.')

    def maybe_daily_jobs(self):
        now = datetime.now()
        if now.hour >= 16:
            key = now.strftime('%Y-%m-%d')
            if self.cfg.get('last_optimize_day','') != key:
                create_proposals(self.db, self.cfg)
                for prop in self.db.proposals():
                    if prop['state']=='PENDING':
                        variant=dict(self.cfg)
                        variant.update(json.loads(prop['patch_json']))
                        self.shadow.add_proposal_variant(variant,prop['id'],self.cfg['data_mode'])
                self.refresh_shadow()
                self.cfg['last_optimize_day'] = key
                self.settings.save()
                self.refresh_proposals()
            if self.cfg['telegram_auto_send'] and self.cfg['last_notification_day'] != key:
                filtered = [r for r in self.results if r.passed][:self.cfg['recommend_count']]
                class Decor:
                    def __init__(self,r):
                        self.__dict__.update(r.stock.__dict__)
                        self.score = r.score
                def mark_sent():
                    self.cfg['last_notification_day'] = key
                    self.settings.save()
                self.send_recs([Decor(x) for x in filtered], on_success=mark_sent)

    def toggle_auto(self):
        if self.timer.isActive():
            self.timer.stop()
            self.auto_button.setText('자동 검색 시작')
        else:
            self.timer.start()
            self.auto_button.setText('자동 검색 중지')
            self.scan()

    def inspect_saved_backup(self):
        """Inspect an explicitly selected snapshot; never overwrite or restore."""
        if self.work and self.work.isRunning():
            self.statusBar().showMessage('진행 중인 작업이 끝난 뒤 백업을 확인해 주세요.')
            return
        mode=current_mode(self.cfg)
        folder=QFileDialog.getExistingDirectory(self,mode.title+' · 확인할 백업 폴더 선택')
        if not folder: return
        from app.record_backup import inspect_backup
        self.run_task(
            lambda:inspect_backup(folder,expected_mode=mode.key),
            lambda info:QMessageBox.information(
                self,'백업 검증 완료',
                f"{mode.title} 기록 사본을 확인했어요.\n"
                f"저장 시각: {info['created_at']}\n"
                f"검색 기록: {info['scan_count']}건\n"
                f"매매 기록: {info['trade_count']}건\n"
                f"추천 묶음: {info['recommendation_count']}건\n"
                '현재 데이터는 변경하지 않았어요. 실제 복원·주문은 하지 않았어요.'),
            '현재 모드 기록 백업 검증 완료')

    def backup_current_records(self):
        if self.work and self.work.isRunning():
            self.statusBar().showMessage('진행 중인 작업이 끝난 뒤 백업해 주세요.')
            return
        mode=current_mode(self.cfg)
        folder=QFileDialog.getExistingDirectory(self,mode.title+' · 기록 백업 폴더 선택')
        if not folder: return
        from app.record_backup import backup_records
        path=self.db.path
        self.run_task(lambda:backup_records(path,folder,mode=mode.key),
            lambda result:QMessageBox.information(self,'기록 백업 완료',
                f'{mode.title} 거래·추천 기록 사본을 검사해 저장했어요.\n{result}\n'
                '연결 키·설정·연구 자료·증권사 주문 기록은 포함하지 않았어요. 자동 복원/주문은 하지 않아요.'),
            '현재 모드 거래·추천 기록 백업 완료')

    def export_trades(self):
        path,_ = QFileDialog.getSaveFileName(self,'가상 매매기록 내보내기',
            str(Path.home() / 'paper_trades.csv'),'CSV (*.csv)')
        if path:
            self.db.export_trades(path)
            self.statusBar().showMessage('CSV 저장: '+path)

    def closeEvent(self,event):
        self.timer.stop()
        if self.mock_watch_worker and self.mock_watch_worker.isRunning():
            self.stop_mock_notices()
            QMessageBox.information(self,'모의 연결 종료 중','모의 알림 연결이 끝난 뒤 다시 닫아 주세요.')
            event.ignore()
            return
        if self.telegram_order_worker and self.telegram_order_worker.isRunning():
            self.stop_telegram_orders()
            QMessageBox.information(self, '수신 종료 중', '텔레그램 수신이 종료된 뒤 다시 닫아주세요. 확인 대기는 철회했습니다.')
            event.ignore()
            return
        if self.work and self.work.isRunning():
            QMessageBox.information(self,'조회 진행 중','진행 중인 요청이 종료된 뒤 닫아주세요.')
            event.ignore()
            return
        event.accept()


def main():
    import sys
    app = QApplication(sys.argv)
    app.setFont(QFont('Malgun Gothic',10))
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
