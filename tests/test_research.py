from datetime import date, timedelta
from pathlib import Path
import tempfile
from app.krx_history import HistoryStore, normalize_krx
from app.research import evaluate_research, make_variants
from sample_data.create_research_demo import generate


def test_krx_normalization():
    raw={'ISU_CD':'005930','ISU_NM':'삼성전자','TDD_OPNPRC':'50,000','TDD_HGPRC':'52,000',
         'TDD_LWPRC':'49,000','TDD_CLSPRC':'51,000','ACC_TRDVOL':'2000000',
         'ACC_TRDVAL':'100000000000','MKTCAP':'10000000000000'}
    b=normalize_krx(raw,'KOSPI','2026-10-15')
    assert b.code=='005930' and b.cap_eok==100000 and b.trade_value_eok==1000
    assert normalize_krx({**raw,'ISU_CD':'KR7005930003'},'KOSPI','2026-10-15') is None


def test_full_research_storage_and_holdout():
    with tempfile.TemporaryDirectory() as t:
        sample=generate(Path(t)/'synthetic.csv')
        store=HistoryStore(Path(t)/'research.db')
        n=store.import_csv(sample,'DEMO_SYNTHETIC')
        assert n > 10000
        bars=store.load_bars('DEMO_SYNTHETIC')
        report=evaluate_research(bars,topk=5)
        assert len(make_variants())==48 and len(report['variants'])==48
        assert report['source_notice'] and report['data_rows']==n
        assert report['chosen_on_train_only'] is None or any(
            v['id']==report['chosen_on_train_only'] for v in report['variants'])
        # Signals from current day's close must have a next-day label; latest
        # recommendations cannot be incorrectly assumed profitable.
        dates=sorted({b.date for b in bars})
        latest=[s for s in report['top_signals'] if s['day']==dates[-1]]
        assert all(s['r1'] is None and s['r3'] is None for s in latest)
        run_id=store.save_run('DEMO_SYNTHETIC',{'topk':5},report)
        assert store.last_run('DEMO_SYNTHETIC')[0]==run_id
        assert store.count('DEMO_SYNTHETIC')[0]==n

def test_missing_fundamentals_fail_closed():
    from app.research import qualify
    variant=make_variants()[0]
    row={'vol_ratio':500,'cap_eok':None,'value_eok':500,'close':50000,
         'change_pct':5,'above_ma':True}
    assert not qualify(row,variant)
