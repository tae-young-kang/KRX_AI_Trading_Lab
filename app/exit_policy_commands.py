"""Pure formatting/validation of default virtual exit settings. No order IO."""
import math
import re

FIELDS={'익절':('exit_take_pct',.1,50),'손절':('exit_stop_pct',.1,30),
        '보유일':('exit_max_days',1,30)}
KEYS=tuple(value[0] for value in FIELDS.values())
SCOPE=('새 일반 연습 매매·일반 아침 가상 목록에 사용할 기본값이에요.\n'
       '기존 보유·고정 목록·키움 모의/실제 주문·테마1 기본 2%/3%는 바꾸지 않아요. 자동매도를 켜지 않아요.')


def validate(policy):
    if not isinstance(policy,dict) or set(policy)!=set(KEYS):raise ValueError('익절·손절·보유일 항목을 확인해 주세요.')
    for key,low,high in FIELDS.values():
        value=policy[key]
        if (type(value) not in (int,float) or not math.isfinite(value) or not low<=value<=high
                or key=='exit_max_days' and type(value) is not int):
            raise ValueError('익절 0.1~50%, 손절 0.1~30%, 보유일 1~30일로 입력해 주세요.')


def patch(text):
    result={}
    for token in text.split():
        match=re.fullmatch(r'(익절|손절|보유일)=([0-9]+(?:\.[0-9]{1,2})?)',token)
        if not match:raise ValueError('예: /청산조건변경 익절=2 손절=3 보유일=5')
        key,low,high=FIELDS[match[1]]
        number=float(match[2])
        if key in result:raise ValueError('같은 항목은 한 번만 입력해 주세요.')
        if not math.isfinite(number) or not low<=number<=high or key=='exit_max_days' and not number.is_integer():
            raise ValueError('익절 0.1~50%, 손절 0.1~30%, 보유일 1~30일로 입력해 주세요.')
        result[key]=int(number) if key=='exit_max_days' else number
    if not result:raise ValueError('예: /청산조건변경 익절=2 손절=3 보유일=5')
    return result


def describe(current):
    policy=current['exit_policy'];validate(policy)
    mode={'DEMO':'합성 연습 시세','MOCK':'키움 모의 시세','REAL_QUOTES':'실제 시세'}[current['mode']]
    return (f'[기본 가상 익절·손절 · {mode} · 조건 {current["version"]}번]\n'
            f'익절 +{policy["exit_take_pct"]:g}% · 손절 -{policy["exit_stop_pct"]:g}%\n'
            f'보유 제한 {policy["exit_max_days"]}달력일\n'+SCOPE)
