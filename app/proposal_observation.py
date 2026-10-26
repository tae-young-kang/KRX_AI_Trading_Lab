"""Connect contextual review candidates to future scan-only paper observation."""
from app.strategy import create_proposals
from app.proposal_application import preview
from app.shadow import ShadowEngine


def compare_and_observe(database,config):
    # Capture once: never combine an old proposal patch with today's new base.
    cfg=dict(config)
    message=create_proposals(database,cfg)
    engine=ShadowEngine(database)
    count=0
    for row in database.proposals():
        if row['state']!='PENDING':continue
        try:target=preview(database,cfg,row['id'])
        except (ValueError,TypeError,KeyError):continue
        with database.connect() as conn:
            legacy=conn.execute('''SELECT 1 FROM shadow_strategies s
                LEFT JOIN shadow_observation_start o ON o.strategy_id=s.id
                WHERE s.source_proposal_id=? AND s.data_source=? AND o.strategy_id IS NULL''',
                (row['id'],cfg['data_mode'])).fetchone()
        if legacy:continue  # Never relabel earlier experiments as future-only evidence.
        engine.add_proposal_variant(target,row['id'],cfg['data_mode'])
        count+=1
    return (message+f'\n현재 기준과 일치하는 개선안 {count}개를 별도 가상 관측에 연결했어요. '
            '새 추천 조회부터 기록하며, 과거 성적을 새 관측으로 복사하지 않아요. '
            '관측용 가상 예산 100만원·종목당 1주·최대 3종목·갭 상한 3%예요. '
            '첫 거래 가격 추적·주문·조건 자동 적용이 아니에요.')
