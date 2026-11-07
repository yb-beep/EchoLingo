from playwright.sync_api import sync_playwright
import json
res=[]
def check(n,ok,d=None):
    res.append({'name':n,'pass':bool(ok),'detail':d}); print(('[PASS] ' if ok else '[FAIL] ')+n+('' if d is None else f' :: {d}'))
with sync_playwright() as p:
    b=p.chromium.launch(headless=True); ctx=b.new_context(viewport={'width':1440,'height':900}); pg=ctx.new_page()
    errs=[]; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto('http://127.0.0.1:5344/', wait_until='domcontentloaded'); pg.wait_for_timeout(2500)
    s=pg.evaluate("""() => ({
      hidden: document.getElementById('cet-import-form').classList.contains('hidden'),
      toggle: document.getElementById('cet-import-toggle').innerText,
      emptyBox: document.getElementById('cet-empty-library').hidden
    })""")
    check('C3-3 空库时导入面板自动展开', s['hidden'] is False and '收起' in s['toggle'] and s['emptyBox'] is False, s)
    check('C3-4 空库无 JS 错误', not errs, errs[:3])
    ctx.close(); b.close()
print(json.dumps({'passed':sum(1 for r in res if r['pass']),'total':len(res),'allPass':all(r['pass'] for r in res)}))
