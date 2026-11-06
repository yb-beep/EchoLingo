import json,sqlite3,subprocess,os,time,socket,shutil,sys
from pathlib import Path
root=Path('/tmp/cet-base-79e9-boundary');repo=Path('/Users/a1111/Documents/ChatGPT/EchoLingo-cet');out={}
data=root/'fresh-data';data.mkdir(exist_ok=True)
with sqlite3.connect(data/'vocab.db') as c:
 c.execute('CREATE TABLE IF NOT EXISTS audit_sentinel(value TEXT)');c.execute("INSERT INTO audit_sentinel VALUES ('keep this test record')")
r=subprocess.run(['bash','scripts/cet/run.sh','--check','--fresh','--data-dir',str(data),'--port','52961'],cwd=repo,capture_output=True,text=True)
out['freshCheck']={'command':'scripts/cet/run.sh --check --fresh --data-dir <temporary-dir> --port 52961','exitCode':r.returncode,'dbExistsAfter':(data/'vocab.db').exists(),'stdout':r.stdout,'stderr':r.stderr}
# Run the unchanged stop script in a temporary skeleton with only our PID files and our two HTTP processes.
project=root/'lifecycle-fixture';(project/'scripts/qa').mkdir(parents=True,exist_ok=True);(project/'.cet-qa/logs').mkdir(parents=True,exist_ok=True)
shutil.copy2(repo/'scripts/qa/stop_cet.sh',project/'scripts/qa/stop_cet.sh')
def freeport():
 s=socket.socket();s.bind(('127.0.0.1',0));p=s.getsockname()[1];s.close();return p
ports=[freeport(),freeport(),freeport()];procs=[]
try:
 for p in ports[:2]:
  procs.append(subprocess.Popen([sys.executable,'-m','http.server',str(p),'--bind','127.0.0.1'],cwd=root,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL))
 for p in ports[:2]:
  for _ in range(40):
   try:
    with socket.create_connection(('127.0.0.1',p),.1):break
   except OSError:time.sleep(.05)
 (project/'.cet-qa/logs/server.pid').write_text(str(procs[1].pid))
 r=subprocess.run(['bash','scripts/qa/stop_cet.sh'],cwd=project,env={**os.environ,'PORT':str(ports[0]),'CET_MOCK_PORT':str(ports[2])},capture_output=True,text=True,timeout=15)
 out['wrongPortStop']={'requestedPort':ports[0],'pidFileForPort':ports[1],'requestedServerAlive':procs[0].poll() is None,'otherServerAlive':procs[1].poll() is None,'exitCode':r.returncode,'stdout':r.stdout,'stderr':r.stderr}
finally:
 for p in procs:
  if p.poll() is None:p.terminate()
  p.wait(timeout=5)
(root/'launch-results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2));print(json.dumps(out,ensure_ascii=False,indent=2))
