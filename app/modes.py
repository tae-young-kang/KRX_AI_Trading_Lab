"""User-facing modes. Real-money orders stay locked in every mode."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    key: str
    title: str
    description: str
    color: str
    data_mode: str
    environment: str


MODES = (
    Mode('DEMO', '가상 연습', '가짜 종목과 가짜 가격으로 연습합니다. 실제 돈은 쓰지 않습니다.',
         '#66b8ff', 'DEMO', 'MOCK'),
    Mode('MOCK', '키움 모의투자', '키움 모의 서버에서 시세를 봅니다. 화면의 매매 기록은 앱 안의 연습이며, 텔레그램 모의 주문은 PC에서 당일 별도 허용해야 합니다. 실제 돈은 쓰지 않습니다.',
         '#6de2bc', 'KIWOOM', 'MOCK'),
    Mode('REAL_QUOTES', '실제 시세 보기', '실제 서버의 시세를 봅니다. 매수 버튼도 연습용이며 실제 계좌에는 주문하지 않습니다.',
         '#ffc76b', 'KIWOOM', 'REAL'),
)


def current_mode(config):
    if config['data_mode'] == 'DEMO':
        return MODES[0]
    return MODES[1] if config['kiwoom_env'] == 'MOCK' else MODES[2]


def telegram_start_guidance(config):
    mode = current_mode(config).key
    if mode == 'DEMO':
        return ('[가상 연습]\n등록한 개인 대화에서 추천 조건과 가짜 서버 주문을 연습해요.\n'
                '키움 모의 서버 주문이나 실제 돈 거래가 아니에요.')
    if mode == 'MOCK':
        return ('[키움 모의투자]\n등록한 개인 대화에서 추천 조건·모의 연결·잔고를 확인해요.\n'
                '기본은 조회만 가능해요. 모의 주문은 PC에서 오늘의 허용과 거래일 확인을 따로 켠 뒤,\n'
                '모의 계좌 확인 → 알림 시작 → 텔레그램에서 주문 내용 확인 순서로 진행해요.\n'
                '이 수신 시작만으로 모의 주문을 허용하지 않아요. 실제 돈 주문은 잠겨 있어요.')
    return ('[실제 시세 보기]\n등록한 개인 대화에서 추천 조건과 상태를 확인해요.\n'
            '실제 시세 조회는 실제 돈 주문 허용이 아니에요. 실계좌 주문은 잠겨 있어요.')


MOCK_REFRESH_GUIDANCE = ('새 알림이 왔어요. 이전 잔고는 최신 값이 아니에요.\n'
                         '텔레그램 /잔고 → /잔고결과로 다시 확인해 주세요.\n'
                         '조회에 실패하거나 연결이 끊겼을 때만 집 PC에서 연결을 확인해 주세요.')
MOCK_LISTEN_GUIDANCE = ('모의 알림 연결을 준비하고 있어요. 실제 돈 주문은 잠겨 있어요.\n'
                       '연결 후 모의 주문 허용 여부는 텔레그램 /주문점검에서 확인해 주세요.')


def practice_db_name(config):
    return 'practice_' + current_mode(config).key.lower() + '.db'


def credential_names(environment):
    if environment not in ('MOCK', 'REAL'):
        raise ValueError('알 수 없는 키움 서버입니다.')
    prefix = 'kiwoom_' + environment.lower()
    return prefix + '_appkey', prefix + '_secret'
