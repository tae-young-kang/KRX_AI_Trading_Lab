"""Private diagnostic observations; never execution proof or order authority."""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
import uuid

from app.order_engine import connect
from app.trading_clock import KST


LABELS = {
    'START': '모의 연결 확인 시작', 'ACCOUNT': '모의 계좌·잔고 조회 관측',
    'STREAM': '모의 알림 등록 확인 관측', 'REFRESH': '요청한 잔고 재조회 관측',
    'NOTICE': '모의 주문 처리 알림 관측', 'STOP': '모의 연결 종료',
    'RECOVERY': '사용자가 모의 재연결 요청',
    'ACKNOWLEDGED': '모의 접수 응답 확인 · 체결 완료 아님',
    'REJECTED': '모의 요청 거절', 'UNKNOWN': '모의 전송 결과 미확인',
    'FAILED': '모의 전송 전 확인 실패', 'WARNING': '모의 기록 충돌로 주문 차단',
    'HALT': '사용자 모의 긴급중지 요청',
}


class MockObservationHistory:
    LIMIT = 1000

    def __init__(self, path):
        self.path = Path(path) if path is not None else None
        self.run = uuid.uuid4().hex
        self.failed = False

    def record(self, event, now):
        if event not in LABELS:
            raise KeyError(event)
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with connect(self.path) as db:
                db.execute('PRAGMA busy_timeout=250')
                db.execute('BEGIN IMMEDIATE')
                db.execute('''CREATE TABLE IF NOT EXISTS mock_observations (
                    id INTEGER PRIMARY KEY, run TEXT NOT NULL, stamp TEXT NOT NULL,
                    event TEXT NOT NULL)''')
                db.execute('INSERT INTO mock_observations(run,stamp,event) VALUES (?,?,?)',
                           (self.run, now.astimezone(KST).isoformat(timespec='seconds'), event))
                db.execute('''DELETE FROM mock_observations WHERE id NOT IN (
                    SELECT id FROM mock_observations ORDER BY id DESC LIMIT ?)''', (self.LIMIT,))
        except (sqlite3.Error, OSError):
            # Diagnostics must not replace order evidence or disrupt its owner.
            self.failed = True

    def summary(self):
        lines = ['[키움 모의 관측 기록 · 실제 돈 사용 안 함]',
                 '로컬에 남긴 최근 10개(최대 1,000개 보관) · 최신 입력 순',
                 '체결·최종 취소·잔고 일치·실계좌 허용의 증명이 아니에요.']
        if self.failed:
            lines.append('기록 저장에 실패한 적이 있어 누락될 수 있어요. 집 PC의 저장 공간과 파일 상태를 확인해 주세요.')
        if self.path is None:
            return '\n'.join(lines+['이 실행은 기록 저장이 설정되지 않았어요.'])
        if not self.path.exists():
            return '\n'.join(lines+['아직 저장된 모의 관측이 없어요.'])
        try:
            with closing(sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1)) as db:
                rows = db.execute('SELECT run,stamp,event FROM mock_observations ORDER BY id DESC LIMIT 10').fetchall()
            for run, stamp, event in rows:
                # Never echo arbitrary database contents, even after tampering.
                parsed = datetime.fromisoformat(stamp)
                if parsed.tzinfo is None or event not in LABELS:
                    raise ValueError('invalid observation')
                group = '현재 실행' if run == self.run else '이전 실행'
                lines.append(f'{parsed.astimezone(KST):%Y-%m-%d %H:%M:%S} · {group} · {LABELS[event]}')
            if not rows:
                lines.append('아직 저장된 모의 관측이 없어요.')
        except (sqlite3.Error, OSError, ValueError, TypeError):
            return '\n'.join(lines[:3]+['저장된 기록을 읽을 수 없어요. 집 PC에서 파일 상태를 확인해 주세요.'])
        lines.append('/모의점검: 이번 연결 · /모의상태: 현재 상태 · /주문점검: 주문 차단 이유')
        return '\n'.join(lines)
