from app.backtest import Bar
from app.krx_history import HistoryStore
from app.recommendation_outcomes import SessionEvidence
from app.theme_store import prepare


def test_cached_detection_is_durable_separate_and_not_a_delivery_claim(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    days=('2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19')
    rows=[Bar(day,'900001','연습','KOSPI',100,110,99,100,1000,100,4000) for day in days]
    history.insert_bars(rows,'DEMO')
    args=dict(sessions=SessionEvidence(days,days[0],days[-1],'DEMO'),through=days[-1],mode='DEMO',source='DEMO')
    first=prepare(history,**args)
    assert first['prepared']==1 and first['matched']==1 and first['coverage']=='CACHED_SYMBOLS_ONLY'
    assert prepare(HistoryStore(history.path),**args)['prepared']==0
    with history.connect() as db:
        row=db.execute('SELECT * FROM theme_alerts').fetchone()
        assert row['state']=='PREPARED' and 'NXT 거래 가능 여부 미확인' in row['message']
    history.insert_bars([Bar(days[-1],'900001','연습','KOSPI',100,120,99,100,1000,200,4000)],'DEMO')
    assert prepare(history,**args)['prepared']==1


def test_missing_symbol_day_is_reported_without_false_negative_claim(tmp_path):
    history=HistoryStore(tmp_path/'history.db')
    days=('2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19')
    history.insert_bars([Bar(day,'900001','연습','KOSDAQ',100,110,99,100,1000,100,4000) for day in days[1:]],'DEMO')
    result=prepare(history,sessions=SessionEvidence(days,days[0],days[-1],'DEMO'),through=days[-1],mode='DEMO',source='DEMO')
    assert result['states']=={'PRICE_MISSING':1} and result['matched']==0


def test_latest_report_drops_corrected_nonmatch_and_separates_sources(tmp_path):
    from app.theme_store import latest_report
    history=HistoryStore(tmp_path/'history.db')
    assert '판정 기록이 없어요' in latest_report(history,mode='DEMO',source='DEMO')
    days=('2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19')
    args=dict(sessions=SessionEvidence(days,days[0],days[-1],'DEMO'),through=days[-1],mode='DEMO',source='DEMO')
    history.insert_bars([Bar(day,'900001','연습','KOSPI',100,110,99,100,1000,100,4000) for day in days],'DEMO')
    result=prepare(history,**args)
    assert len(result['missing_market_days'])==7
    text=latest_report(history,mode='DEMO',source='DEMO')
    assert '1/1페이지' in text and '전체 시장 수집 완료는 미확인' in text
    assert '이 모드' in latest_report(history,mode='MOCK',source='KRX')
    history.insert_bars([Bar(day,'900001','연습','KOSPI',100,101,99,100,1000,99,4000) for day in days],'DEMO')
    prepare(history,**args)
    text=latest_report(history,mode='DEMO',source='DEMO')
    assert '감지 0개' in text and '종목 감지' not in text
    with history.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM theme_alerts').fetchone()[0]==1
