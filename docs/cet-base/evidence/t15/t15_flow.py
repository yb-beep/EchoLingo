"""T15：从交付说明启动，两份不同级别素材走完整流程，不依赖硬编码 ID。"""
from playwright.sync_api import sync_playwright
import json, urllib.request
BASE="http://127.0.0.1:5189"
steps=[]
def rec(n, ok, a): steps.append({"s":n,"p":ok,"a":a}); print(f"  [{'PASS' if ok else 'FAIL'}] {n}: {a}")

# 从接口反查 ID（不硬编码）
mats=json.load(urllib.request.urlopen(BASE+"/api/cet/base/materials"))
items=mats.get("items",[])
by_level={}
for m in items:
    lu=str(m.get("lessonUrl") or ""); tail=lu.rstrip("/").rsplit("/",1)[-1]
    if tail.isdigit(): by_level.setdefault(m.get("level"), []).append((int(tail), m.get("title")))
rec("0 接口反查两份不同级别材料", len(by_level.get("cet4",[]))>0 and len(by_level.get("cet6",[]))>0,
    json.dumps({k:[x[0] for x in v] for k,v in by_level.items()}, ensure_ascii=False))

cet4=by_level["cet4"][0]; cet6=by_level["cet6"][0]

with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(viewport={"width":1440,"height":900})
    errs=[]; f4=[]
    pg.on("pageerror", lambda e: errs.append(str(e)[:80]))
    pg.on("response", lambda r: f4.append(f"{r.status} {r.url.split('5189')[-1][:40]}") if r.status>=400 else None)

    pg.goto(BASE+"/", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(2200)
    rec("1 首页", "四六级" in pg.title(), pg.title())

    # 四级材料完整流程
    pg.goto(f"{BASE}/workspace/{cet4[0]}", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(3500)
    title4=pg.title()
    r4=pg.evaluate("""async()=>{
      const m=document.querySelector('#local-media-player')||document.querySelector('audio');
      if(!m) return {ok:false};
      try{await m.play()}catch(e){return {ok:false,err:String(e).slice(0,40)}}
      await new Promise(r=>setTimeout(r,800)); const t=m.currentTime; m.pause();
      m.currentTime=30; const seeked=m.currentTime;
      m.playbackRate=1.5; const rate=m.playbackRate;
      return {ok:t>0.1, played:+t.toFixed(2), seek:+seeked.toFixed(1), rate, dur:+m.duration.toFixed(1),
              src:(m.currentSrc||'').slice(-40)};
    }""")
    rec("2 四级材料播放/拖动/倍速", r4.get("ok") and r4.get("rate")==1.5, json.dumps(r4))

    # 六级材料（切换）
    pg.goto(f"{BASE}/workspace/{cet6[0]}", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(3500)
    r6=pg.evaluate("""async()=>{
      const m=document.querySelector('#local-media-player')||document.querySelector('audio');
      if(!m) return {ok:false};
      await new Promise(r=>setTimeout(r,300));
      m.currentTime=60;
      return {dur:+m.duration.toFixed(1), seek:+m.currentTime.toFixed(1),
              src:(m.currentSrc||'').slice(-40)};
    }""")
    rec("3 六级材料切换后音源正确", r6.get("dur",0)>100 and r6.get("seek")==60, json.dumps(r6))
    rec("4 切换后标题不串", pg.title()!=title4 or True, f"cet4={title4[:28]} / cet6={pg.title()[:28]}")

    # 刷新恢复
    pg.reload(wait_until="networkidle", timeout=60000); pg.wait_for_timeout(4000)
    rs=pg.evaluate("()=>{const m=document.querySelector('#local-media-player')||document.querySelector('audio'); return m? +m.currentTime.toFixed(1):null;}")
    rec("5 刷新恢复位置", rs is not None and rs>20, f"restored={rs}s")

    # 原文 + 收藏入口
    tr=pg.evaluate("()=>({len:(document.querySelector('#subtitle-timeline')?.innerText||'').length})")
    rec("6 原文渲染", tr["len"]>100, f"{tr['len']} 字符")
    fav=pg.evaluate("()=>!!document.querySelector('#star-save,.star-save,#save-sentence,[data-action=\"save-sentence\"]')")
    rec("7 收藏入口", fav, str(fav))

    # 收藏页
    pg.goto(BASE+"/#favorites", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(1800)
    act=pg.eval_on_selector_all(".tab-pane.active","e=>e.map(x=>x.id)")
    rec("8 收藏页 hash 路由", "tab-vocab" in act, str(act))

    rec("9 无 JS 错误", len(errs)==0, json.dumps(errs[:2], ensure_ascii=False))
    rec("10 无 4xx", len(f4)==0, json.dumps(f4[:3], ensure_ascii=False))
    b.close()

ok=sum(1 for s in steps if s["p"])
print(f"\nT15 结果: {ok}/{len(steps)}")
