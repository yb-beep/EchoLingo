import requests,uuid,json
from pathlib import Path
base='http://127.0.0.1:5193/api/cet'
s=requests.Session();s.trust_env=False
pfx='ind-'+uuid.uuid4().hex[:8]
def call(method,path,body=None):
 r=s.request(method,base+path,json=body);return r
mat=call('GET','/materials/cet6-202512-conv2').json()['material'];ver=mat['materialVersion'];mid=mat['materialId']
r=call('POST','/attempts',{'attemptId':pfx,'materialId':mid,'materialVersion':ver,'listeningSessionId':pfx,'answers':[]});assert r.status_code==201,r.text
mark=call('POST','/marks',{'materialId':mid,'materialVersion':ver,'rawSeconds':6.49,'listeningSessionId':pfx}).json()
mk=mark.get('markId',mark.get('id'));sid=mark['suggestedSentenceId']
r=call('PATCH','/marks/'+mk,{'confirmedSentenceId':sid,'materialVersion':ver});assert r.status_code==200,r.text
pr=call('POST','/practices',{'markId':mk}).json();pid=pr['practiceId'];old=pr['currentStepId']
def nxt(step,fb,**extra):return call('POST','/practices/'+pid+'/next',{'stepId':step,'materialVersion':ver,'feedback':fb,**extra})
r=nxt(old,'sound',requestId=pfx+'sound');assert r.status_code==200,r.text
sound=r.json();r=nxt(old,'meaning');after=call('GET','/practices/'+pid).json()
result={'practiceId':pid,'stale':{'status':r.status_code,'latestFeedback':after['latestFeedback'],'history':len(after['feedbackHistory']),'revisionBefore':sound['revision'],'revisionAfter':after['revision']}}
r=nxt(after['currentStepId'],'meaning',requestId=pfx+'meaning');assert r.status_code==200,r.text
meaning=r.json();result['meaning']={'status':r.status_code,'latestFeedback':meaning['latestFeedback'],'modelInputLog':Path('/tmp/cet-9a487ec-audit/model-input.jsonl').read_text()}
r=nxt(meaning['currentStepId'],'heard',requestId=pfx+'heard');result['transfer']={'status':r.status_code,'body':r.json()}
Path('/tmp/cet-9a487ec-audit/api-probe.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False,indent=2)[:5000])
