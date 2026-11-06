#!/usr/bin/env bash
# 四六级听力黑匣子 · 停止服务（C 负责）
#
# 任务书要求「检查服务停止后能正常再次启动」。这个脚本**按实例**停止：
# 每个「端口 + 数据目录」组合有自己的一份实例记录（PID/端口/数据库），
# 停止前逐项核验身份，核不上就拒绝，绝不误杀别的实例。
#
# BASE-04-C 修复要点（原缺陷：所有实例共用 .cet-qa/logs/server.pid，
# 对实例 A 执行 PORT=A stop 会杀掉 PID 文件里的 B，A 还活着且退出码 0）：
#   1. 实例记录按端口隔离：.cet-qa/logs/instances/<port>/{server.pid,instance.env}
#   2. PID 只是**候选**，不是凭据：必须同时满足
#        a) 该 PID 真的在监听目标端口
#        b) 该 PID 用的数据库就是实例登记的库（/health 的 sqlite.path 或 lsof 打开的文件）
#   3. 共享 PID 文件仅作候选来源，永不单独作为停止依据
#   4. 端口没真正释放就返回非 0（不再「杀了别人还报成功」）
#
# 用法：
#   scripts/qa/stop_cet.sh                 # 停当前实例（PORT 指定的端口）
#   scripts/qa/stop_cet.sh --mock          # 只停 Mock
#   scripts/qa/stop_cet.sh --all           # 身份核不上时也按端口强制停止（危险，需明确）
#   scripts/qa/stop_cet.sh --list          # 列出所有登记实例
#   PORT=5174 scripts/qa/stop_cet.sh
#   ELT_USER_DB=/tmp/x/vocab.db PORT=5174 scripts/qa/stop_cet.sh
#
# 退出码：0 已停止且端口已释放｜1 未能停止/端口仍被占用｜3 身份不符，拒绝停止

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${PORT:-${ELT_PORT:-5173}}"
MOCK_PORT="${CET_MOCK_PORT:-5199}"
LOG_DIR="${CET_QA_LOG_DIR:-$ROOT/.cet-qa/logs}"
INSTANCES_DIR="$LOG_DIR/instances"
ONLY_MOCK=0
FORCE_ALL=0
LIST_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --mock) ONLY_MOCK=1 ;;
    --all)  FORCE_ALL=1 ;;
    --list) LIST_ONLY=1 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 64 ;;
  esac
done

info()  { printf '  %s\n' "$*"; }
warn()  { printf '  [!] %s\n' "$*"; }
err()   { printf '  [X] %s\n' "$*" >&2; }

# 规范化路径（macOS /tmp ↔ /private/tmp 要对得上）
norm_path() {
  local p="$1" dir base up rest
  dir="$(dirname "$p")"; base="$(basename "$p")"
  if [ -d "$dir" ]; then
    printf '%s/%s\n' "$(cd "$dir" 2>/dev/null && pwd -P)" "$base"
    return 0
  fi
  up="$dir"; rest="$base"
  while [ -n "$up" ] && [ "$up" != "/" ] && [ ! -d "$up" ]; do
    rest="$(basename "$up")/$rest"; up="$(dirname "$up")"
  done
  if [ -d "$up" ]; then
    printf '%s/%s\n' "$(cd "$up" 2>/dev/null && pwd -P)" "$rest"
  else
    printf '%s\n' "$p"
  fi
}

port_listener_pids() {
  lsof -nP -tiTCP:"$1" -sTCP:LISTEN 2>/dev/null | tr -d ' ' | sort -u
}

pid_listens_on() {
  local pid="$1" port="$2"
  port_listener_pids "$port" | grep -qx "$pid"
}

pid_cmd() { ps -p "$1" -o command= 2>/dev/null | head -1; }

# 打印进程命令行（不截断字节，避免破坏 UTF-8）
say_ps() { printf '        %s\n' "$(pid_cmd "$1")"; }

# 该 PID 是不是**本项目底座**？
# 只看「监听端口」不够：端口上可能是别人的服务（验收方也强调不能误杀）。
# 判据：命令行里出现本项目的后端入口 backend/fastapi_server.py。
pid_is_project_backend() {
  local cmd
  cmd="$(pid_cmd "$1")"
  [ -n "$cmd" ] || return 1
  case "$cmd" in
    *backend/fastapi_server.py*) return 0 ;;
  esac
  return 1
}

# 进程是否持有指定数据库文件（DB / WAL / SHM 任一）
pid_holds_db() {
  local pid="$1" db="$2" f
  for f in "$db" "$db-wal" "$db-shm"; do
    if lsof -nP -p "$pid" 2>/dev/null | grep -F -- "$f" >/dev/null 2>&1; then
      return 0
    fi
  done
  return 1
}

# 服务自己报告的库路径（/health → environment.sqlite.path）
health_db_path() {
  local port="$1" body
  body="$(curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" 2>/dev/null || true)"
  [ -n "$body" ] || return 1
  printf '%s' "$body" | grep -q '"sqlite"' || return 1
  printf '%s' "$body" | sed -n 's/.*"sqlite"[^}]*"path"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
}

instance_dir() { printf '%s/%s\n' "$INSTANCES_DIR" "$1"; }

# 从实例记录 / 共享 PID 文件 / 端口监听者收集**候选** PID（都还要过身份核验）
candidate_pids() {
  local port="$1" idir legacy pid
  idir="$(instance_dir "$port")"
  legacy="$LOG_DIR/server.pid"
  {
    [ -f "$idir/server.pid" ] && tr -dc '0-9' < "$idir/server.pid"
    printf '\n'
    [ -f "$legacy" ] && tr -dc '0-9' < "$legacy"
    printf '\n'
    port_listener_pids "$port"
  } | tr ' ' '\n' | sed '/^$/d' | sort -u
}

expected_db_for_port() {
  local port="$1" idir envf
  idir="$(instance_dir "$port")"; envf="$idir/instance.env"
  if [ -f "$envf" ]; then
    sed -n 's/^ELT_INSTANCE_DB=//p' "$envf" | head -1
    return 0
  fi
  if [ -n "${ELT_USER_DB:-}" ]; then
    printf '%s\n' "$ELT_USER_DB"
  fi
}

list_instances() {
  echo "== 已登记的实例 =="
  if [ ! -d "$INSTANCES_DIR" ]; then
    info "（无实例记录目录 ${INSTANCES_DIR}）"
    return 0
  fi
  local found=0 d port pid db alive
  for d in "$INSTANCES_DIR"/*/; do
    [ -d "$d" ] || continue
    found=1
    port="$(basename "$d")"
    pid="$(tr -dc '0-9' < "$d/server.pid" 2>/dev/null || true)"
    db="$(sed -n 's/^ELT_INSTANCE_DB=//p' "$d/instance.env" 2>/dev/null | head -1)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then alive="运行中"; else alive="已停止"; fi
    info "端口 $port  PID ${pid:-?}  $alive  DB ${db:-?}"
  done
  [ "$found" -eq 1 ] || info "（无实例记录）"
}

if [ "$LIST_ONLY" -eq 1 ]; then
  list_instances
  exit 0
fi

echo "== 停止四六级检测服务 =="
info "端口 ${PORT}（实例目录 $INSTANCES_DIR/${PORT}）"

# ── 底座 ────────────────────────────────────────────────
stop_server() {
  local port="$1"
  local want_db_raw want_db cands pid verified=0 mismatched=0
  want_db_raw="$(expected_db_for_port "$port")"
  want_db=""
  [ -n "$want_db_raw" ] && want_db="$(norm_path "$want_db_raw")"

  cands="$(candidate_pids "$port")"
  if [ -z "$cands" ]; then
    info "底座：无实例记录，且端口 $port 上没有监听进程"
  fi

  for pid in $cands; do
    if ! kill -0 "$pid" 2>/dev/null; then
      info "底座：候选 PID $pid 已不在"
      continue
    fi
    # 身份核验 1：必须真的监听目标端口
    if ! pid_listens_on "$pid" "$port"; then
      # 注意：不在这里做字节截断（cut -c 在 macOS 上按字节切，会把 UTF-8 命令切坏）
      warn "底座：PID $pid 不监听端口 $port —— 不是本实例，跳过"
      say_ps "$pid"
      mismatched=$((mismatched+1))
      continue
    fi
    # 身份核验 2：必须**是本项目底座**（端口上的可能是任何别人的服务）
    if ! pid_is_project_backend "$pid"; then
      err "底座：PID $pid 监听端口 ${port}，但不是本项目底座 —— 拒绝停止"
      say_ps "$pid"
      mismatched=$((mismatched+1))
      continue
    fi
    # 身份核验 3：必须使用实例登记的数据库
    if [ -n "$want_db" ]; then
      local hdb=""
      hdb="$(health_db_path "$port" || true)"
      if [ -n "$hdb" ]; then
        if [ "$(norm_path "$hdb")" != "$want_db" ]; then
          err "底座：PID $pid 监听端口 ${port}，但用的是 ${hdb}，不是本次目标 $want_db —— 拒绝停止"
          mismatched=$((mismatched+1))
          continue
        fi
      elif ! pid_holds_db "$pid" "$want_db"; then
        err "底座：PID $pid 监听端口 ${port}，但未持有目标库 $want_db —— 拒绝停止"
        mismatched=$((mismatched+1))
        continue
      fi
    fi
    # 到这里身份成立：端口 + 库都对
    info "底座：身份核验通过（PID $pid 监听 ${port}，DB ${want_db:-未登记}）"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      info "底座：进程 $pid 未响应 TERM，改用 KILL"
      kill -9 "$pid" 2>/dev/null || true
      sleep 0.5
    fi
    info "底座：已停止（PID ${pid}）"
    rm -f "$(instance_dir "$port")/server.pid"
    verified=1
  done

  if [ "$verified" -eq 0 ]; then
    if [ "$mismatched" -gt 0 ]; then
      err "底座：候选进程身份均不符，**没有停止任何进程**"
      info "若确认端口 $port 上就是要停的实例，显式加 --all"
      return 3
    fi
    info "底座：无匹配实例（可能未启动）"
  fi
  return 0
}

# ── Mock ────────────────────────────────────────────────
stop_mock() {
  local port="$1" idir pid cands found=0
  idir="$INSTANCES_DIR/mock-$port"
  cands="$( { [ -f "$idir/mock.pid" ] && tr -dc '0-9' < "$idir/mock.pid"; printf '\n'; port_listener_pids "$port"; } \
            | tr ' ' '\n' | sed '/^$/d' | sort -u )"
  for pid in $cands; do
    kill -0 "$pid" 2>/dev/null || continue
    pid_listens_on "$pid" "$port" || continue
    info "Mock API：身份核验通过（PID $pid 监听 ${port}）"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    kill -0 "$pid" 2>/dev/null && { info "Mock API：改用 KILL"; kill -9 "$pid" 2>/dev/null || true; sleep 0.3; }
    info "Mock API：已停止（PID ${pid}）"
    rm -f "$idir/mock.pid"
    found=1
  done
  [ "$found" -eq 1 ] || info "Mock API：无匹配实例（可能未启动）"
  return 0
}

rc=0
if [ "$ONLY_MOCK" -eq 0 ]; then
  stop_server "$PORT"; r1=$?
  # 3（身份不符，拒绝停止）优先级最高：后面的端口残留检查只会把它降级成 1，
  # 而「拒绝误杀」比「端口仍占用」更该被调用方看见。
  [ "$r1" -eq 3 ] && rc=3
fi
stop_mock "$MOCK_PORT" || true

# ── 端口残留检查：没真正释放就是失败，不再报成功 ────────
check_port() {
  local label="$1" port="$2"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    if [ "$FORCE_ALL" -eq 1 ]; then
      local pids
      pids="$(port_listener_pids "$port" | tr '\n' ' ')"
      warn "${label}：端口 $port 仍被占用（PID ${pids}），按 --all 强制停止"
      # shellcheck disable=SC2086
      kill -9 $pids 2>/dev/null || true
      sleep 0.5
      if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        err "${label}：端口 $port 仍被占用 —— 需要手工处理"
        return 1
      fi
      info "${label}：端口 $port 已释放（--all 强制）"
    else
      err "${label}：端口 $port 仍被其它进程占用："
      lsof -nP -iTCP:"$port" -sTCP:LISTEN | tail -n +2 | sed 's/^/      /' >&2
      info "如确认可停，用 scripts/qa/stop_cet.sh --all"
      return 1
    fi
  else
    info "${label}：端口 $port 已释放"
  fi
  return 0
}

[ "$ONLY_MOCK" -eq 0 ] && { check_port "底座" "$PORT" || { [ "$rc" -eq 3 ] || rc=1; }; }
check_port "Mock API" "$MOCK_PORT" || { [ "$rc" -eq 3 ] || rc=1; }

if [ "$rc" -eq 0 ]; then
  cat <<EOF

== 状态 ==
  已停止，端口已释放，可直接再次启动：
    scripts/qa/start_cet.sh --mock
  数据保留（未删除，重启后可验证恢复）
  查看所有实例：scripts/qa/stop_cet.sh --list
EOF
else
  cat <<EOF

== 状态 ==
  未能干净停止（退出码 ${rc}）：
    3 = 身份不符，拒绝停止（没有任何进程被误杀）
    1 = 端口仍被占用
  查实例：scripts/qa/stop_cet.sh --list
EOF
fi
exit "$rc"
