import sys,json,sqlite3,threading,concurrent.futures,uuid
from pathlib import Path
import requests
root=Path('/tmp/cet-b5c75a5-audit');base='http://127.0.0.1:5192/api/cet'
def call(method,path,body=None):
 s=requests.Session();s.trust_env=False
 return s.request(method,base+path,json=body,timeout=15)
pid=json.loads((root/'api-probe.json').read_text())['practiceId'];out={'same_step':[]}
for round in range(5):
 before=call('GET','/practices/'+pid).json();barrier=threading.Barrier(2)
 def send(i):
  barrier.wait();r=call('POST',f'/practices/{pid}/next',{'feedback':'sound','requestId':f'pair-{round}-{i}','stepId':before['currentStepId'],'materialVersion':before['materialVersion']});return r.status_code
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:codes=list(ex.map(send,[1,2]))
 after=call('GET','/practices/'+pid).json()
 out['same_step'].append({'statuses':codes,'revisionDelta':after['revision']-before['revision'],'historyDelta':len(after['feedbackHistory'])-len(before['feedbackHistory'])})
mid=after['materialId'];qs=call('GET',f'/materials/{mid}/questions').json()['items'];sid='audit-concurrent-'+uuid.uuid4().hex[:8];barrier=threading.Barrier(6)
def submit(_):
 barrier.wait();r=call('POST','/attempts',{'materialId':mid,'materialVersion':after['materialVersion'],'listeningSessionId':sid,'answers':[{'questionId':q['questionId'],'choice':0} for q in qs]});return {'status':r.status_code,'attemptId':r.json().get('attemptId')}
with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:out['attempts']=list(ex.map(submit,range(6)))
sys.path.insert(0,str(Path.cwd()/'backend'))
from webapp.services import cet_service as c
out['migration']=[]
for session in ['','same-session']:
 con=sqlite3.connect(':memory:');con.execute('PRAGMA foreign_keys=ON')
 con.executescript('CREATE TABLE cet_attempts(id TEXT PRIMARY KEY,material_id TEXT,material_version TEXT,listening_session_id TEXT); CREATE TABLE cet_attempt_answers(attempt_id TEXT REFERENCES cet_attempts(id) ON DELETE CASCADE,answer TEXT);')
 con.executemany('INSERT INTO cet_attempts VALUES(?,?,?,?)',[('historical-a','m','v1',session),('historical-b','m','v2',session)])
 con.executemany('INSERT INTO cet_attempt_answers VALUES(?,?)',[('historical-a','A'),('historical-b','B')]);con.commit()
 c._ensure_attempt_idempotency(con)
 out['migration'].append({'session':session,'beforeAttempts':2,'beforeAnswers':2,'afterAttempts':con.execute('select * from cet_attempts').fetchall(),'afterAnswers':con.execute('select * from cet_attempt_answers').fetchall(),'indexCreated':bool(con.execute("select 1 from sqlite_master where name='cet_attempts_session_uniq'").fetchone())});con.close()
(root/'concurrency-migration.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
