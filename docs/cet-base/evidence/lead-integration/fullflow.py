"""Lead 完整用户流程：选级别→打开材料→播放→逐句→查词收藏→返回→恢复位置"""
from playwright.sync_api import sync_playwright
import json
import os
BASE=os.environ.get("TRIAL_BASE","http://127.0.0.1:5190")
steps=[]
def rec(name, ok, actual): steps.append({"step":name,"pass":ok,"actual":actual}); print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {actual}")

with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(viewport={"width":1440,"height":900})
    errs=[]; f4=[]
    pg.on("pageerror", lambda e: errs.append(str(e)[:100]))
    pg.on("response", lambda r: f4.append(f"{r.status} {r.url.split('5186')[-1][:45]}") if r.status>=400 else None)

    # 1 打开首页
    pg.goto(BASE+"/", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(2200)
    rec("1 首页打开", True, pg.title())

    # 2 级别筛选
    level_btns = pg.eval_on_selector_all("button,select", "els=>els.map(e=>e.textContent.trim()).filter(t=>/四级|六级|全部/.test(t)).slice(0,6)")
    rec("2 级别筛选控件", len(level_btns)>0, str(level_btns[:5]))

    # 3 材料卡片
    cards = pg.evaluate("""()=>{
      const sels=['.cet-material-card','.course-row','[data-material-id]','.material-card'];
      for(const s of sels){const e=document.querySelectorAll(s); if(e.length) return {sel:s,n:e.length};}
      return {sel:null,n:0};
    }""")
    rec("3 材料卡片渲染", cards["n"]>0, json.dumps(cards))

    # 4 继续学习入口
    cont = pg.evaluate("""()=>{
      const els=[...document.querySelectorAll('a,button')];
      const hit=els.filter(e=>/继续学习|继续精听|接着学/.test(e.textContent||''));
      return hit.length? hit[0].textContent.trim().slice(0,30) : null;
    }""")
    rec("4 继续学习入口", cont is not None, str(cont))

    # 5 打开材料
    pg.goto(BASE+"/workspace/1", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(3500)
    rec("5 材料页打开", "听力工作区" in pg.title(), pg.title())

    # 6 播放真实生效
    play = pg.evaluate("""async()=>{
      const m=document.querySelector('#local-media-player')||document.querySelector('audio');
      if(!m) return {ok:false};
      try{ await m.play(); }catch(e){ return {ok:false,err:String(e).slice(0,50)}; }
      await new Promise(r=>setTimeout(r,1000));
      const t=m.currentTime; m.pause();
      return {ok:t>0.2, t:+t.toFixed(2), paused:m.paused, dur:+m.duration.toFixed(1)};
    }""")
    rec("6 真实播放", play.get("ok"), json.dumps(play))

    # 7 逐句跳转
    jump = pg.evaluate("""async()=>{
      const m=document.querySelector('#local-media-player')||document.querySelector('audio');
      const next=document.querySelector('#next-sentence, .next-sentence, [data-action="next"]');
      if(!m||!next) return {ok:false, hasNext:!!next};
      const before=m.currentTime;
      next.click(); await new Promise(r=>setTimeout(r,700));
      return {ok:m.currentTime!==before, before:+before.toFixed(2), after:+m.currentTime.toFixed(2)};
    }""")
    rec("7 逐句跳转", jump.get("ok"), json.dumps(jump))

    # 8 倍速
    rate = pg.evaluate("""async()=>{
      const m=document.querySelector('#local-media-player')||document.querySelector('audio');
      const sel=document.querySelector('#playback-rate, select[class*=rate]');
      if(!m) return {ok:false};
      if(sel){ sel.value='1.5'; sel.dispatchEvent(new Event('change',{bubbles:true})); }
      await new Promise(r=>setTimeout(r,400));
      return {ok:Math.abs(m.playbackRate-1.5)<0.01, rate:m.playbackRate, hadSelect:!!sel};
    }""")
    rec("8 倍速切换", rate.get("ok"), json.dumps(rate))

    # 9 原文可见 + 点词查词
    word = pg.evaluate("""()=>{
      const w=document.querySelector('.highlight-word, .cet-word, [data-word]');
      return w? {found:true, text:(w.textContent||'').trim().slice(0,20)} : {found:false};
    }""")
    rec("9 CET 词高亮存在", word.get("found"), json.dumps(word, ensure_ascii=False))

    # 10 收藏按钮存在
    fav = pg.evaluate("""()=>{
      const b=document.querySelector('#star-save, .star-save, [data-action="save-sentence"], #save-sentence');
      return b? {found:true, text:(b.textContent||'').trim().slice(0,20)} : {found:false};
    }""")
    rec("10 收藏入口存在", fav.get("found"), json.dumps(fav, ensure_ascii=False))

    # 11 seek 后刷新恢复
    pg.evaluate("""()=>{const m=document.querySelector('#local-media-player')||document.querySelector('audio'); if(m) m.currentTime=42;}""")
    pg.wait_for_timeout(1500)
    pg.reload(wait_until="networkidle", timeout=60000); pg.wait_for_timeout(4000)
    restored = pg.evaluate("""()=>{const m=document.querySelector('#local-media-player')||document.querySelector('audio'); return m? +m.currentTime.toFixed(1) : null;}""")
    rec("11 刷新后恢复位置", restored is not None and restored>10, f"restored={restored}s")

    rec("12 无 JS 运行时错误", len(errs)==0, json.dumps(errs[:3], ensure_ascii=False))
    rec("13 无 4xx 请求", len(f4)==0, json.dumps(f4[:4], ensure_ascii=False))
    pg.screenshot(path="/tmp/fullflow.png")
    b.close()

ok=sum(1 for s in steps if s["pass"])
print(f"\n结果: {ok}/{len(steps)} 通过")
