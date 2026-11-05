import os,sys,sqlite3,json,threading,concurrent.futures
from pathlib import Path
r=Path('/tmp/cet-944b1c0-audit');s=sqlite3.connect(r/'vocab.db');d=sqlite3.connect(r/'transaction.db');s.backup(d);s.close();d.close()
os.environ['ELT_USER_DB']=str(r/'transaction.db');os.environ['ELT_CONFIG_DIR']=str(r/'config');sys.path.insert(0,str(Path.cwd()/'backend'))
import db
db.DB_PATH=r/'transaction.db'
from webapp.services import cet_service as c
from fastapi import HTTPException
pid=json.loads((r/'api-probe.json').read_text())['practiceId']
p=c._practice_payload(pid)
def brief(p):return {'latestFeedback':p['latestFeedback'],'revision':p['revision'],'history':len(p['feedbackHistory']),'steps':len(p['steps']),'currentStepId':p['currentStepId']}
original=c._local_practice_action_conn
def fail_after_update(*a,**kw):
 original(*a,**kw)
 raise HTTPException(409,{'code':'AUDIT_INJECTED_FAILURE'})
c._local_practice_action_conn=fail_after_update
try:c.next_practice_step(pid,{'feedback':'sound','requestId':'atomic-rollback','stepId':p['currentStepId'],'materialVersion':p['materialVersion']})
except HTTPException as e: rollback_status=e.status_code
after=c._practice_payload(pid);out={'rollback':{'status':rollback_status,'before':brief(p),'after':brief(after)}}
c._local_practice_action_conn=original
# 同步两次初始校验，使两请求都读取同一旧步骤后才尝试写入。没有改校验或写入逻辑。
barrier=threading.Barrier(2);persist=c._persist_feedback_conn
def simultaneous(*a,**kw):
 barrier.wait(timeout=5)
 return persist(*a,**kw)
c._persist_feedback_conn=simultaneous
body={'feedback':'sound','stepId':after['currentStepId'],'materialVersion':after['materialVersion']}
def send(i):
 try:
  v=c.next_practice_step(pid,{**body,'requestId':f'concurrent-{i}'})
  return {'status':200,'stepId':v['currentStepId'],'revision':v['revision']}
 except HTTPException as e:return {'status':e.status_code,'detail':e.detail}
 except Exception as e:return {'error':type(e).__name__,'message':str(e)}
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex: results=list(ex.map(send,[1,2]))
out['concurrent']={'before':brief(after),'requests':results,'after':brief(c._practice_payload(pid))}
(r/'transaction-checks.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
