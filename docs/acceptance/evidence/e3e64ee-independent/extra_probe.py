import os,sys,sqlite3,json,uuid,threading,importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
root=Path('/tmp/cet-e3e64ee-audit');os.environ['ELT_CONFIG_DIR']=str(root/'config');os.environ['ELT_USER_DB']=str(root/'legacy_duplicates.db');os.environ['AI_API_KEY']=''
sys.path.insert(0,str(Path.cwd()/'backend'))
import db
from webapp.services import cet_service as svc,cet_ai
svc.init_cet_schema(root/'legacy_duplicates.db');mat=svc.get_material('cet6-202512-conv2');out={}
# Existing supported client IDs with a non-empty session must have the same uniqueness rule.
original=svc.list_questions;barrier=threading.Barrier(6)
def synced(mid):
 r=original(mid);barrier.wait(timeout=10);return r
svc.list_questions=synced;session='explicit-'+uuid.uuid4().hex[:8]
def submit(i):return svc.submit_attempt({'attemptId':session+'-'+str(i),'materialId':mat['materialId'],'materialVersion':mat['materialVersion'],'listeningSessionId':session,'answers':[]})['attemptId']
with ThreadPoolExecutor(6) as pool:ids=list(pool.map(submit,range(6)))
svc.list_questions=original
out['explicitIdsSameSession']={'returnedIds':ids,'uniqueCount':len(set(ids)),'state':svc.attempt_idempotency_state()}
# Existing ambiguous historical key is selected without a conflict or migration decision.
result=svc.submit_attempt({'materialId':mat['materialId'],'materialVersion':mat['materialVersion'],'listeningSessionId':session,'answers':[]})
out['ambiguousExistingKey']={'returnedId':result['attemptId'],'duplicates':len(set(ids))}
# Test executor's success fixture using production validation, not only its raw function.
spec=importlib.util.spec_from_file_location('controlled',Path.cwd()/'scripts/cet/run_contract_controlled.py');mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);mod.install_controlled_call()
results=[]
for sentence in mat['sentences'] if 'sentences' in mat else svc.list_sentences(mat['materialId']):
 try:
  value,model=cet_ai.generate('explain',sentence,'meaning');results.append({'sentenceId':sentence['sentenceId'],'ok':True})
 except cet_ai.CETAIError as e:results.append({'sentenceId':sentence['sentenceId'],'ok':False,'code':e.code,'message':e.message})
out['executorControlledExplain']=results
(root/'extra-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
