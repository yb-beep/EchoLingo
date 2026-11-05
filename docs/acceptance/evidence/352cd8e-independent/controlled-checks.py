import os,sys,sqlite3,json,uuid
from pathlib import Path
r=Path('/tmp/cet-352cd8e-audit')
s=sqlite3.connect(r/'vocab.db');d=sqlite3.connect(r/'interleave.db');s.backup(d);s.close();d.close()
os.environ['ELT_USER_DB']=str(r/'interleave.db');os.environ['ELT_CONFIG_DIR']=str(r/'config');sys.path.insert(0,str(Path.cwd()/'backend'))
import db
db.DB_PATH=r/'interleave.db'
from webapp.services import cet_service as c
from fastapi import HTTPException
p=json.loads((r/'api-probe.json').read_text())['practiceId'];pr=c._practice_payload(p)
original=c._next_action
# 在反馈独立 commit 后、动作推进前执行另一合法本地请求，确定性重现并发交错。
def interleave(conn,practice,requested):
 c.local_practice_action(p,{'action':'replay','actionId':'ind-competitor','stepId':practice['current_step_id'],'materialVersion':practice['material_version']})
 return original(conn,practice,requested)
c._next_action=interleave
before=c._practice_payload(p)
try:
 c.next_practice_step(p,{'requestId':'ind-interleaved','feedback':'sound','stepId':pr['currentStepId'],'materialVersion':pr['materialVersion']})
 result={'status':200}
except HTTPException as e: result={'status':e.status_code,'detail':e.detail}
after=c._practice_payload(p)
result.update(beforeFeedback=before['latestFeedback'],afterFeedback=after['latestFeedback'],beforeHistory=len(before['feedbackHistory']),afterHistory=len(after['feedbackHistory']))
(r/'interleave.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
