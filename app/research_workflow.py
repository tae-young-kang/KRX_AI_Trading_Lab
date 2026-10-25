"""Shared manual/automatic research over one explicitly selected stored source."""
from datetime import datetime
from app.research import evaluate_research
from app.research_wfo import evaluate_wfo,METHOD
from app.research_preflight import check_research_coverage
from app.trading_clock import KST

HOLDOUT='HOLDOUT_V1'


def research_coverage(history,source,bars,*,reference_path=None,now=None):
    quality=check_research_coverage(history,source,bars,reference_path=reference_path)
    now=now or datetime.now(KST)
    if source=='KRX':
        past=[b for b in bars if b.date<now.astimezone(KST).date().isoformat()]
        if past:
            eligible=history.recommendation_bars(source,min(b.date for b in past),max(b.date for b in past),now=now)
            confirmed={(b.market,b.date,b.code):b for b in eligible}
            unknown={(b.market,b.date) for b in past if confirmed.get((b.market,b.date,b.code))!=b}
            quality=dict(quality,unconfirmed_krx_market_days=len(unknown))
            if unknown:
                quality['state']='ALERT';quality['warning_counts']=dict(quality.get('warning_counts',{}),UNCONFIRMED_KRX_SNAPSHOT=len(unknown))
    return quality


def run_saved(history,source,method,*,topk=5,reference_path=None,now=None):
    if method not in (HOLDOUT,METHOD):raise ValueError('연구 방법을 확인해 주세요.')
    now=now or datetime.now(KST)
    if now.tzinfo is None or now.utcoffset() is None:raise ValueError('연구 평가 시각을 확인해 주세요.')
    bars=history.load_bars(source)
    if not bars:raise ValueError('연구할 저장 시세가 없어요.')
    quality=research_coverage(history,source,bars,reference_path=reference_path,now=now) if method==METHOD else check_research_coverage(history,source,bars,reference_path=reference_path)
    if method==METHOD:report=evaluate_wfo(bars,source=source,coverage_quality=quality,topk=topk,now=now or datetime.now(KST))
    else:
        report=evaluate_research(bars,topk=topk);report['coverage_quality']=quality;report['method_version']=HOLDOUT;report['source']=source
    run_id=history.save_run(source,dict(source=source,topk=topk,method_version=method),report)
    return run_id,report
