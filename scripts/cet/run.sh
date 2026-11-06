#!/bin/bash
# 四六级听力训练 · 一键启动（M4 交付用）
#
# 用途：验收方或用户在**任意一台装好环境的机器**上，从零跑到能用。
# 与 scripts/qa/start_cet.sh 的区别：这个脚本会先检查并补齐材料，再启动。
#
# 用法：
#   scripts/cet/run.sh                    # 启动（含环境检查）
#   scripts/cet/run.sh --check            # 只检查，不启动
#   scripts/cet/run.sh --port 5184        # 换端口（默认 5173）
#   PORT=5184 scripts/cet/run.sh          # 同上（环境变量写法）
#   scripts/cet/run.sh --fresh            # 用全新空库启动（演示 T01 空库场景）
#   scripts/cet/run.sh --data-dir /tmp/x  # 指定隔离数据目录
#
# 退出码：0 成功｜1 检查有阻断项｜2 端口被占用

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
MEDIA="$ROOT/.cet-qa/media"

# ── 端口与数据目录：全部可覆盖，不硬编码 ──────────────────
# 冻结约定 §6 给「验证与交付」分配 5184；默认仍沿用 5173 保持向后兼容。
PORT="${PORT:-${ELT_PORT:-5173}}"
DATA_DIR="${ELT_DATA_DIR:-$ROOT/.cet-qa/data}"
FRESH=0
CHECK_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --check)    CHECK_ONLY=1 ;;
    --fresh)    FRESH=1 ;;
    --port)     shift; PORT="${1:-5173}" ;;
    --port=*)   PORT="${1#--port=}" ;;
    --data-dir) shift; DATA_DIR="${1:-}" ;;
    --data-dir=*) DATA_DIR="${1#--data-dir=}" ;;
    --reset)    FRESH=1; rm -rf "$DATA_DIR" ;;
    -h|--help)  sed -n '2,16p' "$0"; exit 0 ;;
    *)          echo "未知参数：$1" >&2; exit 64 ;;
  esac
  shift
done

DB="$DATA_DIR/vocab.db"

fail=0
say()  { printf '%s\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; }
warn() { printf '  [!]  %s\n' "$*"; }
bad()  { printf '  [X]  %s\n' "$*"; fail=$((fail+1)); }

say "=================================================================="
say "  四六级听力训练 · 启动检查"
say "  端口：$PORT   数据：$DB"
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
  ok "ffprobe $(ffprobe -version 2>/dev/null | head -1 | awk '{print $3}')"
else
  bad "缺少 ffprobe（FFmpeg）"
fi

# ── 2. 端口占用检测（不误连其他服务）────────────────────
say ""
say "-- 2. 端口占用 --"
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  owner=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | tail -n +2 | head -1)
  health=$(curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)

  # 三重判据，缺一不可复用：
  #   1. /health 能返回本项目形状（含 sqlite/ffmpeg）
  #   2. 它报告的 sqlite.path 与本次要用的 $DB **是同一个库**
  #   3. 否则一律视为「别的实例」，拒绝复用——避免「连上了但看的是别的数据」
  #
  # 第 2 条是实测踩坑后加的：本机 5186 上有一个旧实例跑 /tmp/cbroute/vocab.db，
  # 只看 /health 形状会误判成「自家底座」而复用它，结果打开的是空库、
  # 与本次指定的数据目录完全不符。
  if [ -z "$health" ]; then
    bad "端口 $PORT 被其它进程占用（/health 无响应），拒绝误连："
    say "     $owner"
    say "     换端口：scripts/cet/run.sh --port 5184"
    exit 2
  fi
  if ! printf '%s' "$health" | grep -q '"sqlite"'; then
    bad "端口 $PORT 上的服务不是本项目底座（/health 形状不符），拒绝误连："
    say "     $owner"
    say "     换端口：scripts/cet/run.sh --port 5184"
    exit 2
  fi

  running_db=$(printf '%s' "$health" | sed -n 's/.*"sqlite"[^}]*"path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')
  want_db=$(cd "$(dirname "$DB")" 2>/dev/null && pwd)/$(basename "$DB")
  running_real=$(cd "$(dirname "$running_db")" 2>/dev/null && pwd)/$(basename "$running_db")

  if [ "$running_real" = "$want_db" ]; then
    warn "端口 $PORT 上已有本项目底座在运行，且使用同一数据库（将复用，不重启）"
    say "     DB: $running_db"
  else
    bad "端口 $PORT 上已有一个底座，但用的是**另一个数据库**，拒绝复用："
    say "     它 在用：$running_db"
    say "     本次要：$DB"
    say "     复用会导致「连上了却看不到本次的数据」。请换端口："
    say "       scripts/cet/run.sh --port 5184"
    say "     或先停掉旧实例："
    say "       PORT=$PORT scripts/qa/stop_cet.sh"
    exit 2
  fi
else
  ok "端口 $PORT 空闲"
fi

# ── 3. 隔离数据 ────────────────────────────────────────
say ""
say "-- 3. 数据隔离 --"
mkdir -p "$DATA_DIR" "$DATA_DIR/config"
if [ "$FRESH" -eq 1 ]; then
  rm -f "$DB" "$DB-shm" "$DB-wal"
  ok "已清空隔离数据库（--fresh）"
elif [ -f "$DB" ]; then
  ok "隔离数据库：${DB#$ROOT/}"
else
  warn "隔离数据库不存在，将自动创建（空库启动）"
fi

if [ -f "$ROOT/resources/vocab.db" ]; then
  warn "检测到正式数据 resources/vocab.db —— 本脚本不会读写它"
fi

# ── 4. 材料 ────────────────────────────────────────────
say ""
say "-- 4. 样例材料 --"
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
  say ""
  say "  注意：材料缺失不阻断「空库启动」——只是没有样例可演示。"
fi

# ── 5. 前端模块 ────────────────────────────────────────
say ""
say "-- 5. 前端接入 --"
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

if [ "$CHECK_ONLY" -eq 1 ]; then
  exit 0
fi

# ── 导入材料（幂等；--fresh 时跳过，保持空库语义）──────
say ""
if [ "$FRESH" -eq 1 ]; then
  say "-- 跳过材料导入（--fresh 空库启动）--"
  say "   要导入样例材料："
  say "   ELT_USER_DB=$DB .venv/bin/python scripts/cet/import_materials.py --all"
else
  say "-- 导入材料（幂等，已存在则复用）--"
  ELT_USER_DB="$DB" "$PY" scripts/cet/import_materials.py --all || {
    say "材料导入失败（不阻断服务启动，可稍后手动导入）"
  }
fi

# ── 启动服务 ──────────────────────────────────────────
say ""
say "-- 启动服务 --"
PORT="$PORT" ELT_USER_DB="$DB" ELT_CONFIG_DIR="$DATA_DIR/config" \
  ./scripts/qa/start_cet.sh "$@"
rc=$?

if [ $rc -eq 0 ]; then
  # F09：不硬编码 lesson id —— 导入脚本用数据库自增 ID，换库就会变。
  # 从材料接口反查真实 ID；查不到就明确说「未导入」，不打印假链接。
  # 端口也从变量取，不硬编码 5173。
  say ""
  say "=================================================================="
  say "  已启动"
  say ""
  "$PY" - "$PORT" <<'PYEOF'
import json, sys, urllib.request
port = sys.argv[1]
base = f"http://127.0.0.1:{port}"
want = [
    ("六级材料", "cet6-202512-conv2"),
    ("四级样例", "cet4-sample-hope-wish"),
]
items = []
try:
    with urllib.request.urlopen(f"{base}/api/cet/materials", timeout=5) as r:
        items = json.loads(r.read().decode()).get("items") or []
except Exception:
    pass
by_id = {m.get("materialId"): m for m in items}
for label, mid in want:
    m = by_id.get(mid)
    url = (m or {}).get("lessonUrl") or ""
    if url:
        print(f"  {label}   {base}{url}")
    else:
        print(f"  {label}   未导入（空库启动或材料缺失）")
print(f"  首页       {base}/")
print(f"  接口       {base}/api/cet/materials")
print(f"  模式配置   {base}/api/cet/mode")
PYEOF
  say ""
  say "  停止       PORT=$PORT scripts/qa/stop_cet.sh"
  say "  自验       .venv/bin/python scripts/cet/verify_base.py --port $PORT"
  say "=================================================================="
fi
exit $rc
