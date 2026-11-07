# 包 A · 卡点模块前端修复交付说明

分支：`codex/cet-marks-fix-a`
基线：`e8fc12b`（`cet-base-delivery`）
修复范围：**仅前端**（`frontend/static/cet-listening.js` / `.css` + 新增测试），未改 `backend/**`。

---

## 一、结论速览

6 项缺陷全部修复，其中 1/3 为 P0，均给出**真实浏览器实测数据**（真实服务 + 真实按键，无 `route.abort()`、无静态 HTML 断言）。

| # | 严重度 | 缺陷 | 状态 | 关键证据 |
| --- | --- | --- | --- | --- |
| 1 | 🔴 P0 | 打完标记自动回听，抢走播放位置 | ✅ 修好 | 改前 3.738s → 0.058s；改后 3.758s → 3.884s（不回退） |
| 2 | 🔴 | 连续标记大量重复落同一句 | ✅ 修好 | 改前 `S.marks` 3→5（delta 2）；改后 3→4（delta 1），与库一致 |
| 3 | 🔴 P0 | 无题/无时间轴材料出现假入口 | ✅ 修好 | 改前 `toolbar=true`；改后 `toolbar=false`，无 `#cet-toggle-mode`/`#cet-review-btn` |
| 4 | 🟡 | 标记时间与候选句口径混乱 | ✅ 修好 | 改前 `0:03.74 … 预览 0:00.01–0:03.49`；改后 `标记于 0:03.76 → 候选句 0:00.01–0:03.49` |
| 5 | 🟡 | 删除按钮无条件可点 | ✅ 修好 | 改前 `disabled=false` 且点击发出 DELETE→409；改后 `disabled=true`、无请求 |
| 6 | 🟡 | 时间线刻度与列表口径不一致 | ✅ 修好 | 改前刻度 `3.742`（按键时刻）；改后 `0.01`（候选句起点） |

测试结果：**静态 28 passed / 浏览器 14 passed**（旧后端下 A5 相关 2 条自动 skip，见 §六）。

---

## 二、问题 1（P0）：自动回听抢走播放位置

### 根因

`createMark()` 末尾无条件调用：

```js
previewMark(mark, mark.suggestedSentenceId, { autoplay: true });   // ← 元凶
```

`previewMark` 的 `autoplay` 分支会 `replayRange(sent.startSeconds, sent.endSeconds)`，
即把 `currentTime` **seek 回候选句起点**并播放。用户按完 M 立刻被拽走，于是连按几次都落在同一处。

### 修法：默认关闭 + 给开关（**保留能力，不是删掉功能**）

```js
if (S.autoplayOnMark) {
  previewMark(mark, mark.suggestedSentenceId, { autoplay: true });
}
```

- 开关位置：`#cet-toolbar` 内，与「开始卡点练习 / 今日复习」并列（`#cet-autoplay-mark`）
- 文案：「标记后自动回听」，带 `title` 说明开启/关闭的差别
- 默认值：**关闭**
- 持久化键：`cet.marks.autoplay.v1`（值为 `'1'`/`'0'`）
  - 为什么用独立键而不是塞进 `cet.state.v2.<materialId>`：这是**跨材料的使用习惯**（用户偏好），
    不是某份材料的练习进度；塞进按材料分键的状态里，换个材料又要重设一次。

### 实测数据（真实浏览器，workspace/1，1x 倍速）

**改前**（`.cet-qa/probe_a_before.json` → `evidence/probe_a_before.json`）：

```
第1次: 按键前 3.738 → 按后120ms 0.058 → 1.5s后 1.457
第2次: 按键前 1.458 → 按后120ms 0.074 → 1.5s后 1.475
第3次: 按键前 1.476 → 按后120ms 0.069 → 1.5s后 1.469
3 个标记的 pressedAtSec: [3.742, 1.459, 1.459]   ← 后两个完全相同
rewind_detected: true
```

**改后**（`evidence/probe_a_after.json`）：

```
第1次: 按键前 3.758 → 按后120ms 3.884 → 1.5s后 5.283
第2次: 按键前 5.284 → 按后120ms 5.404 → 1.5s后 6.804
第3次: 按键前 6.804 → 按后120ms 6.938 → 1.5s后 8.338
3 个标记的 pressedAtSec: [3.76, 5.284, 6.805]   ← 各不相同
rewind_detected: false
```

**能力保留验证**（打开开关后行为回到原样）：

```
autoplay_on_state:  { s: true, stored: '1' }
autoplay_on_steals: { before: 40.742, after_300ms: 36.995, steal_detected: true }
```

### 附带修掉的一个真实缺陷（实测发现）

勾完复选框后焦点留在 `<input>` 上，而 `isTypingTarget()` 把 `input` 一律当输入目标
→ **之后按 M 全被吞掉**（实测：勾完开关按 M 不产生标记）。
修法：`setAutoplayOnMark(..., { blur: true })` 里主动 `blur()`（与 `setListening()` 里 blur 按钮同一道理）。

---

## 三、问题 2：连续标记重复

### 两层原因

1. **前端层（问题 1 的后果）**：位置被拽回 → 按键时刻重复 → 后端去重命中。
2. **前端层（独立缺陷）**：后端 1.5s 内去重时返回**已有**标记（同一个 `markId`），
   而 `createMark()` 无条件 `S.marks.push(mark)` → UI 显示 5 个、库里只有 2 条。

### 修法：按 markId 查重，存在则就地更新

```js
const idx = S.marks.findIndex((x) => x.markId === mark.markId);
const isNew = idx < 0;
if (isNew) {
  S.marks.push(mark);
} else {
  Object.assign(S.marks[idx], mark);      // 同一条标记，不新增行
}
```

去重命中时提示改为「与 X 的标记是同一处（1.5s 内重复），未新增」，避免「按了没反应」的困惑。

### 实测数据

| | `S.marks` 变化 | DOM 行数 | 库内条数 |
| --- | --- | --- | --- |
| 改前 | 3 → **5**（delta 2） | 5 | 2 |
| 改后 | 3 → **4**（delta 1） | 4 | 4 |

改后浏览器用例还做了三方对账：`S.marks` 长度 == DOM 行数 == `GET /api/cet/marks` 条数，
且 `markId` 无重复。

---

## 四、问题 3（P0）：无时间轴材料出现假入口

### 根因

`boot()` 拿到材料详情后**直接 `mount()`**，不看 `sentences` 是否为空。

### 修法：在 `boot()` 里加「材料是否适用」这一层

```js
if (!S.sentences.length) {
  console.info(`[cet] 材料 ${S.materialId} 没有逐句时间轴（sentences=0），不挂载卡点工具栏：…`);
  return;      // ← 不 mount()
}
```

### 关键：没有改错层

- 模板里的 `{% if cet_features.cet_listening_agent %}` 管的是「**模块是否加载**」（功能开关 / 有没有接入）
- 这里管的是「**这份材料能不能用**」（有没有逐句时间轴）

两层不同，模板一行未改。测试 `test_a3_template_feature_flag_is_untouched` 专门锁定这一点。

### 实测数据（真实浏览器）

先导入一份**只有音频、无时间轴**的材料：

```bash
scripts/cet/import_materials.py --audio <mp3> --title "无时间轴材料（仅音频）" --level cet4
# → 新建 v2-3｜无时间轴材料（仅音频）｜80.5s｜无逐句时间轴
# by-lesson/3 → sentenceCount 0 / sentences len 0
```

**改前**（`evidence/probe_a3_before.json`）：

```
lesson3: toolbar=true, toolbarText="开始卡点练习 今日复习", cetS={mid:v2-3, sent:0}
点「开始卡点练习」→ listening=true, bodyClass="cet-listening cet-hide-text"
按 M → hint="标记失败：该材料没有可用句子时间轴。（可重试）", marks=0
```

→ 入口能点、点了才报错 = 假入口（复现主代理审计结论）。

**改后**（`evidence/probe_a_after.json`）：

```
lesson3: toolbar=false, toolbarText=null, cetS={mid:v2-3, sent:0}
lesson1（对照，32 句）: toolbar=true, toolbarText="开始卡点练习 今日复习 标记后自动回听"
pageerrors: []
```

→ 无时间轴材料不再挂载；有题材料照常挂载。

---

## 五、问题 4/5/6

### 问题 4：时间口径混乱

**改前**（同一行两个不同口径并排）：

```
0:03.74 待确认 It's getting late… 预览 0:00.01–0:03.49
```

**改后**（两段式 + 箭头 + 显式标签）：

```
标记于 0:03.76 待确认 → 候选句 0:00.01–0:03.49 It's getting late…
```

- `标记于 X`：`title` = 「按 M 那一刻的播放位置（不是候选句位置）」
- `→`：`.cet-mark-arrow`，视觉上分开两段
- `候选句 A–B` / `确认句 A–B`：待确认时是候选句，已确认后是确认句
- 无候选句时显示「（无可用候选句）」，不让用户以为标记没生效

### 问题 5：删除按钮无条件可点

**首版修法有缺陷（B 发现、主代理核实、已改）**：首版用 `markState(m) === 'done'` 判定，
而 `markState()` 只在 `S.practice.status === 'complete'` 时返回 `'done'`；
`cet_practices.status` 默认是 `'active'`，只有带练**完整走完**才变 `'complete'`。
后端 `delete_mark()` 的拒绝条件是「`cet_practices` 里有没有对应行」，**与 status 无关**。
所以「已开始带练但未完成（active）」这个**最常见**状态下，首版按钮仍可点 → 用户点下去才收到 409。

**最终修法：读后端透出的 `inPractice`（backend `b1c51cd`）**

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

- 严格 `=== true`：`inPractice` 可能是 `null`（旧接口 / 未查场景），`null` 按「可点」处理，由后端兜底
- 不用 `!m.inPractice` 取反：语义容易反，`null` 会被误判成禁用 → 旧路径下所有标记都删不掉

**补充修掉一个窗口期**（B 特别肯定）：`startPractice()` 成功后本地 `m.inPractice` 还停在建立标记时的
`false`，要等下次刷新列表才禁用。现在成功后立刻 `m.inPractice = true`（占用状态由「刚建的带练」唯一确定，
不需要再问后端）。

**实测数据（active 态，真实链路）**

改前（首版 `done` 判定，`evidence/probe_active_before.json`）：

```
practice_status: "active"
markState_of_row: { badge: "待练", inPractice: true }     ← 徽章是「待练」，不是「本轮已练」
delete_button_in_active: { disabled: false, title: "删除这个标记", ariaDisabled: null }
点击删除 → delete_requests: ["DELETE /api/cet/marks/mark-7fcbb006b121"]
         → hint: "该标记已开始带练，不能删除"            ← 点了才知道
```

改后（`evidence/probe_active_after.json`）：

```
practice_status: "active"
markState_of_row: { badge: "待练", inPractice: true }
delete_button_in_active: { disabled: true, title: "该标记已开始带练，不能删除",
                           ariaDisabled: "true", blocked: "1" }
点击删除 → delete_requests: []                            ← 不发请求
api_list_marks: [{ markId: …, inPractice: true, hasField: true }]
pageerrors: []
```

### 问题 6：时间线刻度口径

**改前**：`const sec = Number(m.pressedAtSec) || 0;` → 刻度画在按键时刻，
而列表说的是候选句、点下去跳的又是候选句 —— 三处两个位置。

**改后**（与列表同口径）：

```js
const sentId = st === 'pending' ? previewSentenceId(m) : m.confirmedSentenceId;
const sent = findSentence(sentId);
const sec = sent ? Number(sent.startSeconds) : (Number(m.pressedAtSec) || 0);
```

- `title` 改为能对账的文案：`0:00.01 候选句 · 待确认（0:00.01–0:03.49｜标记于 0:03.76）`
- 保留 `data-cet-mark-sec`（测试依赖），新增 `data-cet-mark-sentence` / `data-cet-mark-pressed`
- 候选句缺失时兜底用 `pressedAtSec`（不丢刻度）

**实测数据**

| | 刻度 `data-cet-mark-sec` | title |
| --- | --- | --- |
| 改前 | `3.742` / `1.459` ×4 | `0:03.74 · 待确认` |
| 改后 | `0.01` / `0.01` / `3.55` / `3.55` | `0:00.01 候选句 · 待确认（0:00.01–0:03.49｜标记于 0:03.76）` |

浏览器用例还验证：刻度秒数与候选句 `startSeconds` 误差 < 0.02s；真实点击刻度后播放位置落在
`[刻度, 刻度+1.2s]` 内（`replayRange` 会继续播放，故按区间判定）。

---

## 六、测试

### 新增文件（未改任何既有 `tests/test_cet_*.py`）

| 文件 | 用例数 | 说明 |
| --- | --- | --- |
| `tests/test_cet_marks_fix_a_frontend.py` | 28 | 静态契约 + 反向锁定（不起服务，秒级） |
| `tests/test_cet_marks_fix_a_browser.py` | 14 | 真实服务 + 真实按键（需 `CET_BASE_URL`） |

设计要点：

- 大量「反向断言」锁旧写法不再出现（只断言新代码存在，会漏掉「旧调用被加回来」这类回归）
- 所有「不得出现」的断言都在**去注释后的代码**上做 —— 本轮注释里会自然出现被删掉的旧代码原文，
  直接在原文上做子串匹配会把「注释里说不要这么写」误判成「代码里还这么写」
- 「调用仍在但必须被守卫」的场景（如 `previewMark(autoplay)`）用 `_assert_guarded()` 检查
  调用前紧邻的代码是不是那个 `if`，而不是简单断言「调用不存在」

### 运行命令与结果原文

```bash
cd /tmp/wt-marks-a

# ① 静态
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_cet_marks_fix_a_frontend.py
# 28 passed in 0.03s

# ② 浏览器（先起 5281 + 导入材料）
CET_BASE_URL=http://127.0.0.1:5281 ELT_CONFIG_DIR=$(mktemp -d) \
  .venv/bin/python -m pytest -q tests/test_cet_marks_fix_a_browser.py
# 14 passed in 67.69s

# ③ 队友链路不回归
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
  tests/test_cet_teammate_e2e.py tests/test_cet_backend.py
# 21 passed, 1 skipped in 1.89s

# ④ 全量回归（排除基线即固定失败的 2 个文件）
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
  --ignore=tests/test_cet_listening_ui.py --ignore=tests/test_cet_contract.py
# 2 failed, 1043 passed, 87 skipped in 208.20s
```

全量回归的 2 failed 与基线**同类且无关**：`test_baidu_pan.py`（bdpan 未安装）、
`test_health_planning_enabled.py`（planning 私有模块缺失）。基线为 2 failed / 1017 passed / 74 skipped，
passed 增加 26 = 本包新增的 26 条静态用例（浏览器用例在未设 `CET_BASE_URL` 时 skip）。

### 变异测试（证明用例真能抓回归）

| 变异 | 预期红 | 实际 |
| --- | --- | --- |
| 把 `previewMark(autoplay)` 改回无条件 | A-1 静态 | ✅ 2 failed |
| 删掉 `boot()` 的无句子守卫 | A-3 静态 | ✅ 2 failed |
| 刻度改回 `pressedAtSec` | A-6 静态 | ✅ 1 failed |
| 删除按钮改回 `st === 'done'` | A-5 静态 | ✅ 2 failed |
| 删除按钮改回 `st === 'done'` | A-5 浏览器（active 态） | ✅ 1 failed（文案精确指出缺陷 5 未修好） |
| **整套改前代码**（`git show HEAD:` 原版 JS+CSS） | 浏览器全套 | ✅ **12 failed / 1 passed**（唯一通过的是「有题材料照常挂载」对照组，符合预期） |

---

## 七、未完成项 / 已知边界

1. **A5 浏览器用例依赖后端 `>= b1c51cd`**
   本包只改前端，基线后端（`e8fc12b`）没有 `inPractice` 字段。
   在旧后端上，A5 的两条浏览器用例会**自动 skip 并说明原因**（`_require_in_practice_field`），
   不是报红 —— 那是后端版本不匹配，不是前端缺陷。
   合并 `b1c51cd` 后 14 条全跑（已在临时应用 `b1c51cd` 后端的环境下验证：14 passed）。

2. **未做视觉截图**
   交付说明用**文本实测数据**（`innerText`、属性值、`currentTime`）而非截图。
   原因：本轮 6 项缺陷都是行为/口径问题，文本数据可精确对账、可复跑；
   截图无法证明 `currentTime` 是否回退。`evidence/` 下留存的是探针的原始 JSON 输出。

3. **问题 2 的后端语义改进不在本包**
   后端 `deduplicated` 字段（`4eef2b4`）由主代理完成；本包前端用 `markId` 查重，
   不依赖该字段（两种后端都能正确去重）。

4. **缓存版本号未改**
   ~~`workspace.html` 里 `?v=20261105-c8` 未动（属模板文件，本包未改模板）。~~
   **已在合并时补上**：主代理把 `cet-listening.js` / `cet-listening.css` 的缓存串
   统一升到 `?v=20261107-marks-fix`（`workspace.html:1950-1951`），
   否则真实用户刷新后会吃到旧 JS，本轮 6 项修复对老用户不生效。
   部署后若浏览器缓存旧 JS，需强制刷新或由集成方统一升版本号。

---

## 八、文件清单

```
frontend/static/cet-listening.js          修改（6 处缺陷 + 1 处 blur 修复 + 1 处状态同步）
frontend/static/cet-listening.css         修改（新增 .cet-autoplay-switch / .cet-mark-arrow / .cet-mark-sentence-none）
tests/test_cet_marks_fix_a_frontend.py    新增（28 用例）
tests/test_cet_marks_fix_a_browser.py     新增（14 用例）
docs/cet-base/卡点修复/包A-交付说明.md      新增（本文件）
docs/cet-base/卡点修复/evidence/*.json     新增（探针原始输出）
```

未改动：`backend/**`、`frontend/static/cet-player.js`、`frontend/templates/index.html`、
`frontend/templates/workspace.html`、任何既有 `tests/test_cet_*.py`。
