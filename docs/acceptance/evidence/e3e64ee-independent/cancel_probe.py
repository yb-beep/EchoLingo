import requests,json,time,uuid,sqlite3
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
root=Path('/tmp/cet-e3e64ee-audit');root.joinpath('mode').write_text('slow')
base='http://127.0.0.1:5194/api/cet';pid=json.loads((root/'api-probe.json').read_text())['practiceId']
def call(method,path,body=None):
 s=requests.Session();s.trust_env=False;r=s.request(method,base+path,json=body,timeout=20);return {'code':r.status_code,'body':r.json()}
def state():return call('GET','/practices/'+pid)['body']
def poll(rid):
 for _ in range(60):
  r=call('GET','/practices/'+pid+'/ai-status');ai=r['body'].get('aiRequest')
  if ai and ai['requestId']==rid and ai['status']=='pending':return r
  time.sleep(.05)
 raise RuntimeError('pending not observed')
p=state();old=p['currentStepId'];ids=['cancel-'+uuid.uuid4().hex[:8]+x for x in ['A','B']];out={'practiceId':pid,'requestIds':ids,'initialStep':old}
def next_request(rid):return call('POST','/practices/'+pid+'/next',{'feedback':'meaning','stepId':old,'materialVersion':p['materialVersion'],'requestId':rid})
with ThreadPoolExecutor(2) as pool:
 a=pool.submit(next_request,ids[0]);out['pendingA']=poll(ids[0]);out['cancelA']=call('POST','/practices/'+pid+'/ai-cancel',{'requestId':ids[0]});out['statusAfterCancel']=call('GET','/practices/'+pid+'/ai-status')
 b=pool.submit(next_request,ids[1]);out['pendingB']=poll(ids[1]);out['retryCancelA']=call('POST','/practices/'+pid+'/ai-cancel',{'requestId':ids[0]})
 with sqlite3.connect(root/'vocab.db') as c:out['dbImmediately']=c.execute('SELECT id,status,error_code FROM cet_ai_requests WHERE id IN (?,?)',ids).fetchall()
 out['lateA']=a.result();out['lateB']=b.result()
out['finalStatus']=call('GET','/practices/'+pid+'/ai-status');out['finalStep']=state()['currentStepId']
with sqlite3.connect(root/'vocab.db') as c:out['dbAfterLate']=c.execute('SELECT id,status,error_code FROM cet_ai_requests WHERE id IN (?,?)',ids).fetchall()
(root/'cancel-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));root.joinpath('mode').write_text('success');print(json.dumps(out,ensure_ascii=False,indent=2))
