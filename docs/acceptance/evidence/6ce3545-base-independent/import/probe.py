import os,sys,json,io,wave,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
root=Path('/tmp/cet-base-6ce-import')
os.environ.update(ELT_USER_DB=str(root/'vocab.db'),ELT_CONFIG_DIR=str(root/'config'),AI_API_KEY='',ELT_CET_MODE='1')
sys.path.insert(0,str(Path.cwd()/'backend'))
import db
db.DB_PATH=root/'vocab.db'
from webapp.storage import user_assets,lessons
from webapp.services import v2_lessons,cet_import,cet_materials
user_assets.GLOBAL_OUTPUT_DIR=root/'output';lessons.OUTPUT_DIR=root/'output';v2_lessons.OUTPUT_DIR=root/'output';(root/'output').mkdir(exist_ok=True)
from fastapi_server import create_app
from fastapi.testclient import TestClient
client=TestClient(create_app(),raise_server_exceptions=False)
b=io.BytesIO()
with wave.open(b,'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(8000);w.writeframes(b'\x00\x00'*80000)
audio=b.getvalue();out={}
def imp(title,sub=None):
 files={'audio':('audit.wav',audio,'audio/wav')}
 if sub is not None:files['subtitle']=('audit.json',json.dumps(sub).encode(),'application/json')
 r=client.post('/api/cet/base/import',data={'title':title,'level':'cet4','category':'练习'},files=files)
 try: body=r.json()
 except: body=r.text[:250]
 return {'status':r.status_code,'body':body}
r=imp('独立验收有效材料',[{'start':0,'end':2,'text':'Hello world.'}]);out['valid']=r
id=r['body']['material']['lessonId']
out['patchLevelOnly']=client.patch(f'/api/cet/base/materials/{id}',json={'level':'cet6'}).json()
client.patch(f'/api/cet/base/materials/{id}',json={'level':'cet4','category':'练习'})
out['patchCategoryOnly']=client.patch(f'/api/cet/base/materials/{id}',json={'category':'样例'}).json()
out['archive']=client.patch(f'/api/cet/base/materials/{id}',json={'archived':True}).json();out['visibleArchivedCount']=client.get('/api/cet/base/materials',params={'q':'独立验收有效材料'}).json()['count'];out['restore']=client.patch(f'/api/cet/base/materials/{id}',json={'archived':False}).json()
out['mixedInvalidJson']=imp('混合坏条目',[{'start':0,'end':2,'text':'Valid one.'},'BROKEN',None,{'start':2,'end':3,'text':'Valid two.'}])
out['nanJson']=imp('NaN时间轴',[{'start':'NaN','end':2,'text':'Broken timeline.'}])
r=client.post('/api/cet/base/import',data={'title':'坏音频','level':'cet4','category':'练习'},files={'audio':('bad.mp3',b'not audio at all','audio/mpeg')})
out['invalidAudio']={'status':r.status_code,'body':r.text[:300]}
# Deterministic overlap after duplicate lookup, without changing lookup outcome.
orig=cet_import.find_duplicate;barrier=threading.Barrier(2)
def synced(**kw):
 value=orig(**kw);barrier.wait(timeout=10);return value
cet_import.find_duplicate=synced
with ThreadPoolExecutor(2) as p:out['concurrentDuplicate']=list(p.map(lambda _:imp('并发同素材'),range(2)))
cet_import.find_duplicate=orig
out['concurrentCount']=client.get('/api/cet/base/materials',params={'q':'并发同素材'}).json()['count']
(root/'results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
print(json.dumps({k: ({'status':v.get('status'),'level':v.get('level'),'category':v.get('category')} if isinstance(v,dict) else v) for k,v in out.items() if k!='concurrentDuplicate'},ensure_ascii=False,indent=2))
