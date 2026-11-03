#!/usr/bin/env bash
# 四六级听力黑匣子 · 停止服务（C 负责）
#
# 任务书要求「检查服务停止后能正常再次启动」。这个脚本按 PID 文件停止，
# 并在停止后确认端口真的释放了 —— 否则下次启动会撞端口。
#
# 用法：
#   scripts/qa/stop_cet.sh           # 停底座与 Mock
#   scripts/qa/stop_cet.sh --mock    # 只停 Mock
#   scripts/qa/stop_cet.sh --all     # 连同占用端口的残留进程一起停
#   PORT=5174 scripts/qa/stop_cet.sh

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PORT="${PORT:-${ELT_PORT:-5173}}"
MOCK_PORT="${CET_MOCK_PORT:-5199}"
LOG_DIR="$ROOT/.cet-qa/logs"
ONLY_MOCK=0
FORCE_ALL=0

for arg in "$@"; do
  case "$arg" in
    --mock) ONLY_MOCK=1 ;;
    --all)  FORCE_ALL=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 64 ;;
  esac
done

info() { printf '  %s\n' "$*"; }

stop_by_pidfile() {
  local label="$1" pidfile="$2" port="$3"
  if [ ! -f "$pidfile" ]; then
    info "${label}：无 PID 文件（可能不是本脚本启动的）"
    return 1
  fi
  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
    info "${label}：进程 $pid 已不在"
    rm -f "$pidfile"
    return 1
  fi
  kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.25
  done
  if kill -0 "$pid" 2>/dev/null; then
    info "${label}：进程 $pid 未响应 TERM，改用 KILL"
    kill -9 "$pid" 2>/dev/null || true
    sleep 0.5
  fi
  info "${label}：已停止（PID ${pid}）"
  rm -f "$pidfile"
  return 0
}

echo "== 停止四六级检测服务 =="

if [ "$ONLY_MOCK" -eq 0 ]; then
  stop_by_pidfile "底座" "$LOG_DIR/server.pid" "$PORT"
fi
stop_by_pidfile "Mock API" "$LOG_DIR/mock.pid" "$MOCK_PORT"

# ── 端口残留检查：PID 文件丢了但端口还占着，是最坑的情况 ──
check_port() {
  local label="$1" port="$2"
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    if [ "$FORCE_ALL" -eq 1 ]; then
      local pids
      pids="$(lsof -nP -tiTCP:"$port" -sTCP:LISTEN | tr '\n' ' ')"
      info "${label}：端口 $port 仍被占用（PID ${pids}），按 --all 强制停止"
      # shellcheck disable=SC2086
      kill -9 $pids 2>/dev/null || true
      sleep 0.5
      if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
        info "${label}：端口 $port 仍被占用 —— 需要手工处理"
        return 1
      fi
      info "${label}：端口 $port 已释放"
    else
      info "${label}：端口 $port 仍被其它进程占用："
      lsof -nP -iTCP:"$port" -sTCP:LISTEN | tail -n +2 | sed 's/^/      /'
      info "如确认可停，用 scripts/qa/stop_cet.sh --all"
      return 1
    fi
  else
    info "${label}：端口 $port 已释放"
  fi
  return 0
}

[ "$ONLY_MOCK" -eq 0 ] && check_port "底座" "$PORT"
check_port "Mock API" "$MOCK_PORT"

cat <<EOF

== 状态 ==
  停止后可直接再次启动：
    scripts/qa/start_cet.sh --mock
  数据保留在 $ROOT/.cet-qa/data（未删除，重启后可验证恢复）
EOF
