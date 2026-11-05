# 仅构造播放事件前置条件，以独立检验前端提交接线；不声称浏览器真的播放过候选。
import requests,json
from pathlib import Path
s=requests.Session();s.trust_env=False
base='http://127.0.0.1:5189/api/cet/practices/practice-e74eb4f9da2c'
p=s.get(base).json();st=p['currentStep'];rng=st['playRange'];common={'stepId':st['id'],'materialVersion':p['materialVersion'],'speed':1,'foreground':True}
out=[]
for kind,t in [('playback_start',rng['startSeconds']),('playback_end',rng['endSeconds'])]:
 body={**common,'eventId':'controlled-'+kind,'type':kind,'audioTime':t}
 if kind=='playback_end':body.update(activeMs=round((rng['endSeconds']-rng['startSeconds'])*1000),playedToEnd=True)
 r=s.post(base+'/events',json=body);out.append({'type':kind,'status':r.status_code});assert r.status_code==200,r.text
Path('/tmp/cet-352cd8e-audit/playback-fixture.json').write_text(json.dumps(out,indent=2));print(out)
