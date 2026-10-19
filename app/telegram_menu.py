"""Small Korean navigation keyboard; buttons send ordinary authorized messages.

Bot API: https://core.telegram.org/bots/api#replykeyboardmarkup
"""
HEADER='[주식 도우미 메뉴]'
HIDDEN='메뉴 버튼을 숨겼어요. /메뉴 로 다시 열 수 있어요.'
ROWS=(('조건 보기','조건 바꾸기'),('새 추천 찾기','검색 진행'),('최근 추천','테마1 보기'),
      ('테마1 새 판정','테마1 진행'),
      ('추천 성적','전체 도움말'),('메뉴 숨기기',))
COMMANDS=dict(zip((label for row in ROWS for label in row),
                  ('/조건','/조건바꾸기','/추천검색','/검색상태','/추천기록','/테마1','/테마1검색','/테마1검색상태','/추천성적','/도움말','/메뉴숨기기')))
CONDITION_HEADER='[추천 조건 바꾸기]'
CONDITION_ROWS=(('시장: 코스피','시장: 코스닥'),('시장: 코스피+코스닥',),
                ('ETF: 포함','ETF: 제외'),('조건 보기','메뉴로 돌아가기'))
CONDITION_COMMANDS={
    '시장: 코스피':'/조건변경 시장=코스피',
    '시장: 코스닥':'/조건변경 시장=코스닥',
    '시장: 코스피+코스닥':'/조건변경 시장=전체',
    'ETF: 포함':'/조건변경 ETF=포함',
    'ETF: 제외':'/조건변경 ETF=제외',
    '메뉴로 돌아가기':'/메뉴',
}


def condition_text():
    return '\n'.join([CONDITION_HEADER,
        '1. 바꿀 시장이나 ETF 버튼을 하나 고르세요.',
        '2. 변경안을 읽고 답장에 있는 /조건확인 코드를 보내세요. 2분 안에 확인해야 해요.',
        '3. 조건 보기에서 적용 결과를 확인한 뒤 새 추천 찾기를 누르세요.',
        '버튼 하나는 그 항목만 바꿔요. 다른 항목은 그대로예요.',
        '시장 전체를 검색한 뒤 조건으로 골라요. 추천 수는 표시할 최대 개수예요.',
        '금액 등 직접 변경: /조건변경 거래대금=100 거래대금사용=켜기',
        '거래대금 단위는 억원이에요. 더 많은 항목: /조건도움말'])


def text(*,practice,active):
    return '\n'.join([HEADER,
        '현재: '+('가짜 서버로 연습 · 실제 돈 사용 안 함' if practice else '시세/조건 관리 · 실제 돈 주문 잠김'),
        '1. 조건 보기: 시장·ETF·고르는 기준 확인',
        '조건 바꾸기: 시장·ETF를 버튼으로 선택',
        '2. 새 추천 찾기: 집 PC에 새 검색 요청',
        '3. 최근 추천: 저장된 추천 종목 확인',
        '검색 진행에서 완료를 확인하세요. 테마1은 추가 발굴 방법이에요.',
        '테마1 새 판정: PC에서 확인한 날짜의 저장 시세로 판정 · 새 수집은 안 함',
        ('새 요청 가능' if active else '새 요청 중지 상태 · PC에서 수신을 다시 시작하세요.'),
        '버튼이 안 보이면 같은 이름을 입력해도 돼요. 자세한 기능은 전체 도움말에서 확인하세요.'])


def markup(message):
    if isinstance(message,str) and message.startswith(CONDITION_HEADER+'\n'):
        return dict(keyboard=[[dict(text=label) for label in row] for row in CONDITION_ROWS],resize_keyboard=True)
    if isinstance(message,str) and message.startswith(HEADER+'\n'):
        return dict(keyboard=[[dict(text=label) for label in row] for row in ROWS],resize_keyboard=True)
    if message==HIDDEN:
        return dict(remove_keyboard=True)
    return None
