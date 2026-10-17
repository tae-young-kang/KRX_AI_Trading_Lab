"""User-facing modes. No mode in this version enables account orders."""
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
    Mode('MOCK', '키움 모의투자', '키움 모의 서버에서 시세를 봅니다. 현재 매매는 앱 안의 연습 기록만 남깁니다.',
         '#6de2bc', 'KIWOOM', 'MOCK'),
    Mode('REAL_QUOTES', '실제 시세 보기', '실제 서버의 시세를 봅니다. 매수 버튼도 연습용이며 실제 계좌에는 주문하지 않습니다.',
         '#ffc76b', 'KIWOOM', 'REAL'),
)


def current_mode(config):
    if config['data_mode'] == 'DEMO':
        return MODES[0]
    return MODES[1] if config['kiwoom_env'] == 'MOCK' else MODES[2]


def practice_db_name(config):
    return 'practice_' + current_mode(config).key.lower() + '.db'


def credential_names(environment):
    if environment not in ('MOCK', 'REAL'):
        raise ValueError('알 수 없는 키움 서버입니다.')
    prefix = 'kiwoom_' + environment.lower()
    return prefix + '_appkey', prefix + '_secret'
