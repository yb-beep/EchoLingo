# 包 K · 卡点前端（三步状态机 + 卡点/带练/复习 UI）交付说明

- 工作树：`/tmp/wt-drill-k`
- 分支：`codex/cet-drill-k`
- 基线：`c4a480d`
- 端口：`5301`
- 文件：`frontend/static/cet-listening.js`、`frontend/static/cet-listening.css`
  - `cet-listening.js` 2793 行；`cet-listening.css` 900 行
- 未修改：`cet_service.py` / 其它后端文件 / `tests/`

## 一、实现了什么（对应任务书 K1–K12）

| 编号 | 实现落点 |
| --- | --- |
| K1 | 三步状态机 `blind → review → drill`，步骤写入现有 `cet.state.v2.<materialId>`，刷新恢复；有题材料盲听+做题→交卷→review→进入精练；无题材料盲听标记→显示原文确认卡点→review→精练 |
| K2 | `blind` 步骤调用 `__ws.setTranscriptHidden(true)`；卡点行只渲染「标记于 m:ss」与「回听」，不渲染候选原文/确认按钮 |
| K3 | `review` 面板显示 `score / gradedCount`（如 `4 / 4`）；`unknownCount` 显示「另 1 题无参考答案，不计分」；每题证据改为「▶ 听原声证据」，按 `evidenceRef.startSec/endSec` 映射到 `__ws.sentences` 下标后调用 `__ws.playSentence` |
| K4 | 卡点行外露「回听」+主动作（`确认这句` / `开始带练`）；前一句/后一句/改一下/删除收进「⋯」菜单；带练中的标记不可改句/删除 |
| K5 | 时间条改为小圆点 + `title` 悬停提示；相邻 `<2%` 宽度合并为计数气泡，点开弹出逐个标记列表（含回听） |
| K6 | drill 面板通过 `__ws.mountStepPanel('drill', el)` 注册到右栏；顶部显示正在练的句子（带可点词）、「▶ 播放」，进入自动播放一次；原句回放/慢放/分块回放全部调用 `__ws.playSentence`，不再直接写 `audio.playbackRate` |
| K7 | `explain` 支持包 D 的 `data.mode === 'offline'`：渲染 `words[{word,ipa,meaning}]`、可选 `translation`、末尾 `hint`；无 key 的 `heard` 走中性本地结束，不出现「模型尚未配置/尚未配置」 |
| K8 | drill 句子按词切按钮，点击优先 `__ws.lookupWord(anchor, word)`；当前 W 未暴露该方法时回退到工作区现有 `window.showWordMeaning(event)`，并阻止冒泡避免弹层被全局 click 关闭 |
| K9 | 保存本轮后清空带练内容并询问一次「把这句收藏到『我的收藏』？」；调现有 `/api/v2/lessons/{lessonId}/phase-b` 句子收藏接口，带 segment_index/start_seconds/end_seconds/text |
| K10 | 今日复习面板渲染在 review 步骤；每条先盲听（自动 `__ws.playSentence`、原文遮住），点「显示原文」才显示句子；面板通过 `mountStepPanel` 注册 |
| K11 | `__ws.registerHotkeyHelp({key:'M',label:'标记没听清'})`；M 只在 `step === 'blind'` 生效 |
| K12 | 不创建旧 `#cet-toggle-mode` / `#cet-toolbar` 二态按钮，改由 W2 顶部步骤条 + `__ws.setStepActions` 承载 |

### 额外接口

- `window.__cet.step`：`null` → `blind|review|drill`
- `document` 事件 `cet:step-change`：`detail = { step, hasQuestions, submitted }`；初始化/恢复完成后补发首帧
- `window.__cet.marks`：只读 getter，返回当前 `S.marks`
- `window.__cet.materialId`：只读 getter

### H3 深链

- `/workspace/<lessonId>?review=1`：boot/restore 完成且确认是 CET 材料后，将步骤置为 `review`，自动调用一次 `startReview()`（`reviewQueryStarted` 防重复）。
- 无待复习条目：保持 review 步骤，面板持久显示「暂时没有待复习的卡点」，同时 `flashHint` 中性提示。
- 已在有复习条目的真实服务验证：`step=review`、`reviewSessionActive=true`、panel 显示「今日复习」；无条目服务验证：`step=review`、`reviewSessionActive=false`、`reviewNotice=暂时没有待复习的卡点`。

### 无题材料确认（K1 的边界）

没有修改后端规则。确认无题材料卡点时，`confirm_mark` 仍会返回 `ATTEMPT_REQUIRED`；K 在首次收到该 409 时静默提交一份 `answers: []` 的空 attempt 后重试一次（用户不看到「交卷」按钮），从而使用现有后端即可确认卡点。真实无题材料夹具（lesson 9301）已验证：`questions=0`、确认后 `attempt` 存在且 `confirmedSentenceId` 落库、可进入 drill。

### K8 需要的 W 配合

- 当前 W 的 `window.__ws` 没有 `lookupWord`；K 已兼容工作区现有全局 `window.showWordMeaning(event)`，真实集成点击可打开词卡。
- 建议 W 增加 `__ws.lookupWord(anchorElement, word)`（或明确保留全局 `showWordMeaning` 作为稳定接口），这样 K 可完全走 `__ws`；不会阻塞当前交付。

## 二、缓存串

- K 的 `cet-listening.js` / `cet-listening.css`：旧串 `20261107-marks-fix` → 新串 **`20261107-drill-k`**
- 本包没有修改 `workspace.html`（该文件属 W）；W 需在模板的 `cet-listening.css/js` 两行写 `20261107-drill-k`，W 自己的 `cet-workspace.css` / `cet-player.js` 用 `20261107-drill-w`。
- 真实 W 集成页已确认模板加载的 K 串为 `20261107-drill-k`。

## 三、测试命令与输出

### 1. 静态检查

```text
$ node --check frontend/static/cet-listening.js
（无输出，exit 0）
```

### 2. 基础静态/契约测试

```text
$ cd /tmp/wt-drill-k && ELT_CONFIG_DIR=$(mktemp -d) \
  .venv/bin/python -m pytest -q tests/test_cet_base_player.py \
  tests/test_cet_base_mode.py tests/test_cet_base_materials.py \
  tests/test_cet_base_routes_registered.py
135 passed in 1.83s

$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
  tests/test_cet_contract.py tests/test_cet_backend.py tests/test_cet_teammate_e2e.py
21 passed, 83 skipped in 2.29s
```

### 3. 回归：`ELT_CET_FEATURE_CET_LISTENING_AGENT=0`（底座行为）

服务：新临时 DB、端口 5318、`ELT_CET_FEATURE_CET_LISTENING_AGENT=0`，导入 `--all`。

```text
$ CET_BASE_URL=http://127.0.0.1:5318 ELT_CONFIG_DIR=$(mktemp -d) \
  .venv/bin/python -m pytest -q tests/test_cet_base_workspace.py
25 passed in 47.51s
```

### 4. 回归：`ELT_CET_MODE=0`（通用模式）

服务：新临时 DB、端口 5320、`ELT_CET_MODE=0`，导入 `--all`。

```text
$ CET_BASE_URL=http://127.0.0.1:5320 ELT_CET_MODE=0 ELT_CONFIG_DIR=$(mktemp -d) \
  .venv/bin/python -m pytest -q tests/test_cet_base_workspace.py
23 passed, 2 failed in 46.07s
```

同命令在基线 `c4a480d`（`/tmp/wt-drill-base`，端口 5321）结果也是 `2 failed, 23 passed`；失败项同为：

- `test_t08_badge_reports_real_transcript_state`：旧测试按 CET 文案断言 `原文/句`，但通用模式模板使用通用文案 `字幕就绪`；基线同样失败。
- `test_t13_space_key_is_owned_by_base_player_when_teammate_off`：旧测试固定按「CET 模式 + 队友关闭」三态断言；通用模式下底座 `ownsSpaceKey=false` 是公开契约；基线同样失败。

K 脚本在 `ELT_CET_MODE=0` 下 boot 直接返回，不挂载三步 UI，避免改变通用工作区；本包未修改模板/底座。

### 5. 真实浏览器验收（W + K 临时集成）

集成方式：把 W 工作树的三份 W 文件与 K 最新 `cet-listening.js/.css` 复制到临时 worktree，真实启动 `backend/fastapi_server.py`、真实导入材料、Playwright 真实点击（无 `route.abort`、无静态 HTML 断言）。

- 首页 `/workspace/1`（有题材料）：
  - 初始 `__cet.step='blind'`，`body.cet-hide-text=true`，W 顶部步骤条显示「①盲听做题」，右栏「本步骤」显示盲听做题面板。
  - 盲听按 M：新增 mark；卡点行文本只有 `标记于 0:01 / 回听`（无候选原文、无确认按钮）。
  - 交卷后：`step=review`，得分卡 `0 / 4`（实测选项全 A 时），未知答案 `另 1 题无参考答案，不计分`，4 个「▶ 听原声证据」按钮；点击证据后真实媒体 `currentTime≈0.7s`、`paused=false`，播完自动暂停。
  - 确认卡点后：`step=review` 主动作 `进入精练 →` 由 disabled 变 enabled；进入 drill 后 `paused=false`（自动播放一次），点「慢放」真实 `playbackRate=0.75`。
  - drill 句子点击单词：`#word-popover.open=true`，显示词义「It's / /ɪts/ / 它」。
  - 保存本轮：出现「把这句收藏到『我的收藏』？」；点「收藏这句」后 `/api/v2/lessons/1/phase-b` 返回 `ok:true`，完成面板替代带练面板。
- 今日复习：保存后点「今日复习」→ `step=review`、`reviewSessionActive=true`、原文初始 `cet-hide-text=true`、媒体自动播放；点「显示原文」后 `cet-hide-text=false` 并显示句子；点「这处练完了」→ `status=complete`、显示「返回学习」。
- 无题材料夹具 `/workspace/9301`：`questions=0`，盲听 M 后显示原文确认；点击「确认这句」自动补空 attempt，`confirmedSentenceId` 落库，`进入精练 →` 解锁，drill 可进入。
- H3 `?review=1`：
  - 有复习条目：`step=review`、`reviewSessionActive=true`、review panel 为今日复习；
  - 无复习条目：`step=review`、`reviewSessionActive=false`、`reviewNotice=暂时没有待复习的卡点`，面板持久显示该文案。
- D2 离线释义实际合并验证（临时 W+D+K 服务，无 AI key）：
  - 点「不懂意思」后 `currentStep.data.mode='offline'`，页面显示 `translation=天晚了，我们都显出疲惫的样子。`、词条 `getting /ˈɡetɪŋ/ 变得`、`weariness /ˈwɪərinəs/ 疲惫`、末尾 `配置模型后可获得讲解`；无「模型尚未配置/尚未配置」。
  - `heard` 在无候选/offline 走本地结束，显示中性「本轮按本地完成处理」。

截图：

- `docs/cet-base/卡点融合/evidence/K/final-blind-1440.png`
- `docs/cet-base/卡点融合/evidence/K/final-review-1440.png`
- `docs/cet-base/卡点融合/evidence/K/final-drill-1440.png`
- `docs/cet-base/卡点融合/evidence/K/final-375-blind.png`
- `docs/cet-base/卡点融合/evidence/K/final-375-review.png`
- `docs/cet-base/卡点融合/evidence/K/final-375-drill.png`

## 四、冲突的旧测试（未改 tests/，按要求报备）

以下旧测试仍按「旧 K 二态按钮/Space 标记/旧卡点行静态结构」断言；K12/K11/K4 改变后预期冲突，不作为本包回归门槛：

- `tests/test_cet_listening_ui.py`：`#cet-toggle-mode`、Space 标记、旧练习模式断言
- `tests/test_cet_marks_fix_a_browser.py`：`#cet-toggle-mode`、旧 `.cet-mark` DOM/回听行为
- 其它包含 `cet-toggle-mode` / `cet-toolbar` 字符串的旧断言文件（见 `grep` 结果）如被主代理合并进新测试集，需按新 K 契约更新。

## 五、未完成 / 风险

- K8：W 尚未暴露 `__ws.lookupWord`，目前通过工作区全局 `showWordMeaning` 兼容；若 W 重命名/移除该全局函数，需要 W 增加 `__ws.lookupWord(anchor, word)`。
- 无题材料使用空 attempt 规避 `ATTEMPT_REQUIRED`；若后端未来收紧 `answers: []` 的交卷校验，需要主代理协调后端或放开确认规则（本包未改 `cet_service.py`）。
- 旧 `tests/test_cet_listening_ui.py` 等与 K12 有意冲突，详见上节。
- 本包未修改 `workspace.html` 缓存串，交由 W 按 `20261107-drill-k` 写入两行。
