"""Research v0.3 regression + basic chronological validity tests."""
import tempfile
import unittest
from datetime import date,timedelta
from pathlib import Path
from unittest.mock import patch
from app.backtest import Bar
from app.krx_history import HistoryStore, KRXDailyClient, normalize_krx
from app.research import evaluate_research, spearman, export_report


def mini_history(n=95):
    days=[];d=date(2025,1,2)
    while len(days)<n:
        if d.weekday()<5:days.append(d.isoformat())
        d+=timedelta(days=1)
    rows=[]
    for market,offset in [('KOSPI',0),('KOSDAQ',500)]:
        for k in range(16):
            price=12000.+1000*k
            for i,dd in enumerate(days):
                o=price * (1+.001*(i%3))
                c=o*(1+(.025 if i%6==k%6 else -.005))
                vol=90000*(2.8 if i%8==k%8 else 1)
                rows.append(Bar(dd,f'{offset+k:06d}',f'FAKE{k}',market,
                    o,max(o,c)*1.005,min(o,c)*.995,c,vol,
                    max(55,vol*c/1e8),5000+k*400))
                price=c
    return rows

class TestKRX(unittest.TestCase):
    def test_conversion_and_invalid(self):
        row={'ISU_CD':'005930','ISU_NM':'TEST','TDD_OPNPRC':'10,100','TDD_HGPRC':'10,500',
             'TDD_LWPRC':'10,000','TDD_CLSPRC':'10,200','ACC_TRDVOL':'10,000',
             'ACC_TRDVAL':'102,000,000','MKTCAP':'1,020,000,000,000'}
        b=normalize_krx(row,'KOSPI','2026-10-15')
        self.assertEqual(b.code,'005930');self.assertAlmostEqual(b.trade_value_eok,1.02)
        self.assertAlmostEqual(b.cap_eok,10200)
        self.assertIsNone(normalize_krx({**row,'TDD_OPNPRC':'0'},'KOSPI','2026-10-15'))

    def test_store_separated_by_source_and_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            s=HistoryStore(Path(folder)/'research.db')
            bars=mini_history(40)
            s.insert_bars(bars,'DEMO_SYNTHETIC')
            self.assertEqual(len(s.load_bars('CSV_USER')),0)
            self.assertEqual(len(s.load_bars('DEMO_SYNTHETIC')),len(bars))
            v={'id':'one','params':{'min_cap_eok':1000}}
            s.decide_research('DEMO_SYNTHETIC',1,v,True)
            self.assertEqual(s.research_states('DEMO_SYNTHETIC')['one'],'APPROVED_RESEARCH')
            s.decide_research('DEMO_SYNTHETIC',2,v,False)
            self.assertEqual(s.research_states('DEMO_SYNTHETIC')['one'],'REJECTED')

    def test_no_api_credential(self):
        with self.assertRaisesRegex(ValueError,'인증키'):
            KRXDailyClient(' ')

class TestResearch(unittest.TestCase):
    def test_spearman(self):
        self.assertAlmostEqual(spearman([1,2,3],[10,20,30]),1)
        self.assertAlmostEqual(spearman([1,2,3],[30,20,10]),-1)
        self.assertIsNone(spearman([1,1,1],[1,2,3]))

    def test_research_variants_and_outputs(self):
        rep=evaluate_research(mini_history())
        self.assertEqual(len(rep['variants']),48)
        self.assertTrue(rep['top_signals'])
        self.assertTrue(all(x['split'] in ('TRAIN','TEST') for x in rep['top_signals']))
        self.assertTrue(all(x['market'] in ('KOSPI','KOSDAQ') for x in rep['top_signals']))
        self.assertIn(rep['status'],('RESEARCH_ONLY','INSUFFICIENT_DATA'))
        with tempfile.TemporaryDirectory() as folder:
            export_report(rep,folder)
            for name in ('signals.csv','summary.csv','manifest.json'):
                self.assertTrue((Path(folder)/name).is_file())

    def test_no_future_price_feature_leak(self):
        a=mini_history()
        base=evaluate_research(a)
        # Future close/volume modified at the LAST day only: cannot alter
        # signals/score at first eligible date (signal uses past and today).
        b=list(a);last=max(x.date for x in b)
        b=[Bar(x.date,x.code,x.name,x.market,x.open,x.high*100,x.low,
               x.close*100,x.volume*100,x.trade_value_eok,x.cap_eok)
           if x.date==last else x for x in b]
        changed=evaluate_research(b)
        for rep in (base,changed):
            first=min(s['day'] for s in rep['top_signals'])
            rep['_first']={(s['strategy'],s['day'],s['code'],s['score']) for s in rep['top_signals'] if s['day']==first}
        self.assertEqual(base['_first'],changed['_first'])

    def test_missing_cap_cannot_pass_filter(self):
        rows=mini_history(60)
        bad=[Bar(x.date,x.code,x.name,x.market,x.open,x.high,x.low,x.close,x.volume,x.trade_value_eok,None)
             for x in rows]
        rep=evaluate_research(bad)
        self.assertEqual(len(rep['top_signals']),0)
        self.assertIsNone(rep['chosen_on_train_only'])

if __name__=='__main__':unittest.main()
