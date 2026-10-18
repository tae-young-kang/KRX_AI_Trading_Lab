"""Small Korean navigation keyboard; buttons send ordinary authorized messages.

Bot API: https://core.telegram.org/bots/api#replykeyboardmarkup
"""
HEADER='[주식 도우미 메뉴]'
HIDDEN='메뉴 버튼을 숨겼어요. /메뉴 로 다시 열 수 있어요.'
ROWS=(('조건 보기','새 추천 찾기'),('최근 추천','검색 진행'),
      ('테마1 보기','추천 성적'),('전체 도움말','메뉴 숨기기'))
COMMANDS=dict(zip((label for row in ROWS for label in row),
                  ('/조건','/추천검색','/추천기록','/검색상태','/테마1','/추천성적','/도움말','/메뉴숨기기')))


def text(*,practice,active):
    return '\n'.join([HEADER,
        '현재: '+('가짜 서버로 연습 · 실제 돈 사용 안 함' if practice else '시세/조건 관리 · 실제 돈 주문 잠김'),
        '1. 조건 보기: 시장·ETF·고르는 기준 확인',
        '2. 새 추천 찾기: 집 PC에 새 검색 요청',
        '3. 최근 추천: 저장된 추천 종목 확인',
        '검색 진행에서 완료를 확인하세요. 테마1은 추가 발굴 방법이에요.',
        ('새 요청 가능' if active else '새 요청 중지 상태 · PC에서 수신을 다시 시작하세요.'),
        '버튼이 안 보이면 같은 이름을 입력해도 돼요. 자세한 기능은 전체 도움말에서 확인하세요.'])


def markup(message):
    if isinstance(message,str) and message.startswith(HEADER+'\n'):
        return dict(keyboard=[[dict(text=label) for label in row] for row in ROWS],resize_keyboard=True)
    if message==HIDDEN:
        return dict(remove_keyboard=True)
    return None
