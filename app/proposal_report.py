"""Read-only proposal whitelist: no free-form rationale or private settings."""
import json
from app.storage import Database
from app.trade_review import FILTER_KEYS,EXIT_KEYS,SNAPSHOT_KEYS
from app.telegram_conditions import KEYS,NUMBERS,SWITCHES,validate_conditions
from app.exit_policy_commands import validate as validate_exit
from app.modes import current_mode


def report(database,mode,*,page=1):
    if mode not in ('DEMO','MOCK','REAL_QUOTES') or type(page) is not int or not 1<=page<=999999:
        raise ValueError('개선안 모드·페이지를 확인해 주세요.')
    reader=Database(database.path,read_only=True)
    with reader.connect() as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='proposal_context'").fetchone():
            return '생성 당시 모드·조건이 확인된 개선안이 없어요. /조건비교로 새로 비교해 주세요.'
        count=db.execute('SELECT COUNT(*) FROM proposals p JOIN proposal_context c ON c.proposal_id=p.id WHERE c.mode=?',(mode,)).fetchone()[0]
        pages=max(1,(count+1)//2)
        if page>pages:return f'개선안은 {pages}페이지까지 있어요.'
        rows=db.execute('SELECT p.id,p.state,p.patch_json,c.base_json FROM proposals p JOIN proposal_context c ON c.proposal_id=p.id WHERE c.mode=? ORDER BY p.id DESC LIMIT 2 OFFSET ?',
            (mode,(page-1)*2)).fetchall()
        observations={}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='shadow_observation_start'").fetchone():
            for row in rows:
                item=db.execute('''SELECT s.id,s.status FROM shadow_strategies s
                    JOIN shadow_observation_start o ON o.strategy_id=s.id
                    WHERE s.source_proposal_id=? AND s.data_source=?''',
                    (row['id'],'DEMO' if mode=='DEMO' else 'KIWOOM')).fetchone()
                if item:
                    signals=db.execute('SELECT COUNT(DISTINCT scan_id) FROM shadow_signals WHERE strategy_id=?',(item['id'],)).fetchone()[0]
                    trades=db.execute('SELECT COUNT(*),COUNT(closed_at) FROM shadow_trades WHERE strategy_id=?',(item['id'],)).fetchone()
                    observations[row['id']]=(item['status'],signals,trades[0],trades[1])
    labels={'market':'시장','include_etf':'ETF',**{v[0]:k for k,v in NUMBERS.items()},
            **{v:k for k,v in SWITCHES.items()},'exit_take_pct':'가상 익절','exit_stop_pct':'가상 손절','exit_max_days':'가상 보유일'}
    states={'PENDING':'검토 대기','APPROVED':'PC에서 승인 기록 있음','REJECTED':'거부 기록 있음',
            'APPLYING':'적용 확인 중 · 재적용 금지','UNKNOWN':'적용 상태 미확인 · 재시작 후 확인'}
    lines=[f'[기록에서 찾은 개선안] {mode} · {page}/{pages}페이지',
        '사용자 선택 가상 기록의 비교 후보예요. AI 학습·독립 미래 검증·최적값 확정은 아니에요.',
        '생성 당시 조건 기준이며 현재 조건과 다를 수 있어요. 조회로 조건/주문을 바꾸지 않아요.']
    if not rows:lines.append('확인된 개선안이 없어요. /조건성적으로 표본을 보고 /조건비교로 비교를 요청하세요.')
    for row in rows:
        base=json.loads(row['base_json']);patch=json.loads(row['patch_json'])
        if (not isinstance(base,dict) or set(base)!=set(SNAPSHOT_KEYS)
                or base['data_mode'] not in ('DEMO','KIWOOM') or base['kiwoom_env'] not in ('MOCK','REAL')
                or type(base['strategy_version']) is not int or base['strategy_version']<1 or current_mode(base).key!=mode
                or not isinstance(patch,dict) or not patch or set(patch)-set(FILTER_KEYS+EXIT_KEYS)):
            raise ValueError('개선안 생성 근거를 확인해 주세요.')
        target=dict(base,**patch)
        validate_conditions({key:target[key] for key in KEYS});validate_exit({key:target[key] for key in EXIT_KEYS})
        lines.append(f'\n개선안 {row["id"]} · {states.get(row["state"],"상태 확인 필요")} · 기준 조건 {base["strategy_version"]}번')
        observation=observations.get(row['id'])
        if observation:
            state,scans,entries,closed=observation
            lines.append(f'별도 가상 관측 {"중지" if state=="STOPPED" else "등록됨"} · 새 조회 {scans}회 · 진입 {entries}건 / 종료 {closed}건')
            lines.append('다음 관측일 조회 가격으로 계산해요. 시초가·실제 체결·검증 완료가 아니에요.')
        else:lines.append('생성 이후 별도 가상 관측 연결은 미확인이에요.')
        for key,value in patch.items():
            if key=='market':value={'전체':'코스피+코스닥','KOSPI':'코스피','KOSDAQ':'코스닥'}[value]
            elif key=='include_etf':value='포함' if value else '제외'
            elif key in SWITCHES.values():value='사용' if value else '사용 안 함'
            else:value=f'{value:g}'
            lines.append(f'{labels[key]} → {value}')
    lines.append('\n검토: /개선안승인 번호 또는 /개선안거부 번호 · 별도 확인 후 PC에서 처리해요. 자동 적용하지 않아요.')
    if page>1:lines.append(f'이전: /개선안 {page-1}')
    if page<pages:lines.append(f'다음: /개선안 {page+1}')
    return '\n'.join(lines)
