"""3-session signal-price coverage; independent of research ranking and means."""
from math import isfinite


def signal_coverage(signals,sessions,*,split,cut):
    if split not in ('TRAIN','TEST') or len(set(sessions))!=len(sessions) or list(sessions)!=sorted(sessions):
        raise ValueError('연구 거래일과 평가 구간을 확인해 주세요.')
    indices={day:index for index,day in enumerate(sessions)}
    counts=dict(selected=0,observed=0,missing=0,embargo=0,tail=0,complete_days=0,partial_days=0,missing_days=0)
    days={};seen=set()
    for signal in signals:
        if signal['split']!=split: continue
        day=signal['day'];key=(day,signal['code'])
        if day not in indices or key in seen or (day<=cut)!=(split=='TRAIN'):
            raise ValueError('추천 날짜·종목이 중복되거나 평가 구간과 달라요.')
        seen.add(key)
        counts['selected']+=1
        target=indices[day]+3
        if split=='TRAIN' and (target>=len(sessions) or sessions[target]>cut):
            counts['embargo']+=1
            continue
        if target>=len(sessions):
            counts['tail']+=1
            continue
        value=signal.get('r3')
        if value is not None and (type(value) not in (int,float) or not isfinite(value)):
            raise ValueError('관측된 3거래일 가격 변화가 유효하지 않아요.')
        observed=value is not None
        counts['observed' if observed else 'missing']+=1
        total,available=days.get(day,(0,0))
        days[day]=(total+1,available+int(observed))
    for total,observed in days.values():
        counts['complete_days' if observed==total else 'partial_days' if observed else 'missing_days']+=1
    return {split.lower()+'_r3_'+key:value for key,value in counts.items()}
