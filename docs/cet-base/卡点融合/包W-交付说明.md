# 包 W · 工作区布局 交付说明

- 分支：`codex/cet-drill-w`
- worktree：`/tmp/wt-drill-w`
- 基线：`c4a480d`
- 端口：`5302`
- 交付时间：2026-11-07
- 文件范围：只改 `frontend/templates/workspace.html`、`frontend/static/cet-workspace.css`、`frontend/static/cet-player.js`（未改 `cet-favorites.js`；未改任何 `tests/`）

---

## 一、W1–W7 完成情况

| 编号 | 状态 | 实现 |
|---|---|---|
| W1 | ✅ | `window.__ws` 新增 `playSentence(index,{rate,loop})`、`stopSentence()`、`setTranscriptHidden(bool)`、`mountStepPanel(stepKey,element)`、`setStepActions(element)`、`registerHotkeyHelp({key,label})`，以及只读 getter `transcriptHidden`。句播放实现放在 `cet-player.js`，与底座 L（句循环）/R（重听）共用 `sentenceIndexAt / seekToSeconds / togglePlay / setLoopAnchor` 原语，只有一套句播放状态。 |
| W2 | ✅ | 顶部步骤条 ① 盲听做题 ② 对答案·找卡点 ③ 精练·复习；读 `window.__cet.step` + 监听 `cet:step-change`；`cet_listening_agent=0` 时模板不渲染步骤条。 |
| W3 | ✅ | 工具栏改为两行：第一行字幕、倍速、逐句精听 →、⌨ 快捷键；第二行本步骤动作区（提示单行截断 + title，固定行高）。单选项「精听」按钮在卡点流程下不再显示。 |
| W4 | ✅ | 右栏单面板 + tab：「本步骤 / 大纲 / 收藏」；K 的题目、带练、复习都通过 `mountStepPanel` 放进「本步骤」，不再竖着堆叠。非当前步骤面板放在隐藏槽位容器里，保证 K 在初始化时一次注册三步面板也能先渲染，再按步骤切换。 |
| W5 | ✅ | 原文隐藏时 `renderOutline()` 只输出 `时间段 + N 个卡点`；卡点数从 `window.__cet.marks` 读取并映射到句子→大纲段；没有 `__cet`/`marks` 时不显示卡点数（不报错）。 |
| W6 | ✅ | ≤768：步骤条横向滚动、工具栏两行网格、右栏移到主区下方；已出 1440 / 1280 / 768 / 375 截图（见证据目录）。 |
| W7 | ✅ | 当前句卡片 `min-height`、`sentence-tools`、`vocab-chips/subtitle-meta` 空占位收紧；卡点列表紧跟在当前句之后（K 的 `#cet-marks` 在字幕卡内）。 |

### §2 接口行为（与主代理裁决一致）

- `playSentence(index, {rate, loop})`：越界 / 无播放器 → `Promise.resolve('interrupted')` + `console.warn`；自然播到句尾或媒体 `ended` → `'ended'`；`loop=true` 永不 `'ended'`，只由 `stopSentence()` / 用户操作打断；同一时间仅一条会话，新调用 / stop / 用户 pause、seek、jumpSentence、改倍速、切学习模式、页面隐藏 → 旧 Promise 立即 `'interrupted'`；`rate` 传入则临时设置并同步倍速下拉框，结束/打断后恢复调用前的值，不传则不改；`play()` 被拒 → `'interrupted'`。
- `stopSentence()`：有程序化会话时暂停并 resolve `'interrupted'`；无会话时也会停掉底座 L 循环（语义一致）。
- `setTranscriptHidden(bool)`：统一 toggle `body.cet-hide-text`（保留为 W/K 共用契约），同步 `aria-hidden`、关闭 word-popover；W 在 `cet-workspace.css` 补齐大纲/收藏面板规则。`false` 强制显示，若字幕下拉在「隐藏字幕」会强制切回可见，不恢复此前手动隐藏状态。
- `mountStepPanel(stepKey, element)`：`stepKey` 只接受 `blind | review | drill`；同 key 覆盖、null/undefined 清空；按步骤存槽位，当前步骤显示对应槽位，挂载当前步骤面板时自动切「本步骤」tab；「本步骤」tab 常驻（无内容显示空态）。
- `setStepActions(element)`：单槽位整体替换第二行；null/undefined 清空。
- `registerHotkeyHelp({key,label})`：Map 去重（同 key 覆盖），支持多次/多 key，仅影响帮助面板展示，不绑键；M 的监听仍在 K。

---

## 二、文件与行号

### `frontend/templates/workspace.html`

| 位置 | 内容 |
|---|---|
| 1676–1690 行 | W2 顶部步骤条 markup |
| 1693–1757 行 | W3 两行工具栏 markup（含 `#ws-hotkey-slot`、`#ws-step-actions`） |
| 1791–1828 行 | W4 右栏 tab「本步骤 / 大纲 / 收藏」+ `#ws-step-panel` + `#ws-step-panel-store` |
| 1992–1993、2043、2046 行 | 四个缓存串（K 两行 `20261107-drill-k`；W 两个 `20261107-drill-w`） |
| 2121 行起 | 步骤槽位/原文隐藏状态变量 |
| 2455–2459 行 | DOMContentLoaded：原文隐藏观察、步骤动作观察、`__cet.step` 首帧兜底 |
| 3479–3494 行 | W5 原文隐藏时的大纲渲染（时间段 + 卡点数） |
| 5780–6098 行 | W1/W2/W4/W5 全部 `window.__ws` 辅助实现 |
| 6159–6167 行 | `window.__ws` 新增字段（`playSentence` / `stopSentence` / `setTranscriptHidden` / `mountStepPanel` / `setStepActions` / `registerHotkeyHelp` / `transcriptHidden` / `hotkeyHelp`） |

### `frontend/static/cet-player.js`

| 位置 | 内容 |
|---|---|
| 77–91 行 | 句播放会话状态（`sentenceSession`、`rateApplyInProgress`、静默窗等） |
| 467–503 行 | `HOTKEY_HINTS` + `REGISTERED_HOTKEY_HINTS`、`hotkeyHintRows`、`registerHotkeyHelp` |
| 696–970 行 | 统一句播放会话：`playSentence` / `stopSentence` / `interruptSentencePlayback` / 句尾/循环/打断/倍速恢复 |
| 1059–1120 行 | `buildHotkeyHint` 支持挂到 `#ws-hotkey-slot`，并合并 K 注册的 M |
| 1214 行 | `version: '20261107-drill-w'` |
| 1245–1249 行 | `window.__cetPlayer` 导出 `playSentence` / `stopSentence` / `interruptSentencePlayback` / `registerHotkeyHelp` |

### `frontend/static/cet-workspace.css`

从 266 行起为本包 W 的增量样式：W2 步骤条（270）、W3 两行工具栏（321）、W4 本步骤单面板（373）、W5 大纲卡点/收藏隐藏（394）、W7 当前句收紧（441）、W6 移动端（450），以及过渡兼容隐藏旧 K 直系面板和 `ws-step-panel-store`。

---

## 三、测试命令与输出

### 1. 真实浏览器验收（W 分支，5302，真实媒体状态）

命令：

```bash
cd /tmp/wt-drill-w && .venv/bin/python /tmp/w_accept.py
```

关键输出（原文摘录）：

```json
{
  "new_ws_keys": true,
  "player_untouched": true,
  "play_ended": {"during": {"rate": 1.5, "paused": false}, "result": "ended", "afterRate": 1.25, "paused": true},
  "play_stop": {"result": "interrupted", "afterRate": 1.25, "paused": true},
  "play_invalid": "interrupted",
  "play_loop": {"result": "interrupted", "drops": [[9.456917, 7.293807], [9.504298, 7.299855]], "start": 7.28, "end": 9.6},
  "transcript": {"before": {"hidden": false, "titles": 4}, "hidden": {"hidden": true, "body": true, "titles": 0}, "after": {"hidden": false, "titles": 4}},
  "step_ui": {"a": true, "b": true, "c": true, "activeStep": "blind", "panel": true, "action": true, "hotkeys": [{"key": "M", "label": "标记没听清"}], "activeTab": "step"},
  "errors": []
}
```

### 2. 旧静态回归（不含 startup 慢测）

命令：

```bash
cd /tmp/wt-drill-w && ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest \
  tests/test_cet_base_player.py tests/test_cet_base_workspace.py tests/test_cet_base_mode.py \
  tests/test_cet_base_routes_registered.py tests/test_cet_base_home.py tests/test_cet_base_import.py \
  tests/test_cet_base_materials.py tests/test_cet_fix2_p1_workspace.py tests/test_cet_fix2_p2_hotkeys.py \
  tests/test_cet_fix2_p4_favorites.py tests/test_cet_fix21_a_frontend.py -q
```

输出原文末行：

```text
1 failed, 314 passed, 41 skipped in 5.58s
```

唯一失败是**预期冲突**：`tests/test_cet_fix2_p1_workspace.py::test_cet_player_cache_buster_bumped`——该旧测试硬编码 `cet-player.js?v=20261106-fix2`，而本轮任务书要求 W 自己用 `20261107-drill-w`，属旧断言过期，不是功能失败。

### 3. 快捷键浏览器回归（旧 K 基线服务 5302）

命令：

```bash
cd /tmp/wt-drill-w && ELT_CONFIG_DIR=$(mktemp -d) \
  CET_BASE_URL=http://127.0.0.1:5302 CET_AGENT_ON_BASE_URL=http://127.0.0.1:5302 \
  CET_GENERIC_BASE_URL=http://127.0.0.1:5308 .venv/bin/python -m pytest tests/test_cet_fix2_p2_hotkeys.py -q
```

输出原文末行：

```text
1 failed, 12 passed in 57.52s
```

唯一失败为 `test_p2_3_hotkey_hint_hidden_when_agent_owns`：旧断言要求「K 开启时底座快捷键提示隐藏」，本轮 W3/§2 要求第一行常驻「⌨ 快捷键」并展示 K 注册的 M，属预期冲突；K 注册的 M 已实测显示在帮助面板中。

### 4. W + K 当前工作副本联调冒烟（真实服务 5306，四宽度）

命令（K 与 W 均使用各自 worktree 当前文件复制到 `/tmp/wt-w-preview` 预览副本，不改 K 文件）：

```bash
cd /tmp/wt-w-preview && .venv/bin/python /tmp/w_integration_check.py
```

四宽度结果一致：`step='blind'`、步骤条可见且 `activeStep='blind'`、可见 tab 为 `本步骤/大纲/收藏`、当前 tab `step`、本步骤面板有 K 的盲听内容、第二行动作含「交卷 / 今日复习」、帮助面板含 `Space/← →/R/L/M`，`errors: []`（1440/1280/768/375 均通过）。

### 5. 通用模式 / 队友关闭态

- `ELT_CET_MODE=0`（5308）：无步骤条/步骤面板，工具栏保持原单行结构，`#study-mode-switch` 显示「精听/阅读」，无 pageerror。
- `ELT_CET_FEATURE_CET_LISTENING_AGENT=0`（5307）：无步骤条/步骤面板，工具栏保持单行结构，底座行为与基线结构一致，无 pageerror。

---

## 四、截图

`docs/cet-base/卡点融合/evidence/W/`：

- `1440.png`（1440×900）
- `1280.png`（1280×800）
- `768.png`（768×900，整页）
- `375.png`（375×812，整页）
- `w-outline-hidden-1440.png`（原文隐藏时大纲只显示时间段 + 卡点数）
- `w-hotkeys-1440.png`（⌨ 快捷键面板，含 K 注册的 M）

说明：四宽度截图与联调冒烟使用 W 分支 + K 并行分支当前工作副本的预览副本 `/tmp/wt-w-preview`；W 分支本身不包含 K 的 `cet-listening.js/.css`。K 正式合入后结构不变，W 不依赖 K 的具体 DOM 实现。

---

## 五、未完成 / 风险 / 需配合项

1. **K 分支尚未合并到 W 分支**：W 分支只含 W 三个文件；K 的三步面板内容由 K 提供，联调截图来自预览副本。主代理合并时需保留 K 的 `cet-listening.js/.css` 版本串 `20261107-drill-k`。
2. **旧测试冲突（按任务书「现有旧测试可能断言旧结构，不要去改 tests」）**：
   - `tests/test_cet_fix2_p1_workspace.py::test_cet_player_cache_buster_bumped`（旧缓存串）。
   - `tests/test_cet_fix2_p2_hotkeys.py::test_p2_3_hotkey_hint_hidden_when_agent_owns`（旧「K 开启时帮助提示隐藏」断言）。
   - 另 `tests/test_cet_listening_ui.py` 与 `tests/test_cet_teammate_e2e.py` 中针对旧 K `#cet-toolbar` / `#cet-questions` 的结构断言预计由 K 包交付说明记录；W 不改 K 文件。
3. **W5 卡点数刷新**：K 当前没有 marks 变更事件契约；W 在原文隐藏期间用 1.5s 轻量轮询只更新 `.ws-outline-mark-count` 文本（不重渲染大纲、不打断滚动），K 后续若有 marks 事件可替换为事件驱动。
4. **K 面板槽位**：W 的 `#ws-step-panel-store` 是隐藏容器，目的是让 K 在初始化时一次注册的 blind/review/drill 三块面板即使不在当前步骤也能按 id 查到并完成渲染；K 侧无需感知该容器。
5. **`cet-favorites.js` 未改**：W5 的收藏面板隐藏通过 `cet-workspace.css` 对 `.cet-fav-text/.cet-fav-meta` 及原 `#reading-saved-items` 做视觉隐藏，不影响收藏存取与跳转按钮。

## 六、缓存串（与模板四行一致）

- `cet-listening.css?v=20261107-drill-k`
- `cet-listening.js?v=20261107-drill-k`
- `cet-workspace.css?v=20261107-drill-w`
- `cet-player.js?v=20261107-drill-w`
