from playwright.sync_api import sync_playwright
import difflib, re, json
BASE_NEW='http://127.0.0.1:5345'
BASE_OLD='http://127.0.0.1:5346'
def norm(t): return re.sub(r'\s+',' ',t or '').strip()
def grab(b,url):
    ctx=b.new_context(viewport={'width':1440,'height':900}); pg=ctx.new_page()
    errs=[]; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(url, wait_until='domcontentloaded'); pg.wait_for_timeout(2500)
    d=pg.evaluate("""() => ({
      text: document.body.innerText,
      controls: [...document.querySelectorAll('button,input,select,a')].filter(e=>{
        const s=getComputedStyle(e); const r=e.getBoundingClientRect();
        return s.display!=='none' && s.visibility!=='hidden' && r.width>0 && r.height>0;}).length
    })""")
    d['errors']=errs; ctx.close(); return d
out={}
with sync_playwright() as p:
    b=p.chromium.launch(headless=True)
    for path in ['/', '/workspace/1/intensive']:
        new=grab(b, BASE_NEW+path); old=grab(b, BASE_OLD+path)
        same=norm(new['text'])==norm(old['text'])
        out[path]={'same_text':same,'same_controls':new['controls']==old['controls'],
                   'new_controls':new['controls'],'old_controls':old['controls'],
                   'errors':new['errors']}
        if not same:
            sm=difflib.SequenceMatcher(None,norm(old['text']),norm(new['text']))
            out[path]['diffs']=[{'tag':t,'old':norm(old['text'])[max(0,i1-60):i2+60],'new':norm(new['text'])[max(0,j1-60):j2+60]}
                                for t,i1,i2,j1,j2 in sm.get_opcodes() if t!='equal'][:5]
    b.close()
print(json.dumps(out, ensure_ascii=False, indent=1))
open('/tmp/generic_compare.json','w').write(json.dumps(out,ensure_ascii=False,indent=1))
