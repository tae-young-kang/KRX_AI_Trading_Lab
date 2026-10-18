"""Frozen theme-run budget preview using captured daily closes, never live quotes."""
import json
from app.budget_preview import equal_budget_preview


def preview_latest(history, *, mode, total_won, fee_bps, buffer_bps):
    with history.connect() as db:
        db.execute('BEGIN')
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='theme_runs'").fetchone():
            raise ValueError('먼저 테마1 판정을 만들어 주세요.')
        run=db.execute('SELECT * FROM theme_runs WHERE mode=? ORDER BY id DESC LIMIT 1',(mode,)).fetchone()
        if run is None:
            raise ValueError('현재 모드의 테마1 판정이 없어요.')
        targets=[]
        for alert_id in json.loads(run['alert_ids_json']):
            row=db.execute('SELECT result_json FROM theme_alerts WHERE id=? AND mode=? AND source=?',
                           (alert_id,mode,run['source'])).fetchone()
            if row is None:
                raise ValueError('판정의 종목 기록을 확인해 주세요.')
            result=json.loads(row['result_json'])['result']
            price=result.get('reference_close')
            # Older records stay unknown; never substitute a newer cache value.
            if type(price) not in (int,float) or price<=0 or price>10**15 or int(price)!=price:
                price=None
            targets.append(dict(code=result['code'],name=result['name'],market=result['market'],
                                reference_won=int(price) if price is not None else None))
        preview=equal_budget_preview(total_won=total_won,targets=targets,available_won=None,
                                     fee_bps=fee_bps,buffer_bps=buffer_bps)
        return dict(preview,run_id=run['id'],price_asof=run['day'],source=run['source'],mode=mode)


def preview_text(preview):
    lines=['[테마1 총예산 미리보기 · 주문 안 함]',
           f'판정 번호 {preview["run_id"]} · {preview["mode"]} · 자료 {preview["source"]}',
           f'가격 기준: {preview["price_asof"]} 저장 종가. 현재가·시초가가 아닙니다.',
           f'총예산 {preview["total_won"]:,}원 · 종목당 {preview["allocated_each_won"]:,}원',
           f'수수료 가정 {preview["fee_bps"]/100:.2f}% · 가격 여유 {preview["buffer_bps"]/100:.2f}%',
           '증권사 주문 가능 금액은 조회하지 않았어요. 실제 매수 가능 여부는 미확인입니다.']
    for target in preview['targets']:
        if target['quantity'] is None:
            detail='기준 가격 미확인'
        elif target['quantity']==0:
            detail='1주 예산 미달 · 예상 0주'
        else:
            detail=f'예상 {target["quantity"]:,}주 · 비용/여유 포함 예상 확보액 {target["reserved_won"]:,}원'
        lines.append(f'{target["name"]} ({target["code"]}) · {detail}')
    lines.extend([f'예상 확보액 합계 {preview["reserved_won"]:,}원 · 미배분/미사용 {preview["unreserved_won"]:,}원',
                  '주수는 소수점 아래를 버렸어요. 남은 돈은 다른 종목에 재배분하지 않습니다.',
                  '가격이 없는 종목의 예산도 남은 돈에 포함됩니다. 실제 주문·예수금 예약은 하지 않았어요.'])
    return '\n'.join(lines)
