from playwright.sync_api import sync_playwright
import json, re

PAIRS = {
  'FEATURE_OFF': ('http://127.0.0.1:5312', 'http://127.0.0.1:5314'),
  'GENERIC': ('http://127.0.0.1:5321', 'http://127.0.0.1:5322'),
}
PAGES = ['/', '/workspace/1', '/workspace/1/intensive']

def norm(t):
    return re.sub(r'\s+', ' ', (t or '')).strip()

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    out = {}
    ok_all = True
    for mode,(base,final) in PAIRS.items():
        out[mode] = {}
        for path in PAGES:
            def grab(url):
                ctx = b.new_context(viewport={'width':1440,'height':900})
                pg = ctx.new_page(); errs=[]; pg.on('pageerror', lambda e: errs.append(str(e)))
                pg.goto(url+path, wait_until='domcontentloaded')
                pg.wait_for_timeout(2500)
                data = pg.evaluate("""() => ({
                    title: document.title,
                    text: document.body.innerText,
                    visibleControls: [...document.querySelectorAll('button,input,select,a')].filter(e => {
                        const s = getComputedStyle(e); const r = e.getBoundingClientRect();
                        return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                    }).length,
                    h: document.documentElement.scrollHeight
                })""")
                data['errors']=errs
                ctx.close()
                return data
            a = grab(base); z = grab(final)
            same_text = norm(a['text']) == norm(z['text'])
            same_controls = a['visibleControls'] == z['visibleControls']
            same_title = a['title'] == z['title']
            ok = same_text and same_controls and same_title and not z['errors']
            ok_all = ok_all and ok
            out[mode][path] = {'pass': ok, 'same_text': same_text, 'same_controls': same_controls,
                               'baseline_controls': a['visibleControls'], 'final_controls': z['visibleControls'],
                               'baseline_len': len(norm(a['text'])), 'final_len': len(norm(z['text'])),
                               'final_errors': z['errors'],
                               'diff_head': None if same_text else {'baseline': norm(a['text'])[:400], 'final': norm(z['text'])[:400]}}
    b.close()
print(json.dumps({'allPass': ok_all, 'modes': out}, ensure_ascii=False, indent=2))
open('/tmp/mode_regression_result.json','w').write(json.dumps({'allPass': ok_all, 'modes': out}, ensure_ascii=False, indent=2))
