"""测试套件公共 fixture。

本文件除默认设置外，还有一处**非默认**的数据隔离护栏，用于修
2026-11-05 实测发现的真实污染问题。改动前请先读下面这段说明。

---

## 为什么有数据隔离护栏（实测事故）

不加任何隔离直接 `pytest -q`，用户正式库 `resources/vocab.db` 会被测试数据污染。
实测证据：

| 现象 | 证据 |
| --- | --- |
| 正式库 md5 变化 | `d2c9f472…` → `81316370…` |
| 全量跑一次后表行数增长 | `v2_media_uploads` 从 5 行涨到 30 行（25 条测试残留） |

### 根因（两条，必须同时处理）

**A. 仓库根 `.env` 在 import 期注入 `ELT_USER_DB`**

`.env` 里写着 `ELT_USER_DB=<仓库>/.cet-qa/data/vocab.db`。
`backend/analyzer.py` 与 `webapp/runtime/ai_config.py` 在 import 期就执行
`load_dotenv()`，把它写进 `os.environ`。

**B. `ELT_USER_DB` 的优先级高于测试的 monkeypatch**

`db.current_db_path()` 解析顺序是 **contextvar → ELT_USER_DB → DB_PATH**。
绝大多数测试用 `monkeypatch.setattr(db, "DB_PATH", tmp_path/"vocab.db")` 隔离，
但 env 优先级更高 —— monkeypatch 被静默覆盖，所有测试写进同一个共享库。

### 处理方式（三道，缺一不可）

1. import 期摘掉 `ELT_USER_DB`
2. `db.DB_PATH` 指向**正式库的副本**（不是空库：部分测试依赖 `v2_tags` 的
   30 行种子数据，用空库会让 30+ 个用例集体失败，实测 61 failed）
3. 每个用例开始前再摘一次 env（防用例内部 import 重新触发 `load_dotenv`）

用例自己写的 `monkeypatch.setattr(db, "DB_PATH", ...)` 在本 fixture 之后执行，
仍可正常覆盖 —— 既有测试写法完全不受影响。

---

## 跑测试的正确姿势

```bash
# 正确：ELT_CONFIG_DIR 指向空目录 → 不加载 .env
ELT_CONFIG_DIR=/tmp/<x> .venv/bin/python -m pytest -q \
  --ignore=tests/test_cet_listening_ui.py --ignore=tests/test_cet_contract.py
# 基线：2 failed / 709 passed / 28 skipped
# 2 项失败是环境问题（bdpan CLI 未装、planning 私有模块缺失），非产品缺陷

# 错误：用 ELT_USER_DB 跑 pytest
#   → 覆盖 monkeypatch，测试互相污染（几十个无关用例集体失败）

# 起服务做数据隔离时仍要用 ELT_USER_DB（那是正确用法，与本文件无关）
```
"""
import os

# 测试套件默认禁用 Docling 子进程（torch 加载慢且本机才装）；
# 需要真实 Docling 的测试自行 monkeypatch 打开并跳过不可用环境。
os.environ.setdefault("ELT_DOCLING", "off")

import shutil
import sys
import tempfile
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

_PRODUCTION_DB = (Path(__file__).resolve().parents[1] / "resources" / "vocab.db").resolve()

# ── 护栏第 1 道：import 期摘掉 .env 注入的 ELT_USER_DB ──
os.environ.pop("ELT_USER_DB", None)

# ── 护栏第 2 道：DB_PATH 指向正式库副本 ──
_SESSION_DB = Path(tempfile.mkdtemp(prefix="echolingo-pytest-")) / "vocab.db"
if _PRODUCTION_DB.exists():
    shutil.copy2(_PRODUCTION_DB, _SESSION_DB)

try:
    import db as _db_module  # noqa: E402  (需 sys.path 就绪)
except ImportError:  # pragma: no cover - 后端不可导入时其它用例会自然失败
    _db_module = None
else:
    if Path(_db_module.DB_PATH).resolve() == _PRODUCTION_DB:
        _db_module.DB_PATH = _SESSION_DB


@pytest.fixture(autouse=True)
def _isolate_from_env_db(monkeypatch):
    """护栏第 3 道：每个用例开始前再摘一次 env。

    防用例内部 import 触发 `load_dotenv()` 重新注入 ELT_USER_DB。
    用例自己的 monkeypatch 在本 fixture 之后执行，仍可覆盖。
    """
    monkeypatch.delenv("ELT_USER_DB", raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """每个测试前清零内存限流器：register/login 的 IP 限额是全局状态，
    不重置会让整套件里大量注册/登录用例在 10 次后集体 429。"""
    try:
        from webapp.runtime import rate_limit
    except ImportError:
        yield
        return
    rate_limit._reset_all()
    yield
    rate_limit._reset_all()
