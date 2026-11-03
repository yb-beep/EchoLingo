#!/usr/bin/env bash
# 四六级听力黑匣子 · 启动底座 + 启动检查（C 负责）
#
# 设计目标（任务书 T01 / D3 换电脑验收）：一条命令从停止状态把环境拉起来，
# 数据与端口都隔离，不碰用户原有学习记录。
#
# 用法：
#   scripts/qa/start_cet.sh              # 检查环境 → 后台启动底座
#   scripts/qa/start_cet.sh --mock       # 另外拉起 Mock API（前端可用）
#   scripts/qa/start_cet.sh --seed       # 顺手预置一节样例课（做有内容的 E2E）
#   scripts/qa/start_cet.sh --fg         # 前台启动（看日志用，Ctrl-C 退出）
#   PORT=5174 scripts/qa/start_cet.sh    # 换端口（多人同机时）
#
# 退出码：0 启动成功｜1 环境检查有阻断项｜2 端口被占用

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PORT="${PORT:-${ELT_PORT:-5173}}"
MOCK_PORT="${CET_MOCK_PORT:-5199}"
DATA_DIR="${ELT_DATA_DIR:-$ROOT/.cet-qa/data}"
LOG_DIR="$ROOT/.cet-qa/logs"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
WITH_MOCK=0
FOREGROUND=0

for arg in "$@"; do
  case "$arg" in
    --mock) WITH_MOCK=1 ;;
    --mock-only) WITH_MOCK=1; MOCK_ONLY=1 ;;
    --seed) WITH_SEED=1 ;;
    --fg)   FOREGROUND=1 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 64 ;;
  esac
done

MOCK_ONLY="${MOCK_ONLY:-0}"
WITH_SEED="${WITH_SEED:-0}"

info() { printf '  %s\n' "$*"; }
fail() { printf 'X  %s\n' "$*" >&2; exit 1; }

echo "== 四六级听力黑匣子 · 启动 =="

# ── 1. 解释器 ──
if [ ! -x "$PY" ]; then
  cat >&2 <<'VENVHELP'
X  未找到虚拟环境解释器 .venv/bin/python

这是新克隆的仓库第一次启动时的正常状态（.venv 不入库）。按顺序执行：

  # 1) 建 Python 3.11 虚拟环境（系统自带 python3 是 3.9，不能用）
  python3.11 -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # 首次约 10-15 分钟

  # 2) 复制配置
  cp .env.example .env

  # 3) 再跑本脚本
  scripts/qa/start_cet.sh --mock

若本机没有 python3.11：brew install python@3.11，或用 uv python install 3.11。
详见 docs/qa/启动与测试说明.md 第 2 节。
VENVHELP
  exit 1
fi
info "解释器：$PY ($("$PY" -V 2>&1))"

# ── 2. 环境检查（阻断项直接退出，避免带着坏环境启动再查半天）──
echo "-- 环境检查 --"
if ! "$PY" scripts/qa/check_env.py --skip-service; then
  code=$?
  if [ "$code" -eq 1 ]; then
    fail "环境检查有阻断项。先按上面提示修好，再重新启动。"
  fi
  info "环境检查有警告（无阻断项），继续启动"
fi

# ── 3. 端口 ──
# 底座端口被占时不再一律失败：若占用者就是本项目已跑着的底座，
# 只补启动 Mock 是合理操作（单独重启 Mock 验证状态、只重启替身给前端用）。
echo "-- 端口 --"
SKIP_SERVER=0
if [ "${MOCK_ONLY}" -eq 1 ]; then
  info "--mock-only：只启动 Mock，不动底座"
  SKIP_SERVER=1
elif lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    info "端口 ${PORT} 上已有本项目底座在响应，跳过底座启动，只补未起的那部分"
    SKIP_SERVER=1
  else
    echo "  端口 ${PORT} 已被其它进程占用："
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN | tail -n +2 | sed 's/^/    /'
    fail "换一个端口：PORT=5174 scripts/qa/start_cet.sh"
  fi
else
  info "端口 ${PORT} 空闲"
fi

# ── 4. 数据目录与配置（隔离，不用用户正式数据）──
mkdir -p "${DATA_DIR}" "${LOG_DIR}" "${ROOT}/.cet-qa/media"

export ELT_HOST="${ELT_HOST:-127.0.0.1}"
export ELT_PORT="${PORT}"
export ELT_USER_DB="${ELT_USER_DB:-${DATA_DIR}/vocab.db}"
export ELT_CONFIG_DIR="${ELT_CONFIG_DIR:-${DATA_DIR}}"

# 读取 .env 但只补充未设置的变量，不覆盖调用方显式传入的值
if [ -f "${ROOT}/.env" ]; then
  while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    if [ -z "${!key:-}" ]; then export "$key=$value"; fi
  done < "${ROOT}/.env"
fi

info "数据目录：${ELT_USER_DB}"
info "监听：http://${ELT_HOST}:${PORT}"

# ── 5. 启动 ──
if [ "${SKIP_SERVER}" -eq 1 ]; then
  echo "-- 跳过底座启动 --"
elif [ "${FOREGROUND}" -eq 1 ]; then
  echo "-- 前台启动（Ctrl-C 停止）--"
  exec "${PY}" backend/fastapi_server.py
fi

if [ "${SKIP_SERVER}" -eq 1 ]; then
  SERVER_LOG="${LOG_DIR}/server.log"
else
SERVER_LOG="${LOG_DIR}/server.log"
: > "${SERVER_LOG}"
nohup "${PY}" backend/fastapi_server.py >> "${SERVER_LOG}" 2>&1 &
SERVER_PID=$!
echo "${SERVER_PID}" > "${LOG_DIR}/server.pid"
info "底座进程 PID=${SERVER_PID}，日志 ${SERVER_LOG}"
fi

# ── 6. 等到真的能响应再报成功（起得来和能服务是两件事）──
if [ "${SKIP_SERVER}" -eq 1 ]; then
  if curl -fsS --max-time 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1; then
    info "底座已在响应，跳过等待"
  else
    info "底座未运行且本次跳过启动"
  fi
  ready=1
else
echo "-- 等待服务就绪 --"
ready=0
for i in $(seq 1 40); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/tmp/cet-health.json 2>/dev/null; then
    ready=1
    break
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "  进程已退出，日志尾部："
    tail -25 "$SERVER_LOG" | sed 's/^/    /'
    fail "底座启动失败（见上）。"
  fi
  sleep 1
done

if [ "$ready" -ne 1 ]; then
  tail -25 "${SERVER_LOG}" | sed 's/^/    /'
  fail "底座 40 秒内未就绪。看日志：${SERVER_LOG}"
fi
if [ "${SKIP_SERVER}" -eq 0 ]; then
  info "就绪：http://127.0.0.1:${PORT}/health"
fi
fi

# ── 7. 可选 Mock ──
if [ "$WITH_MOCK" -eq 1 ]; then
  echo "-- 启动 Mock API --"
  if lsof -nP -iTCP:"$MOCK_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    info "Mock 端口 $MOCK_PORT 已占用，跳过（可能已在运行）"
  else
    MOCK_LOG="$LOG_DIR/mock.log"
    : > "$MOCK_LOG"
    CET_MOCK_PORT="$MOCK_PORT" nohup "$PY" tests/mock_cet_api.py >> "$MOCK_LOG" 2>&1 &
    MOCK_PID=$!
    echo "$MOCK_PID" > "$LOG_DIR/mock.pid"
    for i in $(seq 1 20); do
      if curl -fsS --max-time 2 "http://127.0.0.1:$MOCK_PORT/api/cet/_mock/info" >/dev/null 2>&1; then
        info "Mock 就绪：http://127.0.0.1:$MOCK_PORT （PID=${MOCK_PID}，日志 ${MOCK_LOG}）"
        MOCK_PORT_OK=1
        break
      fi
      sleep 1
    done
    if [ "${MOCK_PORT_OK:-0}" -ne 1 ]; then
      tail -15 "$MOCK_LOG" | sed 's/^/    /'
      info "Mock 未就绪（底座可用，Mock 故障单独排查）"
    fi
  fi
fi

# ── 8. 可选：预置数据 ──
if [ "${WITH_SEED}" -eq 1 ] && [ "${SKIP_SERVER}" -eq 0 ]; then
  echo "-- 预置底座数据 --"
  if "${PY}" scripts/qa/seed_base_data.py 2>&1 | tail -6 | sed 's/^/  /'; then
    info "数据预置完成"
  else
    info "数据预置失败（不影响服务启动，单独排查）"
  fi
elif [ "${WITH_SEED}" -eq 1 ]; then
  info "--seed 需要本次启动底座才生效；当前跳过了底座启动"
fi

cat <<EOF

== 启动完成 ==
  底座页面    http://127.0.0.1:$PORT/
  API 文档    http://127.0.0.1:$PORT/api/docs   （依赖 CDN，离线会白屏）
  健康检查    http://127.0.0.1:$PORT/health
$( [ "$WITH_MOCK" -eq 1 ] && printf '  Mock API    http://127.0.0.1:%s/api/cet/_mock/docs\n' "$MOCK_PORT" )
  日志        $LOG_DIR/
  停止        scripts/qa/stop_cet.sh

下一步：.venv/bin/python -m pytest tests/test_cet_contract.py -v
EOF
