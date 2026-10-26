"""User-entered virtual cost assumptions, never broker charges or tax advice."""
import math
import re
from app.modes import current_mode

FIELDS={'수수료':('paper_fee_pct',1),'세금':('paper_sell_tax_pct',1),'슬리피지':('paper_slippage_pct',3)}
KEYS=tuple(item[0] for item in FIELDS.values())
SCOPE=('가상 계산용 가정이며 실제 수수료·세율이 아니에요.\n'
       '새 매매의 조건·새 아침 고정 목록에 반영해요. 과거 고정 목록은 바꾸지 않아요.\n'
       '일반 연습의 기존 보유를 나중에 팔 때는 현재 비용을 쓰는 기존 방식이에요.\n'
       '실제/키움 모의 주문·익절손절·테마1 기준을 바꾸거나 자동매도를 켜지 않아요.')


def validate(value):
    if not isinstance(value,dict) or set(value)!=set(KEYS):raise ValueError('가상 비용 세 항목을 확인해 주세요.')
    for key,high in FIELDS.values():
        if type(value[key]) not in (int,float) or not math.isfinite(value[key]) or not 0<=value[key]<=high:
            raise ValueError('수수료·세금 0~1%, 슬리피지 0~3%로 입력해 주세요.')


def patch(text):
    changes={}
    for token in text.split():
        match=re.fullmatch(r'(수수료|세금|슬리피지)=([0-9]+(?:\.[0-9]{1,3})?)',token)
        if not match:raise ValueError('예: /가상비용변경 수수료=0.015 세금=0.2 슬리피지=0.1')
        key,high=FIELDS[match[1]]
        if key in changes:raise ValueError('같은 항목은 한 번만 입력해 주세요.')
        value=float(match[2])
        if not math.isfinite(value) or not 0<=value<=high:raise ValueError('수수료·세금 0~1%, 슬리피지 0~3%로 입력해 주세요.')
        changes[key]=value
    if not changes:raise ValueError('바꿀 비용을 적어 주세요. /가상비용')
    return changes


def snapshot(config):
    if (not isinstance(config,dict) or config.get('data_mode') not in ('DEMO','KIWOOM')
            or config.get('kiwoom_env') not in ('MOCK','REAL') or type(config.get('strategy_version')) is not int
            or config['strategy_version']<1):raise ValueError('비용의 저장 모드·버전을 확인해야 해요.')
    policy={key:config[key] for key in KEYS};validate(policy)
    return dict(mode=current_mode(config).key,version=config['strategy_version'],cost_policy=policy)


def describe(current):
    policy=current['cost_policy'];validate(policy)
    label={'DEMO':'가상 연습','MOCK':'키움 모의 시세','REAL_QUOTES':'실제 시세 보기'}[current['mode']]
    return (f'[가상 비용 · {label} · 조건 {current["version"]}번]\n'
            f'편도 수수료 {policy["paper_fee_pct"]:g}% · 매도세금 {policy["paper_sell_tax_pct"]:g}%\n'
            f'편도 슬리피지 {policy["paper_slippage_pct"]:g}%\n'+SCOPE)
