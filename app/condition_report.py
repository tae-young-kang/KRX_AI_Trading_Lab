"""Read-only, mode-specific virtual condition scorecards for Telegram."""
from app.storage import Database
from app.modes import current_mode


def report(database,mode,*,min_train=20,page=1):
    if mode not in ('DEMO','MOCK','REAL_QUOTES') or type(page) is not int or not 1<=page<=999999:
        raise ValueError('조건 성적의 시세 모드와 페이지를 확인해 주세요.')
    if type(min_train) is not int or not 20<=min_train<=2000:raise ValueError('조건 비교 최소 기록 수를 확인해 주세요.')
    reader=Database(database.path,read_only=True)
    reviews=[item for item in reader.condition_reviews(min_train) if current_mode(item['settings']).key==mode]
    pages=max(1,(len(reviews)+1)//2)
    if page>pages:return f'조건 성적은 {pages}페이지까지 있어요.'
    label={'DEMO':'합성 시세 연습','MOCK':'키움 모의 시세로 앱 안 연습','REAL_QUOTES':'실제 시세로 앱 안 연습'}[mode]
    lines=[f'[내가 사용한 조건 성적] {label} · {page}/{pages}페이지',
        '증권사 모의계좌/실제 체결 손익과 별도인 앱 안의 가상 기록이에요.',
        f'조건 비교 {len(reviews)}개 · 매수 당시 조건·익절/손절·비용별로 나눠요.',
        f'앞 기록 {min_train}건·5일 이상, 뒤 기록10건·3일 이상과 양쪽 수익이 있어야 추가 검증 후보예요.',
        '사용자가 골라 거래한 기록의 탐색 비교예요. AI 학습·독립 미래 검증·최적 조건 확정은 아니에요.']
    if not reviews:lines.append('비교할 조건 기록이 없어요. 조건이 보관된 가상 매매 기록부터 모아 주세요.')
    for number,item in enumerate(reviews[(page-1)*2:page*2],(page-1)*2+1):
        total=item['total'];cfg=item['settings']
        lines.extend([f'\n조건 {number} · {item["state"]}',item['label'],
            f'익절 +{cfg["exit_take_pct"]:g}% · 손절 -{cfg["exit_stop_pct"]:g}% · 보유 제한 {cfg["exit_max_days"]}달력일',
            f'완료 {total["count"]}건 · 보유 {item["open_count"]}건 · 승 {total["wins"]}/패 {total["losses"]}/본전 {total["draws"]}',
            ('승률 미산출 · 완료 기록 없음' if not total['count'] else
             f'승률 {total["winrate"]:.1f}% · 저장 가상 실현손익 {total["pnl"]:+,.2f}원'),
            f'앞 기록 {item["train"]["count"]}건 · 뒤 기록 {item["test"]["count"]}건'])
        for key,label in (('train','앞'),('test','뒤')):
            stats=item[key]
            lines.append(f'{label} 기록 평균 수익률 {stats["mean_pct"]:+.2f}%' if stats['count'] else f'{label} 기록 평균 미산출')
        costs=item['exit_costs']
        if costs is None:lines.append('청산 비용 가정 미확인/서로 다름 · 추가 검증 후보로 사용하지 않아요.')
        else:lines.append(f'청산 비용 가정: 수수료 {costs["paper_fee_pct"]:g}% · 세금 {costs["paper_sell_tax_pct"]:g}% · 가격차 {costs["paper_slippage_pct"]:g}%')
    lines.append('\n조회만 했어요. 조건을 자동 변경하거나 주문하지 않아요.')
    if page>1:lines.append(f'이전: /조건성적 {page-1}')
    if page<pages:lines.append(f'다음: /조건성적 {page+1}')
    return '\n'.join(lines)
