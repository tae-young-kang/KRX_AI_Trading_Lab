"""MOCK amendments/cancellations use fake HTTP/socket and temporary stores."""
from datetime import timedelta
import re
import pytest
from app.order_engine import connect
from test_mock_telegram_orders import rig,queue,handle,add_capture,mutations
from test_kiwoom_mock_review import wire
from test_kiwoom_mock_dispatch import accepted,Response
from test_telegram_orders import sandbox,update


def snapshot(*,filled=1,remaining=2,order='13',original='0',quantity=3):
    data=wire()
    item=dict(data[2]['acnt_ord_cntr_prst_array'][1])
    item.update(ord_no='0000013',orig_ord_no='0',cntr_no='0000001',cntr_qty='1',cntr_uv='1000')
    fills=[item] if filled else []
    if filled>1:
        fills.append(dict(item,cntr_no='0000002',cntr_qty=str(filled-1)))
    data[2]['acnt_ord_cntr_prst_array']=fills
    data[-1]['oso']=([dict(ord_no=order,orig_ord_no=original,stk_cd='900001',ord_qty=str(quantity),oso_qty=str(remaining))]
                      if remaining else [])
    return data


def ready(tmp_path):
    cmd,session,http,socket,clock=rig(tmp_path)
    queue(cmd);add_capture(http);http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    data=snapshot()
    refresh(cmd,session,http,data)
    return cmd,session,http,socket,clock,data


def refresh(cmd,session,http,data):
    add_capture(http,data)
    session.refresh();cmd.observe(session._last_capture)


def action(cmd,text,uid=10,scope='owner'):
    preview=handle(cmd,text,uid=uid,scope=scope)
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    assert '실제 돈 사용 안 함' in preview
    assert '전송을 요청' in handle(cmd,'/모의확인 '+token,uid=uid+1,scope=scope)
    return preview,token


@pytest.mark.parametrize('kind',['amend','cancel'])
def test_followup_uses_parent_number_explicit_remaining_and_one_attempt(tmp_path,kind):
    cmd,session,http,_,_,data=ready(tmp_path)
    text='/정정 13 2 1100' if kind=='amend' else '/취소 13'
    preview,token=action(cmd,text)
    assert '남은 2주' in preview and len(mutations(http))==1
    add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert cmd.process(session) is None and len(mutations(http))==2
    request=mutations(http)[-1]
    assert request['headers']['api-id']==('kt10002' if kind=='amend' else 'kt10003')
    assert request['json']['orig_ord_no']=='0000013'
    assert request['json']['mdfy_qty' if kind=='amend' else 'cncl_qty']=='2'
    assert '유효한' in handle(cmd,'/모의확인 '+token,uid=13)
    with connect(cmd.path) as db:
        last=db.execute('SELECT id FROM intents ORDER BY rowid DESC LIMIT 1').fetchone()[0]
    receipt=cmd.journal.get(last)
    assert receipt['parent_key'] and receipt['root_key']!=receipt['request_key']
    assert '0000014' in handle(cmd,'/주문상태')
    cmd.disarm();session.close()


@pytest.mark.parametrize('kind',['amend','cancel'])
def test_additional_fill_after_preview_blocks_without_clamping_or_resend(tmp_path,kind):
    cmd,session,http,_,_,_=ready(tmp_path)
    action(cmd,'/정정 13 2 1100' if kind=='amend' else '/취소 13')
    add_capture(http,snapshot(filled=2,remaining=1))
    assert cmd.process(session)=='FAILED' and len(mutations(http))==1
    assert not cmd.armed and '새 조회와 새 확인' in handle(cmd,'/주문상태')
    cmd.disarm();session.close()


def test_requires_fresh_cache_owned_latest_parent_and_no_quantity_increase(tmp_path):
    cmd,session,http,_,clock,data=ready(tmp_path)
    assert '본인이' in handle(cmd,'/취소 13',scope='other')
    assert '본인이' in handle(cmd,'/취소 99',uid=11)
    assert '현재 남은 수량 이하' in handle(cmd,'/정정 13 3 1100',uid=12)
    assert '정정:' in handle(cmd,'/취소 13 0',uid=13)
    clock[0]+=timedelta(seconds=31)
    assert '새 조회' in handle(cmd,'/취소 13',uid=14)
    refresh(cmd,session,http,data)
    action(cmd,'/정정 13 1 1100',uid=15)
    add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert '본인이' in handle(cmd,'/취소 13',uid=20)
    assert len(mutations(http))==2
    cmd.disarm();session.close()


def test_amendment_checks_only_additional_reserved_cash(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    action(cmd,'/정정 13 2 1100')
    data[0]['ord_alow_amt']=data[0]['100stk_ord_alow_amt']='100'
    add_capture(http,data)
    assert cmd.process(session)=='FAILED' and len(mutations(http))==1
    cmd.disarm();session.close()


def test_amendment_does_not_charge_existing_reservation_twice(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    action(cmd,'/정정 13 2 1100')
    data[0]['ord_alow_amt']=data[0]['100stk_ord_alow_amt']='300'
    add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED' and len(mutations(http))==2
    cmd.disarm();session.close()


@pytest.mark.parametrize('kind',['amend','cancel'])
def test_sell_followups_use_existing_reserved_remaining_quantity(tmp_path,kind):
    cmd,session,http,_,_=rig(tmp_path)
    queue(cmd,'/매도 900001 2 1000')
    add_capture(http);http.responses.append(accepted('0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    data=snapshot(quantity=2,remaining=1)
    data[2]['acnt_ord_cntr_prst_array'][0]['io_tp_nm']='현금매도'
    data[1]['acnt_evlt_remn_indv_tot'][0].update(rmnd_qty='2',trde_able_qty='0')
    refresh(cmd,session,http,data)
    action(cmd,'/정정 13 1 1100' if kind=='amend' else '/취소 13')
    add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert mutations(http)[-1]['headers']['api-id']==('kt10002' if kind=='amend' else 'kt10003')
    cmd.disarm();session.close()


def test_cancel_ack_and_absent_remaining_never_claim_final_cancellation(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    action(cmd,'/취소 13')
    add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    cmd.process(session)
    refresh(cmd,session,http,snapshot(remaining=0))
    text=handle(cmd,'/주문상태')
    assert '취소 접수 응답 확인' in text and '남은 주문 0주' in text and '최종 취소 미확인' in text
    assert '취소 완료' not in text
    assert '최종 상태 대조' in handle(cmd,'/매수 900001 1 1000',uid=30)
    assert '최종 상태 대조' in handle(cmd,'/정정 13 1 1000',uid=31)
    cmd.disarm();session.close()


def test_amend_then_cancel_tracks_latest_number_and_all_prior_fills(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    action(cmd,'/정정 13 2 1100')
    add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    cmd.process(session)
    later=snapshot(order='14',original='13',quantity=2,remaining=1)
    parent_fill=later[2]['acnt_ord_cntr_prst_array'][0]
    later[2]['acnt_ord_cntr_prst_array'].append(dict(parent_fill,ord_no='0000014',orig_ord_no='0000013',
                                                  cntr_no='0000003',cntr_qty='1',cntr_uv='1100'))
    refresh(cmd,session,http,later)
    preview,_=action(cmd,'/취소 14',uid=20)
    assert '지금까지 체결 2주 · 남은 1주' in preview
    add_capture(http,later);http.responses.append(accepted('0000015','0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED'
    assert mutations(http)[-1]['json']['orig_ord_no']=='0000014'
    assert mutations(http)[-1]['json']['cncl_qty']=='1'
    cmd.disarm();session.close()


@pytest.mark.parametrize('kind',['amend','cancel'])
def test_uncertain_followup_never_retries_or_releases_unknown(tmp_path,kind):
    cmd,session,http,_,_,data=ready(tmp_path)
    action(cmd,'/정정 13 2 1100' if kind=='amend' else '/취소 13')
    add_capture(http,data);http.responses.append(TimeoutError('PRIVATE'))
    assert cmd.process(session)=='UNKNOWN'
    assert cmd.process(session) is None and len(mutations(http))==2
    assert '미확인' in handle(cmd,'/취소 13',uid=25)
    assert 'PRIVATE' not in handle(cmd,'/주문상태')
    cmd.disarm();session.close()


def test_cancel_remains_possible_after_new_and_amend_budget_is_used(tmp_path):
    cmd,session,http,_,_,data=ready(tmp_path)
    # Two rejected amendments use the remaining trial count, preserving a cancel path.
    for uid in (10,20):
        action(cmd,'/정정 13 2 1100',uid=uid)
        add_capture(http,data);http.responses.append(Response(dict(return_code=1)))
        assert cmd.process(session)=='REJECTED'
    assert '3회·20만원' in handle(cmd,'/정정 13 2 1100',uid=30)
    action(cmd,'/취소 13',uid=31)
    add_capture(http,data);http.responses.append(accepted('0000014','0000013'))
    assert cmd.process(session)=='ACKNOWLEDGED' and len(mutations(http))==4
    cmd.disarm();session.close()


def test_telegram_router_authenticates_followups_and_replays_only_reply(sandbox,tmp_path):
    service,broker,_,_=sandbox
    cmd,session,http,_,_,data=ready(tmp_path)
    service.orders_enabled=False
    service.watch_orders=cmd.handle
    with connect(cmd.path) as db: db.execute('UPDATE intents SET scope=?',(service.scope,))
    bad=update(sandbox,4,'/취소 13');bad['message']['chat']['type']='group'
    assert service.handle(bad) is None
    message=update(sandbox,5,'/취소 13')
    preview=service.handle(message)
    assert service.handle(message)==preview
    token=re.search('/모의확인 ([A-Za-z0-9_-]+)',preview)[1]
    confirmation=update(sandbox,6,'/모의확인 '+token)
    assert service.handle(confirmation)==service.handle(confirmation)
    assert len(mutations(http))==1 and broker.calls==[]
    cmd.disarm();session.close()
