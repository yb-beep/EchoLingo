from pathlib import Path
import json,sys,uuid
src=Path('/tmp/cet-base-6ce-import/probe.py').read_text();exec(src.split("r=imp('独立验收有效材料'")[0])
results={};case='复验-'+uuid.uuid4().hex[:6]
r=imp(case,[{'start':0,'end':2,'text':'Valid.'}]);assert r['status']==200
id=r['body']['material']['lessonId'];db.set_v2_lesson_tags(id,['cet4','练习','自定义保留'])
x=client.patch(f'/api/cet/base/materials/{id}',json={'level':'cet6'}).json();assert x['level']=='cet6' and x['category']=='练习' and '自定义保留' in x['tags']
results['levelOnly']=x['tags']
x=client.patch(f'/api/cet/base/materials/{id}',json={'category':'样例'}).json();assert x['level']=='cet6' and x['category']=='样例' and '自定义保留' in x['tags'];results['categoryOnly']=x['tags']
x=client.patch(f'/api/cet/base/materials/{id}',json={'level':'unspecified'}).json();assert x['category']=='样例' and x['level']=='unspecified';results['clearLevel']=x['tags']
x=client.patch(f'/api/cet/base/materials/{id}',json={'category':''}).json();assert x['category'] is None and '自定义保留' in x['tags'];results['clearCategory']=x['tags']
def counts():
 with db._db() as conn:return {t:conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ['v2_lessons','v2_subtitle_segments','v2_media_uploads']}
before=counts();bad=[{'start':0,'end':2,'text':'Valid one.'},'BROKEN',None,{'start':2,'end':3,'text':'Valid two.'}]
r=imp(case+'坏JSON',bad);assert r['status']==422;assert [x['index'] for x in r['body']['detail']['errors']]==[2,3];assert counts()==before;results['mixedNoWrites']={'before':before,'after':counts(),'errors':r['body']['detail']['errors']}
r=client.post('/api/cet/base/import',data={'title':case+'坏音频','level':'cet4'},files={'audio':('bad.mp3',b'not audio at all','audio/mpeg')});assert r.status_code==422;assert r.json()['detail']['errors'][0]['reason']=='invalid_audio';assert counts()==before;results['badAudioNoWrites']={'status':r.status_code,'body':r.json(),'counts':counts()}
orig=cet_import.find_duplicate;barrier=threading.Barrier(6)
def synced(**kw):
 value=orig(**kw);barrier.wait(timeout=10);return value
cet_import.find_duplicate=synced
with ThreadPoolExecutor(6) as p:out=list(p.map(lambda _:imp(case+'并发'),range(6)))
cet_import.find_duplicate=orig
assert all(r['status']==200 for r in out);ids={r['body']['material']['lessonId'] for r in out};flags=[r['body']['duplicate'] for r in out];assert len(ids)==1 and flags.count(False)==1 and flags.count(True)==5
results['concurrentSix']={'lessonIds':list(ids),'duplicateFlags':flags,'count':client.get('/api/cet/base/materials',params={'q':case+'并发'}).json()['count']}
r=client.post('/api/cet/base/import',data={'title':case+'并发','level':'cet4','allow_duplicate':'true'},files={'audio':('audit.wav',audio,'audio/wav')});assert r.status_code==200 and r.json()['duplicate']==False and r.json()['material']['lessonId'] not in ids;results['explicitDuplicate']={'lessonId':r.json()['material']['lessonId'],'count':client.get('/api/cet/base/materials',params={'q':case+'并发'}).json()['count']}
Path('/tmp/cet-base-6ce-import/verification.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print('All targeted assertions passed')
