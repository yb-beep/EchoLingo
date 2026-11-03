#!/usr/bin/env bash
# 四六级听力黑匣子 · 干净机器验收演练（C 负责，D3 硬性要求的可行替代）
#
# 任务书 D3 第 2 条要求在「另一台电脑」按说明从头启动并操作同一段样例。
# 没有第二台实体机时，这个脚本用**全新克隆 + 全新虚拟环境 + 全新数据目录**
# 模拟一台干净机器，把「照着文档能不能装起来」这件事真正跑一遍。
#
# 它和真实换机的差别（必须如实知道）：
#   ✅ 验证：文档完整性、依赖能否装、数据目录是否隔离、启动是否成功、
#            音频能否就位、契约测试能否通过、合规是否干净
#   ❌ 不验证：不同操作系统（Windows）、不同硬件、系统级依赖（FFmpeg 预装）、
#            网速、另一台机器的 Python 版本差异
#
# 用法：
#   scripts/qa/rehearse_clean_machine.sh              # 完整演练（会装依赖，约 10-15 分钟）
#   scripts/qa/rehearse_clean_machine.sh --fast       # 复用已有 wheel 缓存，跳过浏览器检查
#   scripts/qa/rehearse_clean_machine.sh --keep       # 保留演练目录供排查
#
# 退出码：0 演练通过｜1 有步骤失败｜2 前置不满足

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REHEARSE_DIR="${REHEARSE_DIR:-/tmp/cet-clean-machine}"
FAST=0
KEEP=0
PORT="${REHEARSE_PORT:-5188}"
MOCK_PORT="${REHEARSE_MOCK_PORT:-5208}"

for arg in "$@"; do
  case "$arg" in
    --fast) FAST=1 ;;
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 64 ;;
  esac
done

STEPS=()
FAILED=0

info() { printf '  %s\n' "$*"; }
step_ok()   { local name="${1:-未命名}"; STEPS+=("OK|${name}"); printf '  [OK] %s\n' "$name"; }
# set -u 下 $2 未传会直接炸，之前正是这个原因让演练在「音频就位」后中断，
# 而且掩盖了真正的样例失败。所有位置参数都要给缺省值。
step_fail() {
  local name="${1:-未命名}"
  local detail="${2:-}"
  STEPS+=("X|${name}|${detail}")
  FAILED=$((FAILED+1))
  printf '  [X ] %s  %s\n' "$name" "$detail"
}

cleanup() {
  if [ "$KEEP" -eq 0 ] && [ "${KEEP_ON_FAIL:-0}" -eq 0 ]; then
    info "清理演练目录 $REHEARSE_DIR"
    rm -rf "$REHEARSE_DIR"
  else
    info "保留演练目录：$REHEARSE_DIR"
  fi
}

echo "== 四六级听力黑匣子 · 干净机器验收演练 =="
echo "源仓库   $ROOT"
echo "演练目录 $REHEARSE_DIR"
echo "端口     ${PORT}（底座）/ ${MOCK_PORT}（Mock）"
echo "模式     $([ "$FAST" -eq 1 ] && echo '快速（复用 wheel 缓存）' || echo '完整')"
echo "----------------------------------------------------------------------"

# ── 0. 前置 ──
echo "-- 前置检查 --"
command -v python3.11 >/dev/null 2>&1 || { echo "X  未找到 python3.11" >&2; exit 2; }
command -v git >/dev/null 2>&1 || { echo "X  未找到 git" >&2; exit 2; }
command -v ffmpeg >/dev/null 2>&1 || { echo "X  未找到 ffmpeg" >&2; exit 2; }
step_ok "前置工具齐全（python3.11 / git / ffmpeg）"

rm -rf "$REHEARSE_DIR"
mkdir -p "$REHEARSE_DIR"

# ── 1. 全新克隆（模拟拿到交付包）──
echo "-- 1. 全新克隆 --"
if git clone -q "$ROOT" "$REHEARSE_DIR/repo" 2>/dev/null; then
  step_ok "克隆成功"
else
  step_fail "克隆失败" "无法从 $ROOT 克隆"
  KEEP_ON_FAIL=1; cleanup; exit 1
fi
cd "$REHEARSE_DIR/repo" || { step_fail "进入克隆目录失败"; exit 1; }

# 克隆里不该带这些
LEAKS=""
for x in .venv .env .cet-qa; do
  [ -e "$x" ] && LEAKS="$LEAKS $x"
done
if [ -z "$LEAKS" ]; then
  step_ok "克隆中不含 .venv / .env / .cet-qa（符合交付约定）"
else
  step_fail "克隆中混入了不该有的内容" "$LEAKS"
fi

# 音频不该入库
if git ls-files | grep -qE '\.(mp3|wav|m4a|flac)$'; then
  step_fail "音频被提交进仓库" "应靠本地映射"
else
  step_ok "仓库不含音频（按约定靠本地映射）"
fi

# ── 2. 按文档建虚拟环境 ──
echo "-- 2. 建虚拟环境（照文档操作）--"
if python3.11 -m venv .venv >/dev/null 2>&1; then
  step_ok "venv 创建成功"
else
  step_fail "venv 创建失败"
  KEEP_ON_FAIL=1; cleanup; exit 1
fi
PY="$REHEARSE_DIR/repo/.venv/bin/python"

if [ "$FAST" -eq 1 ]; then
  info "快速模式：复用 pip 缓存"
fi
if "$PY" -m pip install -q -U pip >/dev/null 2>&1; then
  step_ok "pip 升级成功"
else
  step_fail "pip 升级失败"
fi

echo "  安装依赖（首次较慢，请等待）…"
if "$PY" -m pip install -q -r requirements.txt -r requirements-dev.txt >/tmp/cet-rehearse-pip.log 2>&1; then
  step_ok "依赖安装成功"
else
  step_fail "依赖安装失败" "见 /tmp/cet-rehearse-pip.log 末尾"
  tail -8 /tmp/cet-rehearse-pip.log | sed 's/^/      /'
  KEEP_ON_FAIL=1; cleanup; exit 1
fi

# ── 3. 配置 ──
echo "-- 3. 配置与环境检查 --"
cp .env.example .env 2>/dev/null && step_ok "复制 .env.example" || step_fail "复制 .env.example 失败"

# 音频就位（文档第 3 步）
if "$PY" scripts/qa/prepare_audio.py --copy-from \
     "$ROOT/.cet-qa/media/cet6-202512-conv2.mp3" >/tmp/cet-rehearse-audio.log 2>&1; then
  step_ok "样例音频就位（哈希已核对）"
else
  step_fail "样例音频就位失败" "见 /tmp/cet-rehearse-audio.log"
  tail -5 /tmp/cet-rehearse-audio.log | sed 's/^/      /'
fi

# copy-from 需要源存在；若源不存在则用环境变量指向
if [ ! -d ".cet-qa/media" ] || [ -z "$(ls -A .cet-qa/media 2>/dev/null)" ]; then
  export CET_QA_MEDIA="$ROOT/.cet-qa/media/cet6-202512-conv2.mp3"
  info "改用 CET_QA_MEDIA 指向源音频"
fi

# 环境检查（不能用本仓库的端口，避开正在跑的服务）
if ELT_PORT="$PORT" "$PY" scripts/qa/check_env.py --json >/tmp/cet-rehearse-env.json 2>&1; then
  step_ok "环境检查无阻断"
elif [ $? -eq 2 ]; then
  step_ok "环境检查仅有警告（无阻断）"
else
  step_fail "环境检查有阻断项"
  grep -i "阻断" /tmp/cet-rehearse-env.json | head -4 | sed 's/^/      /'
fi

# ── 4. 启动 ──
echo "-- 4. 启动服务 --"
export PORT ELT_PORT="$PORT" CET_MOCK_PORT="$MOCK_PORT"
if ./scripts/qa/start_cet.sh --mock >/tmp/cet-rehearse-start.log 2>&1; then
  step_ok "启动成功（底座 $PORT + Mock ${MOCK_PORT}）"
else
  step_fail "启动失败" "见 /tmp/cet-rehearse-start.log"
  tail -12 /tmp/cet-rehearse-start.log | sed 's/^/      /'
  KEEP_ON_FAIL=1; cleanup; exit 1
fi

# 真能响应才算数
if curl -fsS --max-time 5 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  step_ok "底座 /health 可响应"
else
  step_fail "底座 /health 无响应"
fi
if curl -fsS --max-time 5 "http://127.0.0.1:$MOCK_PORT/api/cet/_mock/info" >/dev/null 2>&1; then
  step_ok "Mock 可响应"
else
  step_fail "Mock 无响应"
fi

# ── 5. 跑测试 ──
echo "-- 5. 在干净环境跑测试 --"
if "$PY" -m pytest tests/test_cet_contract.py -q >/tmp/cet-rehearse-test.log 2>&1; then
  PASSED="$(grep -oE '[0-9]+ passed' /tmp/cet-rehearse-test.log | tail -1)"
  step_ok "契约测试通过（${PASSED}）"
else
  step_fail "契约测试失败" "见 /tmp/cet-rehearse-test.log"
  tail -10 /tmp/cet-rehearse-test.log | sed 's/^/      /'
fi

if "$PY" tests/fixtures/cet/build_fixtures.py --check >/dev/null 2>&1; then
  step_ok "契约样例自校验通过"
else
  step_fail "契约样例自校验失败"
fi

if "$PY" scripts/qa/gen_api_spec.py --check >/dev/null 2>&1; then
  step_ok "接口规格与实测一致"
else
  info "接口规格不一致（不影响验收，属信息项）"
fi

if [ "$FAST" -eq 0 ]; then
  if "$PY" scripts/qa/browser_check.py >/tmp/cet-rehearse-browser.log 2>&1; then
    step_ok "浏览器层检查通过"
  else
    info "浏览器层检查未通过（可能是本机无 Chrome；不阻断验收）"
    tail -5 /tmp/cet-rehearse-browser.log | sed 's/^/      /'
  fi
fi

# ── 6. 合规 ──
echo "-- 6. 合规检查 --"
if "$PY" scripts/qa/audit_delivery.py >/tmp/cet-rehearse-audit.log 2>&1; then
  step_ok "合规审计无阻断"
else
  step_fail "合规审计有阻断项"
  grep -E "^\[X" /tmp/cet-rehearse-audit.log | head -5 | sed 's/^/      /'
fi

# ── 7. 停止 ──
echo "-- 7. 停止并验证可重启 --"
if PORT="$PORT" ./scripts/qa/stop_cet.sh >/dev/null 2>&1; then
  step_ok "服务已停止"
else
  info "停止脚本返回非 0（可能无残留进程）"
fi
if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  step_fail "停止后端口仍在响应"
else
  step_ok "停止后端口已释放"
fi

# ── 汇总 ──
OK_COUNT=0; for s in "${STEPS[@]}"; do case "$s" in OK\|*) OK_COUNT=$((OK_COUNT+1));; esac; done
echo "----------------------------------------------------------------------"
echo "演练结果：$OK_COUNT 项通过，$FAILED 项失败"
if [ "$FAILED" -ne 0 ]; then
  echo
  echo "失败项："
  for s in "${STEPS[@]}"; do
    case "$s" in X\|*) echo "  · $(echo "$s" | cut -d'|' -f2)  $(echo "$s" | cut -d'|' -f3)";; esac
  done
fi
echo
echo "演练目录：${REHEARSE_DIR}（--keep 可保留）"
echo "日志：/tmp/cet-rehearse-*.log"

KEEP_ON_FAIL=$([ "$FAILED" -ne 0 ] && echo 1 || echo 0)
cleanup

[ "$FAILED" -eq 0 ] && exit 0 || exit 1
