from playwright.sync_api import sync_playwright
import json, urllib.request
BASE='http://127.0.0.1:5343'
res=[]
def check(name, ok, detail=None):
    res.append({'name':name,'pass':bool(ok),'detail':detail})
    print(('[PASS] ' if ok else '[FAIL] ')+name+('' if detail is None else f' :: {detail}'))

with sync_playwright() as p:
    b=p.chromium.launch(headless=True, args=['--autoplay-policy=no-user-gesture-required'])
    ctx=b.new_context(viewport={'width':1440,'height':900})
    pg=ctx.new_page(); errs=[]; pg.on('pageerror', lambda e: errs.append(str(e)))

    # C3: import panel default collapsed with materials present
    pg.goto(BASE+'/', wait_until='domcontentloaded'); pg.wait_for_timeout(2500)
    state=pg.evaluate("""() => {
      const form=document.getElementById('cet-import-form');
      const toggle=document.getElementById('cet-import-toggle');
      return {hidden: form? form.classList.contains('hidden'):null,
              visible: form? form.getBoundingClientRect().height>0:null,
              toggleText: toggle? toggle.innerText:null,
              libTop: document.getElementById('course-library-list')?.getBoundingClientRect().top};
    }""")
    check('C3-1 有材料时导入面板默认收起', state['hidden'] is True and state['visible'] is False and '展开' in (state['toggleText'] or ''), state)
    # toggle expands
    pg.click('#cet-import-toggle'); pg.wait_for_timeout(400)
    expanded=pg.evaluate("() => ({hidden: document.getElementById('cet-import-form').classList.contains('hidden'), text: document.getElementById('cet-import-toggle').innerText})")
    check('C3-2 点展开可见', expanded['hidden'] is False and '收起' in expanded['text'], expanded)
    pg.click('#cet-import-toggle'); pg.wait_for_timeout(300)

    # C4: meta line
    cards=pg.evaluate("""() => [...document.querySelectorAll('.cet-material-card')].map(c => ({
        id: c.dataset.cetLesson,
        undated: c.querySelector('.cet-undated') ? c.querySelector('.cet-undated').innerText : null,
        text: c.innerText.slice(0,220)
    }))""")
    six=[c for c in cards if c['id']=='1']
    four=[c for c in cards if c['id']=='2']
    check('C4-1 六级卡显示真实年份/套次/题型',
          bool(six) and six[0]['undated'] and '2025年12月' in six[0]['undated'] and '第一套' in six[0]['undated'] and '长对话' in six[0]['undated'], six[0] if six else None)
    check('C4-2 无元数据材料不显示该行', bool(four) and four[0]['undated'] is None, four[0] if four else None)
    check('C4-3 不再出现「未标注」占位行', '未标注' not in (six[0]['text'] if six else ''), six[0]['text'] if six else None)

    # C6: AI block hidden without key
    health=json.loads(urllib.request.urlopen(BASE+'/health').read())
    ai_status=health.get('api_keys',{}).get('AI_API_KEY') or health.get('ai_key')
    pg.goto(BASE+'/workspace/1/intensive', wait_until='domcontentloaded'); pg.wait_for_timeout(3000)
    ai=pg.evaluate("""() => {
      const section=document.getElementById('ai-rec-section');
      const btn=document.getElementById('ai-rec-generate-btn');
      const filterBtn=document.querySelector('[data-filter="ai-recommended"]');
      return {sectionHidden: section? section.hidden:null,
              sectionVisible: section? (section.getBoundingClientRect().height>0):null,
              btnExists: !!btn,
              filterHidden: filterBtn? filterBtn.hidden:null,
              aiBody: document.getElementById('ai-rec-body')?.innerText || ''};
    }""")
    check('C6-1 无 key 时隐藏「本课 AI 推荐」整块',
          ai_status=='missing' and ai['sectionHidden'] is True and ai['sectionVisible'] is False and ai['btnExists'] is False, {'health':ai_status, **ai})
    check('C6-2 无 key 时隐藏「AI 推荐」筛选', ai['filterHidden'] is True, ai)

    # C2: three-state mark badge on intensive page
    # create a mark + practice + finish via API in same session
    sid=pg.evaluate("() => { let s=localStorage.getItem('cet.session.v1'); if(!s){s=`cet-session-${Date.now().toString(36)}-${Math.random().toString(36).slice(2,7)}`;localStorage.setItem('cet.session.v1',s);} return s; }")
    def post(path, body):
        req=urllib.request.Request(BASE+path, data=json.dumps(body).encode(), headers={'Content-Type':'application/json'}, method='POST')
        return json.loads(urllib.request.urlopen(req).read())
    mat=json.loads(urllib.request.urlopen(BASE+'/api/cet/materials/by-lesson/1').read())
    mid=mat['materialId']; mver=mat['materialVersion']
    # attempt
    post('/api/cet/attempts', {'materialId':mid,'materialVersion':mver,'listeningSessionId':sid,'answers':[{'questionId':'q-05','answer':1},{'questionId':'q-06','answer':1},{'questionId':'q-07','answer':3},{'questionId':'q-08','answer':2}]})
    # mark 1 -> confirm -> practice -> finish  (done)
    m1=post('/api/cet/marks', {'materialId':mid,'materialVersion':mver,'listeningSessionId':sid,'pressedAtSec':5.0,'reactionDelaySeconds':2.5,'speed':1.0})
    patch=urllib.request.Request(BASE+f"/api/cet/marks/{m1['markId']}", data=json.dumps({'confirmedSentenceId':m1['suggestedSentenceId']}).encode(), headers={'Content-Type':'application/json'}, method='PATCH')
    urllib.request.urlopen(patch)
    pr=post('/api/cet/practices', {'markId':m1['markId']})
    detail=json.loads(urllib.request.urlopen(BASE+f"/api/cet/practices/{pr['practiceId']}").read())
    post(f"/api/cet/practices/{pr['practiceId']}/finish", {'stepId':detail['currentStepId'],'materialVersion':mver})
    # mark 2 -> confirm only (ready)
    m2=post('/api/cet/marks', {'materialId':mid,'materialVersion':mver,'listeningSessionId':sid,'pressedAtSec':40.0,'reactionDelaySeconds':2.5,'speed':1.0})
    patch2=urllib.request.Request(BASE+f"/api/cet/marks/{m2['markId']}", data=json.dumps({'confirmedSentenceId':m2['suggestedSentenceId']}).encode(), headers={'Content-Type':'application/json'}, method='PATCH')
    urllib.request.urlopen(patch2)
    # mark 3 pending
    m3=post('/api/cet/marks', {'materialId':mid,'materialVersion':mver,'listeningSessionId':sid,'pressedAtSec':70.0,'reactionDelaySeconds':2.5,'speed':1.0})
    pg.reload(wait_until='domcontentloaded'); pg.wait_for_timeout(3200)
    marks=pg.evaluate("""() => [...document.querySelectorAll('.sentence-cet-mark')].map(e => ({text:e.innerText, cls:e.className}))""")
    texts=' | '.join(m['text'] for m in marks)
    check('C2-1 精听页出现「本轮已练」第三态', '本轮已练' in texts, marks)
    check('C2-2 同时保留待确认/待练', ('待确认' in texts) and ('待练' in texts), marks)
    check('C2-3 三态颜色类齐全', all(any(c in m['cls'] for m in marks) for c in ['sentence-cet-mark--pending','sentence-cet-mark--ready','sentence-cet-mark--done']), marks)

    # C5: CET styling
    style=pg.evaluate("""() => {
      const body=getComputedStyle(document.body);
      const h1=document.querySelector('h1');
      const sentence=document.querySelector('.sentence-copy');
      const btn=document.querySelector('.sentence-header-play');
      return {bodyBg: body.backgroundColor, bodyFont: body.fontFamily,
              h1Font: h1? getComputedStyle(h1).fontFamily : null,
              sentenceFont: sentence? getComputedStyle(sentence).fontFamily : null,
              btnBg: btn? getComputedStyle(btn).backgroundColor : null,
              eyebrow: document.querySelector('.eyebrow')? getComputedStyle(document.querySelector('.eyebrow')).color : null};
    }""")
    check('C5-1 页面底色改为工作区浅灰绿', style['bodyBg']=='rgb(246, 247, 244)', style['bodyBg'])
    check('C5-2 字体改为无衬线（不再是 Georgia）',
          'Georgia' not in (style['bodyFont'] or '') and 'Georgia' not in (style['h1Font'] or '') and 'Georgia' not in (style['sentenceFont'] or ''), style)
    check('C5-3 主按钮改为工作区绿底', style['btnBg']=='rgb(47, 111, 94)', style['btnBg'])
    check('C5-4 无 JS 错误', not errs, errs[:3])

    pg.set_viewport_size({'width':1440,'height':900}); pg.screenshot(path='/tmp/demo-intensive-1440.png', full_page=True)
    pg.goto(BASE+'/', wait_until='domcontentloaded'); pg.wait_for_timeout(2200); pg.screenshot(path='/tmp/demo-home-1440.png', full_page=True)
    ctx.close(); b.close()
print(json.dumps({'passed':sum(1 for r in res if r['pass']),'total':len(res),'allPass':all(r['pass'] for r in res)}, ensure_ascii=False))
open('/tmp/demo_home_verify.json','w').write(json.dumps(res, ensure_ascii=False, indent=2))
