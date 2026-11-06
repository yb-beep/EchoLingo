import os,sys,time
from pathlib import Path
root=Path('/tmp/cet-9a487ec-audit')
os.environ['ELT_USER_DB']=str(root/'vocab.db')
os.environ['ELT_CONFIG_DIR']=str(root/'config')
os.environ['AI_API_KEY']=''
sys.path.insert(0,str(Path.cwd()/'backend'))
import db
db.DB_PATH=root/'vocab.db'
from webapp.services import cet_ai
original=cet_ai._call
def controlled(action,sentence,feedback,candidate):
    mode=(root/'mode').read_text().strip()
    import json
    with (root/'model-input.jsonl').open('a') as log: log.write(json.dumps({'action':action,'feedback':feedback},ensure_ascii=False)+'\n')
    if mode=='off':return original(action,sentence,feedback,candidate)
    if mode=='slow':time.sleep(6)
    if action=='explain':return {'explanation':'[验收模拟正文] 这是一条受控解释，用于验证显示与刷新恢复，不代表真实模型。','keyPhrases':[]},'audit-fixture'
    if action=='transfer':return {'question':'[验收模拟题] 选择正确含义','options':['选项甲','选项乙'],'correctAnswer':'A','explanation':'验收模拟依据'},'audit-fixture'
    return original(action,sentence,feedback,candidate)
cet_ai._call=controlled
from fastapi_server import create_app
import uvicorn
app=create_app()
@app.middleware('http')
async def audit_transport(request,call_next):
    import asyncio,json
    is_next=request.url.path.endswith(('/next','/finish'))
    body=await request.json() if is_next else {}
    response=await call_next(request)
    if is_next:
        with (root/'transport.jsonl').open('a') as log:
            log.write(json.dumps({'path':request.url.path,'feedback':body.get('feedback'),'stepId':body.get('stepId'),'status':response.status_code})+'\n')
        if (root/'mode').read_text().strip()=='hold-first' and response.status_code==200 and request.url.path.endswith('/next') and not (root/'held').exists():
            (root/'held').write_text('yes')
            await asyncio.sleep(20)
    return response
uvicorn.run(app,host='127.0.0.1',port=5193,lifespan='off',log_level='warning')
