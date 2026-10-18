"""Paged private reports remain mode-scoped and do not execute commands."""
from copy import deepcopy
import pytest
from app.config import DEFAULTS
from test_recommendation_reports import store
from test_recommendations import candidates,save
from test_telegram_orders import sandbox,update


def history(tmp_path):
    db,_=store(tmp_path)
    for _ in range(4): save(db,candidates(),deepcopy(DEFAULTS))
    return db


def test_pages_cover_history_without_overlap_and_modes_do_not_change_page_count(tmp_path):
    db=history(tmp_path)
    assert db.recommendation_page_count('DEMO')==3
    assert db.recommendation_page_count('MOCK')==1
    pages=[db.recommendation_summary('DEMO',page=n) for n in (1,2,3)]
    assert all(f'{n}/3' in page for n,page in enumerate(pages,1))
    assert pages[0].count('위 시험')==5 and pages[-1].count('위 시험')==5
    assert '검색 #1' not in pages[0] and '검색 #1' in pages[-1]
    assert '/추천기록 2' in pages[0] and '/추천기록 2' in pages[-1]
    assert '3페이지까지' in db.recommendation_summary('DEMO',page=4)
    assert '저장된 추천이 없어요' in db.recommendation_summary('MOCK')
    assert '/추천성적 2' in db.recommendation_summary('DEMO',performance=True)
    assert all(len(page)<4096 for page in pages)


@pytest.mark.parametrize('page',[0,-1,True,1000000,'2'])
def test_invalid_page_is_not_converted_to_database_offset(tmp_path,page):
    db=history(tmp_path)
    with pytest.raises(ValueError): db.recommendation_summary('DEMO',page=page)


def test_paged_telegram_commands_auth_replay_invalid_syntax_and_stop(tmp_path,sandbox):
    db=history(tmp_path)
    service,broker,_,_=sandbox
    calls=[]
    def report(performance,page=1):
        calls.append((performance,page))
        return db.recommendation_summary('DEMO',performance=performance,page=page)
    service.recommendation_report=report
    bad=update(sandbox,1,'/추천기록 2');bad['message']['from']['id']=99
    assert service.handle(bad) is None
    event=update(sandbox,2,'/추천기록 2')
    reply=service.handle(event)
    assert '2/3' in reply and service.handle(event)==reply and calls==[(False,2)]
    service.handle(update(sandbox,3,'/중지'))
    assert '3/3' in service.handle(update(sandbox,4,'/추천성적 3'))
    before=len(calls)
    for uid,text in enumerate(('/추천기록 0','/추천기록 -1','/추천성적 x','/추천기록 1000000'),5):
        assert '사용법' in service.handle(update(sandbox,uid,text))
    assert len(calls)==before and broker.calls==[]
