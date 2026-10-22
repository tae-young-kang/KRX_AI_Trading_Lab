"""Read-only daily view across independently funded morning virtual cohorts."""
from datetime import date
import re
from app.morning_virtual import MorningVirtual,record_path,aware
from app.practice_daily import summarize
from app.storage import Database
from app.trading_clock import KST


def report(root,mode,day,*,now,page=1):
    if (not aware(now) or type(page) is not int or not 1<=page<=999999
            or date.fromisoformat(day).isoformat()!=day or day>now.astimezone(KST).date().isoformat()):
        raise ValueError('오늘까지의 결산 날짜와 페이지를 확인해 주세요.')
    folder=record_path(root,mode,day).parent
    entries=[]
    cohorts=0
    # Only our exact mode/date filenames; no other application/account databases.
    for path in sorted(folder.glob('*.db')):
        if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',path.stem):continue
        try:canonical=date.fromisoformat(path.stem).isoformat()
        except ValueError:continue
        if canonical!=path.stem or canonical>day:continue
        if path.is_symlink() or path.resolve().parent!=folder.resolve():
            raise ValueError('가상 목록의 저장 위치를 확인해 주세요.')
        tracker=MorningVirtual(Database(path,read_only=True))
        plan=tracker.plan()
        if plan['mode']!=mode or plan['day']!=canonical:
            raise ValueError('가상 목록의 날짜·시세 모드가 저장 위치와 달라요.')
        with tracker.db.connect() as db:daily=summarize(db,day)
        pages=max(1,(len(daily['symbols'])+4)//5)
        cohorts+=1
        entries.extend((tracker,canonical,detail,pages) for detail in range(1,pages+1))
    if not entries:
        return f'[가상 하루 기록] {day}\n이 날짜까지 저장된 아침 추천 목록이 없어요. 손익은 미산출이며 0원 성과가 아니에요.'
    if page>len(entries):return f'가상 하루 기록은 {len(entries)}페이지까지 있어요.'
    tracker,cohort,detail,detail_pages=entries[page-1]
    text=tracker.daily_text(day,page=detail)
    # The shared practice renderer's navigation points at a different database.
    text='\n'.join(line for line in text.splitlines() if not line.startswith('다음: /연습결산 '))
    header=(f'[가상 하루 기록] 결산일 {day} · {page}/{len(entries)}페이지\n'
            f'아침 목록 {cohorts}개를 날짜순으로 조회해요. 현재 목록 {cohort} · 종목 내역 {detail}/{detail_pages}\n'
            '목록마다 독립 가상 예산이에요. 실제 계좌 수익률이나 계좌 합계로 계산하지 않아요.\n'
            '이전 목록의 이월 보유도 결산일 기준으로 확인해요. 당일 가격이 없으면 미확인이에요.\n')
    navigation=[]
    if page>1:navigation.append(f'이전: /가상하루 {day} {page-1}')
    if page<len(entries):navigation.append(f'다음: /가상하루 {day} {page+1}')
    return header+text+('\n'+'\n'.join(navigation) if navigation else '')
