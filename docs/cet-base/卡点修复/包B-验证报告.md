# 卡点模块 6 项缺陷 · 包 B 独立交叉验证报告

| 项 | 值 |
| --- | --- |
| 验证者 | 子代理 B（独立交叉验证者，非修复者） |
| 验证对象 | 子代理 A 的前端修复（`codex/cet-marks-fix-a`）+ 主代理后端加固（`b1c51cd`） |
| worktree | `/tmp/wt-marks-b`（分支 `codex/cet-marks-fix-b`） |
| 基线 | `e8fc12b` → rebase 到 `cet-base-delivery`（`b1c51cd`） |
| 服务 | 端口 5282，隔离数据目录 `/tmp/marks-b-svc` |
| 我的测试 | `tests/test_cet_marks_fix_b_verify.py`（**自己写，不复用 A 的测试文件**） |

## 0. 一句话结论

**6 项缺陷全部修好（24/24 测试绿）。**

但缺陷 5 不是一次修好的 —— 它走了一个闭环，而这个闭环正是本轮交叉验证的核心产出：
1. A 首版：`markState()==='done'` 时禁用删除按钮 → **我判定未修好**，因为
   `markState` 只在 `practice.status==='complete'` 时返回 `'done'`，而后端
   `delete_mark()` 的拒绝条件是「`cet_practices` 有没有行」，与 status 无关；
   `status` 默认是 `'active'`，所以「已开始带练但没练完」这个最常见状态下按钮仍可点。
2. 主代理据我的证据补了后端 `inPractice` 字段（`b1c51cd`）。
3. A 采纳 `inPractice` 并补了「开始带练后立刻同步本地状态」。
4. 我复验：**24/24 绿**，三个边界（`false`/`true`/`null`）全部正确。

**如果我只是「跑一下 A 的测试看绿不绿」，这个缺陷会被漏掉** —— 因为 A 的首版在他自己
的测试口径下是「通过」的。

## 1. 三列对照（这是本报告的核心证据）

同一套 24 个用例，在三个代码状态上各跑一遍：

| # | 缺陷 | 基线<br>（无 A 改动） | A 中期预览<br>（A 首版 + 后端 `b1c51cd`） | A 最终预览<br>（A 采纳 `inPractice` 后） |
| --- | --- | --- | --- | --- |
| 1 | 自动回听抢播放位置 | ❌ 未修 | ✅ 修好 | ✅ **修好** |
| 2 | 连续标记重复落同一句 | ❌ 未修 | ✅ 修好 | ✅ **修好** |
| 3 | 无时间轴材料假入口 | ❌ 未修 | ✅ 修好 | ✅ **修好** |
| 4 | 时间与候选句口径混乱 | ❌ 未修 | ✅ 修好 | ✅ **修好** |
| 5 | 删除按钮无条件可点 | ❌ 未修 | ❌ **仍未修** | ✅ **修好** |
| 6 | 时间线刻度口径不一致 | ❌ 未修 | ✅ 修好 | ✅ **修好** |
| | **合计** | **14 failed / 10 passed** | **2 failed / 22 passed** | **0 failed / 24 passed** |

**最终结论：6 项全部修好（24/24 绿）。** 缺陷 5 经过一个「A 首版不够 → 主代理补后端
`inPractice` → A 采纳并同步本地状态」的闭环才真正修完 —— 这个闭环正是本轮交叉验证抓出来的。

> 「A 工作区预览」是怎么跑的：A 当时未 commit，我用 `git -C /tmp/wt-marks-a diff` 导出补丁，
> `git apply --check` 确认干净后应用到我的临时分支。**没有 stash、没有改动 A 的工作区**。

## 2. 逐项验证详情

### 缺陷 1 · 打完标记自动回听，抢走播放位置 —— ✅ 修好

**原始症状**（主代理实测）：按 M 时 `currentTime=2.96` → 按完 100ms 变成 `0.03`。

**A 的修法**：新增「标记后自动回听」开关（`S.autoplayOnMark`，默认 `false`，持久化到
`localStorage['cet.marks.autoplay.v1']`），`createMark` 里用 `if (S.autoplayOnMark)` 包住
`previewMark(..., {autoplay:true})`。

**我的验证**：
- 静态（`test_defect1_no_unconditional_autoplay_in_source`）：`previewMark(..., {autoplay:true})`
  这个调用**可以存在**（正确修法是保留能力），但它前面必须有 `autoplayOnMark` 守卫。实测通过。
- 浏览器（`test_browser_defect1_playhead_monotonic_when_autoplay_off`）：真实 chromium，
  关闭自动回听 → 进入标记模式 → `currentTime=3.0` → 连按 3 次 M（间隔 420ms，> 前端 300ms
  防连发阈值、< 后端 1.5s 去重窗口），每次按键前后读 `currentTime`。

实测样本（`evidence/defect1-playhead.json`）：
```json
[{"i":0,"before":3,"after":3},{"i":1,"before":4.2,"after":4.2},{"i":2,"before":5.4,"after":5.4}]
```
**按键前后位置完全一致，单调递增，没有回退。** 基线版本此项红（位置被 seek 回候选句起点）。

### 缺陷 2 · 连续标记重复落同一句 —— ✅ 修好

**原始症状**：连按 3 次 M：`5.60 → 2.14 → 2.14`（后两次同点）；库里同 session 内 `v2-1-0` 出现 2 次。

**A 的修法**：`createMark` 里按 `markId` 用 `findIndex` 查重 —— 已存在就 `Object.assign` 更新，不新增。

**我的验证**：
- 后端（`test_defect2_backend_dedup_returns_same_mark_id` / `..._stores_only_one_row`）：
  1.5s 内连按 2 次返回同一 `markId`、`deduplicated` 第 1 次 `false` 第 2 次 `true`、库里只有 1 行。
- 浏览器（`test_browser_defect2_no_duplicate_mark_ids_in_state`）：连按 3 次 M，
  断言 `S.marks` 里 **markId 唯一** 且 **前端数量 ≤ 库里行数**。

实测（`evidence/defect2-dedup.json`）：
```json
{"frontend": {"marksLen": 1, "markIds": ["mark-46336ad1025f"], "pressed": [5.6], "domRows": 1},
 "dbRowCount": 1, "dbIds": ["mark-46336ad1025f"]}
```
基线版本实测（同一用例）：`S.marks.length=3` / 库里 2 行 / `mark-1697932fadbc` 出现 2 次 —— 红。

### 缺陷 3 · 无题/无时间轴材料出现假入口 —— ✅ 修好

**原始症状**：lesson 3（`sentenceCount=0`）仍有「开始卡点练习/今日复习」工具栏；
点进去按 M 才报「该材料没有可用句子时间轴」。

**A 的修法**：`boot()` 里在 `mount()` **之前**加守卫 —— `if (!S.sentences.length) return;`。

**我的验证**：
- 静态（`test_defect3_source_guards_against_empty_sentences`）：守卫位置必须在 `mount()` 之前，
  且守卫体内有 `return`。
- 浏览器（`test_browser_defect3_no_toolbar_without_timeline`）：真实打开无时间轴材料工作区，
  断言 `#cet-toolbar` / `#cet-toggle-mode` / `#cet-review-btn` **全部不存在**。
- 反向保护（`test_browser_defect3_workspace_still_renders`）：不挂工具栏 ≠ 页面坏掉 ——
  播放器必须正常渲染、无 JS 错误。实测通过。

我造的无时间轴材料：`lessonId=3`、`materialId=v2-3`、`sentenceCount=0`、`hasTimeline=false`、
`hasTranscript=false`（只导入音频，无 SRT/TXT）。
后端侧 `409 TIMELINE_UNAVAILABLE` 也已独立验证。

### 缺陷 4 · 标记时间与候选句口径混乱 —— ✅ 修好

**原始症状**：列表显示 `0:05.60 | 待确认 | ... | 预览 0:00.01–0:03.49` —— 前者是按键时刻，
后者是回溯 2.5s 吸附的句子，并排看着像标错。

**A 的修法**：改成 `标记于 0:05.60 → 候选句 0:00.01–0:03.49`，两段显式标注关系，
并给 `title` 说明「按 M 那一刻的播放位置（不是候选句位置）」。

**我的验证**：
- 静态：`cet-mark-time` 的渲染表达式必须含「标记于」或「按键」；`候选句` 字样必须存在；
  旧的含糊写法「预览 X–Y」必须消失。
- 浏览器（`test_browser_defect4_mark_row_shows_both_denominations_distinctly`）：
  真实渲染的标记行 `innerText` 必须同时含「标记于」与「候选句」。实测通过。

### 缺陷 5 · 删除按钮无条件可点 —— ❌ **未修完**（本轮最重要发现）

**原始症状**：已带练的标记删除按钮照样可点，点了才弹「该标记已开始带练，不能删除」。

**A 的修法**：`markState()` 返回 `'done'` 时禁用删除按钮。

**为什么不够** —— 这是我在 A 交付前就抓到的，证据链如下：

```js
// 前端：只有 practice.status === 'complete' 才返回 'done'
function markState(m) {
  if (m.confirmedSentenceId) {
    return S.practice && S.practice.markId === m.markId && S.practice.status === 'complete'
      ? 'done' : 'ready';
  }
  return 'pending';
}
```
```python
# 后端：只看 cet_practices 有没有行，与 status 无关
practice = conn.execute("SELECT id FROM cet_practices WHERE mark_id=?", (mark_id,)).fetchone()
if practice:
    _error(409, "MARK_IN_PRACTICE", "该标记已开始带练，不能删除。", markId=mark_id)
```
而 `cet_practices.status` 的**默认值是 `'active'`**（`cet_service.py:114`），
只有带练完整走完才变 `'complete'`。

**真实链路实测**（走完整 API 流程，不是静态推断）：
```
1) attempt:  201 attempt-199d23f727d7
2) mark:     201 mark-07b9f03d65ec  suggested: v2-1-0
3) confirm:  200 v2-1-0
4) practice: 201 practice-f64ad24c7bf0  STATUS = 'active'      ← 最常见状态
5) delete:   409 MARK_IN_PRACTICE                              ← 后端拒绝
```
`practice.status='active'` → `markState` 返回 `'ready'` → **删除按钮仍可点** → 点下去才 409。
**A 的修法只覆盖了「本轮已练完」（`done`）—— 而那恰恰是用户已经练完、不太会去删的状态。**

**主代理已按我提的方案补了后端**（`b1c51cd`）：`_mark_payload` 新增 `inPractice` 字段，
其取值与 `delete_mark()` 的 409 拒绝条件**完全一致**。我独立验证了它的语义：

```
1) create     inPractice = False
2) practice   status = 'active'
3) list_marks inPractice = [True]
4) delete   -> 409 MARK_IN_PRACTICE
>>> inPractice=True 与后端 409 拒绝条件一致？ True
```
（`_mark_payload` 共 5 处注入：`create_mark` 的 existing / nearby / 新建三条路径、
`list_marks` 批量查、`confirm_mark`。）

**A 首版验证时前端还没采纳这个字段**（`grep -n "inPractice"` 零命中），所以当时实测是：
```json
{"inPractice": true, "delDisabled": false, "delTitle": "删除这个标记"}
```
**后端说「不能删」，前端按钮可点 —— 缺陷 5 未修完。** 这是我在 A 交付前抓到的核心问题。

**A 采纳后的最终状态：✅ 修好。** 他的实现（我逐行核对过）：
```js
if (m.inPractice === true) {
  del.disabled = true;
  del.title = '该标记已开始带练，不能删除';
  del.setAttribute('aria-disabled', 'true');
  del.setAttribute('data-cet-mark-delete-blocked', '1');
} else {
  del.title = '删除这个标记';
}
```
外加一处我特别赞赏的补充：`startPractice()` 成功后立刻 `m.inPractice = true`（只改本地对象、
不发请求），否则要等下一次刷新列表才会禁用 —— 这是用户点完「开始带练」马上就想删的窗口期。

**我验证的三个边界全部正确：**

| 边界 | `inPractice` | 删除按钮 | title |
| --- | --- | --- | --- |
| 未带练 | `false` | 可点 ✅ | 删除这个标记 |
| 已带练（刷新后） | `true` | **禁用** ✅ | 该标记已开始带练，不能删除 |
| 旧接口/未查 | `null` | 可点 ✅ | 删除这个标记 |

第三条尤其重要：A 写成 `=== true` 才禁用，而不是 `!m.inPractice` —— 后者会把 `null`
当成「禁用」，导致旧路径下所有标记都删不掉。他这里写对了。

### 缺陷 6 · 时间线刻度与列表口径不一致 —— ✅ 修好

**原始症状**：时间线刻度用 `pressedAtSec`（5.084），列表说的是候选句（0–3.49）；
点刻度跳到候选句而非刻度位置。

**A 的修法**：刻度位置改用候选句 `startSeconds`（候选句缺失时才退回 `pressedAtSec`），
并新增 `data-cet-mark-sentence` / `data-cet-mark-pressed` 两个属性。

**我的验证**：
- 静态：必须用 `startSeconds`；若保留 `pressedAtSec`，必须写成 `sent ? startSeconds : pressedAtSec`
  这种兜底形式（不能无条件用它）。
- 浏览器（`test_browser_defect6_timeline_tick_position_matches_candidate_sentence`）：
  读真实渲染的 `data-cet-mark-sec`，与 `S.sentences` 里候选句的 `startSeconds` 比对（容差 0.05s），
  并断言 `data-cet-mark-sentence` 与候选句 ID 一致。实测通过。

## 3. 我自己测试的 3 处缺陷（如实记录）

交叉验证者的测试也可能错。这三处我都是先跑出**假结果**、再定位、再改断言 ——
记录下来是因为它们直接影响结论可信度：

### 3.1 缺陷 2 的断言原本是**假绿**

我最初断言 `S.marks.length == DOM 行数`。但 DOM 行本来就是按 `S.marks` 渲染的，
两者**天然相等**，基线代码也能通过 —— 这个断言完全无法区分修好没修好。

实测发现：基线跑这个用例竟然 PASS，但真实症状明明存在。
换成能区分的口径后基线立刻红：
```
S.marks.length = 3 / 库里 2 行 / mark-1697932fadbc 出现 2 次
```
**正确断言**：`S.marks` 里 markId 必须唯一，且前端数量 ≤ 库里真实行数。

### 3.2 缺陷 5 的静态断言**假阳性**

我最初写 `assert "disabled = true" in src`。基线竟然 PASS —— 因为文件别处有
`$('#cet-transfer-play').disabled = true`（换句播放按钮），被宽松匹配骗过。

**收窄**：只截取 `renderMarks()` 里 `const del = el('button'` 之后 900 字符的**删除按钮块**。
收窄后基线立刻红。

### 3.3 缺陷 1 / 6 的断言**过严**（会把 A 判错）

我最初要求 `previewMark(..., {autoplay:true})` 和 `m.pressedAtSec` **字符串完全消失**。
但正确修法是：
- 缺陷 1：**保留能力 + 开关控制**（默认关闭），调用本身应该留着
- 缺陷 6：候选句缺失时**允许**用 `pressedAtSec` 兜底

按我最初的断言，A 做得对也会被判红。改成「必须被守卫包住」「必须写成兜底三元表达式」后，
A 的修复正确通过。

> **教训**：交叉验证的价值不在于「把对方判红」，而在于**断言必须精确对准缺陷的语义**。
> 断言太松 → 假绿；断言太紧 → 把对的判错。两者都会让报告失效。

## 4. 测试运行原文

### 4.1 基线（无 A 改动，只有后端 `b1c51cd`）

```bash
$ cd /tmp/wt-marks-b
$ CET_BASE_URL=http://127.0.0.1:5282 ELT_CONFIG_DIR=$(mktemp -d) \
    .venv/bin/python -m pytest -q tests/test_cet_marks_fix_b_verify.py
14 failed, 10 passed in 27.00s
```
失败清单（每条对应一个真实缺陷）：
```
test_defect1_no_unconditional_autoplay_in_source
test_defect2_frontend_dedupes_by_mark_id_in_source
test_defect3_source_guards_against_empty_sentences
test_defect4_list_labels_pressed_time_explicitly
test_defect4_candidate_sentence_is_labelled_as_candidate
test_defect5_frontend_disable_condition_uses_inpractice
test_defect6_timeline_uses_candidate_sentence_start
test_defect6_timeline_click_jumps_to_same_place_as_tick
test_browser_defect1_playhead_monotonic_when_autoplay_off
test_browser_defect2_no_duplicate_mark_ids_in_state
test_browser_defect3_no_toolbar_without_timeline
test_browser_defect6_timeline_tick_position_matches_candidate_sentence
test_browser_defect4_mark_row_shows_both_denominations_distinctly
test_browser_defect5_delete_button_state_for_practiced_mark
```

### 4.2 A 工作区预览（A 的改动 + 后端 `b1c51cd`）

```bash
$ CET_BASE_URL=http://127.0.0.1:5282 ELT_CONFIG_DIR=$(mktemp -d) \
    .venv/bin/python -m pytest -q tests/test_cet_marks_fix_b_verify.py
2 failed, 22 passed in 26.77s
```
失败清单（都是缺陷 5）：
```
test_defect5_frontend_disable_condition_uses_inpractice
test_browser_defect5_delete_button_state_for_practiced_mark
```

### 4.3 无服务时的静态子集

```bash
$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_cet_marks_fix_b_verify.py
```
（服务相关用例自动 skip，只剩静态断言）

## 5. 我的测试覆盖清单（24 个用例）

| 缺陷 | 用例 | 类型 |
| --- | --- | --- |
| 1 | `test_defect1_no_unconditional_autoplay_in_source` | 静态（守卫） |
| 1 | `test_defect1_marking_does_not_move_playhead` | 后端（数据契约） |
| 1 | `test_browser_defect1_playhead_monotonic_when_autoplay_off` | 🔴 浏览器 |
| 2 | `test_defect2_backend_dedup_returns_same_mark_id` | 后端 |
| 2 | `test_defect2_backend_stores_only_one_row` | 后端 |
| 2 | `test_defect2_frontend_dedupes_by_mark_id_in_source` | 静态 |
| 2 | `test_browser_defect2_no_duplicate_mark_ids_in_state` | 🔴 浏览器 |
| 3 | `test_defect3_source_guards_against_empty_sentences` | 静态 |
| 3 | `test_defect3_backend_rejects_mark_without_timeline` | 后端 |
| 3 | `test_defect3_no_timeline_material_reports_zero_sentences` | 前提核对 |
| 3 | `test_browser_defect3_no_toolbar_without_timeline` | 🔴 浏览器 |
| 3 | `test_browser_defect3_workspace_still_renders` | 浏览器（反向保护） |
| 4 | `test_defect4_list_labels_pressed_time_explicitly` | 静态 |
| 4 | `test_defect4_candidate_sentence_is_labelled_as_candidate` | 静态 |
| 4 | `test_browser_defect4_mark_row_shows_both_denominations_distinctly` | 浏览器 |
| 5 | `test_defect5_practice_status_is_active_not_complete` | 后端（取证） |
| 5 | `test_defect5_backend_rejects_delete_when_practice_exists` | 后端 |
| 5 | `test_defect5_backend_exposes_inpractice_field` | 后端（权威口径） |
| 5 | `test_defect5_inpractice_null_falls_back_to_clickable` | 静态（边界） |
| 5 | `test_defect5_frontend_disable_condition_uses_inpractice` | 静态 |
| 5 | `test_browser_defect5_delete_button_state_for_practiced_mark` | 浏览器 |
| 6 | `test_defect6_timeline_uses_candidate_sentence_start` | 静态 |
| 6 | `test_defect6_timeline_click_jumps_to_same_place_as_tick` | 静态 |
| 6 | `test_browser_defect6_timeline_tick_position_matches_candidate_sentence` | 浏览器 |

**任务书点名要真实浏览器验证的 3 条**（缺陷 1 单调递增、缺陷 2 状态一致、缺陷 3 无工具栏）
全部用真实 chromium + 真实按键 + 真实 DOM 完成，无 `route.abort()`、无静态 HTML 断言。

## 6. 发现的新问题

1. **`inPractice: null` 的语义需要前端明确**（我已加边界测试锁住）：
   主代理约定 `null` 按「可点」处理（后端兜底）。前端若写成 `if (!m.inPractice) del.disabled = true`
   会把 `null` 当成「禁用」，导致旧路径下所有标记都删不掉。必须写成 `inPractice === true` 才禁用。
   这条目前 A 还没实现，等他一并改。

2. **`markState()` 的 `done` 语义与后端不对齐**（缺陷 5 的根因，已由后端 `inPractice` 解决）：
   建议后续不要再让前端用 `S.practice.status` 推断「能否删除」—— 那是**本轮带练的本地状态**，
   刷新后拿不到其他标记的 practice 状态，天然不可靠。一律走后端字段。

3. **A 的两处改动没有对应测试**（我读他 diff 时看到的）：
   `setAutoplayOnMark()` 里有一个 `blur` 处理（勾完开关后焦点留在复选框上，
   `isTypingTarget()` 把 `input` 当输入目标 → 之后按 M 全被吞掉）。
   这是一个真实且隐蔽的 bug，A 修对了，但**他自己的测试文件里没有覆盖这条**。
   我建议补一条：勾选开关后立刻按 M，必须能产生标记。

## 7. 未完成项

| 项 | 状态 | 说明 |
| --- | --- | --- |
| A 正式 commit 后复跑 | ⏳ **待补** | A 验证时仍未 commit（`git log e8fc12b..HEAD` 为空，改动全在工作区）。我是用 `git diff` 导出补丁验证的。他 commit 后我 merge 正式复跑一次确认 SHA 一致。 |
| 缺陷 5 最终确认 | ✅ **已完成** | A 采纳 `inPractice` 后 24/24 全绿，三个边界也已验证。 |
| `inPractice: null` 边界 | ✅ **已完成** | A 写成 `=== true` 才禁用，`null` 按可点处理，验证通过。 |
| 全量回归 | ⏸ 未跑 | 我的改动只新增测试文件 + 文档，不改产品代码（`frontend/**` 的改动是 A 的预览补丁，不在我的交付里）。全量回归应由主代理在合并后跑。 |
| 证据截图 | ✅ 已存 | `docs/cet-base/卡点修复/evidence/`：`defect1-playhead.{png,json}`、`defect2-dedup.{png,json}`、`defect3-no-timeline.png`、`defect4-row.png`、`defect5-delete-button.png` |

## 8. 复现步骤

```bash
# 1. 服务（隔离数据）
mkdir -p /tmp/marks-b-svc/config
cd /tmp/wt-marks-b
ELT_USER_DB=/tmp/marks-b-svc/vocab.db ELT_CONFIG_DIR=/tmp/marks-b-svc/config \
  ELT_PORT=5282 ELT_DATA_DIR=/tmp/marks-b-svc \
  nohup .venv/bin/python backend/fastapi_server.py > /tmp/marks-b-svc/server.log 2>&1 &
sleep 10
ELT_USER_DB=/tmp/marks-b-svc/vocab.db ELT_CONFIG_DIR=/tmp/marks-b-svc/config \
  .venv/bin/python scripts/cet/import_materials.py --all

# 2. 造无时间轴材料（缺陷 3 需要）
.venv/bin/python /tmp/make_no_timeline.py     # 见下

# 3. 跑验证
CET_BASE_URL=http://127.0.0.1:5282 ELT_CONFIG_DIR=$(mktemp -d) \
  .venv/bin/python -m pytest -q tests/test_cet_marks_fix_b_verify.py
```

> pytest **只设 `ELT_CONFIG_DIR`**，绝不设 `ELT_USER_DB`（会覆盖 monkeypatch 造成库污染）。

## 9. 环境与纪律

- 未碰 5173（主代理在用）、未推送、未动 `cet-base-delivery`、未 merge 到共享分支。
- **未改 `frontend/**` 的产品代码**：A 的预览补丁只存在于我的**临时分支 / stash**，
  用于跑对照列，不进我的交付 commit。
- 未改 `backend/db.py`。
- 后端改动只有主代理的 `b1c51cd` / `4eef2b4`（我 rebase 带上，不是我自己写的）。
- A 的 worktree 我只读（`git diff` 导出补丁），**没有 stash、没有 checkout、没有改他的文件**。
