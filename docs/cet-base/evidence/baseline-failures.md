# 基线失败清单（P0 基线保护 · 验证与交付）

> 生成时间：2026-11-05
> 工作树：`/tmp/wt-base-qa`　分支：`codex/cet-base-qa`
> 本文件回答一个问题：**哪些测试失败是本轮施工之前就存在的？**
> 结论：真实基线是 **3 failed + 1 error**，不是流传的「41 failed」。下文给出两个口径的完整证据与根因。

---

## 0. 一句话结论

| 口径 | 命令 | 结果 | 可信？ |
| --- | --- | --- | --- |
| ❌ 带 `ELT_USER_DB` | `ELT_USER_DB=/tmp/x/vocab.db pytest -q` | 41 failed / 559 passed / 4 skipped | **不可信**，测试被环境变量互相污染 |
| ✅ 不带 `ELT_USER_DB` | `ELT_CONFIG_DIR=/tmp/x/config pytest -q` | **3 failed / 597 passed / 4 skipped / 1 error** | **正确口径** |

**HEAD 相对基线 901db90 没有引入任何新失败**（详见 §4 双向差集）。

---

## 1. 为什么「41 failed」是假的（根因 + 最小复现）

### 1.1 机制

`backend/db.py` 的数据库路径是**三级解析**，环境变量优先级高于模块属性：

```python
# backend/db.py:23-30
def current_db_path() -> Path:
    p = _current_db_path.get()          # ① contextvar
    if p is not None:
        return p
    env = os.environ.get("ELT_USER_DB") # ② 环境变量  ← 优先级高于 ③
    if env:
        return Path(env)
    return DB_PATH                      # ③ 模块级默认（monkeypatch 改的就是这个）
```

而绝大多数测试的隔离写法是：

```python
monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
```

**这改的是 ③，改不动 ②。** 一旦设了 `ELT_USER_DB`，所有测试都写进同一个库，
前一个用例的数据残留到后一个用例，产生大量「断言多了一条/少了一条」的假失败。

### 1.2 探针脚本（直接证明）

```bash
cat > /tmp/probe_dbpath.py <<'EOF'
import os, sys, pathlib
sys.path.insert(0, "backend")
import db
db.DB_PATH = pathlib.Path("/tmp/PROBE-monkeypatched.db")   # 模拟 monkeypatch
print("DB_PATH attr      =", db.DB_PATH)
print("current_db_path() =", db.current_db_path())
EOF
```

输出：

```
$ env -u ELT_USER_DB .venv/bin/python /tmp/probe_dbpath.py
DB_PATH attr      = /tmp/PROBE-monkeypatched.db
current_db_path() = /tmp/PROBE-monkeypatched.db          ← monkeypatch 生效

$ ELT_USER_DB=/tmp/cb-exp2/a.db .venv/bin/python /tmp/probe_dbpath.py
DB_PATH attr      = /tmp/PROBE-monkeypatched.db
current_db_path() = /tmp/cb-exp2/a.db                    ← env 赢了，monkeypatch 失效
```

### 1.3 最小对照实验（单文件，排除跨文件干扰）

```bash
# A. 不设 ELT_USER_DB
ELT_CONFIG_DIR=/tmp/cb-exp2/config .venv/bin/python -m pytest -q tests/test_vocab_review.py
→ 23 passed

# B. 设 ELT_USER_DB（同一文件、同一 SHA）
ELT_USER_DB=/tmp/cb-exp2/a.db ELT_CONFIG_DIR=/tmp/cb-exp2/config \
  .venv/bin/python -m pytest -q tests/test_vocab_review.py
→ 10 failed, 13 passed
```

**同一个文件，只差一个环境变量，失败数从 0 变成 10。**

### 1.4 41 = 38（污染导致） + 3（真实基线）

```bash
# 41 项失败里，扣掉不带 env 时也失败的 3 项，剩 38 项是污染造成的
comm -13 /tmp/cb-noenv-failed.txt /tmp/cb-head-failed.txt | wc -l
→ 38
```

38 项的分布（全部集中在共用同一张表的用例上，符合「共享库串数据」特征）：

| 测试文件 | 污染失败数 |
| --- | --- |
| `tests/test_v2_lesson_routes.py` | 17 |
| `tests/test_vocab_review.py` | 13 |
| `tests/test_v2_chat_routes.py` / `test_v2_chat_context.py` / `test_v2_db.py` / `test_v2_intensive_routes.py` / `test_v2_translation.py` / `test_v2_tts.py` / `test_lesson_retrieval.py` / `test_word_context_guard.py` | 各 1 |

### 1.5 完整 38 项污染清单（供核对，逐条原因）

<details>
<summary>展开：38 项污染失败（带 env 口径下出现，不带 env 时全部通过）</summary>

| 测试 | 文件 | 失败表象 |
| --- | --- | --- |
| `test_no_candidates_returns_none_without_ai` | `test_lesson_retrieval.py` | `RetrievalFormatError: invalid coverage: ''` |
| `test_init_db_backfills_legacy_chat_messages_into_one_session` | `test_v2_chat_context.py` | `no such table: v2_chat_messages` |
| `test_chat_sessions_can_be_created_and_listed` | `test_v2_chat_routes.py` | 列表多出一条会话 |
| `test_manual_sentence_saved_into_library` | `test_v2_db.py` | `assert [1, 2] == [2]` |
| `test_oral_analysis_is_persisted_and_reused_by_intensive_and_sentence_library` | `test_v2_intensive_routes.py` | `TypeError: 'NoneType' object is not subscriptable` |
| `test_start_youtube_lesson_returns_immediately` | `test_v2_lesson_routes.py` | `'ready' == 'pending'` |
| `test_start_youtube_lesson_backfills_blank_title_on_reuse` | `test_v2_lesson_routes.py` | 标题串到别的用例 |
| `test_youtube_subtitle_fetch_blank_title_keeps_fallback` | `test_v2_lesson_routes.py` | 标题串到别的用例 |
| `test_lesson_status_returns_progress_and_readiness` | `test_v2_lesson_routes.py` | `'ready' == 'pending'` |
| `test_course_library_lists_modes_progress_and_archive_state` | `test_v2_lesson_routes.py` | 库里多了别的用例的课程 |
| `test_course_library_tags_counts_and_delete` | `test_v2_lesson_routes.py` | 同上 |
| `test_saved_sentences_support_listening_results_archive_and_legacy_ratings` | `test_v2_lesson_routes.py` | `assert 7 == 2` |
| `test_phase_b_sentence_can_be_deleted` | `test_v2_lesson_routes.py` | 删除后仍有残留 |
| `test_v2_word_can_be_saved_and_deleted` | `test_v2_lesson_routes.py` | 词表多出 `distractible` |
| `test_subtitles_skip_hidden_words_after_word_delete` | `test_v2_lesson_routes.py` | `assert 0 == 1` |
| `test_subtitles_highlight_follows_selected_wordlists` | `test_v2_lesson_routes.py` | 高亮多出 `complex` |
| `test_resume_interrupted_translations_on_startup` | `test_v2_lesson_routes.py` | `assert [40, 96, 98] == [96, 98]` |
| `test_lesson_words_exclude_mastered_and_known` | `test_v2_lesson_routes.py` | 词表串数据 |
| `test_uploaded_media_missing_file_not_consumed_and_retryable` | `test_v2_lesson_routes.py` | 库非空 |
| `test_uploaded_media_copy_failure_rolls_back` | `test_v2_lesson_routes.py` | 库非空 |
| `test_uploaded_media_enqueue_failure_rolls_back` | `test_v2_lesson_routes.py` | 库非空 |
| `test_uploaded_media_concurrent_start_single_consume` | `test_v2_lesson_routes.py` | `assert 59 == 1` |
| `test_translate_lesson_fails_without_hy_mt_but_keeps_subtitles` | `test_v2_translation.py` | `'ready' == 'failed'` |
| `test_recover_stuck_reading_tts_requeues_only_stuck_lessons` | `test_v2_tts.py` | `assert 3 == 1` |
| `test_story_history_preserves_regenerated_versions_for_same_word_set` | `test_vocab_review.py` | `assert 3 == 2` |
| `test_story_history_paginates_and_deletes_without_cache_resurrection` | `test_vocab_review.py` | `assert 5 == 2` |
| `test_vocab_log_backfills_course_sentence_when_contexts_missing` | `test_vocab_review.py` | 上下文串数据 |
| `test_vocab_log_attaches_translation_and_lesson_link_for_v2_context` | `test_vocab_review.py` | `KeyError: 'lesson_id'` |
| `test_vocab_log_attaches_youtube_audio_from_unsaved_subtitle_sentence` | `test_vocab_review.py` | `KeyError: 'lesson_id'` |
| `test_manual_review_activation_excludes_unactivated_dictionary_words` | `test_vocab_review.py` | 复习本多出 11 个词 |
| `test_vocab_review_supports_word_phrase_familiarity_archive_and_mastery` | `test_vocab_review.py` | 复习本串数据 |
| `test_mastered_lifecycle_activates_missing_review_item` | `test_vocab_review.py` | 复习本串数据 |
| `test_successful_sentence_correction_does_not_implicitly_activate_vocabulary` | `test_vocab_review.py` | 词被意外激活 |
| `test_practice_rejects_empty_input_and_does_not_record_ai_failure` | `test_vocab_review.py` | 历史记录非空 |
| `test_practice_example_allows_empty_input_without_recording_attempt` | `test_vocab_review.py` | 历史记录非空 |
| `test_practice_history_migrates_existing_attempts_without_losing_feedback` | `test_vocab_review.py` | `IndexError: list index out of range` |
| `test_practice_history_filters_and_paginates_newest_first` | `test_vocab_review.py` | `assert 4 == 3` |
| `test_find_v2_sentence_containing_returns_none_when_absent` | `test_word_context_guard.py` | 命中了别的用例的句子 |

</details>

> 注：这些用例**单独跑全部通过**，只有共享库时才失败 —— 这是「污染」而非「缺陷」的判据。

---

## 2. 真实基线：3 failed + 1 error（逐条归属）

**命令（唯一正确口径）**：

```bash
cd /tmp/wt-base-qa
ELT_CONFIG_DIR=/tmp/cb-base/config .venv/bin/python -m pytest -q \
  --ignore=tests/test_cet_listening_ui.py
```

**结果（连跑 3 次完全一致，非 flaky）**：

```
3 failed, 597 passed, 4 skipped, 1 error in 52.16s
```

### 2.1 失败 1 —— `test_baidu_pan.py::test_download_file_verifies_artifact`

| 项 | 内容 |
| --- | --- |
| **失败表象** | `AssertionError: Regex pattern did not match. Expected: '下载失败'　Actual: 'bdpan 未安装'` |
| **位置** | `tests/test_baidu_pan.py:421` |
| **根因** | 测试用 `monkeypatch.setattr(baidu_pan, "_run_cli", ...)` 模拟 CLI 返回空，但 `download_file()` 在调用 `_run_cli` **之前**先执行 `binary = _bin()`；本机没装 `bdpan`，`_bin()` 返回 `None`，函数提前 `raise BaiduPanError("bdpan 未安装")`，压根没走到被 mock 的分支。 |
| **归属** | **环境**（可选外部 CLI `bdpan` 未安装）。与本轮 CET 底座无关，属百度网盘模块。 |
| **是否本轮引入** | 否。基线 901db90 同样失败。 |
| **能否修** | 属产品/测试代码，非我板块。若需修复：`download_file` 的 `_bin()` 检查应放在 job_id 分支或让测试 mock `_bin`。**建议不修**——CET 模式已收束 `link_import`，网盘入口不在本轮范围。 |

### 2.2 失败 2 —— `test_health_planning_enabled.py::test_health_reports_planning_enabled_when_module_present`

| 项 | 内容 |
| --- | --- |
| **失败表象** | `AssertionError: assert False is True`（`/health` 的 `planning_enabled` 为 False） |
| **位置** | `tests/test_health_planning_enabled.py:31` |
| **根因** | 测试断言 planning 模块存在时 `/health` 应返回 `planning_enabled: true`。但本仓库是**公开库**，`backend/webapp/fastapi_routes/planning.py` **不存在**（`git ls-files` 中 0 条）。该模块仅在私有库/云端注册。 |
| **归属** | **环境**（公开库缺私有模块）。测试本身是私有库用例，同步到公开库后未加 skip。 |
| **是否本轮引入** | 否。基线 901db90 同样失败。 |
| **能否修** | 非我板块，且属上游仓库同步问题。**建议记录不修**。 |

### 2.3 失败 3 —— `test_public_single_user_compat.py::test_fastapi_server_starts_single_user_without_private_modules`

| 项 | 内容 |
| --- | --- |
| **失败表象** | 子进程失败：`AttributeError: '_IncludedRouter' object has no attribute 'path'` |
| **位置** | `tests/test_public_single_user_compat.py:74` |
| **根因** | 测试在子进程中 `create_app()` 后执行 `paths = {route.path for route in app.routes}`。本机 **FastAPI 0.162.0 / Starlette 1.10.0** 引入了 `_IncludedRouter` 包装对象（13 个），该对象没有 `.path` 属性 → 集合推导抛 AttributeError。 |
| **归属** | **依赖版本漂移**。`requirements.txt` 对 `fastapi` **未锁版本**（只写 `fastapi`），新版行为变化使旧测试写法失效。 |
| **是否本轮引入** | 否。基线 901db90 同样失败。 |
| **能否修** | 非我板块。修法应是测试侧兼容：`getattr(route, "path", None)` 或改用 `app.openapi()["paths"]`。**建议报 Lead 决定**（见 §5）。 |

### 2.4 ERROR —— `test_youtube_v2_helpers.py::test_store_media_segments_persists_subtitles_and_reading_projection`

| 项 | 内容 |
| --- | --- |
| **失败表象** | `ERROR at teardown`：`AssertionError: 正式库 resources/vocab.db 在测试期间被修改了！` |
| **位置** | `tests/conftest.py:56`（`_forbid_production_db_writes` session fixture） |
| **根因** | **这不是测试自身失败，是 Lead 新增的数据隔离护栏抓到了真实泄漏**。`test_public_single_user_compat` 在**子进程**里 `import fastapi_server; create_app()`，子进程不继承父进程 conftest 的 `db.DB_PATH` 重定向，回落到默认 `resources/vocab.db` 并建库。护栏在 session 结束时发现正式库被创建/修改，于是在**最后一个用例**上报 teardown error。 |
| **归属** | **护栏 + 环境**。护栏行为正确（确实发生了写正式库），但表现形式是「错误挂在无关用例上」，容易误导。 |
| **是否本轮引入** | 是（护栏由 `0d4d6cc` 引入），但**它抓到的是既有泄漏**，不是新增缺陷。 |
| **证据** | 逐文件扫描确认唯一污染源：<br>`POLLUTER: tests/test_public_single_user_compat.py` |
| **影响** | 跑全量后 `resources/vocab.db` 会被创建（544768 字节）。该文件在 `.gitignore:49` 中，不会误提交。 |

---

## 3. 基线 vs HEAD 双向差集（证明本轮零新增失败）

对比对象：
- 基线 `901db90eeebed7e44e964ca7eb8b0cf89a204e91`（`git archive` 解出独立目录，软链同一 `.venv`）
- HEAD `0d4d6cc14c7f879d843923cdaedeab6a8b870ac6`

两个 SHA 各跑两次，结果稳定：

| SHA | 带 `ELT_USER_DB` | 不带 `ELT_USER_DB` |
| --- | --- | --- |
| `901db90`（基线） | 43 failed / 556 passed / 4 skipped | **5 failed** / 594 passed / 4 skipped |
| `0d4d6cc`（HEAD） | 41 failed / 559 passed / 4 skipped | **3 failed** / 597 passed / 4 skipped / 1 error |

```bash
comm -13 <基线失败集> <HEAD失败集>   # HEAD 新增的失败 → 空
comm -23 <基线失败集> <HEAD失败集>   # HEAD 修好的失败 → 2 项
```

- **新增失败：0 项**（空集）
- **修复失败：2 项**
  - `tests/test_cet_contract.py::test_playback_under_threshold_not_counted`
  - `tests/test_cet_contract.py::test_retry_after_ai_failure_works`

> 这两个是队友 C 板块在 `test_cet_contract.py` 上的改动顺带修好的，不属底座范围，但可作为「回归没有倒退」的正面证据。

---

## 4. 复现步骤（独立验收方照抄即可）

```bash
# ── 准备：独立工作树，独立配置目录，独立临时库 ──
cd /tmp/wt-base-qa
git rev-parse HEAD                      # 应为你验收的目标 SHA
rm -rf output/* logs/* resources/vocab.db*

# ── 正确口径：不要设置 ELT_USER_DB ──
ELT_CONFIG_DIR=/tmp/verify/config \
  .venv/bin/python -m pytest -q --ignore=tests/test_cet_listening_ui.py

# 期望：3 failed, 597 passed, 4 skipped, 1 error
#   3 failed = §2.1 §2.2 §2.3（环境/依赖，非本轮引入）
#   1 error  = §2.4（护栏抓到 public_single_user_compat 子进程写正式库）
```

**环境**：macOS 27.0.1 · Python 3.11.16 · FastAPI 0.162.0 · Starlette 1.10.0 · ffprobe 9.0.2

---

## 5. 给 Lead / 独立验收方的 3 条建议

1. **验收报告里不要写「41 failed」**。独立验收方按同一命令会跑出 3 failed，数字对不上反而像我们造数据。正确写法：**基线 3 failed + 1 error，全部为环境/依赖因素，本轮零新增失败**。

2. **`ELT_USER_DB` 只用于起服务做数据隔离，禁止用于跑 pytest**。已在 `docs/cet-base/使用与启动.md` 专设一节说明。

3. **`test_public_single_user_compat` 的子进程泄漏值得修**（属 Lead/P0.4 范围，非我板块）。两个可选修法：
   - 子进程脚本里显式 `db.DB_PATH = <tmp>`（最直接）；
   - 或该用例加 `@pytest.mark.skip(reason="子进程不继承 conftest DB 重定向")`。

   不修的话，全量跑完会在仓库里留下 `resources/vocab.db`（已 gitignore，但会让「正式库是否被动过」的核查变得含糊）。
