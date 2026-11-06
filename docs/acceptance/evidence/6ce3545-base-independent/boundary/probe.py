import os, pathlib, subprocess, socket, sqlite3, json, urllib.request, hashlib, time
base=pathlib.Path('/tmp/cet-base-6ce-boundary'); repo=base/'repo'; out={}
def port():
 s=socket.socket();s.bind(('127.0.0.1',0));p=s.getsockname()[1];s.close();return p
pa,pb,pm=port(),port(),port()
env=os.environ.copy()
for k in list(env):
 if k.startswith(('ELT_','CET_','AI_')):env.pop(k)
env.update(CET_QA_LOG_DIR=str(base/'logs'),CET_MOCK_PORT=str(pm),ELT_CET_MODE='1',AI_API_KEY='',OPENAI_API_KEY='',PYTHONDONTWRITEBYTECODE='1')
def run(name,args,e=None):
 r=subprocess.run(args,cwd=repo,env=e or env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
 (base/(name+'.log')).write_text(r.stdout); out[name]={'rc':r.returncode}; save();return r

def save():(base/'results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2))
def get(p,path):
 with urllib.request.urlopen(f'http://127.0.0.1:{p}{path}',timeout=6) as r:return r.read().decode()
# check combinations retain real sqlite and sibling sentinel
check=base/'check';check.mkdir(exist_ok=True)
c=sqlite3.connect(check/'vocab.db');c.execute('create table if not exists sentinel(value)');c.execute("insert into sentinel values ('keep')");c.commit();c.close();(check/'keep.txt').write_text('keep')
def snapshot():return {str(p.relative_to(check)):hashlib.sha256(p.read_bytes()).hexdigest() for p in check.rglob('*') if p.is_file()}
for flag in ['--fresh','--reset']:
 before=snapshot();r=run('check'+flag,['bash','scripts/cet/run.sh','--check',flag,'--data-dir',str(check),'--port',str(pa)]);out['check'+flag]['unchanged']=before==snapshot();save()
for name,p,on in [('a',pa,False),('b',pb,True)]:
 data=base/name; en=env.copy();en['ELT_DATA_DIR']=str(data)
 if on:en['ELT_CET_FEATURE_CET_LISTENING_AGENT']='1'
 r=run('start-'+name,['bash','scripts/cet/run.sh','--fresh','--data-dir',str(data),'--port',str(p)],en)
 if r.returncode: print(name,'START FAILED',r.stdout[-2000:]);break
 health=json.loads(get(p,'/health'));mode=json.loads(get(p,'/api/cet/mode'));html=get(p,'/workspace/1')
 (base/(name+'-mode.json')).write_text(json.dumps(mode,ensure_ascii=False,indent=2));(base/(name+'-workspace.html')).write_text(html)
 out['start-'+name].update(port=p,health=health,mode=mode,agent_js='src="/static/cet-listening.js' in html,agent_css='href="/static/cet-listening.css' in html,pid=int((base/'logs'/'instances'/str(p)/'server.pid').read_text()));save()
 if name=='a':
  c=sqlite3.connect(data/'vocab.db');c.execute('create table audit_keep(value)');c.execute("insert into audit_keep values ('keep-live')");c.commit();c.close()
  r=run('fresh-live-a',['bash','scripts/cet/run.sh','--fresh','--data-dir',str(data),'--port',str(p)],en)
  c=sqlite3.connect(data/'vocab.db');value=c.execute('select * from audit_keep').fetchall();c.close()
  out['fresh-live-a'].update(preserved=value==[('keep-live',)],healthy=json.loads(get(p,'/health'))['status']);save()
if 'start-b' in out and out['start-b']['rc']==0:
 for name,p in [('a',pa),('b',pb)]:
  en=env.copy();en.update(PORT=str(p),ELT_USER_DB=str(base/name/'vocab.db'))
  run('stop-'+name,['bash','scripts/qa/stop_cet.sh'],en)
  try:get(p,'/health');out['stop-'+name]['target_stopped']=False
  except Exception:out['stop-'+name]['target_stopped']=True
  if name=='a':out['stop-a']['b_still_healthy']=json.loads(get(pb,'/health'))['status'];out['stop-a']['b_pid_unchanged']=int((base/'logs'/'instances'/str(pb)/'server.pid').read_text())==out['start-b']['pid']
  save()
print(json.dumps(out,ensure_ascii=False,indent=2))
