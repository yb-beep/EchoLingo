import os,sys,sqlite3,json,uuid
from pathlib import Path
root=Path('/tmp/cet-9a487ec-audit')
os.environ['ELT_CONFIG_DIR']=str(root/'config');os.environ['ELT_USER_DB']=str(root/'migration.db')
sys.path.insert(0,str(Path.cwd()/'backend'))
import db
from webapp.services import cet_service as svc
out={}
for case in ['duplicates','empty_sessions','old_full_index']:
 c=sqlite3.connect(':memory:');c.execute('PRAGMA foreign_keys=ON')
 c.executescript('CREATE TABLE cet_attempts(id TEXT PRIMARY KEY,material_id TEXT,listening_session_id TEXT,material_version TEXT); CREATE TABLE cet_attempt_answers(attempt_id TEXT REFERENCES cet_attempts(id) ON DELETE CASCADE,answer TEXT);')
 if case=='old_full_index': c.execute('CREATE UNIQUE INDEX cet_attempts_session_uniq ON cet_attempts(material_id,listening_session_id)')
 count=1 if case=='old_full_index' else 2
 for i in range(count):
  c.execute('INSERT INTO cet_attempts VALUES (?,?,?,?)',(str(i),'mat','same' if case=='duplicates' else '',f'v{i}'));c.execute('INSERT INTO cet_attempt_answers VALUES (?,?)',(str(i),f'answer{i}'))
 c.commit();svc._ensure_attempt_idempotency(c)
 idx=c.execute("SELECT sql FROM sqlite_master WHERE name='cet_attempts_session_uniq'").fetchone()
 state=svc.attempt_idempotency_state()
 error=None
 if case=='old_full_index':
  try:c.execute("INSERT INTO cet_attempts VALUES ('new','mat','','v3')")
  except sqlite3.IntegrityError as e:error=str(e)
 out[case]={'state':state,'indexSql':idx[0] if idx else None,'attempts':c.execute('SELECT * FROM cet_attempts').fetchall(),'answers':c.execute('SELECT * FROM cet_attempt_answers').fetchall(),'secondEmptySessionError':error}
# Actual service upgrade path on a copied database; create old index after removing only empty rows in this disposable copy.
src=sqlite3.connect(root/'vocab.db');dest=sqlite3.connect(root/'migration.db');src.backup(dest);src.close()
dest.execute('DROP INDEX IF EXISTS cet_attempts_session_uniq');dest.execute("DELETE FROM cet_attempts WHERE listening_session_id='' OR listening_session_id IS NULL");dest.execute('CREATE UNIQUE INDEX cet_attempts_session_uniq ON cet_attempts(material_id,listening_session_id)');dest.commit();dest.close()
db.DB_PATH=root/'migration.db';svc.init_cet_schema(root/'migration.db')
mat=svc.get_material('cet6-202512-conv2');pfx='upgrade-'+uuid.uuid4().hex[:6];ids=[]
for suffix in ['first','second']:
 result=svc.submit_attempt({'attemptId':pfx+suffix,'materialId':mat['materialId'],'materialVersion':mat['materialVersion'],'answers':[]});ids.append(result['attemptId'])
out['actualUpgrade']={'requestedIds':[pfx+'first',pfx+'second'],'returnedIds':ids,'state':svc.attempt_idempotency_state()}
(root/'migration-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
