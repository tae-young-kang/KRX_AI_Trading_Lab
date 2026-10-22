"""Private confirmed review queue; only the GUI owner applies saved settings."""
import hashlib
import json
import re
import secrets
from time import time
import uuid
from app.order_engine import connect
from app.proposal_application import snapshot,encoded,preview,apply
from app.telegram_conditions import describe,KEYS
from app.exit_policy_commands import describe as exit_description,KEYS as EXIT_KEYS
from app.modes import current_mode


class ProposalReviewCommands:
    def __init__(self,path,database,config,*,clock=time):
        self.path,self.database,self.clock=path,database,clock
        self.owner=uuid.uuid4().hex;self.mode=current_mode(snapshot(config)).key
        with connect(path) as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS proposal_review_current(owner TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE IF NOT EXISTS proposal_review_requests(token TEXT PRIMARY KEY,owner TEXT,mode TEXT,scope TEXT,session TEXT,
                    uid INTEGER,proposal_id INTEGER,action TEXT,base_json TEXT,target_json TEXT,expires REAL,state TEXT,message TEXT);
                CREATE TABLE IF NOT EXISTS proposal_review_replies(owner TEXT,scope TEXT,session TEXT,uid INTEGER,reply TEXT,PRIMARY KEY(owner,scope,session,uid));''')
            db.execute("UPDATE proposal_review_requests SET state='UNKNOWN',message='이전 처리 중 종료됐어요. /개선안에서 저장 결과를 확인해 주세요.' WHERE mode=? AND state='APPLYING'",(self.mode,))
            db.execute("UPDATE proposal_review_requests SET state='EXPIRED' WHERE mode=? AND state IN ('PREVIEW','QUEUED')",(self.mode,))
            db.execute('INSERT INTO proposal_review_current VALUES(?,?)',(self.owner,encoded(snapshot(config))))

    def cancel_session(self,scope,session):
        with connect(self.path) as db:
            db.execute("UPDATE proposal_review_requests SET state='CANCELED',message='수신 중지로 확인 대기를 철회했어요.' WHERE owner=? AND scope=? AND session=? AND state IN ('PREVIEW','QUEUED')",(self.owner,scope,session))

    def handle(self,text,*,scope,session,uid,now):
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            prior=db.execute('SELECT reply FROM proposal_review_replies WHERE owner=? AND scope=? AND session=? AND uid=?',(self.owner,scope,session,uid)).fetchone()
            if prior:return prior[0]
            db.execute("UPDATE proposal_review_requests SET state='EXPIRED' WHERE owner=? AND expires<=? AND state IN ('PREVIEW','QUEUED')",(self.owner,now))
            base=json.loads(db.execute('SELECT payload FROM proposal_review_current WHERE owner=?',(self.owner,)).fetchone()[0])
            try:
                if text=='/개선안처리상태':
                    row=db.execute('SELECT proposal_id,action,state,message FROM proposal_review_requests WHERE scope=? AND mode=? ORDER BY rowid DESC LIMIT 1',(scope,self.mode)).fetchone()
                    labels={'PREVIEW':'확인 대기','QUEUED':'PC 처리 대기','APPLYING':'저장 대조 중','APPLIED':'적용 확인 완료','DECLINED':'거부 완료','FAILED':'처리 실패','UNKNOWN':'결과 미확인','EXPIRED':'기한 만료','CANCELED':'철회됨'}
                    reply=('[개선안 처리 상태]\n'+f'개선안 {row[0]} · {labels.get(row[2],"확인 필요")}\n'+(row[3] or '') if row else '개선안 처리 요청이 없어요.')
                elif text.split(' ',1)[0] in ('/개선안승인','/개선안거부'):
                    match=re.fullmatch(r'/(개선안승인|개선안거부) ([1-9][0-9]{0,8})',text)
                    if not match:raise ValueError('사용법: /개선안승인 번호 또는 /개선안거부 번호')
                    if db.execute("SELECT 1 FROM proposal_review_requests WHERE owner=? AND state IN ('PREVIEW','QUEUED','APPLYING')",(self.owner,)).fetchone():raise ValueError('다른 검토 요청이 있어요. /개선안처리상태로 확인하세요.')
                    action='APPROVE' if match[1]=='개선안승인' else 'REJECT';pid=int(match[2])
                    target=preview(self.database,base,pid,require_base=action=='APPROVE')
                    nonce=secrets.token_urlsafe(12);token=hashlib.sha256(nonce.encode()).hexdigest()
                    db.execute('INSERT INTO proposal_review_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)',
                        (token,self.owner,self.mode,scope,session,uid,pid,action,encoded(base),encoded(target),now+120,'PREVIEW'))
                    reply=f'[개선안 {pid} · '+('승인 검토]\n' if action=='APPROVE' else '거부 검토]\n')
                    if action=='APPROVE':
                        value=dict(mode=self.mode,version=target['strategy_version'],conditions={key:target[key] for key in KEYS},exit_policy={key:target[key] for key in EXIT_KEYS})
                        reply+=describe(value)+'\n'+exit_description(value)+'\n최신 성적·저장 상태는 적용 직전에 다시 확인해요. AI/독립 검증 완료는 아니에요.\n'
                    else:reply+='조건은 바꾸지 않고 이 후보를 거부로 기록해요.\n'
                    reply+=f'2분 안에 /개선안확인 {nonce}\n철회: /개선안철회 {nonce}'
                else:
                    match=re.fullmatch(r'/(개선안확인|개선안철회) ([A-Za-z0-9_-]{16})',text)
                    if not match:raise ValueError('개선안 명령을 확인해 주세요.')
                    token=hashlib.sha256(match[2].encode()).hexdigest()
                    row=db.execute('SELECT * FROM proposal_review_requests WHERE token=? AND owner=? AND scope=? AND session=?',(token,self.owner,scope,session)).fetchone()
                    if not row or row['state']!='PREVIEW' or now>=row['expires']:raise ValueError('확인 코드가 만료됐거나 사용됐어요. 새로 검토해 주세요.')
                    if json.loads(row['base_json'])!=base:raise ValueError('그사이 조건이 바뀌었어요. 다시 검토해 주세요.')
                    state='QUEUED' if match[1]=='개선안확인' else 'CANCELED'
                    db.execute('UPDATE proposal_review_requests SET state=? WHERE token=?',(state,token))
                    reply='확인했어요. PC 처리 후 /개선안처리상태로 결과를 확인하세요. 아직 적용 완료는 아니에요.' if state=='QUEUED' else '개선안 검토 요청을 철회했어요.'
            except ValueError as error:reply=str(error)
            db.execute('INSERT INTO proposal_review_replies VALUES(?,?,?,?,?)',(self.owner,scope,session,uid,reply))
            return reply

    def sync(self,config,save,read_saved,*,now,busy=False,active=lambda:True):
        base=snapshot(config);enabled=active() and current_mode(base).key==self.mode
        with connect(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE proposal_review_current SET payload=? WHERE owner=?',(encoded(base),self.owner))
            db.execute("UPDATE proposal_review_requests SET state='EXPIRED' WHERE owner=? AND expires<=? AND state IN ('PREVIEW','QUEUED')",(self.owner,now))
            if not enabled:db.execute("UPDATE proposal_review_requests SET state='CANCELED' WHERE owner=? AND state IN ('PREVIEW','QUEUED')",(self.owner,))
            if busy or not enabled:return False
            row=db.execute("SELECT * FROM proposal_review_requests WHERE owner=? AND state='QUEUED' ORDER BY rowid LIMIT 1",(self.owner,)).fetchone()
            if not row:return False
            db.execute("UPDATE proposal_review_requests SET state='APPLYING' WHERE token=?",(row['token'],))
        changed=False
        try:
            if json.loads(row['base_json'])!=base:raise ValueError('그사이 기본 조건이 바뀌었어요.')
            def authorize():
                if self.clock()>=row['expires'] or not active():raise ValueError('기한이 지났거나 수신이 중지됐어요.')
            authorize()
            target=json.loads(row['target_json'])
            if row['action']=='APPROVE':
                apply(self.database,config,row['proposal_id'],save,read_saved,expected_target=target,authorize=authorize)
                state,message,changed='APPLIED','저장한 조건과 승인 기록을 대조했어요. 새 일반 추천/가상 기본값에 적용했으며 주문은 하지 않았어요.',True
            else:
                if preview(self.database,base,row['proposal_id'],require_base=False)!=target:raise ValueError('개선안이 바뀌었어요.')
                self.database.review(row['proposal_id'],False,authorize=authorize)
                state,message='DECLINED','후보를 거부로 기록했어요. 조건은 바꾸지 않았어요.'
        except Exception:
            with self.database.connect() as db:source=db.execute('SELECT state FROM proposals WHERE id=?',(row['proposal_id'],)).fetchone()
            state='UNKNOWN' if source and source[0] in ('APPLYING','UNKNOWN','APPROVED') else 'FAILED'
            message='처리를 완료하지 못했어요. 생성 기준·최신 성적·저장 결과를 확인하고 재적용 전 /개선안을 확인해 주세요.'
            if source and source[0]=='PENDING' and self.clock()>=row['expires']:
                state,message='EXPIRED','기한이 지나 설정을 저장하지 않았어요. 새로 검토해 주세요.'
            elif source and source[0]=='PENDING' and not active():
                state,message='CANCELED','수신 중지로 설정을 저장하지 않았어요.'
            try:
                if source and source[0]=='APPROVED' and snapshot(read_saved())==json.loads(row['target_json']):
                    state,message,changed='APPLIED','해당 설정이 이미 저장·승인된 것을 확인했어요. 다시 적용하지 않았어요.',True
                elif source and source[0]=='REJECTED':state,message='DECLINED','해당 후보의 거부 기록을 확인했어요.'
            except Exception:pass
        with connect(self.path) as db:
            db.execute('UPDATE proposal_review_requests SET state=?,message=? WHERE token=?',(state,message,row['token']))
            db.execute('UPDATE proposal_review_current SET payload=? WHERE owner=?',(encoded(snapshot(config)),self.owner))
        return changed
