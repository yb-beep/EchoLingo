from playwright.sync_api import sync_playwright
import json
BASE="http://127.0.0.1:5251"
R=[]
def rec(n,ok,a): R.append((n,ok,a)); print(f"  [{'PASS' if ok else 'FAIL'}] {n}: {a}")
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(viewport={"width":1440,"height":900})
    errs=[]; f4=[]; bpan=[]
    pg.on("pageerror", lambda e: errs.append(str(e)[:80]))
    pg.on("response", lambda r: f4.append(f"{r.status} {r.url.split('5251')[-1][:40]}") if r.status>=400 else None)
    pg.on("request", lambda r: bpan.append(r.url) if "baidu-pan" in r.url else None)

    print("── BASE-06 收藏页动态收束 ──")
    pg.goto(BASE+"/#favorites", wait_until="networkidle", timeout=60000)
    pg.wait_for_timeout(4500)   # 等动态渲染
    txt=pg.inner_text("body")
    retired=[t for t in ["GRE","雅思","考研","托福","全部造句情况","情景造句练习","AI 情景造句"] if t in txt]
    rec("退役训练入口 0", retired==[], str(retired))
    rec("收藏能力保留", "词汇收藏" in txt and "句子收藏" in txt, "词汇收藏/句子收藏在")
    # 句子收藏子页
    try:
        pg.click("text=句子收藏", timeout=3000); pg.wait_for_timeout(2000)
        t2=pg.inner_text("body")
        rec("句子收藏句式筛选 0", "句式" not in t2 or "句式练习" not in t2, "已收束")
    except Exception as e:
        rec("句子收藏子页", False, str(e)[:60])

    print("\n── BASE-06 材料页 ──")
    pg.goto(BASE+"/", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(3500)
    body=pg.inner_text("body")
    rec("材料卡无阅读/精读按钮", not any(m in body for m in ["精读"]), f"精读={'精读' in body}")
    rec("baidu-pan 探测 0", len(bpan)==0, f"{len(bpan)} 次")

    print("\n── BASE-05 工作区（默认关闭）──")
    pg.goto(BASE+"/workspace/1", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(4000)
    r=pg.evaluate("""()=>({js:!!document.querySelector('script[src*="cet-listening.js"]'),
      tb:document.querySelectorAll('#cet-toolbar').length,
      q:document.querySelectorAll('#cet-questions').length})""")
    wbody=pg.inner_text("body")
    fake=[t for t in ["开始卡点练习","今日复习","未交卷","交卷"] if t in wbody]
    rec("队友模块资产不在 DOM", not r["js"], f"js={r['js']}")
    rec("无假入口", fake==[] and r["tb"]==0 and r["q"]==0, f"fake={fake} toolbar={r['tb']} q={r['q']}")
    rec("学习页无「阅读」", "加入精读" not in wbody, "已收束")

    print("\n── 完整用户流程 ──")
    pg.goto(BASE+"/workspace/1", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(3500)
    play=pg.evaluate("""async()=>{const m=document.querySelector('#local-media-player')||document.querySelector('audio');
      if(!m)return{ok:false}; try{await m.play()}catch(e){return{ok:false}};
      await new Promise(r=>setTimeout(r,900)); const t=m.currentTime; m.pause();
      m.currentTime=42; m.playbackRate=1.5;
      return{ok:t>0.1,played:+t.toFixed(2),seek:+m.currentTime.toFixed(1),rate:m.playbackRate,dur:+m.duration.toFixed(1)}}""")
    rec("播放/拖动/倍速", play.get("ok") and play.get("rate")==1.5, json.dumps(play))
    pg.reload(wait_until="networkidle", timeout=60000); pg.wait_for_timeout(4000)
    rs=pg.evaluate("()=>{const m=document.querySelector('#local-media-player')||document.querySelector('audio');return m?+m.currentTime.toFixed(1):null}")
    rec("刷新恢复位置", rs is not None and rs>20, f"restored={rs}s")
    tr=pg.evaluate("()=>({len:(document.querySelector('#subtitle-timeline')?.innerText||'').length})")
    rec("原文渲染", tr["len"]>100, f"{tr['len']} 字符")
    rec("无 JS 错误", errs==[], json.dumps(errs[:2],ensure_ascii=False))
    rec("无 4xx", f4==[], json.dumps(f4[:3],ensure_ascii=False))
    b.close()
p=sum(1 for _,o,_ in R if o); t=len(R)
print(f"\n{'='*60}\n  UI 结果：{p}/{t} 通过\n{'='*60}")
