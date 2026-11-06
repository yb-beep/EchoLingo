#!/bin/bash
# 四六级听力训练 · 一键启动（M4 交付用）
#
# 用途：验收方或用户在**任意一台装好环境的机器**上，从零跑到能用。
# 与 scripts/qa/start_cet.sh 的区别：这个脚本会先检查并补齐材料，再启动。
#
# 用法：
#   scripts/cet/run.sh              # 启动（含环境检查）
#   scripts/cet/run.sh --check      # 只检查，不启动
#   scripts/cet/run.sh --reset      # 清空隔离数据后重建（谨慎）

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
DB="$ROOT/.cet-qa/data/vocab.db"
MEDIA="$ROOT/.cet-qa/media"

fail=0
say()  { printf '%s\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; }
warn() { printf '  [!]  %s\n' "$*"; }
bad()  { printf '  [X]  %s\n' "$*"; fail=$((fail+1)); }

say "=================================================================="
say "  四六级听力训练 · 启动检查"
say "=================================================================="
say ""

# ── 1. 环境 ────────────────────────────────────────────
say "-- 1. 运行环境 --"
if [ -x "$PY" ]; then
  ver=$("$PY" -V 2>&1 | awk '{print $2}')
  case "$ver" in
    3.11.*) ok "Python $ver" ;;
    *)      warn "Python $ver（要求 3.11）" ;;
  esac
else
  bad "缺少虚拟环境：$VENV/bin/python"
  say "     建法：python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt"
fi

if command -v ffprobe >/dev/null 2>&1; then
  ok "ffprobe $(ffprobe -v quiet -show_entries format=duration -of csv=p=0 /dev/null 2>/dev/null; ffprobe -version 2>/dev/null | head -1 | awk '{print $3}')"
else
  bad "缺少 ffprobe（FFmpeg）"
fi

# ── 2. 隔离数据 ────────────────────────────────────────
say ""
say "-- 2. 数据隔离 --"
if [ -f "$DB" ]; then
  ok "隔离数据库：${DB#$ROOT/}"
else
  warn "隔离数据库不存在，将自动创建"
fi

if [ -f "$ROOT/resources/vocab.db" ]; then
  warn "检测到正式数据 resources/vocab.db —— 本脚本不会读写它"
fi

# ── 3. 材料 ────────────────────────────────────────────
say ""
say "-- 3. 样例材料 --"
missing=0
for mid in cet6-202512-conv2 cet4-sample-hope-wish; do
  if [ -f "$MEDIA/$mid.mp3" ] && [ -f "$MEDIA/$mid.sentences.source.json" ]; then
    ok "${mid}（音频 + 时间轴）"
  else
    warn "${mid} 材料缺失"
    missing=$((missing+1))
  fi
done

if [ "$missing" -gt 0 ]; then
  say ""
  say "  材料音频不入库（体积与版权）。获取方式："
  say "    A. 向 C 索取 .cet-qa/media/*.mp3 与 *.sentences.source.json"
  say "    B. 若本机有旧项目："
  say "       .venv/bin/python scripts/qa/prepare_audio.py --copy-from <路径>"
  say ""
  say "  期望 SHA-256（六级）："
  say "    de98e7de987d400cb05fe957f21b5a4fa57112df9676bdb4c3f6214ff8a34259"
  fail=$((fail+1))
fi

# ── 4. 前端模块 ────────────────────────────────────────
say ""
say "-- 4. 前端接入 --"
for f in frontend/static/cet-listening.js frontend/static/cet-listening.css; do
  [ -f "$ROOT/$f" ] && ok "${f##*/}" || bad "缺少 $f"
done
if grep -q "cet-listening.js" frontend/templates/workspace.html 2>/dev/null; then
  ok "workspace.html 已引用卡点模块"
else
  bad "workspace.html 未引用卡点模块"
fi
if grep -q "natural-tts.js" frontend/templates/workspace.html 2>/dev/null; then
  ok "原 natural-tts 引用保留"
else
  warn "原 natural-tts 引用丢失（可能破坏原功能）"
fi

# ── 结论 ──────────────────────────────────────────────
say ""
say "=================================================================="
if [ "$fail" -gt 0 ]; then
  say "  检查未通过：$fail 项需处理"
  say "=================================================================="
  exit 1
fi
say "  检查通过"
say "=================================================================="

if [ "${1:-}" = "--check" ]; then
  exit 0
fi

# ── 导入材料（幂等）────────────────────────────────────
say ""
say "-- 导入材料（幂等，已存在则复用）--"
"$PY" scripts/cet/import_materials.py --all || {
  say "材料导入失败"
  exit 1
}

# ── 启动服务 ──────────────────────────────────────────
say ""
say "-- 启动服务 --"
# F07：默认用真实服务，不自动起 mock（mock 只在显式 --mock 时启动）
if [ "${1:-}" = "--mock" ]; then
  ./scripts/qa/start_cet.sh --mock
else
  ./scripts/qa/start_cet.sh
fi
rc=$?

if [ $rc -eq 0 ]; then
  # F09：不硬编码 lesson id —— 导入脚本用数据库自增 ID，换库就会变。
  # 从材料接口反查真实 ID；查不到就明确说「未导入」，不打印假链接。
  say ""
  say "=================================================================="
  say "  已启动"
  say ""
  six_url=$("$PY" - <<'PYEOF' 2>/dev/null
import json, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:5173/api/cet/materials", timeout=5) as r:
        items = json.loads(r.read().decode()).get("items") or []
    for m in items:
        if m.get("materialId") == "cet6-202512-conv2":
            print(m.get("lessonUrl") or "")
            break
except Exception:
    pass
PYEOF
)
  four_url=$("$PY" - <<'PYEOF' 2>/dev/null
import json, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:5173/api/cet/materials", timeout=5) as r:
        items = json.loads(r.read().decode()).get("items") or []
    for m in items:
        if m.get("materialId") == "cet4-sample-hope-wish":
            print(m.get("lessonUrl") or "")
            break
except Exception:
    pass
PYEOF
)
  if [ -n "$six_url" ]; then
    say "  六级材料   http://127.0.0.1:5173${six_url}"
  else
    say "  六级材料   未导入（先跑 .venv/bin/python scripts/cet/import_materials.py --all）"
  fi
  if [ -n "$four_url" ]; then
    say "  四级样例   http://127.0.0.1:5173${four_url}"
  else
    say "  四级样例   未导入"
  fi
  say "  首页       http://127.0.0.1:5173/"
  say "  接口       http://127.0.0.1:5173/api/cet/materials"
  say ""
  say "  停止       scripts/qa/stop_cet.sh --all"
  say "=================================================================="
fi
exit $rc
