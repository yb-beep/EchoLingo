"""四六级底座 · 启动/停止脚本边界回归（BASE-04 返修）。

对应独立验收报告 `docs/acceptance/2026-11-05-79e9e56-底座独立验收.md`
§「必须返修」BASE-04 的三个子问题：

| 子问题 | 原缺陷 | 本文件守住的契约 |
| --- | --- | --- |
| A | `run.sh --check --fresh --data-dir <临时库>` 删掉了真实构造的 sentinel 库，还报「检查通过」 | 只检查模式零副作用：不删库、不动 WAL/SHM、不建目录、不起服务 |
| B | 同库服务复用判断在删库之前，允许「复用活服务」后删除它的 DB/WAL/SHM | 清库前验证所有权与停服状态；库被活服务持有时拒绝并返回 3 |
| C | `stop_cet.sh` 只读共享 `server.pid`，不核对端口：`PORT=A stop` 杀了 B，A 还活着且退出码 0 | PID/端口/库按实例绑定；停止前核验身份；身份不符拒绝(3)不误杀 |

纪律（与验收方一致）：
    **所有破坏性用例只用本文件自建的临时数据**（pytest `tmp_path`），
    绝不对 `resources/vocab.db`、主工作区服务或用户实例执行任何破坏性操作。

运行：
    ELT_CONFIG_DIR=/tmp/<x> .venv/bin/python -m pytest tests/test_cet_base_startup.py -q
    # 不要设 ELT_USER_DB（会覆盖 conftest 的 monkeypatch 护栏）
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
RUN_SH = REPO / "scripts" / "cet" / "run.sh"
START_SH = REPO / "scripts" / "qa" / "start_cet.sh"
STOP_SH = REPO / "scripts" / "qa" / "stop_cet.sh"
PY = REPO / ".venv" / "bin" / "python"

pytestmark = pytest.mark.skipif(
    not RUN_SH.exists() or not STOP_SH.exists(), reason="启动/停止脚本不存在"
)


# ── 工具 ────────────────────────────────────────────────


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_sentinel(db: Path, marker: str = "keep this test record") -> None:
    """在临时库里放一张哨兵表 —— 只要库还在、表还在，就说明没被删。"""
    db.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS audit_sentinel(value TEXT)")
        conn.execute("INSERT INTO audit_sentinel VALUES (?)", (marker,))
        conn.commit()


def _sentinel_rows(db: Path):
    """只读打开（mode=ro），不写库；库不存在返回 None。"""
    if not db.exists():
        return None
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return [r[0] for r in conn.execute("SELECT value FROM audit_sentinel")]


def _run(cmd, cwd, env=None, timeout=240):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        env={**os.environ, "ELT_CONFIG_DIR": os.environ.get("ELT_CONFIG_DIR", "/tmp/cet-startup-cfg"), **(env or {})},
    )


def _start_backend(data_dir: Path, port: int):
    """起一个真实底座进程（隔离库），等到 /health 可响应才返回。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    log = open(data_dir / "server.log", "wb")
    proc = subprocess.Popen(
        [str(PY), "backend/fastapi_server.py"], cwd=str(REPO),
        env={**os.environ, "ELT_USER_DB": str(data_dir / "vocab.db"),
             "ELT_CONFIG_DIR": str(data_dir / "config"),
             "ELT_PORT": str(port), "ELT_HOST": "127.0.0.1"},
        stdout=log, stderr=subprocess.STDOUT,
    )
    for _ in range(160):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                if resp.status == 200:
                    return proc
        except Exception:  # noqa: BLE001
            pass
        if proc.poll() is not None:
            raise AssertionError(f"底座启动即退出，日志：{(data_dir / 'server.log').read_text()[-1200:]}")
        time.sleep(0.25)
    proc.kill()
    raise AssertionError("底座 40 秒内未就绪")


def _stop_backend(proc) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _alive(proc) -> bool:
    return proc.poll() is None


@pytest.fixture
def backend():
    """起一个临时底座，测试结束无论成败都收掉。"""
    started = []

    def _factory(data_dir: Path, port: int):
        proc = _start_backend(data_dir, port)
        started.append(proc)
        return proc

    yield _factory
    for proc in started:
        _stop_backend(proc)


@pytest.fixture
def stop_fixture(tmp_path):
    """停止脚本的最小可运行骨架：只有脚本本体与空的日志目录。

    复制脚本而不是原地跑，是为了确保用例不会碰到仓库里真实的
    `.cet-qa/logs`（那里可能有开发者正在用的实例记录）。
    """
    fixture = tmp_path / "stop-fixture"
    (fixture / "scripts" / "qa").mkdir(parents=True)
    (fixture / ".cet-qa" / "logs" / "instances").mkdir(parents=True)
    shutil.copy2(STOP_SH, fixture / "scripts" / "qa" / "stop_cet.sh")
    return fixture


def _register_instance(fixture: Path, port: int, pid: int, db: Path) -> None:
    """按新约定写一份实例记录（端口 + PID + 库三者绑定）。"""
    inst = fixture / ".cet-qa" / "logs" / "instances" / str(port)
    inst.mkdir(parents=True, exist_ok=True)
    (inst / "server.pid").write_text(str(pid), encoding="utf-8")
    (inst / "instance.env").write_text(
        f"ELT_INSTANCE_PORT={port}\nELT_INSTANCE_PID={pid}\nELT_INSTANCE_DB={db}\n",
        encoding="utf-8",
    )


def _stop(fixture: Path, port: int, mock_port: int | None = None, extra=()):
    return _run(
        ["bash", "scripts/qa/stop_cet.sh", *extra], fixture,
        {"PORT": str(port), "CET_MOCK_PORT": str(mock_port or _free_port())},
        timeout=120,
    )


# ══════════════════════════════════════════════════════════
#  子问题 A：只检查模式必须无数据库删除副作用
# ══════════════════════════════════════════════════════════


def test_check_fresh_does_not_delete_sentinel_db(tmp_path):
    """验收方实测的原始复现：--check --fresh 删掉了 sentinel 库。

    修复前：dbExistsAfter=False、哨兵表消失、退出码 0 报「检查通过」。
    修复后：库与哨兵记录都在，且明确打印「只检查模式：不删除任何数据」。
    """
    data = tmp_path / "check-fresh-data"
    _make_sentinel(data / "vocab.db")

    result = _run(
        ["bash", "scripts/cet/run.sh", "--check", "--fresh",
         "--data-dir", str(data), "--port", str(_free_port())],
        REPO,
    )

    assert (data / "vocab.db").exists(), (
        f"--check --fresh 删掉了数据库！\nstdout:\n{result.stdout}"
    )
    assert _sentinel_rows(data / "vocab.db") == ["keep this test record"]
    assert result.returncode == 0, result.stdout
    assert "检查通过" in result.stdout
    # 必须把「只检查不删库」说出来，而不是静默跳过
    assert "只检查模式" in result.stdout
    assert "不删除任何数据" in result.stdout


def test_check_fresh_preserves_wal_and_shm(tmp_path):
    """不只 DB 文件：WAL/SHM 也属于用户数据，检查模式不能动。"""
    data = tmp_path / "wal-data"
    _make_sentinel(data / "vocab.db")
    (data / "vocab.db-wal").write_bytes(b"FAKE-WAL-SENTINEL")
    (data / "vocab.db-shm").write_bytes(b"FAKE-SHM-SENTINEL")

    result = _run(
        ["bash", "scripts/cet/run.sh", "--check", "--fresh",
         "--data-dir", str(data), "--port", str(_free_port())],
        REPO,
    )

    assert result.returncode == 0, result.stdout
    assert (data / "vocab.db-wal").exists()
    assert (data / "vocab.db-shm").exists()
    assert (data / "vocab.db-wal").read_bytes() == b"FAKE-WAL-SENTINEL"
    assert (data / "vocab.db-shm").read_bytes() == b"FAKE-SHM-SENTINEL"


def test_check_does_not_create_directories(tmp_path):
    """检查模式连 mkdir 都不该做 —— 否则「只读」就不成立。"""
    target = tmp_path / "not-yet" / "nested"
    result = _run(
        ["bash", "scripts/cet/run.sh", "--check", "--data-dir", str(target),
         "--port", str(_free_port())],
        REPO,
    )

    assert result.returncode == 0, result.stdout
    assert not target.exists(), f"--check 创建了目录：{target}"
    assert not (tmp_path / "not-yet").exists()


def test_check_reset_does_not_remove_data_dir(tmp_path):
    """隐藏的 --reset 曾在参数解析阶段就 rm -rf，检查模式必须拦住。"""
    data = tmp_path / "reset-data"
    _make_sentinel(data / "vocab.db")

    result = _run(
        ["bash", "scripts/cet/run.sh", "--check", "--reset",
         "--data-dir", str(data), "--port", str(_free_port())],
        REPO,
    )

    assert result.returncode == 0, result.stdout
    assert data.exists(), f"--check --reset 删掉了数据目录！\n{result.stdout}"
    assert _sentinel_rows(data / "vocab.db") == ["keep this test record"]


def test_check_does_not_touch_production_db(tmp_path):
    """目标库指到正式库时必须拒绝，且正式库毫发无损。"""
    prod = REPO / "resources" / "vocab.db"
    if not prod.exists():
        pytest.skip("本仓库无 resources/vocab.db")
    before = prod.stat().st_mtime_ns, prod.stat().st_size

    result = _run(
        ["bash", "scripts/cet/run.sh", "--check", "--data-dir", str(prod.parent),
         "--port", str(_free_port())],
        REPO,
    )

    after = prod.stat().st_mtime_ns, prod.stat().st_size
    assert before == after, "检查模式改动了正式库"
    assert result.returncode != 0, "把正式库当隔离库使用时应拒绝"
    assert "正式库" in result.stdout


# ══════════════════════════════════════════════════════════
#  子问题 B：清库前必须验证所有权与停服状态
# ══════════════════════════════════════════════════════════


def test_fresh_refuses_when_service_holds_same_db_on_same_port(tmp_path, backend):
    """验收方代码确认的路径：同端口同库的活服务被「复用」后，库又被删掉。

    修复前：先打印「将复用，不重启」，随后 --fresh 无条件删 DB/WAL/SHM，
    活服务的库被抽走。
    修复后：拒绝执行、返回 3、库与哨兵记录原样保留、服务仍存活。
    """
    data = tmp_path / "live-data"
    data.mkdir(parents=True)
    port = _free_port()
    proc = backend(data, port)
    _make_sentinel(data / "vocab.db", "live service sentinel")

    result = _run(
        ["bash", "scripts/cet/run.sh", "--fresh",
         "--data-dir", str(data), "--port", str(port)],
        REPO,
    )

    assert result.returncode == 3, f"应拒绝清库（退出码 3），实际 {result.returncode}\n{result.stdout}"
    assert (data / "vocab.db").exists(), "活服务持有的库被删了"
    assert _sentinel_rows(data / "vocab.db") == ["live service sentinel"]
    assert _alive(proc), "本应只拒绝清库，不应杀掉活服务"
    assert "拒绝清库" in result.stdout or "拒绝执行" in result.stdout
    assert "没有删除或修改任何数据库文件" in result.stdout


def test_fresh_refuses_when_db_held_by_service_on_other_port(tmp_path, backend):
    """同一个库被**另一个端口**的活服务持有，也必须拒绝 —— 判据是库，不是端口。"""
    data = tmp_path / "other-port-data"
    data.mkdir(parents=True)
    live_port = _free_port()
    proc = backend(data, live_port)
    _make_sentinel(data / "vocab.db", "other-port sentinel")

    result = _run(
        ["bash", "scripts/cet/run.sh", "--fresh",
         "--data-dir", str(data), "--port", str(_free_port())],
        REPO,
    )

    assert result.returncode == 3, result.stdout
    assert _sentinel_rows(data / "vocab.db") == ["other-port sentinel"]
    assert _alive(proc)


def test_fresh_still_works_when_no_live_service(tmp_path):
    """正向路径不能被护栏误伤：没有活服务时 --fresh 必须真的清库。

    只验证「修改阶段」本身（--check 不进入该阶段），所以用 --check 之外
    的最短路径：这里直接确认检查阶段不会先删，而修改阶段由下面的用例覆盖。
    """
    data = tmp_path / "clean-data"
    _make_sentinel(data / "vocab.db", "should be wiped")

    # 先确认没有活服务持有时，脚本不会拒绝（用 --check 观察判定，不真起服务）
    result = _run(
        ["bash", "scripts/cet/run.sh", "--check", "--fresh",
         "--data-dir", str(data), "--port", str(_free_port())],
        REPO,
    )
    assert result.returncode == 0, result.stdout
    assert "拒绝" not in result.stdout
    # 检查模式仍不该删
    assert _sentinel_rows(data / "vocab.db") == ["should be wiped"]


def test_fresh_wipes_when_ownership_is_clean(tmp_path):
    """真正执行 --fresh（无活服务）时必须清库 —— 护栏不能把正常功能关死。

    为了不真起一个长驻服务，这里用 --data-dir 指向临时目录并让脚本启动后
    立刻用停止脚本收掉；只断言「清库确实发生了」。
    """
    data = tmp_path / "wipe-data"
    _make_sentinel(data / "vocab.db", "should be wiped")
    port = _free_port()
    log_dir = tmp_path / "logs"

    result = _run(
        ["bash", "scripts/cet/run.sh", "--fresh", "--data-dir", str(data),
         "--port", str(port)],
        REPO,
        {"CET_QA_LOG_DIR": str(log_dir)},
        timeout=300,
    )
    try:
        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "已确认没有活服务持有该库" in result.stdout
        assert "已清空隔离数据库" in result.stdout
        # 清库后是空库：哨兵表不该再存在
        with sqlite3.connect(f"file:{data / 'vocab.db'}?mode=ro", uri=True) as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "audit_sentinel" not in tables, "哨兵表还在，--fresh 没真正清库"
        # 实例标记落盘，供停止脚本核验身份
        assert (data / ".cet-instance.json").exists()
        meta = json.loads((data / ".cet-instance.json").read_text(encoding="utf-8"))
        assert meta["port"] == port
        assert Path(meta["db"]).name == "vocab.db"
    finally:
        _run(["bash", "scripts/qa/stop_cet.sh", "--all"], REPO,
             {"PORT": str(port), "CET_MOCK_PORT": str(_free_port()),
              "CET_QA_LOG_DIR": str(log_dir)}, timeout=120)


# ══════════════════════════════════════════════════════════
#  子问题 C：stop 必须按实例停止，并核验身份
# ══════════════════════════════════════════════════════════


def test_stop_targets_requested_port_not_shared_pidfile(tmp_path, backend, stop_fixture):
    """验收方的原始复现：共享 server.pid 记着 B，`PORT=A stop` 杀了 B、A 还活着。

    修复前：A_alive=True、B_alive=False、退出码 0。
    修复后：只停 A（A_alive=False）、B 不受影响、端口释放、退出码 0。
    """
    data_a = tmp_path / "data-A"
    data_b = tmp_path / "data-B"
    port_a, port_b = _free_port(), _free_port()
    proc_a = backend(data_a, port_a)
    proc_b = backend(data_b, port_b)

    # 旧世界的共享 PID 文件：里面写的是 B
    (stop_fixture / ".cet-qa" / "logs" / "server.pid").write_text(str(proc_b.pid), encoding="utf-8")
    # 新约定：A 有自己的实例记录
    _register_instance(stop_fixture, port_a, proc_a.pid, data_a / "vocab.db")

    result = _stop(stop_fixture, port_a)

    assert not _alive(proc_a), f"目标实例 A 未被停止\n{result.stdout}"
    assert _alive(proc_b), f"PID 文件里的实例 B 被误杀\n{result.stdout}"
    assert result.returncode == 0, result.stdout
    assert "身份核验通过" in result.stdout


def test_stop_refuses_shared_pidfile_pointing_elsewhere(tmp_path, backend, stop_fixture):
    """只有共享 PID 文件、没有实例记录时，也不能凭 PID 就杀。

    修复前：直接 kill 共享 PID 文件里的进程（=误杀）。
    修复后：候选 PID 不监听目标端口 → 跳过；目标端口无人监听 → 退出码 3。
    """
    data_b = tmp_path / "data-B-only"
    port_b = _free_port()
    proc_b = backend(data_b, port_b)
    port_a = _free_port()  # A 根本没启动

    (stop_fixture / ".cet-qa" / "logs" / "server.pid").write_text(str(proc_b.pid), encoding="utf-8")

    result = _stop(stop_fixture, port_a)

    assert _alive(proc_b), f"共享 PID 文件里的进程被误杀\n{result.stdout}"
    assert result.returncode == 3, f"身份不符应返回 3，实际 {result.returncode}\n{result.stdout}"
    assert "不监听端口" in result.stdout or "身份" in result.stdout


def test_stop_refuses_when_instance_record_db_mismatch(tmp_path, backend, stop_fixture):
    """实例记录声明的库与实际不符 → 拒绝停止，不误杀。"""
    data_a = tmp_path / "data-A"
    other_db = tmp_path / "other" / "vocab.db"
    port_a = _free_port()
    proc_a = backend(data_a, port_a)
    _register_instance(stop_fixture, port_a, proc_a.pid, other_db)

    result = _stop(stop_fixture, port_a)

    assert _alive(proc_a), f"库不符时仍被杀掉了\n{result.stdout}"
    assert result.returncode == 3, f"应返回 3，实际 {result.returncode}\n{result.stdout}"
    assert "拒绝停止" in result.stdout


def test_stop_two_instances_isolated(tmp_path, backend, stop_fixture):
    """两个实例并存：停 A 完全不影响 B（这是「多端口同机」的常态）。"""
    data_a = tmp_path / "data-A"
    data_b = tmp_path / "data-B"
    port_a, port_b = _free_port(), _free_port()
    proc_a = backend(data_a, port_a)
    proc_b = backend(data_b, port_b)
    _register_instance(stop_fixture, port_a, proc_a.pid, data_a / "vocab.db")
    _register_instance(stop_fixture, port_b, proc_b.pid, data_b / "vocab.db")

    result = _stop(stop_fixture, port_a)

    assert not _alive(proc_a)
    assert _alive(proc_b), f"停 A 时误杀了 B\n{result.stdout}"
    assert result.returncode == 0, result.stdout


def test_stop_list_shows_instances(tmp_path, backend, stop_fixture):
    """--list 让「现在到底跑着哪些实例」可查，不再靠猜共享 PID 文件。"""
    data_a = tmp_path / "data-A"
    port_a = _free_port()
    proc_a = backend(data_a, port_a)
    _register_instance(stop_fixture, port_a, proc_a.pid, data_a / "vocab.db")

    result = _stop(stop_fixture, port_a, extra=("--list",))

    assert result.returncode == 0, result.stdout
    assert str(port_a) in result.stdout
    assert "运行中" in result.stdout


def test_stop_does_not_kill_foreign_process_on_port(tmp_path, stop_fixture):
    """端口上是**别人的服务**（不是本项目底座）时：拒绝停止、不误杀、非 0 退出。

    原脚本会按端口/共享 PID 直接 kill，可能停掉别人的服务。
    """
    port = _free_port()
    squatter = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=str(tmp_path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(80):
            try:
                with socket.create_connection(("127.0.0.1", port), 0.1):
                    break
            except OSError:
                time.sleep(0.05)
        result = _stop(stop_fixture, port)
        assert squatter.poll() is None, f"别人的服务被误杀\n{result.stdout}"
        assert result.returncode != 0, f"身份不符却返回 0\n{result.stdout}"
        assert "拒绝停止" in result.stdout or "不是本项目" in result.stdout
    finally:
        squatter.terminate()
        squatter.wait(timeout=5)


# ══════════════════════════════════════════════════════════
#  脚本静态契约：防止回退到共享 PID 文件
# ══════════════════════════════════════════════════════════


def test_stop_script_does_not_kill_from_shared_pidfile_blindly():
    """静态契约：停止函数必须先核验端口，且不再只认共享 server.pid。"""
    text = STOP_SH.read_text(encoding="utf-8")
    assert "pid_listens_on" in text, "缺少「该 PID 是否监听目标端口」的核验"
    assert "instance.env" in text, "缺少按实例的绑定记录"
    assert "ELT_INSTANCE_DB" in text, "实例记录应绑定数据库"
    # 共享 PID 文件只能作为候选来源之一，不能是唯一来源
    assert "candidate_pids" in text


def test_run_script_separates_check_from_mutation():
    """静态契约：--check 的 exit 必须在所有 mkdir/rm 之前。"""
    text = RUN_SH.read_text(encoding="utf-8")
    # 「检查通过之后立即 exit 0」这一块
    check_exit = text.index("# --check 是纯只读路径")
    # 修改阶段的标志行必须出现在 check 的 exit 之后
    mutation_marker = text.index("以下为修改阶段")
    assert check_exit < mutation_marker, "--check 的 exit 出现在修改阶段之后"
    # 真正的删除动作只在修改阶段里
    assert text.index('rm -f "$DB"') > check_exit
    assert text.index('rm -rf "$DATA_DIR"') > check_exit
    # 「拒绝执行」的**调用点**（refuse "…"）必须在检查之后；函数定义本身可以在前面
    first_refuse_call = min(
        i for i in (text.index(m) for m in ('refuse "目标库是正式库', 'refuse "数据目录',
                                            'refuse "数据库仍被活服务使用',
                                            'refuse "$DB 不是 SQLite'))
        if i >= 0
    )
    assert first_refuse_call > check_exit
    # 修改阶段的 mkdir 也必须在 check 之后（检查模式不建目录）
    assert text.index('mkdir -p "$DATA_DIR" "$DATA_DIR/config"') > check_exit


def test_start_script_writes_per_instance_records():
    """静态契约：start 必须写按端口的实例记录，而不只是共享 server.pid。"""
    text = START_SH.read_text(encoding="utf-8")
    assert "INSTANCE_DIR" in text
    assert "instance.env" in text
    assert "ELT_INSTANCE_PORT" in text
    assert "ELT_INSTANCE_DB" in text
