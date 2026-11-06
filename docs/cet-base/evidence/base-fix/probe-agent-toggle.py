from playwright.sync_api import sync_playwright
import sys
BASE=sys.argv[1]
with sync_playwright() as p:
    b=p.chromium.launch(); pg=b.new_page(viewport={"width":1440,"height":900})
    pg.goto(BASE+"/workspace/1", wait_until="networkidle", timeout=60000); pg.wait_for_timeout(4000)
    r = pg.evaluate("""()=>({
      js: !!document.querySelector('script[src*="cet-listening.js"]'),
      css: !!document.querySelector('link[href*="cet-listening.css"]'),
      toolbar: document.querySelectorAll('#cet-toolbar').length,
      questions: document.querySelectorAll('#cet-questions').length,
      timeline: document.querySelectorAll('#cet-timeline').length,
    })""")
    body = pg.inner_text("body")
    fake = [t for t in ["开始卡点练习","今日复习","未交卷","交卷"] if t in body]
    print("  cet-listening.js:", r["js"], "| css:", r["css"])
    print("  toolbar/questions/timeline:", r["toolbar"], r["questions"], r["timeline"])
    print("  假入口:", fake)
    b.close()
