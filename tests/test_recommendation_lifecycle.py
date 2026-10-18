"""Complete local recommendation lifecycle; no Telegram or brokerage network."""
from app.recommendation_import import import_package
from app.storage import Database
from app.telegram_receiver import receive_once
from test_recommendation_reports import store
from test_recommendation_import import package,write,TODAY
from test_telegram_orders import sandbox,update


class Inbox:
    def __init__(self,events):
        self.events=events
        self.sent=[]

    def updates(self,offset):
        return self.events

    def send(self,chat_id,text):
        self.sent.append(text)


def test_saved_recommendations_import_reports_redelivery_and_restart(tmp_path,sandbox):
    db,_=store(tmp_path)
    original=db.recommendation_history('DEMO')
    result=import_package(db,write(tmp_path,package()),'DEMO',today=TODAY)
    assert result['added']==3
    service,broker,_,_=sandbox
    service.recommendation_report=lambda performance:db.recommendation_summary('DEMO',performance=performance)
    service.recommendation_overview=lambda:db.recommendation_overview('DEMO')
    foreign=update(sandbox,1,'/추천성적');foreign['message']['from']['id']=99
    events=[foreign]+[update(sandbox,uid,text) for uid,text in enumerate(
        ('/추천기록','/추천성적','/추천요약','/중지','/추천성적'),2)]
    inbox=Inbox(events)
    receive_once(service,inbox)
    assert len(inbox.sent)==5 and service.cursor()==7
    assert '900001' in inbox.sent[0] and '+1.00%' in inbox.sent[1]
    assert '자료 부족' in inbox.sent[2] and '실제 수익률·예측이 아니에요' in inbox.sent[-1]
    replay=Inbox([events[2]])
    receive_once(service,replay)
    assert replay.sent==[inbox.sent[1]] and service.cursor()==7
    reopened=Database(db.path)
    assert reopened.recommendation_history('DEMO')==original
    assert reopened.recommendation_summary('DEMO',performance=True)==inbox.sent[-1]
    assert '저장된 추천이 없어요' in reopened.recommendation_summary('REAL_QUOTES')
    assert import_package(reopened,write(tmp_path,package()),'DEMO',today=TODAY)['added']==0
    with reopened.connect() as record:
        assert record.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]==3
        assert record.execute('SELECT COUNT(*) FROM paper_trades').fetchone()[0]==0
    assert broker.calls==[]


def test_reply_failure_and_replay_do_not_trigger_price_import_or_order(tmp_path,sandbox):
    db,_=store(tmp_path)
    service,broker,_,_=sandbox
    service.recommendation_report=lambda performance:db.recommendation_summary('DEMO',performance=performance)
    event=update(sandbox,1,'/추천성적')
    class FailedReply(Inbox):
        def send(self,chat_id,text):
            raise RuntimeError('가짜 전송 실패')
    try:
        receive_once(service,FailedReply([event]))
    except RuntimeError:
        pass
    else:
        raise AssertionError('Expected fake reply failure')
    assert service.cursor()==2
    retry=Inbox([event]);receive_once(service,retry)
    assert '평가 자료 없음' in retry.sent[0]
    with db.connect() as record:
        assert record.execute('SELECT COUNT(*) FROM recommendation_reviews').fetchone()[0]==0
    assert broker.calls==[]
