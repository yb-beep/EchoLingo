# 包 A · 前端 —— 返修 2.1 交付说明

> 分支：`codex/cet-fix2.1-a`（从 `3573e90` 切出）
> 依据：[53-CET底座返修2.1任务书-2026-11-06.md](../../../53-CET底座返修2.1任务书-2026-11-06.md) §3 包 A
> 前置：[52-返修2复验报告-2026-11-06.md](../../../52-返修2复验报告-2026-11-06.md)

## 0. 一句话结论

A1–A5 **全部完成**，另有 **1 条 A1 附加修复**（任务书未列、实测发现）。

本轮的实质是**拆掉多余通道**，不是补通道：上一轮 P1-3 / P4-1 的前提
「工作区收藏进不了我的收藏」经复验是错的 —— `save_word` 本来就会 `activate_word_review`。
所以 A1 / A2 / A3 都是「删」。

| 编号 | 状态 | 一句话 |
| --- | --- | --- |
| A1（阻断） | ✅ 完成 | CET 模式不再自动同步高亮词；**依赖已确认**（见 §2） |
| A1-附加（阻断） | ✅ 完成 | 精听页存在**第二条**同根因通道，一并门控（见 §3） |
| A2（阻断） | ✅ 完成 | 打开「我的收藏」不再写库 |
| A3 | ✅ 完成 | 收藏后不再重复调 `activate` |
| A4 | ✅ 完成 | 卡片文案走 `cet_copy`，「生词」「复习本」CET 下 0 次 |
| A5 | ✅ 完成 | `.cet-study-item[hidden]` 真的隐藏 |

## 1. 改动文件与精确范围

| 文件 | 改动 | 说明 |
| --- | --- | --- |
| `frontend/templates/workspace.html` | A1 / A3 / A4 / A5 缓存串 | 见下 |
| `frontend/templates/index.html` | **仅** `initVocab()` 尾部（原 5710–5758） | **A2 只改这一处** |
| `frontend/templates/intensive.html` | **仅** `syncIntensiveHighlightedWords()` | A1 附加（主代理授权，见 §3） |
| `frontend/static/cet-workspace.css` | 新增 1 条 `[hidden]` 规则 | A5 |
| `tests/test_cet_fix21_a_frontend.py` | 新建，25 条 | 静态契约 + 反向锁定 |
| `tests/test_cet_fix21_a_browser.py` | 新建，12 条 | 真实浏览器端到端 |

### 1.1 `index.html` 精确行号范围（任务书要求写明）

只改了 `initVocab()` 的**尾部一处**：

- **改前**：`5710`（注释「P4-1：历史上只进了本课…」）+ `5711`（`await cetSyncLessonCollections();`）
  + `5714`–`5758`（`cetSyncLessonCollections` 函数整体 + 段注释）
- **改后**：`5710` 起替换为一段说明注释，**函数整体删除**，`initVocab()` 在 `5719` 收尾
- 净变化：−43 行 / +12 行

**未触碰**：导入面板（包3）、首页文案（包4）、`#favorites` 渲染、`loadVocabWordlistMembership`、
`renderTodayPanel`、`renderVocabList`、`updateHeaderStat`、`initStoryWords`。

### 1.2 `workspace.html` 改动点

| 位置（改后行号） | 内容 |
| --- | --- |
| `2001` | `cet-workspace.css` 缓存串 `20261105-p1` → `20261106-fix21`（A5 要能到真实用户） |
| `2905`–`2923` | `syncHighlightedWordsAfterRender()` 开头加 `if (CET_ON) return;`（A1） |
| `4202`–`4211` | 「本课收藏」面板：CET 分支文案改走键 + 兜底改 `★ 已收藏`（A4） |
| `4249`–`4257` | 删除 `autoActivateFavoriteWord()` 整段（A3） |
| `4755`–`4785` | `openWordPopover()`：文案走 `cetCopy` 三键；CET 不渲染「加入复习本」（A4） |
| `4855`–`4864` | `toggleWordSave()` 新增分支删掉 `if (CET_ON) await autoActivateFavoriteWord(...)`（A3） |

## 2. A1 依赖确认结论（任务书点名要求）

> 任务书原文：「**先确认**：字幕高亮（`segment.highlighted_words`）、`★` 标记、
> 精听页「重点词汇 N」筛选不依赖这次同步；如有依赖，改成从 `highlighted_words` 读，
> 不得为此回写 `v2_lesson_words`」

### 2.1 三者各自的数据来源（代码层）

| 用户看到的东西 | 数据来源 | 是否读 `v2_lesson_words` |
| --- | --- | --- |
| 字幕词高亮（`.highlight-word`） | `GET /api/v2/lessons/{id}/subtitles` → `highlight_segments()`（`v2_vocab.py:696`）**当场按词表算** | **否** |
| `★` 已收藏标记 | `savedWordSet` ← `GET /api/v2/lessons/{id}/words`（`workspace.html:2843 loadSavedWords`） | 是，但那是**用户真实收藏**，不是自动高亮词 |
| 精听页「重点词汇 N」 | `GET /api/v2/lessons/{id}/intensive` → `build_intensive_document()`（`v2_intensive.py:92`）→ `sentence.highlighted_words` | **否**（见 2.3 的注意点） |

关键代码证据：

```
# v2_lessons.py:1144  subtitles 端点
source_words, source_lists, hidden_words = _highlight_context(lesson_id, wordlists)
highlighted = highlight_segments(segments, hidden_words=..., source_words=...)
```

`highlight_segments()`（`v2_vocab.py:704`）只看 `source_words` / `intermediate`（词表）
与 `hidden_words`，**完全不查 `v2_lesson_words`**。

### 2.2 浏览器实测（`3573e90` 基线 vs 本分支，全新库）

同一份材料（lesson 1，32 句），同一台机器，同一隔离库重建：

| 观测项 | `3573e90` 基线 | 本分支 | 结论 |
| --- | --- | --- | --- |
| `highlighted-words/sync` 是否发出 | ✅ 发出 | ❌ 不发出 | A1 生效 |
| `v2_lesson_words`（打开工作区后） | 10 个自动词 | **`[]`** | A1 生效 |
| 状态条「本课收藏」 | **10** | **0** | 任务书验收点 ✅ |
| 字幕 `.subtitle-word` | 10 | **10** | 高亮照常 ✅ |
| 字幕 `.highlight-word` | 1 | **1** | 高亮照常 ✅ |
| 精听页「重点词汇」 | **9 / 全部 32** | **9 / 全部 32** | 与基线一致 ✅ |
| `/vocab-log`（我的收藏数据源） | `{}` | `{}` | 没有被冒充 ✅ |

> 注：`.highlight-word` 只有 1 个而不是 10 个，是因为工作区 `#current-subtitle`
> **只渲染当前一句**，不是整篇时间轴。基线与本分支在这点上一致（都是 1），
> 说明与 A1 无关。实测 seek 到第 4 句（`Did you notice any sentences with hope or wish?`）
> 后 `.highlight-word` = 1 且正是 API 给的 `sentences` —— 高亮数据确实来自 `/subtitles`。

### 2.3 一个必须写明的注意点（不是依赖，但相关）

`build_intensive_document()`（`v2_intensive.py:108`）确实读了 `v2_lesson_words`：

```python
lesson_words = {item["word"] for item in db.get_v2_lesson_words(lesson_id)}
...
"highlighted_words": _merge_saved_highlights(text, ..., lesson_words, hidden_words)
```

但这是**合并**语义（`_merge_saved_highlights`：先放词表高亮词，再**追加**已收藏词），
不是「只从 `v2_lesson_words` 取」。A1 拆掉自动写入后 `lesson_words` 为空，
合并结果退化成「只有词表高亮词」—— 也就是**精听页「重点词汇」只显示真正的词表重点词**，
不再混入自动写入的词。实测计数 9 与基线**完全相同**（因为基线里那 10 个自动词本来
就已经被词表高亮覆盖，合并没有新增）。

**结论：无依赖，不需要按任务书「改成从 `highlighted_words` 读」。未回写 `v2_lesson_words`。**

## 3. A1 附加修复：精听页的第二条自动写入通道

### 3.1 发现过程

做 A1 依赖确认时，用「分步隔离探针」逐页量写入，发现只堵工作区那条不够：

```
── 0-before              words: []
── 1-只开工作区           sync_called: False   words: []            ← A1 生效
── 2-再开精听页           sync_called: True    words: [10 个自动词]  ← 还有第二条
```

然后返回工作区：`本课收藏 10`、`/reading-saved-items` 里出现 10 条
「xxx · 收藏后进入「我的收藏」」—— **用户一个词都没收藏**。

### 3.2 根因

`frontend/templates/intensive.html`：

```js
:2300  async function syncIntensiveHighlightedWords() {
:2302      await fetch(`/api/v2/lessons/${LESSON_ID}/highlighted-words/sync...`, {method:'POST'});
:2389      await syncIntensiveHighlightedWords();     // loadIntensiveStudy() 里无条件调用
:678   const CET_ON = window.__CET_BOOT__.enabled;     // 已定义，可直接用
```

与 A1 **完全同根因、同写入端点、同后果**。返修2 刚把「逐句精听 →」做成主入口
（P1-1），用户实际路径必然经过精听页 —— 不堵这条，A1 在真实使用中等于没修完。

### 3.3 修复与授权

- 该文件**不在**任务书 §2 的文件所有权表里（§2 只列了 `workspace.html`）—— 所有权空档。
- 已向主代理报告并获**书面授权**（裁决 1），授权范围严格限定：只改
  `syncIntensiveHighlightedWords()` 一处，加 `if (CET_ON) return;`，不动其它任何地方。
- 实现：守卫放在**函数开头**（数据入口），不是改调用点 —— 将来多一个调用方也安全。

### 3.4 实测（全新库，lesson 2）

| 观测项 | 修复前 | 修复后 |
| --- | --- | --- |
| 精听页 `highlighted-words/sync` | ✅ 发出 | ❌ 不发出 |
| 打开精听页后 `v2_lesson_words` | 4 个自动词 | **`[]`** |
| 返回工作区「本课收藏」 | **4** | **0** |
| 精听页「重点词汇 N」 | 4 / 32 | **4 / 32**（不变）✅ |

「重点词汇」数量不变的原因见 §2.1 / §2.3：它读的是 `sentence.highlighted_words`，
由 `build_intensive_document()` 当场算，不依赖这次同步。

### 3.5 回归确认（主代理要求）

改了 `intensive.html` 后跑了 `tests/test_cet_fix2_p1_workspace_browser.py`（精听页主入口的 10 条）：

```
1 failed, 9 passed in 32.43s
```
唯一失败是 `test_p1_3_favoriting_a_word_makes_it_appear_in_my_collections`
（它断言「点收藏后必须发出 `vocab-review/activate`」）—— 这正是 A3 要删的行为，
已由主代理认领改写为反向断言（见 §6）。**精听页 3 条 P1-1 测试全过。**

## 4. 逐编号证据

### A1（阻断）CET 不再自动同步高亮词

**实现**：`workspace.html:2923` `if (CET_ON) return;`，开关读集中配置
（`window.__CET_BOOT__.enabled` ← `cet_mode.template_context()` 的 `cet_on`），不新增第二套判断。

| 模式 | 证据 |
| --- | --- |
| CET | 见 §2.2 表格（本课收藏 0、sync 未发出、高亮 9/32 与基线一致） |
| 通用 `ELT_CET_MODE=0` | 5283 实测：`sync_called: True`、`words: [10 个]`、`本课收藏 10` —— **与 `3573e90` 完全一致** |

### A2（阻断）打开「我的收藏」不写库

**实现**：删掉 `initVocab()` 尾部的 `await cetSyncLessonCollections()`，并删除
`cetSyncLessonCollections()` 函数本体（留着就是一条随时可被调用的写库通道）。

| 模式 | 证据 |
| --- | --- |
| CET | 打开 `#favorites`：网络记录**无** `sync-collections`；页面正常渲染（`body.innerText` 非空）、无 JS 报错 |
| 通用 | 5283 实测：同样无 `sync-collections`（原版守卫本来就不跑） |

**删除后的行为确认**：页面只读 `/vocab-log`，没有收藏就是 0 词 / 空态，不白屏不报错。

### A3 去掉收藏后多余的 activate

**实现**：删 `toggleWordSave()` 新增分支里的 `if (CET_ON) await autoActivateFavoriteWord(...)`，
并删除 `autoActivateFavoriteWord()` 函数整段。`addWordToReviewFromPopover()` /
`addSavedWordToReview()` / `activateWordReview()` **保留未删**（通用模式的用户主动入口）。

| 模式 | 证据（真实点击「☆ 收藏单词」） |
| --- | --- |
| CET | `requests_during_save: ["POST /api/v2/lessons/2/word"]`、`post_word_count: 1`、`activate_calls: []`、`/vocab-log` 里有该词 `okay`（带原句 `Okay, I'm hoping to finish in about an hour.`）✅ |
| 通用 | 5283 实测：同样 `post_word_count: 1`、`activate_calls: []`（原版本来就没有自动 activate） |

### A4 查词卡片文案

**实现**：走 `cet_copy` 三个键 + CET 分支不渲染「加入复习本」。

| 元素 | CET | 通用（= `3573e90`） |
| --- | --- | --- |
| 未收藏按钮 | **☆ 收藏单词** | **☆ 加入生词** |
| 已收藏按钮 | **✓ 已收藏** | **★ 已收藏** |
| 「加入复习本」按钮 | **不渲染** | **＋ 加入复习本** |
| 「已掌握」按钮 | ✓ 已掌握 | ✓ 已掌握 |
| 「已掌握」title | 标记已掌握：移出收藏，不再高亮 | 标记已掌握：移出生词本，不再高亮 |

实测 CET 工作区可见文本计数：`生词: 0`、`复习本: 0`、`加入生词: 0`、`加入复习本: 0` ✅

「本课收藏」面板同样处理：CET 只出状态标签（`收藏后进入「我的收藏」`/`✓ 已收藏`），
通用保留原「加入复习本」按钮。

### A5 `[hidden]` 真的隐藏

**实现**：`cet-workspace.css` 在 `.cet-study-item{display:inline-flex}` **之后**新增：

```css
.cet-study-bar .cet-study-item[hidden] { display: none !important; }
```

| 模式 | 证据 |
| --- | --- |
| CET | 对 `.cet-study-item` 设 `hidden=true` → `getComputedStyle().display === 'none'`；对照组（未设 hidden 时 `display: inline-flex`）成立，证明不是「本来就 none」 |
| 通用 | 同一 CSS 规则不分模式，5283 实测同样生效 |

**`setShown()` 保留**：`cet-player.js` **一字未改**（任务书 A5 明确要求），行内样式
优先级更高，与新增规则不冲突。

**缓存串**：`cet-workspace.css?v=20261105-p1` → `?v=20261106-fix21`，否则真实用户刷新后
仍拿旧 CSS，A5 对老用户不生效。

## 5. 测试

### 5.1 新增测试（2 个文件，37 条）

```bash
cd /tmp/wt-fix21-a
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_cet_fix21_a_*.py
```

**静态契约（25 条，0.5 秒，不需要服务）**：

```
25 passed in 0.46s
```

> 注：`test_a4_copy_keys_are_paired_and_use_fallbacks` 在本分支会失败 1 条 ——
> 因为它断言「模板用到的 `fix2_master_hint` 键必须在 `cet_mode.py` 里」，
> 而该键由主代理在集成分支维护（本分支无 `cet_mode.py` 改动权限）。
> 在合并态（`cet_mode.py` 含主代理修复）下 **25 passed**，已验证。

**浏览器端到端（12 条）**：

```bash
# CET 模式（全新库）
CET_BASE_URL=http://127.0.0.1:5281 ELT_CONFIG_DIR=$(mktemp -d) \
  .venv/bin/python -m pytest -q tests/test_cet_fix21_a_browser.py
# → 10 passed, 2 skipped in 42.22s

# 通用模式对照（ELT_CET_MODE=0 服务）
CET_BASE_URL=http://127.0.0.1:5283 CET_MODE=generic ELT_CONFIG_DIR=$(mktemp -d) \
  .venv/bin/python -m pytest -q tests/test_cet_fix21_a_browser.py
# → 5 passed, 7 skipped in 22.56s
```

跳过的都是「模式专用」用例（`CET_MODE=generic` 时跳过 CET 专用 7 条；反之跳过通用专用）。

**测试自身踩过的一个坑（已修，记录备查）**：`test_a1_subtitle_highlight_still_works`
最初直接断言「进页面后 DOM 有 `.highlight-word`」→ 失败。原因是工作区
`#current-subtitle` **只渲染当前一句**，第 0 句恰好没有高亮词。改成「先 seek 到
API 里第一个有高亮的句子再断言」后通过。这是测试写错，不是产品缺陷。

### 5.2 老测试回归

```bash
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
  tests/test_cet_fix2_p1_workspace.py tests/test_cet_base_player.py tests/test_cet_fix2_p4_favorites.py
# → 3 failed, 84 passed, 5 skipped in 1.08s
```

3 条失败**全部**是本轮反转导致的、已由主代理认领改写的断言（见 §6）。

### 5.3 全量回归

```bash
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
  --ignore=tests/test_cet_listening_ui.py --ignore=tests/test_cet_contract.py
# → 6 failed, 925 passed, 74 skipped in 234.58s
```

基线（`3573e90`）：**2 failed / 904 passed / 62 skipped**（2 项固定失败：bdpan CLI 未装、
planning 私有模块缺失）。

6 条失败拆解：

| # | 失败 | 归属 | 说明 |
| --- | --- | --- | --- |
| 1 | `test_baidu_pan.py::test_download_file_verifies_artifact` | 基线已有 | bdpan CLI 未装，与本轮无关 |
| 2 | `test_health_planning_enabled.py::test_health_reports_planning_enabled_when_module_present` | 基线已有 | planning 私有模块缺失，与本轮无关 |
| 3 | `test_cet_fix2_p1_workspace.py::test_p1_3_auto_activate_uses_existing_endpoint_with_listening_source` | **本轮反转** | 主代理认领改写为反向断言 |
| 4 | `test_cet_fix2_p4_favorites.py::test_p4_1_frontend_syncs_on_favorites_load` | **本轮反转** | 同上 |
| 5 | `test_cet_fix2_p4_favorites.py::test_p4_1_sync_does_not_run_in_generic_mode` | **本轮反转** | 同上 |
| 6 | `test_cet_fix21_a_frontend.py::test_a4_copy_keys_are_paired_and_use_fallbacks` | **本分支缺 `cet_mode.py`** | 合并态下通过（已验证 25 passed） |

**净新增失败 0 条**（除上述 4 条待主代理改写 + 1 条合并即消）。

## 6. 需要主代理在合并时处理的事项

按主代理裁决 1，以下返修2 断言因本轮反转失效，**由主代理统一改写为反向断言**，
本包**未改动**这两个文件：

- `tests/test_cet_fix2_p1_workspace.py:145` `test_p1_3_auto_activate_uses_existing_endpoint_with_listening_source`
- `tests/test_cet_fix2_p1_workspace_browser.py:537` `test_p1_3_favoriting_a_word_makes_it_appear_in_my_collections`
  （这条是**浏览器测试**，主代理裁决里未列，但同样会挂 —— 它断言点收藏后必须发出 `activate`）
- `tests/test_cet_fix2_p4_favorites.py:256`（端点存在）、`:268`（activate 幂等，保留+改名）、
  `:277`（前端补同步）、`:290`（通用模式不跑）、`:406` / `:417` / `:425`（服务端点）、`:432`（收藏页显示）

## 7. `cet_copy` 键

**本包未新增、未修改 `cet_mode.py`**（不在我名下）。模板侧用到的键：

| 键 | CET 值 | 通用值 | 状态 |
| --- | --- | --- | --- |
| `fix2_favorite_word_cta` | ☆ 收藏单词 | ☆ 加入生词 | 主代理已修正通用值 ✅ |
| `fix2_favorite_added` | ✓ 已收藏 | ★ 已收藏 | 主代理已修正通用值 ✅ |
| `fix2_word_in_favorites` | 收藏后进入「我的收藏」 | 收藏后进入「我的收藏」 | 已有 ✅ |
| `fix2_master_hint` | 标记已掌握：移出收藏，不再高亮 | 标记已掌握：移出生词本，不再高亮 | **本包提出、主代理已加** ✅ |
| `fix2_lesson_collections` / `_note` | 本课收藏 / … | 学习收集 / … | 已有 ✅ |
| `fix2_intensive_entry` / `_need_timeline` | 逐句精听 → / 需要逐句时间轴 | 同 | 已有 ✅ |

**无缺失键。** 所有取值都带兜底（`cetCopy(key, fallback)`），键缺失时不白屏
（有测试 `test_a4_copy_keys_are_paired_and_use_fallbacks` 锁定）。

> 我原先发现的两个真 bug（`fix2_favorite_word_cta` 通用值「☆ 收藏」≠ 原版「☆ 加入生词」；
> `fix2_favorite_added` 通用值「✓ 已收藏」≠ 原版「★ 已收藏」）已由主代理在集成分支修正，
> 本分支的浏览器测试在合并态下验证通过。

## 8. 未完成 / 已知限制（如实写）

1. **`buildReviewMarkdown()` 里的 `'## 生词 / 不理解的表达'` 未改**（`workspace.html:4950`）。
   它在 `<script>` 内，是「复制 Markdown」导出的**文件标题**，不在页面上，用户看不到。
   任务书 A4 的验收口径是「CET 模式工作区**可见文本**中『生词』『复习本』出现 0 次」，
   已满足（实测 0 次）。若要连导出文案也改，需要 `cet_copy` 新键，属额外范围 —— 未做。
2. **`addWordToReviewFromPopover()` / `addSavedWordToReview()` 两个函数保留**，
   只在 CET 模式下不渲染入口按钮。这是主代理明确要求的（删函数会牵动更大范围）。
   函数里仍有「加入复习本」中文字面量（`button.textContent = '＋ 加入复习本'`），
   但 CET 模式下这些按钮不存在，用户看不到。
3. **`tests/test_cet_fix2_p1_workspace_browser.py` 未跑通用模式**。该文件是返修2 的
   CET 专用浏览器测试，不在本包范围。
4. **精听页只验证了「重点词汇」计数与同步门控**，未重跑精听页的全部交互
   （音标、口语分析、标签编辑等）—— 那些不在本包改动面内，且
   `test_cet_fix2_p1_workspace_browser.py` 的 3 条 P1-1 测试已覆盖主入口。
5. **`test_cet_fix21_a_frontend.py` 有 1 条在本分支单跑会失败**（缺 `cet_mode.py` 的
   `fix2_master_hint`），合并态下通过。已如实记录，不是隐藏失败。

## 9. 环境与复现

```bash
# 服务（CET）
W=/tmp/wt-fix21-a; D=/tmp/fix21-a-fresh; mkdir -p "$D/config"
cd "$W" && ELT_USER_DB="$D/vocab.db" ELT_CONFIG_DIR="$D/config" ELT_PORT=5281 \
  ELT_DATA_DIR="$D" .venv/bin/python backend/fastapi_server.py
ELT_USER_DB="$D/vocab.db" ELT_CONFIG_DIR="$D/config" ELT_DATA_DIR="$D" \
  .venv/bin/python scripts/cet/import_materials.py --all

# 服务（通用模式对照）
D=/tmp/fix21-a-generic; mkdir -p "$D/config"
cd "$W" && ELT_CET_MODE=0 ELT_USER_DB="$D/vocab.db" ELT_CONFIG_DIR="$D/config" ELT_PORT=5283 \
  ELT_DATA_DIR="$D" .venv/bin/python backend/fastapi_server.py
```

截图见 `evidence/`：
- `a1-a4-workspace-cet-wordcard.png` —— CET 工作区 + 查词卡片（☆ 收藏单词 / ✓ 已掌握，无「加入复习本」）
- `a1-intensive-cet-focus-count.png` —— 精听页「重点词汇 9」
- `a5-before-hidden.png` / `a5-after-hidden.png` —— 「本句循环」设 hidden 前后
