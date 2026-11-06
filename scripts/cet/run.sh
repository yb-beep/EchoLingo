#!/bin/bash
# 四六级听力训练 · 一键启动（M4 交付用）
#
# 用途：验收方或用户在**任意一台装好环境的机器**上，从零跑到能用。
# 与 scripts/qa/start_cet.sh 的区别：这个脚本会先检查并补齐材料，再启动。
#
# 用法：
#   scripts/cet/run.sh                    # 启动（含环境检查）
#   scripts/cet/run.sh --check            # 只检查，不启动；**不写、不删任何数据**
#   scripts/cet/run.sh --port 5184        # 换端口（默认 5173）
#   PORT=5184 scripts/cet/run.sh          # 同上（环境变量写法）
#   scripts/cet/run.sh --fresh            # 用全新空库启动（演示 T01 空库场景）
#   scripts/cet/run.sh --data-dir /tmp/x  # 指定隔离数据目录
#   scripts/cet/run.sh --reset            # 删除并重建整个数据目录（非检查模式）
#
# 检查与修改分离（BASE-04-A）：
#   --check 是**纯只读**路径：不 mkdir、不 rm、不导入材料、不起服务，
#   即使同时给了 --fresh / --reset 也不会产生删除副作用。
#   清理动作一律发生在「检查通过之后」的修改阶段。
#
# 清库前先验所有权与停服状态（BASE-04-B）：
#   --fresh / --reset 之前会确认目标库不是正式库、不是仓库目录，
#   且**没有任何活服务或进程正在使用该库**；否则拒绝并返回 3，一个字节都不删。
#
# 退出码：0 成功｜1 检查有阻断项｜2 端口被占用｜3 拒绝破坏性操作（库仍被活服务持有等）

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1

VENV="$ROOT/.venv"
PY="$VENV/bin/python"
MEDIA="$ROOT/.cet-qa/media"
LOG_DIR="${CET_QA_LOG_DIR:-$ROOT/.cet-qa/logs}"
INSTANCES_DIR="$LOG_DIR/instances"

# ── 端口与数据目录：全部可覆盖，不硬编码 ──────────────────
# 冻结约定 §6 给「验证与交付」分配 5184；默认仍沿用 5173 保持向后兼容。
PORT="${PORT:-${ELT_PORT:-5173}}"
DATA_DIR="${ELT_DATA_DIR:-$ROOT/.cet-qa/data}"
FRESH=0
RESET=0
CHECK_ONLY=0
SAME_DB_SERVICE=0

usage() {
  sed -n '8,20p' "$0"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --check)    CHECK_ONLY=1 ;;
    --fresh)    FRESH=1 ;;
    # --reset 只登记意图；真正的 rm -rf 放到修改阶段，检查模式绝不执行
    --reset)    RESET=1 ;;
    --port)     shift; PORT="${1:-5173}" ;;
    --port=*)   PORT="${1#--port=}" ;;
    --data-dir) shift; DATA_DIR="${1:-}" ;;
    --data-dir=*) DATA_DIR="${1#--data-dir=}" ;;
    -h|--help)  usage; exit 0 ;;
    *)          echo "未知参数：$1" >&2; exit 64 ;;
  esac
  shift
done

if [ -z "$DATA_DIR" ]; then
  echo "--data-dir 不能为空" >&2
  exit 64
fi

DB="$DATA_DIR/vocab.db"

fail=0
say()  { printf '%s\n' "$*"; }
ok()   { printf '  [OK] %s\n' "$*"; }
warn() { printf '  [!]  %s\n' "$*"; }
bad()  { printf '  [X]  %s\n' "$*"; fail=$((fail+1)); }

# 拒绝破坏性操作：打印原因后立刻退出，保证「一个字节都不删」
refuse() {
  say ""
  say "=================================================================="
  say "  拒绝执行：$*"
  say "  本次没有删除或修改任何数据库文件。"
  say "=================================================================="
  exit 3
}

# 规范化绝对路径（不要求目标存在；macOS 上 /tmp 与 /private/tmp 要能对上）
norm_path() {
  local p="$1" dir base up rest
  dir="$(dirname "$p")"
  base="$(basename "$p")"
  if [ -d "$dir" ]; then
    printf '%s/%s\n' "$(cd "$dir" 2>/dev/null && pwd -P)" "$base"
    return 0
  fi
  up="$dir"
  rest="$base"
  while [ -n "$up" ] && [ "$up" != "/" ] && [ ! -d "$up" ]; do
    rest="$(basename "$up")/$rest"
    up="$(dirname "$up")"
  done
  if [ -d "$up" ]; then
    printf '%s/%s\n' "$(cd "$up" 2>/dev/null && pwd -P)" "$rest"
  else
    printf '%s\n' "$p"
  fi
}

# 哪些活进程正打开这个库（DB / WAL / SHM）——排除本脚本自身
db_holder_pids() {
  local f pid
  for f in "$DB" "$DB-wal" "$DB-shm"; do
    [ -e "$f" ] || continue
    lsof -nP -t -- "$f" 2>/dev/null || true
  done | tr -d ' ' | sort -u | while read -r pid; do
    [ -n "$pid" ] || continue
    [ "$pid" = "$$" ] && continue
    [ "$pid" = "${PPID:-0}" ] && continue
    printf '%s\n' "$pid"
  done
}

# 某个端口上 /health 报告的 sqlite.path（本项目底座才有的形状）。
# 拿不到就输出空。这是**不依赖文件句柄**的库归属判据：
# 本应用每请求开/关 SQLite 连接，服务空闲时 lsof 看不到任何 vocab.db 句柄
# （实测：刚启动未处理任何请求 → 0 个句柄），但 /health 始终能报出真实路径。
health_sqlite_path() {
  local port="$1" body
  body="$(curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
  [ -n "$body" ] || return 1
  printf '%s' "$body" | grep -q '"sqlite"' || return 1
  printf '%s' "$body" | sed -n 's/.*"sqlite"[^}]*"path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

# 该 PID 是不是本项目底座？
# 只看「监听端口」不够：端口上可能是别人的服务（不能按端口盲杀）。
pid_is_project_backend() {
  local cmd
  cmd="$(ps -p "$1" -o command= 2>/dev/null | head -1)"
  [ -n "$cmd" ] || return 1
  case "$cmd" in
    *backend/fastapi_server.py*) return 0 ;;
  esac
  return 1
}

# 枚举**所有**活着的本项目底座进程。
# 这是补「时间窗口漏洞」的关键：不再只查「有没有进程打开这个库」，
# 而是把所有在跑的后端都问一遍「你用的是哪个库」。
#
# 必须严格判定「这个进程**就是**后端」，不能只看命令行里有没有那个字符串：
# 否则 `bash -c "... backend/fastapi_server.py ..."` 这类包装 shell、以及
# grep/编辑器/日志命令都会被误判成底座，进而让保守兜底把正常清库也拦掉
# （探针里真实踩到：一条带该字符串的 bash -c 让 B3 正向路径被误拒）。
# 判据：命令行按空白分词后，**某个词以 backend/fastapi_server.py 结尾**，
# 且第一个词是 python 解释器（不是 bash/sh/env 之类）。
live_backend_pids() {
  local pid cmd first tok is_backend
  ps -eo pid=,command= 2>/dev/null | while read -r pid cmd; do
    [ -n "$pid" ] || continue
    is_backend=0
    first=""
    # 分词后逐词判断（不用 case 通配整行，避免包装 shell 误判）
    read -r -a __toks <<< "$cmd"
    for tok in "${__toks[@]}"; do
      [ -z "$first" ] && first="$tok"
      case "$tok" in
        backend/fastapi_server.py|*/backend/fastapi_server.py) is_backend=1 ;;
      esac
    done
    [ "$is_backend" -eq 1 ] || continue
    # 第一个词必须是 python 解释器（排除 bash -c / sh -c / tail / grep 等）
    case "$first" in
      *python|*python3|*python3.11) ;;
      *) continue ;;
    esac
    printf '%s\n' "$pid"
  done | while read -r pid; do
    [ -n "$pid" ] || continue
    [ "$pid" = "$$" ] && continue
    [ "$pid" = "${PPID:-0}" ] && continue
    kill -0 "$pid" 2>/dev/null || continue
    printf '%s\n' "$pid"
  done
}

# 某 PID 监听的所有 TCP 端口
pid_listen_ports() {
  lsof -nP -p "$1" -iTCP -sTCP:LISTEN 2>/dev/null \
    | tail -n +2 | sed -n 's/.*TCP[^:]*:\([0-9][0-9]*\) (LISTEN).*/\1/p' | sort -u
}

# 找出**正在使用 $1 这个库**的活底座服务。
# 输出：<pid> <port> <来源>（来源 = health|lsof|instance）
#
# 判据按可靠性排序，任一命中即算「持有」：
#   1. health    —— 向它的监听端口问 /health，比对 sqlite.path。
#                   服务空闲、无文件句柄时**仍然有效**（本漏洞的主判据）。
#   2. lsof      —— 它确实打开着 DB/WAL/SHM 文件句柄（服务忙时更快）。
#   3. instance  —— 实例登记声明该库，且该 PID 存活且是本项目底座。
services_using_db() {
  local want="$1" pid port hdb iport idb
  for pid in $(live_backend_pids); do
    # 1) /health 判据（最可靠：不依赖是否持有文件句柄）
    for port in $(pid_listen_ports "$pid"); do
      hdb="$(health_sqlite_path "$port" || true)"
      if [ -n "$hdb" ] && [ "$(norm_path "$hdb")" = "$want" ]; then
        printf '%s %s %s\n' "$pid" "$port" "health"
        continue 2
      fi
    done
    # 2) lsof 文件句柄判据
    if pid_holds_db "$pid" "$want"; then
      port="$(pid_listen_ports "$pid" | head -1)"
      printf '%s %s %s\n' "$pid" "${port:--}" "lsof"
      continue
    fi
    # 3) 实例登记判据：登记声明该库 + PID 存活 + 确实是本项目底座
    if [ -d "$INSTANCES_DIR" ]; then
      for idb in "$INSTANCES_DIR"/*/instance.env; do
        [ -f "$idb" ] || continue
        iport="$(sed -n 's/^ELT_INSTANCE_PORT=//p' "$idb" 2>/dev/null | head -1)"
        local rpid
        rpid="$(sed -n 's/^ELT_INSTANCE_PID=//p' "$idb" 2>/dev/null | head -1)"
        local rdb
        rdb="$(sed -n 's/^ELT_INSTANCE_DB=//p' "$idb" 2>/dev/null | head -1)"
        [ -n "$rdb" ] && [ -n "$rpid" ] || continue
        [ "$rpid" = "$pid" ] || continue
        [ "$(norm_path "$rdb")" = "$want" ] || continue
        printf '%s %s %s\n' "$pid" "${iport:--}" "instance"
        break
      done
    fi
  done
}

# 实例登记里指向同一个库、且进程真的活着的记录（保留给「报告线索」用）
instance_holders_for_db() {
  local want="$1" f db pid
  [ -d "$INSTANCES_DIR" ] || return 0
  for f in "$INSTANCES_DIR"/*/instance.env; do
    [ -f "$f" ] || continue
    db="$(sed -n 's/^ELT_INSTANCE_DB=//p' "$f" 2>/dev/null | head -1)"
    [ -n "$db" ] || continue
    [ "$(norm_path "$db")" = "$want" ] || continue
    pid="$(sed -n 's/^ELT_INSTANCE_PID=//p' "$f" 2>/dev/null | head -1)"
    if [ -z "$pid" ]; then
      pid="$(tr -dc '0-9' < "$(dirname "$f")/server.pid" 2>/dev/null || true)"
    fi
    [ -n "$pid" ] || continue
    kill -0 "$pid" 2>/dev/null || continue
    pid_is_project_backend "$pid" || continue
    printf '%s %s\n' "$pid" "$f"
  done
}

pid_cmd() { ps -p "$1" -o command= 2>/dev/null | head -1; }

# 进程是否真的打开着目标库（DB/WAL/SHM 任一）
pid_holds_db() {
  local pid="$1" db="$2" f
  for f in "$db" "$db-wal" "$db-shm"; do
    if lsof -nP -p "$pid" 2>/dev/null | grep -F -- "$f" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

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
    *)      warn "Python ${ver}（要求 3.11）" ;;
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
  want_db="$(norm_path "$DB")"
  running_real="$(norm_path "$running_db")"

  if [ "$running_real" = "$want_db" ]; then
    warn "端口 $PORT 上已有本项目底座在运行，且使用同一数据库（将复用，不重启）"
    say "     DB: $running_db"
    SAME_DB_SERVICE=1
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

# ── 3. 隔离数据（只读检查，绝不删改）────────────────────
say ""
say "-- 3. 数据隔离（只读检查）--"
DB_ABS="$(norm_path "$DB")"
DATA_DIR_ABS="$(norm_path "$DATA_DIR")"
PROD_DB_ABS="$(norm_path "$ROOT/resources/vocab.db")"

if [ "$DB_ABS" = "$PROD_DB_ABS" ]; then
  bad "目标数据库就是**正式库** $PROD_DB_ABS —— 本脚本拒绝把它当隔离库使用"
  say "     换一个数据目录：--data-dir /tmp/cet-demo"
else
  ok "目标库不是正式库（正式库：${PROD_DB_ABS#$ROOT/}）"
fi

if [ -f "$DB" ]; then
  ok "隔离数据库已存在：$DB_ABS"
else
  warn "隔离数据库不存在（非检查模式会自动创建空库）"
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
  if [ "$FRESH" -eq 1 ] || [ "$RESET" -eq 1 ]; then
    ok "只检查模式：不删除任何数据（--fresh/--reset 的清理已跳过）"
    say "     本次没有删除或修改任何数据库文件。"
  else
    ok "只检查模式：不写入任何数据"
  fi
fi

# 谁在用这个库：把所有活着的本项目底座都问一遍（/health → sqlite.path），
# 不依赖「服务是否恰好持有文件句柄」。本应用每请求开/关连接，空闲时 lsof
# 看不到任何 vocab.db 句柄 —— 只查 lsof 会漏判（BASE-04-B 时间窗口漏洞）。
db_users="$(services_using_db "$DB_ABS")"
if [ -n "$db_users" ]; then
  warn "该库正被活服务使用（--fresh/--reset 会被拒绝）："
  printf '%s\n' "$db_users" | while read -r pid port src; do
    say "       PID $pid  端口 ${port}  （判据：${src}）"
  done
fi
holders="$(db_holder_pids)"
if [ -n "$holders" ]; then
  warn "有活进程正打开该库的文件句柄："
  for p in $holders; do
    say "       PID $p  $(pid_cmd "$p")"
  done
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

# ── 结论（检查阶段到此为止）────────────────────────────
say ""
say "=================================================================="
if [ "$fail" -gt 0 ]; then
  say "  检查未通过：$fail 项需处理"
  say "=================================================================="
  exit 1
fi
say "  检查通过"
say "=================================================================="

# --check 是纯只读路径：到此直接退出，后面的 mkdir/rm/导入/启动一律不执行。
# BASE-04-A：--check --fresh 曾经删掉真实库还报「检查通过」，就是因为它跑到了下面。
if [ "$CHECK_ONLY" -eq 1 ]; then
  exit 0
fi

# ══════════════════════════════════════════════════════
#  以下为修改阶段：只有非 --check 才会执行
# ══════════════════════════════════════════════════════
say ""
say "-- 6. 数据目录写入（修改阶段）--"

# 所有权：不碰正式库，不把仓库目录/家目录当隔离数据目录
if [ "$DB_ABS" = "$PROD_DB_ABS" ]; then
  refuse "目标库是正式库 $PROD_DB_ABS"
fi
if [ "$DATA_DIR_ABS" = "$(norm_path "$ROOT")" ] \
   || [ -d "$DATA_DIR/.git" ] || [ -d "$DATA_DIR/backend" ] || [ -d "$DATA_DIR/resources" ]; then
  refuse "数据目录 $DATA_DIR 看起来是仓库/源码目录，不是隔离实例目录"
fi
if [ "$DATA_DIR_ABS" = "$(norm_path "$HOME")" ] || [ "$DATA_DIR_ABS" = "/" ]; then
  refuse "数据目录 $DATA_DIR 是家目录或根目录，不是隔离实例目录"
fi
if [ -f "$DB" ] && [ ! -f "$DB-wal" ]; then
  if ! head -c 16 "$DB" 2>/dev/null | grep -q "SQLite format 3"; then
    refuse "$DB 不是 SQLite 数据库文件（可能是别的重要文件），不删除"
  fi
fi

# 停服状态：清库前确认该库没有任何活的使用者（BASE-04-B）
#
# 时间窗口漏洞（Lead 复验发现）：本应用每请求开/关 SQLite 连接，服务刚启动、
# 还没处理任何请求时 lsof 看不到任何 vocab.db 句柄。只查文件句柄会漏判 →
# 护栏失效 → 把活服务的库删掉。修法：
#   主判据 = 枚举所有活底座进程，向它们的监听端口问 /health 比对 sqlite.path
#            （空闲服务也能报出真实库路径）
#   辅判据 = lsof 文件句柄（服务忙时更快）
#   兜底   = 有活底座但**无法确认**它用哪个库时，默认拒绝（宁可拒绝不可误删）
if [ "$FRESH" -eq 1 ] || [ "$RESET" -eq 1 ]; then
  live_problem=0
  if [ "$SAME_DB_SERVICE" -eq 1 ]; then
    warn "端口 $PORT 上有活服务正在使用同一个库 —— 拒绝清库"
    say "     先停掉它：PORT=$PORT scripts/qa/stop_cet.sh"
    live_problem=1
  fi

  db_users="$(services_using_db "$DB_ABS")"
  if [ -n "$db_users" ]; then
    warn "以下活服务正在使用该库 —— 拒绝清库："
    printf '%s\n' "$db_users" | while read -r pid port src; do
      say "       PID $pid  端口 ${port}  （判据：${src}）"
    done
    say "     先停掉它们：PORT=<端口> scripts/qa/stop_cet.sh"
    live_problem=1
  fi

  holders="$(db_holder_pids)"
  if [ -n "$holders" ]; then
    warn "以下活进程正打开该库的文件句柄 —— 拒绝清库："
    for p in $holders; do
      say "       PID $p  $(pid_cmd "$p")"
    done
    live_problem=1
  fi

  # 保守兜底：还有活底座，但我们既没确认它用这个库、也没确认它不用。
  # 判据是「该 PID 的端口能报出 /health 但路径对不上」→ 明确是别的库，放行；
  # 「该 PID 的端口问不出 /health」→ 无法确认，拒绝。
  if [ "$live_problem" -eq 0 ]; then
    unverified=""
    for pid in $(live_backend_pids); do
      confirmed_other=0
      for port in $(pid_listen_ports "$pid"); do
        hdb="$(health_sqlite_path "$port" || true)"
        if [ -n "$hdb" ]; then
          confirmed_other=1
          break
        fi
      done
      [ "$confirmed_other" -eq 1 ] || unverified="$unverified $pid"
    done
    unverified="$(printf '%s' "$unverified" | tr -s ' ')"
    if [ -n "$unverified" ]; then
      warn "有活底座进程但无法确认它用哪个库 —— 保守拒绝清库："
      for p in $unverified; do
        say "       PID $p  $(pid_cmd "$p")"
      done
      say "     先停掉它们再清库，或确认后手工处理："
      say "       PORT=<端口> scripts/qa/stop_cet.sh --list"
      refuse "无法确认数据库未被活服务使用，保守拒绝 --fresh/--reset"
    fi
  fi

  if [ "$live_problem" -eq 1 ]; then
    refuse "数据库仍被活服务使用，不能执行 --fresh/--reset"
  fi
  ok "已确认没有活服务使用该库（/health 路径比对 + 文件句柄 + 保守兜底）"
fi

if [ "$RESET" -eq 1 ]; then
  rm -rf "$DATA_DIR"
  ok "已删除并重建数据目录（--reset）"
fi
mkdir -p "$DATA_DIR" "$DATA_DIR/config"

if [ "$FRESH" -eq 1 ]; then
  rm -f "$DB" "$DB-shm" "$DB-wal"
  ok "已清空隔离数据库（--fresh，清库前已确认无活服务持有）"
elif [ -f "$DB" ]; then
  ok "隔离数据库：${DB#$ROOT/}"
else
  ok "将新建空库：${DB#$ROOT/}"
fi

# 实例标记：记录这个数据目录归谁用（端口/库路径），便于停止脚本核验身份
cat > "$DATA_DIR/.cet-instance.json" <<EOF
{"schema":1,"created_by":"scripts/cet/run.sh","port":$PORT,"db":"$DB_ABS","updated_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

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
  CET_QA_LOG_DIR="$LOG_DIR" \
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
