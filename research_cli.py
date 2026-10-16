"""Headless test of selector research. Does not send broker orders."""
from __future__ import annotations
import argparse
from datetime import datetime
from pathlib import Path
from app.config import data_dir
from app.krx_history import HistoryStore
from app.research import evaluate_research,export_report


def main():
    p=argparse.ArgumentParser(description='KRX stock-selection research v0.3 (no live trading)')
    p.add_argument('--source',choices=['DEMO_SYNTHETIC','CSV_USER','KRX'],default='DEMO_SYNTHETIC')
    p.add_argument('--csv',default=None,help='Real historical CSV, if source CSV_USER')
    p.add_argument('--topk',type=int,default=5)
    args=p.parse_args()
    db=HistoryStore(data_dir()/'research.db')
    if args.source=='DEMO_SYNTHETIC':
        filename=Path(__file__).parent/'sample_data'/'DEMO_research_ohlcv.csv'
        db.import_csv(filename,args.source)
        print('DEMO_SYNTHETIC — FAKE PRICES / NO INVESTMENT INFERENCE')
    elif args.csv:
        db.import_csv(args.csv,args.source)
    rows=db.load_bars(args.source)
    report=evaluate_research(rows,topk=args.topk)
    rid=db.save_run(args.source,{'mode':'CLI','source':args.source,'topk':args.topk},report)
    out=data_dir()/'research_exports'/f'{args.source}_{datetime.now():%Y%m%d_%H%M%S}_{rid}'
    export_report(report,out)
    print('rows:',len(rows),'strategies:',len(report['variants']), 'status:',report['status'])
    print('research candidate by TRAIN only:',report['chosen_on_train_only'] or '(not enough history)')
    print('output:',out)
    for v in report['variants'][:5]:
        print(v['name'],'TRAIN excess(3d)',v['train_excess3'],'TEST excess(3d)',v['test_excess3'])

if __name__=='__main__':main()
