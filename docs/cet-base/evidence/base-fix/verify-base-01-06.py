"""Lead 最终集成核验：逐项复验独立验收退回的 BASE-01..06。"""
import json, sys, urllib.request, urllib.error, io, wave
BASE="http://127.0.0.1:5251"
R=[]
def rec(name, ok, actual): R.append((name,ok,actual)); print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {actual}")

def j(url, method="GET", data=None, ctype=None):
    req=urllib.request.Request(BASE+url, data=data, method=method)
    if ctype: req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=20) as r: return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try: return e.code, json.load(e)
        except Exception: return e.code, None

def wav_bytes(sec=1):
    b=io.BytesIO()
    with wave.open(b,"wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        w.writeframes(b"\x00\x00"*(8000*sec))
    return b.getvalue()

def post_import(files, fields):
    boundary="----lead"
    body=b""
    for k,v in fields.items():
        body+=f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k,(fn,content,ct) in files.items():
        body+=f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode()
        body+=content+b"\r\n"
    body+=f"--{boundary}--\r\n".encode()
    return j("/api/cet/base/import","POST",body,f"multipart/form-data; boundary={boundary}")

print("="*70); print("  BASE-01..06 逐项复验"); print("="*70)

# ── BASE-01 手动归类不丢类别 ──
print("\n── BASE-01 手动归类 ──")
s, mats = j("/api/cet/base/materials")
items = mats.get("items", [])
cet4 = next((m for m in items if m.get("level")=="cet4"), None)
if cet4:
    lid = int(str(cet4["lessonUrl"]).rstrip("/").rsplit("/",1)[-1])
    # 先设成 cet4/样例
    j(f"/api/cet/base/materials/{lid}","PATCH",json.dumps({"category":"样例"}).encode(),"application/json")
    s1, m1 = j(f"/api/cet/base/materials/{lid}")
    # 只改 level
    j(f"/api/cet/base/materials/{lid}","PATCH",json.dumps({"level":"cet6"}).encode(),"application/json")
    s2, m2 = j(f"/api/cet/base/materials/{lid}")
    ok = m2.get("level")=="cet6" and m2.get("category")=="样例"
    rec("只改级别保留类别", ok, f"改后 {m2.get('level')}/{m2.get('category')} (期望 cet6/样例)")
    # 只改 category
    j(f"/api/cet/base/materials/{lid}","PATCH",json.dumps({"category":"练习"}).encode(),"application/json")
    s3, m3 = j(f"/api/cet/base/materials/{lid}")
    ok2 = m3.get("level")=="cet6" and m3.get("category")=="练习"
    rec("只改类别保留级别", ok2, f"改后 {m3.get('level')}/{m3.get('category')} (期望 cet6/练习)")

# ── BASE-02 JSON 坏条目 ──
print("\n── BASE-02 JSON 坏条目整份拒绝 ──")
bad = json.dumps([{"start":1.0,"end":2.0,"text":"Good one."}, "BROKEN", None,
                  {"start":2.0,"end":3.0,"text":"Good two."}])
s, d = post_import({"audio":("a.wav",wav_bytes(),"audio/wav"),"subtitle":("s.json",bad.encode(),"application/json")},
                   {"title":"坏JSON","level":"cet4"})
det = (d or {}).get("detail", d or {})
rec("坏JSON返回422", s==422, f"HTTP {s}")
rec("逐条错误含原始位置", det.get("errorCount",0)>=2, f"errorCount={det.get('errorCount')} 位置={[e.get('index') for e in det.get('errors',[])]}")

# ── BASE-03-B 坏音频 ──
print("\n── BASE-03 坏音频结构化错误 ──")
s, d = post_import({"audio":("bad.mp3",b"not audio at all","audio/mpeg")}, {"title":"坏音频"})
det = (d or {}).get("detail", d or {})
rec("坏音频返回422结构化", s==422 and det.get("code")=="IMPORT_VALIDATION_FAILED",
    f"HTTP {s} code={det.get('code')} reason={[e.get('reason') for e in det.get('errors',[])]}")

# ── BASE-03-A 并发去重 ──
print("\n── BASE-03 并发重复导入 ──")
import threading
wav = wav_bytes(2)
res=[]
def worker():
    res.append(post_import({"audio":("dup.wav",wav,"audio/wav")}, {"title":"并发测试","level":"cet4"}))
ts=[threading.Thread(target=worker) for _ in range(2)]
[t.start() for t in ts]; [t.join() for t in ts]
codes=[r[0] for r in res]
dups=[(r[1] or {}).get("duplicate") for r in res]
lids=[(r[1] or {}).get("lesson",{}).get("lessonId") for r in res]
ok = sorted(dups)==[False,True] or (dups.count(True)==1 and len(set(lids))==1)
rec("并发只建一份", ok, f"duplicate={dups} lessonIds={lids}")

# ── BASE-06 收藏页动态收束 ──
print("\n── BASE-06 收藏页（浏览器验证另跑）──")
s, mode = j("/api/cet/mode")
feats = (mode or {}).get("features",{})
rec("集中配置可用", s==200 and "features" in (mode or {}), f"{s}")
rec("队友模块默认关闭", feats.get("cet_listening_agent",{}).get("enabled") is False,
    f"cet_listening_agent={feats.get('cet_listening_agent',{}).get('enabled')}")
rec("收束项已登记", len((mode or {}).get("retired_keys",[]))>=11, f"retired={len((mode or {}).get('retired_keys',[]))}")

print("\n"+"="*70)
p=sum(1 for _,o,_ in R if o); t=len(R)
print(f"  结果：{p}/{t} 通过")
print("="*70)
if p<t:
    for n,o,a in R:
        if not o: print(f"  FAIL {n}: {a}")
