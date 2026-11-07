# 包 H · 首页与收藏 · 交付说明

## 0. 基本信息

| 项 | 值 |
|---|---|
| Worktree | `/tmp/wt-drill-h` |
| 分支 | `codex/cet-drill-h` |
| 基线 | `c4a480d` |
| 服务端口 | `5303` |
| 状态 | H1 / H2 / H3 已完成；H4 按任务书允许的「不能区分就不做」处理（原因见 §5） |

改动文件（只动本包名下文件）：

- `backend/webapp/fastapi_routes/cet.py`
- `frontend/templates/index.html`
- `frontend/static/cet-base.css`
- 本说明与 `docs/cet-base/卡点融合/evidence/H/` 截图/日志

**没有修改** `backend/webapp/services/cet_service.py`（业务规则原样保留），没有改 `tests/`，没有动 `resources/vocab.db`、5173 演示实例或 `cet-base-delivery`。

---

## 1. 改动清单（文件与行号）

### 1.1 `backend/webapp/fastapi_routes/cet.py`

只新增一个只读接口及只读查询辅助函数，未改任何已有接口行为。

| 行号 | 内容 |
|---|---|
| 19-40 | 新接口契约与统计口径的注释说明 |
| 43-58 | `_summary_lesson_material_ids()`：一份材料两种存量 ID（`v2-<lessonId>` / 业务稳定 ID）的匹配集合 |
| 61-107 | `_summary_count()` / `_summary_row()` / `_summary_rows()` / `_empty_material_summary()` / `_word_is_due()` 只读辅助函数（表缺失/查询失败按 0） |
| 114-203 | `_cet_home_summary()`：只读汇总实现 |
| 206-209 | `GET /api/cet/summary` 路由 `get_cet_summary()` |

实现细节：

- 全流程只有 `SELECT` / `COUNT`，没有 `INSERT` / `UPDATE` / `DELETE`；查询走 `db._db()` 的只读会话，不写业务表。
- `materials` 的键是课程 `lessonId` 的字符串（前端 `String(item.lessonId)` 直接查）。
- 材料在存量表里可能是 `v2-<lessonId>` 或业务稳定 ID（如 `cet6-202512-conv2`），统计时两种都匹配。
- 空库 / 表缺失 / 数据库不可用时返回全 0，不抛 500。

### 1.2 `frontend/templates/index.html`

| 行号 | 内容 |
|---|---|
| 9 | `cet-base.css` 缓存串 `20261105-1` → `20261107-drill-h` |
| 1616-1632 | CET 首页「今日复习」卡片 DOM（`#cet-today-review`，仅 `cet_features.cet_listening_agent` 为真时渲染） |
| 7946 | `_cetDrillSummary` 全局状态（`GET /api/cet/summary` 结果） |
| 8024-8048 | `cetMaterialDrillSummary()` / `cetMaterialSummaryHtml()`：材料卡摘要行（H2） |
| 8051-8070 | `cetBuildCard()` 中插入 `${cetMaterialSummaryHtml(item)}`（最近学习卡与材料库卡共用） |
| 8195-8255 | `cetPendingReviewMaterials()` / `cetSummaryLessonTitle()` / `cetRenderTodayReview()`：今日复习三组渲染（H3） |
| 8258-8294 | `cetLoadLibrary()`：并行请求 `/api/cet/summary`（仅卡点陪练开关开启时），拿到后渲染 H2/H3 |
| 8296-8345 | 兜底路径 `cetLoadLibraryFallback()` 同样读取 summary 并渲染今日复习 |

行为要点：

- H2 卡片摘要只在有真实数据时渲染：`已交卷 {score}/{total} · 卡点 {marks} · 待复习 {pendingReview}`；无交卷/无卡点/无待复习时整行不出现。
- `unscored > 0` 时放在 `title` 提示「另有 N 题因题库无可靠答案，未计分」，不改变主行文案。
- H3 三组固定为「卡点 / 收藏句 / 收藏词」，各组显示数量；三项全 0 时显示空态。
- 卡点组按材料列出可点击行，链接统一为 `/workspace/<lessonId>?review=1`（K10 约定）；收藏句链接 `/#favorites` + `cetSetFavoritesPane('sentences')`；收藏词链接 `/#favorites` + `cetSetFavoritesPane('vocab')`。
- 所有新增 JS 均只在 CET 模式初始化路径中调用；`ELT_CET_FEATURE_CET_LISTENING_AGENT=0` 时 H2/H3 DOM 不渲染、也不请求 `/api/cet/summary`。

### 1.3 `frontend/static/cet-base.css`

| 行号 | 内容 |
|---|---|
| 303-314 | `.cet-material-progress`：材料卡摘要小条 |
| 316-458 | `.cet-today-review` 及三组卡片、数量、链接、空态样式（复用既有 CSS 变量） |
| 461-467 | `@media (max-width: 768px)`：今日复习三组改单列 |

没有新增 `:root` 变量，没有硬编码超过 360px 的固定宽度。

---

## 2. H1：`GET /api/cet/summary` 契约与口径

响应结构（示例为本地真实服务 + 隔离库实测，`evidence/H/summary-api.json`）：

```json
{
  "materials": {
    "1": {
      "submitted": true,
      "score": 4,
      "total": 5,
      "unscored": 1,
      "marks": 1,
      "pendingReview": 1
    },
    "2": {
      "submitted": false,
      "score": 0,
      "total": 5,
      "unscored": 0,
      "marks": 0,
      "pendingReview": 0
    }
  },
  "reviewDue": {"marks": 1, "sentences": 1, "words": 1}
}
```

| 字段 | 口径 |
|---|---|
| `materials[lessonId].submitted` | 布尔：该材料是否已有交卷记录（取最近一次 `cet_attempts`） |
| `materials[lessonId].score` | 最近一次交卷得分（`cet_attempts.score`） |
| `materials[lessonId].total` | 该材料题目总数（`cet_questions`，按 `v2-<lessonId>` 统计） |
| `materials[lessonId].unscored` | 最近一次交卷中「题库无可靠答案、未计分」的题数（`cet_attempts.unknown_count`） |
| `materials[lessonId].marks` | 该材料卡点总数（`cet_marks`） |
| `materials[lessonId].pendingReview` | 该材料待复习卡点条目数（`cet_review_items.status='pending'`） |
| `reviewDue.marks` | 全库待复习卡点条目数（`cet_review_items.status='pending'`） |
| `reviewDue.sentences` | 全库到期收藏句数量（`v2_sentences`：未归档、`next_review` 空或 ≤ 今日，且属于收藏来源） |
| `reviewDue.words` | 全库到期收藏词数量（`word_review_items` 未归档/未掌握，按与前端 `reviewStatus()` 相同的间隔规则判断） |

**空库安全**：空库（无任何课程）实测返回 `{"materials": {}, "reviewDue": {"marks": 0, "sentences": 0, "words": 0}}`，HTTP 200。

**只读证据**：对真实服务调用一次 summary 前后，逐表计数完全不变：

```
BEFORE/AFTER: (2, 10, 1, 1, 1, 1, 1, 1)
```

（依次为 `cet_attempts, cet_attempt_answers, cet_marks, cet_practices, cet_review_items, v2_phase_b_sentences, v2_sentences, word_review_items`。）

---

## 3. H2：首页材料卡摘要

- `cetBuildCard()` 在卡片信息区新增一行 `.cet-material-progress`，格式严格为
  `已交卷 {score}/{total} · 卡点 {marks} · 待复习 {pendingReview}`（只输出有数据的段）。
- 全部为 0 / 接口无该 lessonId 时整行不渲染。
- 最近学习卡与材料库列表卡共用同一渲染函数，因此两处都会显示（截图可见两处一致）。
- 实测文本：`已交卷 4/5 · 卡点 1 · 待复习 1`（截图 `home-1440.png` / `home-375.png`）。

---

## 4. H3：首页「今日复习」卡

- 位置：CET 模式首页 hero 内、继续学习卡下方。
- 三组（固定顺序）：**卡点 / 收藏句 / 收藏词**，每组显示真实数量。
- 卡点组：按 `pendingReview` 按材料聚合，逐材料链接 `/workspace/<lessonId>?review=1`；超过 4 份时提示「另有 N 份材料」。若只有总数无材料明细，显示「共 N 处待复习卡点」。
- 收藏句组：链接 `/#favorites`，`onclick` 调 `cetSetFavoritesPane('sentences')`（localStorage `cet-favorites-pane` = `sentences`）。
- 收藏词组：链接 `/#favorites`，`onclick` 调 `cetSetFavoritesPane('vocab')`（`cet-favorites-pane` = `vocab`）。
- 三项全 0 时显示空态：「今天没有待复习内容。完成一次听力带练后，卡点会自动出现在这里。」
- `ELT_CET_FEATURE_CET_LISTENING_AGENT=0` 时整个卡片不渲染（与 c4a480d 对齐，见 §6）。

浏览器实测（真实服务、真实点击，均通过）：

- 三组数量为 `["1", "1", "1"]`，组名 `["卡点", "收藏句", "收藏词"]`；
- 卡点链接 `href="/workspace/1?review=1"`，点击后 URL 为 `http://127.0.0.1:5303/workspace/1?review=1`；
- 收藏句点击后 `#tab-sentences` 激活、`localStorage['cet-favorites-pane']='sentences'`、`location.hash='#favorites'`；
- 收藏词点击后 `#tab-vocab` 激活、`localStorage['cet-favorites-pane']='vocab'`。

---

## 5. H4：未做（现有数据结构无法区分「来自卡点」）

结论：**不做**，按任务书「不能区分就不做，写进交付说明」处理。依据（以基线 `backend/db.py` schema 为准）：

- `v2_phase_b_sentences`（`db.py:266-276`）只有 `lesson_id / sentence_id / segment_index / start_seconds / end_seconds / text / created_at`，没有来源/渠道列。
- `v2_sentences`（`db.py:278-292`）同样没有来源列。
- `v2_sentence_review_items`（`db.py:552-558`）虽有 `source` 列，但唯一写入口 `db.activate_sentence_review()` 在 backend 中**零调用点**；卡点带练链路（`cet_service.create_practice()` / `finish_practice()`）只写 `cet_practices` / `cet_review_items` / `cet_practice_*`，从不写 `v2_phase_b_sentences`、`v2_sentences` 或 `v2_sentence_review_items`。
- 因此当前没有任何一条「卡点带练收藏句」的收藏记录带可区分来源；要支持必须新增 schema 列或改服务写入路径，均超出包 H 的文件所有权，故未做。

**给主代理 / K 的接口预留建议**：若后续 K 的卡点带练增加「收藏本句」，建议收藏动作经 `db.activate_sentence_review(sentence_id, source='cet_mark')` 写 `v2_sentence_review_items.source`；届时「我的收藏 · 句子」可在 `sentence.review_source === 'cet_mark'` 时渲染「来自卡点」标签（`/api/v2/lessons/sentence-review` 现已返回 `review_source` 字段）。

---

## 6. 两种回归

### 6.1 `ELT_CET_FEATURE_CET_LISTENING_AGENT=0`

- 与 `c4a480d` 渲染对比：可见 HTML（剥 `<script>/<style>` 后按空白归一）**逐字一致**（`visible normalized identical: True`，两边长度均 26661）。
- 真实浏览器实测（5320 临时服务，隔离库）：`#cet-today-review` 数量 0、`.cet-material-progress` 数量 0、`/api/cet/summary` 请求数 0、导航仍为底座 3 项。

### 6.2 `ELT_CET_MODE=0`（通用模式）

- 与 `c4a480d` 渲染对比：可见 HTML（剥脚本/样式后按空白归一）**逐字一致**（`visible normalized identical: True`）。
- 新增卡片只在 `{% if cet_on %}` 分支内渲染；通用模式不出现「今日复习 / 卡点 / 待复习」等新 UI。

---

## 7. 测试命令与输出原文

### 7.1 本包相关测试（全部通过）

```
$ cd /tmp/wt-drill-h
$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
    tests/test_cet_base_home.py tests/test_cet_base_mode.py \
    tests/test_cet_base_routes_registered.py tests/test_cet_base_materials.py \
    tests/test_cet_fix2_p4_favorites.py
........................................................................ [ 45%]
........................................................................ [ 91%]
...........sss                                                           [100%]
155 passed, 3 skipped in 2.41s
```

### 7.2 冲突的旧测试（按任务书要求不改 tests/）

```
$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_home_practice_ui_contract.py
...
FAILED tests/test_home_practice_ui_contract.py::test_sentence_library_is_browse_first_and_has_inline_listening_controls
1 failed, 29 passed in 0.09s
```

原因：该旧测试断言 `"今日复习" not in INDEX_HTML`（整个模板源码），而 H3 必须新增「今日复习」卡片标题。属**预期冲突**，需主代理在集成分支按新任务书更新该断言（建议只约束句子库 pane 而不是整个模板）。

### 7.3 全量 pytest

```
$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q
3 failed, 1053 passed, 222 skipped in 655.89s (0:10:55)
```

3 个失败：

1. `tests/test_home_practice_ui_contract.py::test_sentence_library_is_browse_first_and_has_inline_listening_controls` —— 上述预期冲突。
2. `tests/test_baidu_pan.py::test_download_file_verifies_artifact` —— 本机 `bdpan 未安装`，**在基线 c4a480d 同样失败**（已在 `/tmp/wt-drill-base` 复跑确认）。
3. `tests/test_health_planning_enabled.py::test_health_reports_planning_enabled_when_module_present` —— planning 模块未安装，**在基线 c4a480d 同样失败**（已复跑确认）。

### 7.4 接口实测（真实服务 5303 + 隔离库）

```
$ cd /tmp/wt-drill-h && .venv/bin/python - <<'PY'
# 断言 materials["1"]/["2"]、reviewDue 与前后表计数
PY
HTTP 200 content-type: application/json
{
  "materials": {
    "1": {"submitted": true, "score": 4, "total": 5, "unscored": 1, "marks": 1, "pendingReview": 1},
    "2": {"submitted": false, "score": 0, "total": 5, "unscored": 0, "marks": 0, "pendingReview": 0}
  },
  "reviewDue": {"marks": 1, "sentences": 1, "words": 1}
}
READ-ONLY COUNTS UNCHANGED: (2, 10, 1, 1, 1, 1, 1, 1)
SUMMARY ASSERTIONS OK
```

空库（全新隔离库，`TestClient`）：

```
{"materials": {}, "reviewDue": {"marks": 0, "sentences": 0, "words": 0}}
EMPTY OK
```

### 7.5 浏览器验证（真实服务、真实点击、真实媒体状态）

脚本：`docs/cet-base/卡点融合/evidence/H/browser-verification.py`；完整输出：`docs/cet-base/卡点融合/evidence/H/browser-verification.log`

关键结果（19 项 PASS，0 FAIL）：

```
PASS 材料卡摘要可见 | ["已交卷 4/5 · 卡点 1 · 待复习 1", "已交卷 4/5 · 卡点 1 · 待复习 1"]
PASS 今日复习三组 | count=3
PASS 今日复习数量 1/1/1 | ["1", "1", "1"]
PASS 卡点链接带 review=1 | /workspace/1?review=1
PASS 真实媒体开始播放（paused=false 且 currentTime>0） | {"paused": false, "currentTime": 2.860692, "playbackRate": 1, "readyState": 4, ...}
PASS 真实媒体暂停（paused=true） | {"paused": true, "currentTime": 2.867237, "playbackRate": 1}
PASS 收藏句链接进入句子收藏 | 
PASS 收藏句链接写 localStorage | 
PASS 收藏句链接 hash=#favorites | #favorites
PASS 收藏词链接进入词汇收藏 | 
PASS 收藏词链接写 localStorage | 
PASS 375 无横向溢出 | {"scrollWidth": 375, "innerWidth": 375}
FAILED: 0
```

说明：媒体状态是在点击卡点链接进入 `/workspace/1?review=1` 后，真实点击 Chromium 原生 `<audio id="local-media-player">` 左侧播放按钮得到；`currentTime` 从 0 推进到 2.86 秒、`playbackRate` 1、`paused=false`，再点一次 `paused=true`。

---

## 8. 截图（1440 / 375）

目录：`docs/cet-base/卡点融合/evidence/H/`

| 文件 | 说明 |
|---|---|
| `home-1440.png` | 1440 宽首页全页：今日复习三组 + 材料卡摘要 |
| `home-375.png` | 375 宽首页全页：三组单列、无横向溢出 |
| `home-375-today-review.png` | 375 宽首屏：今日复习卡 |
| `workspace-review-1440.png` | `/workspace/1?review=1`：真实媒体播放验证现场 |
| `summary-api.json` | `GET /api/cet/summary` 实测响应 |
| `browser-verification.log` | 浏览器验证完整输出 |

---

## 9. 缓存串

| 文件 | 旧版本串 | 新版本串 |
|---|---|---|
| `frontend/static/cet-base.css`（index.html 引用） | `?v=20261105-1` | `?v=20261107-drill-h` |

本次没有改任何独立 JS 文件；`index.html` 内联脚本随模板生效。`cet-base.css` 已按任务书要求升缓存串。

---

## 10. 未完成 / 风险 / 需配合

1. **H4 未做**：现有数据结构无法区分「由卡点带练收藏的句子」，详见 §5；这是任务书允许的「不能区分就不做」。
2. **K10 依赖**：H3 卡点链接统一生成 `/workspace/<lessonId>?review=1`；当前 H 分支的 workspace 尚未读取 `review=1`（属 K 包工作），点击后只进入材料页，不会自动打开复习。集成时需 K 确认 K10 已实现，或在 K 分支验收该链接行为。
3. **旧测试冲突**：`tests/test_home_practice_ui_contract.py::test_sentence_library_is_browse_first_and_has_inline_listening_controls` 会因「今日复习」字符串断言失败，需主代理在集成分支更新该断言（不要在本包改 tests/）。
4. **全量测试环境失败两项**：`test_baidu_pan.py::test_download_file_verifies_artifact`、`test_health_planning_enabled.py::test_health_reports_planning_enabled_when_module_present` 在基线 c4a480d 同样失败（本机缺 `bdpan` / planning 模块），与本包无关。
5. **W 配合**：无强制项；若 K10 需要在 `workspace.html` 增加 `?review=1` 的读取/自动展开逻辑，按文件所有权应由 W 或 K 其实现，本包只保证链接参数正确。
6. 数据口径提示：`reviewDue.marks` 按「待复习卡点条目（pending）」统计，与 H2 的 `pendingReview` 同源；若主代理希望「今日复习 · 卡点」显示全部历史卡点数而非待复习数，需要统一改口径（本包按 reviewDue 语义取 pending）。
