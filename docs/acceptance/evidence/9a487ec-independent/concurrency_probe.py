import os,sys,sqlite3,json,uuid,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
root=Path('/tmp/cet-9a487ec-audit');os.environ['ELT_CONFIG_DIR']=str(root/'config');os.environ['ELT_USER_DB']=str(root/'race.db')
sys.path.insert(0,str(Path.cwd()/'backend'))
import db
from webapp.services import cet_service as svc
out={};original=svc.list_questions
for kind in ['clean','legacy_duplicates']:
 path=root/(kind+'.db');source=sqlite3.connect(root/'vocab.db');dest=sqlite3.connect(path);source.backup(dest);source.close()
 if kind=='legacy_duplicates':
  dest.execute('DROP INDEX IF EXISTS cet_attempts_session_uniq')
  for id in ['legacy-duplicate-1','legacy-duplicate-2']:
   dest.execute("INSERT INTO cet_attempts(id,material_id,material_version,listening_session_id,score,graded_count,unknown_count,created_at) VALUES (?, 'historical-material', 'old', 'historical-session',0,0,0,'2026-11-01')",(id,))
  dest.commit()
 dest.close();os.environ['ELT_USER_DB']=str(path);db.DB_PATH=path;svc.init_cet_schema(path);mat=svc.get_material('cet6-202512-conv2');barrier=threading.Barrier(6)
 def synced_questions(mid):
  result=original(mid);barrier.wait(timeout=10);return result
 svc.list_questions=synced_questions
 session='race-'+uuid.uuid4().hex[:8]
 def submit(i):return svc.submit_attempt({'materialId':mat['materialId'],'materialVersion':mat['materialVersion'],'listeningSessionId':session,'answers':[]})['attemptId']
 with ThreadPoolExecutor(6) as pool: ids=list(pool.map(submit,range(6)))
 svc.list_questions=original
 with sqlite3.connect(path) as c:count=c.execute('SELECT COUNT(*) FROM cet_attempts WHERE listening_session_id=?',(session,)).fetchone()[0]
 out[kind]={'state':svc.attempt_idempotency_state(),'returnedIds':ids,'databaseCount':count}
(root/'concurrency-results.json').write_text(json.dumps(out,indent=2));print(json.dumps(out,indent=2))
