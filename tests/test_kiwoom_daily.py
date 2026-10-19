"""Official ka10081 contract checked with synthetic payloads only."""
from copy import deepcopy
import pytest
from requests.structures import CaseInsensitiveDict
from app.kiwoom_daily import read_daily

DAYS=['2026-10-08','2026-10-12','2026-10-13','2026-10-14','2026-10-15','2026-10-16','2026-10-19']


def row(day):
    return dict(dt=day.replace('-',''),cur_prc='-10000',open_pric='10000',high_pric='+10500',
                low_pric='9900',trde_qty='1000',trde_prica='10000')


class Client:
    def __init__(self,pages):self.pages=pages;self.calls=[]
    def query(self,*args):self.calls.append(args);return self.pages.pop(0)


def page(rows,cont='N',key=''):
    return dict(stk_cd='900001',return_code=0,stk_dt_pole_chart_qry=rows),CaseInsensitiveDict({'cont-yn':cont,'next-key':key})


def read(client,**kwargs):
    return read_daily(client,code='900001',name='합성종목',market='KOSPI',days=DAYS,**kwargs)


def test_unadjusted_krx_pagination_and_million_won_conversion():
    rows=[row(d) for d in reversed(DAYS)]
    client=Client([page(rows[:3],'Y','page-two'),page(rows[3:])])
    result=read(client)
    assert result['missing_days']==[] and result['api_calls']==2
    assert all(b.trade_value_eok==100 and b.cap_eok is None for b in result['bars'])
    assert result['bars'][-1].high==10500 and result['bars'][-1].close==10000
    assert result['venue']=='KRX' and not result['adjusted']
    assert client.calls[0][0:3]==('ka10081','/api/dostk/chart',dict(stk_cd='900001',base_dt='20261019',upd_stkpc_tp='0'))
    assert client.calls[1][3:5]==('Y','page-two')


def test_missing_today_is_not_filled_from_another_session():
    client=Client([page([row(d) for d in reversed(DAYS[:-1])])])
    result=read(client)
    assert result['missing_days']==[DAYS[-1]] and len(result['bars'])==6


@pytest.mark.parametrize('damage',['code','future','duplicate','order','price','value','missing','continuation'])
def test_bad_response_never_becomes_usable_daily_data(damage):
    rows=[row(d) for d in reversed(DAYS)]
    data,headers=page(rows)
    if damage=='code':data['stk_cd']='900001_NX'
    elif damage=='future':rows[0]['dt']='20261020'
    elif damage=='duplicate':rows[1]=deepcopy(rows[0])
    elif damage=='order':rows.reverse()
    elif damage=='price':rows[0]['high_pric']='100'
    elif damage=='value':rows[0]['trde_prica']='-1'
    elif damage=='missing':del rows[0]['cur_prc']
    elif damage=='continuation':
        data['stk_dt_pole_chart_qry']=rows[:1];headers['cont-yn']='Y'
    with pytest.raises(ValueError):read(Client([(data,headers)]))


def test_cancel_prevents_any_request():
    client=Client([])
    with pytest.raises(ValueError):read(client,cancelled=lambda:True)
    assert client.calls==[]
