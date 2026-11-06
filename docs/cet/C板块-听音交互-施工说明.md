# C 板块（听音交互）施工与验证说明

分支：`codex/cet-c-ui`｜基线：`e3e64ee`｜范围：F08 / F05 / F10（前端）

改动文件（严格在 task-3 授权范围内）：

| 文件 | 改动 |
| --- | --- |
| `frontend/static/cet-listening.js` | F08 回溯参数与预览、F05 隐藏原文/原速/换句闭环、F10 时间线标记 |
| `frontend/static/cet-listening.css` | 对应样式（隐藏原文、时间线、换句区、预览标签） |
| `frontend/templates/workspace.html` | 仅把静态资源版本号从 `20261105-1` 提到 `20261105-c8`，未改结构 |

未改 `backend/`、未改 `tests/`。

---

## 一、F08 回溯参数与候选预览

### 根因
1. `CFG.lookbackSec = 2.5` 声明了但**从未使用**，`createMark()` 只发
   `pressedAtSec`，后端 `create_mark()` 就用自己的默认值
   `reactionDelaySeconds=0.6`、`speed=1.0`，前端配置等于不存在。
2. `adjustMark()` 直接调 `confirmMark()` —— 点「前一句」就落库确认，
   按钮随即消失，用户无法继续试听比较。

### 改动
- `createMark()`：新增发送 `reactionDelaySeconds: CFG.lookbackSec` 与
  实际 `speed: media().playbackRate`。后端据此算
  `adjusted = pressedAtSec - reactionDelaySeconds * speed` 再吸附候选句。
- 新增 `S.previews`（markId → sentenceId）保存**预览目标**，与
  `confirmedSentenceId` 分开，预览不改任何后端状态。
- 新增 `previewMark(m, sentenceId, {autoplay})`：只切试听目标 + 立即回听该句。
- `adjustMark()` 重写：**只调 `previewMark`**，不再碰 `confirmMark`，
  并提示「预览第 N 句，听完再点确认这句」。
- `confirmMark()` 只在用户点「确认这句」时调用；确认后清预览态。
- 新增 `reopenMark()` + 「改一下」按钮：确认后仍能重新预览、重新确认覆盖原值。
- `markState()` 支持 `__amending`：已确认的标记可临时退回待确认态。
- 标记创建后自动进入预览并回听候选句（用户不需要先手动点「回听」）。
- 列表里待确认的标记显示「预览 0:00.01–0:03.49」标签，明确它还不是最终确认句。
- **两个身份都保留**：`pressedAtSec`（原按键秒数，后端从不改写）与
  `confirmedSentenceId`（确认句）同时存在，恢复后两者都在。

---

## 二、F05 隐藏原文 / 恢复原速 / 换句闭环

### 1. 隐藏原文
进入卡点练习模式（`setListening(true)`）即给 `body` 加 `cet-hide-text`，
**只控显隐，不改原 DOM 结构**（退出即恢复）。

覆盖的原文出口（都是实测发现的，不是照文档猜的）：

| 区域 | 发现方式 |
| --- | --- |
| `#current-subtitle` / `#subtitle-translation` | 代码审查 |
| `.outline-preview` / `.outline-title`（内容大纲摘要） | 代码审查 |
| `#reading-passage`（整篇阅读） | 代码审查 |
| `#vocab-chips` 里的 `.vocab-chip` | **截图实测**：仍显示「displaying」 |
| `#subtitle-meta` / `#subtitle-position` | 截图实测 |
| `#word-popover`（点词查询浮层） | **截图实测**：浮层显示「displaying / v. 展出」 |
| `.lookup-word-wrap` 里的 `.subtitle-word` + `<rt>.word-gloss` | **截图实测**：ruby 注解逐词带词义 |

最后一项最隐蔽：当前句区的「点词查询」把每个词渲染成
`<ruby class="lookup-word-wrap"><button class="subtitle-word">displaying</button><rt class="word-gloss">v. 展出；使展现</rt></ruby>`，
按钮和 `<rt>` 各有自己的 `color`，只给父级设 `transparent` 挡不住，
必须对整棵子树 `visibility: hidden`。

无障碍：隐藏时同步 `aria-hidden="true"`，读屏也不会读出原文。

### 2. 恢复原速
`restoreOriginalSpeed()` 同时改三处，避免「界面显示 1x、实际还在 0.75」：
- `#local-media-player.playbackRate = 1`
- 原「倍速」下拉框 `#playback-rate` 置 `1` 并触发 `onPlaybackRateChange()`
- 换句候选播放器 `#cet-transfer-audio.playbackRate = 1`

「听懂了」→ 换句时调用（`hideTextForTransfer()`），并在输出区明确提示
「原文与字幕已隐藏，播放已恢复 1.0x 原速」。

### 3. 换句闭环（有候选）
后端硬约束（读 `cet_service.py` 确认，不能靠改文案绕过）：

- `transfer_answer` 要求当前步骤先有 `playedToEnd=1` 的 `playback_end`；
- `playedToEnd` 由**后端**计算，需同时满足：start 的 `audioTime ≤ step.start+0.25`、
  end 的 `audioTime` 落在 `step.end` ±0.25~0.7、`activeMs ≥ 理论时长-400ms`；
- `playback_*` 的 `speed` 必须等于 `step.speed`，否则 422 `PLAYBACK_SPEED_MISMATCH`。

实现：
- 候选音频用**独立 `<audio id="cet-transfer-audio">`** 播放真实地址
  （候选属于别的材料，复用页面播放器会播错音源）。
- `playTransferCandidate()`：定位到候选句起点 → 真播 → 播到终点 →
  发 `playback_start` / `playback_end`（`speed` 取 `step.speed`）→
  读后端回包里的 `event.playedToEnd` 判定是否解锁。
- 选项**默认禁用**，播完且 `playedToEnd=true` 才解锁；没播完显示
  「没有完整播完…请点重播候选听完再作答」。
- 作答走 `POST /practices/{id}/events` `type=transfer_answer`，真正落库。
- 不伪造 `activeMs`：按真实播放时长发送，`playedToEnd` 由后端判定。

### 4. 无候选
`renderTransfer()` 判定 `noCandidate` 时输出
「这次没有合适的换句材料，本轮记为**未检验**——不能算作已通过」，
带 `data-cet-unverified="1"`，**不渲染任何选项、不发任何事件、不算通过**。

---

## 三、F10 时间线颜色标记

- 在播放器下方渲染 `#cet-timeline`（`data-cet-mark-layer`），
  内含 `#cet-timeline-track` 与图例。
- 每个标记是 `[data-cet-mark]` 元素，带：
  - `data-cet-mark-sec`（真实按键秒数）
  - `data-cet-mark-state`（`pending` / `ready` / `done`）
  - `style.left = sec / duration * 100%`（位置对应真实秒数）
  - `title` 与 `aria-label`：「卡点 0:01.27，状态：待练」
  - 可见文字：「待」/「练」/「已」
- **颜色 + 文字双重标识**：橙 `#e08e0b` 待确认 / 蓝 `#276fbf` 待练 /
  绿 `#2f8f4e` 本轮已练，同时每个标记带状态文字，下方还有文字图例，
  色盲用户不依赖颜色也能分辨。
- 挂载点选 `.video-card`（`#youtube-player` 的兄弟节点）：
  实测 `initLocalMedia()` 会整块重写 `#youtube-player` 的 `innerHTML`，
  时间线挂在里面会被立刻清掉（MutationObserver 抓到 removed）。

---

## 四、验证

### 命令

```bash
# 隔离工作树
cd /tmp/wt-c-ui
export ELT_USER_DB=/tmp/wt-c-ui/.cet-qa/data/vocab.db
export ELT_CONFIG_DIR=/tmp/wt-c-ui/.cet-qa/data

# 1) 起隔离服务（受控模型，让换句候选路径能真的生成；不用 5173）
ELT_PORT=5188 .venv/bin/python scripts/cet/serve_c_ui_controlled.py &

# 2) 跑 C 板块浏览器验证（自动准备候选夹具）
.venv/bin/python scripts/cet/verify_c_ui.py

# 3) 跑既有 UI 回归（确认没破坏原功能）
CET_UI_BASE=http://127.0.0.1:5188 .venv/bin/python -m pytest tests/test_cet_listening_ui.py -q
```

### 实测输出

```
== C 板块浏览器验证（F08 / F05 / F10）==
服务：http://127.0.0.1:5188   课程：/workspace/2181
候选夹具：v2-9009（/output/v2_assets/cet4/cet4-sample-hope-wish.mp3）

-- F08：回溯参数与候选预览 --
  [PASS] F08-1 创建标记带 reactionDelaySeconds 与实际 speed — reactionDelaySeconds=2.5 speed=1 实际倍速=1
  [PASS] F08-1b 回溯参数用的是 CFG.lookbackSec（不是后端默认 0.6） — 发送值=2.5
  [PASS] F08-0 空格产生标记 — marks=1
  [PASS] F08-0b 标记处于「待确认」预览态 — btns=['回听','前一句','后一句','确认这句']
  [PASS] F08-3 预览态显示候选句并标注预览区间 — 预览标签=['预览 0:00.01–0:03.49']
  [PASS] F08-2 点「前一句」不确认（未发 PATCH） — PATCH 0→0 confirmedSentenceId=None
  [PASS] F08-2b 切换后仍在待确认态且保留调整按钮
  [PASS] F08-3b 切换预览后立即回听（音频在播） — paused=False
  [PASS] F08-2c 预览句子随「前一句」变化
  [PASS] F08-2d 可再点「后一句」切回（预览可来回切换） — PATCH 仍为 0
  [PASS] F08-4 点「确认这句」才真正确认 — PATCH 0→1 confirmed=v2-2181-1
  [PASS] F08-4b 确认后状态前进到「待练」
  [PASS] F08-6 原按键时间与确认句身份都保留 — pressedAtSec=1.266 confirmed=v2-2181-1
  [PASS] F08-5 点「改一下」回到可调整状态
  [PASS] F08-5b 改一下后确认句真的变了（且原按键时间不变） — 原=v2-2181-1 新=v2-2181-0

-- F08 回溯参数生效验证（倍速矩阵，直连 API 交叉核对）--
  [PASS] F08-M1 回溯参数进入 adjustedSeconds 计算 — 1.0x: 30-2.5*1.0=27.5；0.75x: 30-2.5*0.75=28.125
  [PASS] F08-M2 同一按键时间、不同倍速 → 回溯位置不同 — 1.0x→27.5  0.75x→28.125
  [PASS] F08-M3 放大回溯窗口后候选句明显前移 — 2.5s→v2-2181-9(26.28s) 5.0s→v2-2181-8(24.48s)
  [PASS] F08-M4 前端回溯窗口声明值与实测发送值一致 — 2.5

-- F10：时间线颜色标记 --
  [PASS] F10-1 播放器时间线图层已渲染 — aria-label=卡点标记时间线
  [PASS] F10-1b 时间线上有标记元素 — count=1 秒数=['1.259'] 位置=['1.19352%']
  [PASS] F10-2 标记带状态属性（三态之一） — states=['ready']
  [PASS] F10-2b 颜色 + 文字双重标识（不只靠颜色） — 文字=['练'] 图例=['待确认','待练','本轮已练']
  [PASS] F10-2c 三态用不同颜色 — 背景色=['rgb(39, 111, 191)']
  [PASS] F10-1c 标记位置对应真实按键秒数 — 1.259s→1.2%(期望1.2%)

-- F05：隐藏原文 / 恢复原速 / 换句闭环 --
  [PASS] F05-1 进入练习模式后字幕原文被隐藏 — color=rgba(0,0,0,0) aria-hidden=true
  [PASS] F05-1b 内容大纲里的原文摘要也被隐藏 — flag=1
  [PASS] F05-1c 原 UI 结构保留（只控显隐，未删元素）
  [PASS] F05-1e 生词卡与句子元信息同样隐藏 — chips 可见文字=[] meta 可见文字=[]
  [PASS] F05-1g 当前句的点词 ruby（词 + 词义）也不可见 — 检查 12 个元素，可见泄露=[]
  [PASS] F05-1f 点词查询浮层在练习模式下不可见 — display=none
  [PASS] F05-1d 退出练习模式恢复显示原文 — color=rgb(32,36,33)
  [PASS] F05-0 带练已开始 — practiceId=practice-...
  [PASS] F05-2 换句时恢复 1.0x 原速 — 0.75→1（下拉框=1）
  [PASS] F05-2b 换句时隐藏提示仍在生效
  [PASS] F05-2c 进入换句区 — kind=presented
  [PASS] F05-3 候选使用独立音频元素播放真实音频（不是原句）
           候选=/output/v2_assets/cet4/cet4-sample-hope-wish.mp3
           原句=/output/v2_assets/2181/cet6-202512-conv2.mp3
  [PASS] F05-3b 候选来自其他材料且时间范围真实 — material=v2-9009 range=[0.5,4]
  [PASS] F05-3c 展示候选题与选项 — 选项数=3
  [PASS] F05-3d 候选音频确实完整播过（后端 playedToEnd 前置成立） — transferPlayed=True
  [PASS] F05-4 播完后选项解锁 — 3 个可用
  [PASS] F05-4b 作答真的落库（transfer_answer 事件）
           {'candidateId':'transfer-3a32...','answer':'A','correctAnswer':'A','isCorrect':True}
  [PASS] F05-4c 落库内容含 candidateId/answer/isCorrect
  [PASS] F05-4d 前端显示服务端判定结果 — 「换句作答正确（辅助检验，不等于独立听懂）」

-- F05 另一分支：无候选必须明确「未检验」 --
  [PASS] F05-5 无候选时明确显示「未检验」且不当通过 — unverified=True
  [PASS] F05-5b 无候选时不提供作答入口 — 选项数=0
  [PASS] F05-5c 无候选不产生 transfer_answer 落库 — transferStatus=no_candidate

-- 恢复：刷新后标记与时间线仍在 --
  [PASS] R 刷新后标记恢复且时间线重绘 — marks=1 timelineMarks=1 layer=True
  [PASS] R-b 恢复的标记仍同时保留两个身份 — pressedAtSec=1.419 confirmed=v2-2181-0
  [PASS] NOJS 无 JS 运行时错误 — 0 个
  [PASS] NOJS 无 console error — 0 个
  [PASS] COV-F05 有候选与无候选两条分支都被验证到 — presented=1 no_candidate=1

------------------------------------------------------------------
结果：52/52 通过，全部通过
```

既有 UI 回归：

```
$ CET_UI_BASE=http://127.0.0.1:5188 .venv/bin/python -m pytest tests/test_cet_listening_ui.py -q
....................................                                     [100%]
36 passed in 358.49s (0:05:58)
```

截图证据：
- [01-practice-mode-hidden.png](./01-practice-mode-hidden.png) 练习模式原文全部隐藏
- [02-mark-preview.png](./02-mark-preview.png) 标记预览态（前/后一句/确认这句）
- [03-before-leak-fix.png](./03-before-leak-fix.png) 修复前泄露（生词 ruby 仍可见）

### 验证边界（必须如实说明）

- 换句候选路径使用 `cet_ai._call` **受控注入**（`serve_c_ui_controlled.py`），
  与 `scripts/cet/run_contract_controlled.py` 同一做法。
  注入的只是模型返回内容，**不代表真实模型质量、速度或训练效果**。
- 本机无 AI key，因此「真实模型下的换句题质量」未验证，程序链路已验证。
- H1 主观听检（候选句是否真的就是没听清那句、`lookbackSec=2.5` 是否合适）
  需要真人试听，本轮只保证参数真的生效、可调。

---

## 五、需要主代理处理的后端事项

本轮**没有**需要后端改动才能完成的功能。仅记录两条已确认的后端行为，
供 H1 调参与后续验收参考：

1. `create_mark()` 对 `reactionDelaySeconds` 的合法范围是 `0 ≤ x ≤ 5`。
   前端默认 2.5 在范围内；若 H1 试听需要更大回溯窗口（比如 8 秒），
   必须放宽这个上限，否则 422 `INVALID_AUDIO_TIME`。
2. `_transfer_candidate()` 的候选池会被历史数据**一次性消耗**
   （排除已交卷材料、已建练习材料、已用过的候选材料及其 `source_url`）。
   日常库里跑几轮后候选必然耗尽，用户会一直看到「未检验」。
   若要让换句检验长期可用，需要扩充候选来源或允许复用，
   属于产品决策，不在本轮前端范围。
