# CET 听力接口契约（实际映射）

> M0 交付物。记录**实际存在的接口**与计划语义的对应关系，供开发与验收使用同一份定义。
>
> 基线：`b04445d`（分支 `codex/cet-listening`）｜最后更新：2026-11-05

---

## 0. 结论先说

**计划 §6 要求的最小契约，现有后端已全部覆盖。** 17 个接口在 `backend/webapp/fastapi_routes/cet.py`，业务逻辑在 `backend/webapp/services/cet_service.py`。

**本轮不需要新增 `/api/cet/` 路由**，只需：
1. 前端接入（M1 起）
2. 少量字段/行为补强（见第 4 节）

---

## 1. 对象映射表

计划 §6 的对象 → 实际实现：

| 计划对象 | 实际位置 | 状态 |
| --- | --- | --- |
| Material | `cet_service._material()`，数据源 `v2_lessons` | ✅ 已有 |
| Segment | `cet_service._sentence_ref()`，数据源 `v2_subtitle_segments` | ✅ 已有（句级） |
| Question | 表 `cet_questions` | ✅ 已有 |
| Attempt | 表 `cet_attempts` / `cet_attempt_answers` | ✅ 已有 |
| Marker | 表 `cet_marks` | ✅ 已有 |
| Practice | 表 `cet_practices` / `cet_practice_steps` / `cet_practice_events` | ✅ 已有 |
| Action | `cet_service.next_practice_step()` + `_action_for_feedback()` | ✅ 已有 |
| Transfer | 表内 `transfer_*` 字段 + `_transfer_case()` | ✅ 已有 |
| Review | 表 `cet_reviews` / `cet_review_items` | ✅ 已有 |
| Error | `cet_service._error(code, message, **ctx)` | ✅ 已有 |

---

## 2. 接口清单与实际字段

### 2.1 材料

**`GET /api/cet/materials`** — 材料列表

查询参数：`level`（可选，`cet4`/`cet6`/`unspecified`）

响应：
```json
{
  "items": [
    {
      "materialId": "cet6-202512-conv2",
      "materialVersion": "sha256-b1420573...",
      "title": "2025年12月六级第一套 · Conversation Two",
      "level": "cet6",
      "section": "",
      "audioUrl": "/output/v2_assets/2181/cet6-202512-conv2.mp3",
      "audioSha256": "",
      "durationSec": 105.486,
      "sentenceCount": 32,
      "questionCount": 5,
      "developmentFixture": false,
      "source": { "type": "...", "value": "...", "materialStore": "echolingo.v2_lessons" }
    }
  ],
  "count": 142
}
```

> **ID 说明**：`materialId` 优先返回**业务稳定 ID**（从 `source_url` 派生，如 `cet6-202512-conv2`）；
> 派生不出来时返回底座原生 `v2-<数字>`。两种形式都能作为后续请求的 `materialId`。

**`GET /api/cet/materials/{material_id}`** — 材料详情（含句子时间轴）

查询参数：`version`（可选）

响应在列表字段基础上增加：
```json
{
  "sentences": [
    {
      "sentenceId": "v2-2181-0",
      "materialId": "cet6-202512-conv2",
      "materialVersion": "sha256-...",
      "startSec": 0.01,
      "endSec": 3.49,
      "text": "It's getting late and we're both displaying signs of weariness.",
      "timingSource": "echolingo.v2_subtitle_segments"
    }
  ]
}
```

> **时间单位**：所有 `*Sec` 是**绝对秒数**（从音频开头算），不是相对偏移。

**`GET /api/cet/materials/{material_id}/questions`** — 题目（交卷前）

响应：
```json
{
  "materialId": "cet6-202512-conv2",
  "materialVersion": "sha256-...",
  "questions": [
    {
      "questionId": "cet-q05",
      "number": 5,
      "stem": "Why does the man suggest they call it a day?",
      "options": ["They have got their work done.", "They both look tired", "..."],
      "materialId": "cet6-202512-conv2",
      "materialVersion": "sha256-..."
    }
  ]
}
```

**交卷前硬约束**：不返回 `answer`、`correct`、`evidenceRef`、`explanation`、`answerSource`。
`answerSource` 会暴露「该题有无可靠答案」，属间接泄露，故一并排除。

### 2.2 交卷

**`POST /api/cet/attempts`**

请求：
```json
{
  "materialId": "cet6-202512-conv2",
  "materialVersion": "sha256-...",
  "listeningSessionId": "session-abc",
  "answers": [
    { "questionId": "cet-q05", "choice": 1 }
  ]
}
```

- `choice` 是 **0-based 选项索引**（也兼容字母 `"B"`）
- `attemptId` **由服务端生成**，客户端不需要传
- 幂等：同一 `listeningSessionId` + `materialId` 重复提交返回同一记录

响应：
```json
{
  "attemptId": "attempt-8bf7f7c0169d",
  "materialId": "cet6-202512-conv2",
  "materialVersion": "sha256-...",
  "listeningSessionId": "session-abc",
  "score": 2,
  "gradedCount": 4,
  "unknownCount": 1,
  "answersVisible": true,
  "results": [
    {
      "questionId": "cet-q05",
      "number": 5,
      "choice": 1,
      "answer": 1,
      "correct": true,
      "answerSource": "user_provided_pdf",
      "evidenceRef": {
        "sentenceId": "v2-2181-0",
        "startSec": 0.01,
        "endSec": 3.49,
        "quote": "It's getting late and we're both displaying signs of weariness."
      },
      "explanation": "",
      "materialId": "cet6-202512-conv2",
      "materialVersion": "sha256-...",
      "answerVisible": true
    }
  ],
  "submittedAt": "2026-11-04T08:07:37Z"
}
```

**未知答案**：`answer: null, correct: null`。**不是答错**，`unknownCount` 单独计数。

### 2.3 标记

**`POST /api/cet/marks`**

请求：
```json
{
  "materialId": "cet6-202512-conv2",
  "materialVersion": "sha256-...",
  "listeningSessionId": "session-abc",
  "pressedAtSec": 36.9
}
```

- `pressedAtSec`：**按下空格时的媒体时间**（秒），不是系统时钟
- `markId` **由服务端生成**
- 服务端会给出 `suggestedSentenceId`（吸附建议，即计划 §4 的「可能是这句」）

响应：
```json
{
  "markId": "mark-019e94b3152a",
  "id": "mark-019e94b3152a",
  "materialId": "cet6-202512-conv2",
  "materialVersion": "sha256-...",
  "listeningSessionId": "session-abc",
  "pressedAtSec": 36.9,
  "rawSeconds": 36.9,
  "adjustedSeconds": 0.0,
  "speed": 1.0,
  "reactionDelaySeconds": 0.6,
  "suggestedSentenceId": "v2-2181-11",
  "suggestionSource": "estimated_alignment",
  "confirmedSentenceId": null,
  "confirmedAtSec": null,
  "createdAt": "...",
  "updatedAt": "..."
}
```

**`PATCH /api/cet/marks/{mark_id}`** — 确认片段

请求：`{ "confirmedSentenceId": "v2-2181-11" }`

**前置条件**：必须先交卷（否则 `409 ATTEMPT_REQUIRED`）。

> 字段别名说明：`markId`/`id`、`pressedAtSec`/`rawSeconds` 是同一值的两种写法，
> 前者为契约字段、后者为底座旧字段，**两者都返回**，前端用前者即可。

**`GET /api/cet/marks`** — 标记列表

查询参数：`materialId`、`listeningSessionId`（均可选）

响应：`{ "items": [...], "count": N }`

### 2.4 带练

**`POST /api/cet/practices`**

请求：`{ "markId": "mark-..." }`

**前置条件（实测确认）**：
1. 必须先交卷（同标记接口）
2. **必须先确认句子**（`PATCH /marks/{id}` 设 `confirmedSentenceId`）
   —— 未确认时建练习会失败。计划 §4「确认后才绑定训练片段」正是这个语义。

响应：
```json
{
  "practiceId": "practice-3c7b308e504b",
  "markId": "mark-...",
  "materialId": "cet6-202512-conv2",
  "materialVersion": "sha256-...",
  "sentenceId": "v2-2181-11",
  "status": "active",
  "currentStepId": "0f218a1eca2e4353...",
  "currentStep": { ... },
  "steps": [ ... ],
  "latestFeedback": null,
  "effectiveMs": 0
}
```

**`POST /api/cet/practices/{practice_id}/next`** — 反馈驱动下一步

请求：
```json
{
  "feedback": "sound",
  "stepId": "0f218a1eca2e4353...",
  "materialVersion": "sha256-..."
}
```

- `feedback` 取值：`sound`（没听清）/ `meaning`（不懂意思）/ `heard`（听懂了）
- **必须带 `stepId` + `materialVersion`**，否则 `409 STALE_RESPONSE`（防过期响应）

反馈 → 动作映射（计划 §5 要求两类反馈有区别）：

| feedback | 动作 | 说明 |
| --- | --- | --- |
| `sound` | `slow` | 先慢放定位 |
| `meaning` | `explain` | 先解释句意 |
| `heard` | `transfer` | 换句检验 |

**`POST /api/cet/practices/{practice_id}/finish`** — 保存

请求：`{ "feedback": "sound", "stepId": "...", "materialVersion": "..." }`

**`POST /api/cet/practices/{practice_id}/local`** — 本地动作（不需 AI）

**`POST /api/cet/practices/{practice_id}/events`** — 播放事件（计时用）

**`GET /api/cet/practices/{practice_id}`** — 练习状态（刷新恢复用）

### 2.5 复习

**`GET /api/cet/review-items`** — 卡点列表

**`POST /api/cet/reviews`** — 建复习会话

请求：`{ "maxItems": 3 }`（计划 §7 M3 要求最多三处）

**`GET /api/cet/reviews/{review_id}`** — 复习状态

**`POST /api/cet/reviews/{review_id}/actions`** — 复习动作（替换/跳过/暂停/结束）

---

## 3. 错误码

| 码 | HTTP | 含义 | 前端应做 |
| --- | --- | --- | --- |
| `MATERIAL_NOT_FOUND` | 404 | 材料不存在 | 提示并返回列表 |
| `MATERIAL_VERSION_CHANGED` | 409 | 材料版本变了 | 提示刷新后重试 |
| `ATTEMPT_REQUIRED` | 409 | 未交卷不能确认句子 | 引导先交卷 |
| `STALE_RESPONSE` | 409 | stepId/版本过期 | 重新拉取当前步骤 |
| `PRACTICE_NOT_FOUND` | 404 | 练习不存在 | 返回卡点列表 |
| `INVALID_FEEDBACK` | 422 | feedback 值非法 | 只传三个合法值 |
| `INVALID_ACTION` | 422 | 动作不支持 | 按允许动作列表 |
| `MARK_ID_REQUIRED` | 422 | 缺 materialId/sessionId | 补必填字段 |
| `UNKNOWN_QUESTION` | 422 | 作答含不存在的题 | 按题目列表作答 |
| `DUPLICATE_ANSWER` | 422 | 同题重复作答 | 去重 |

---

## 4. 计划要求但当前缺失/需补强的点

M1 起逐项处理：

| # | 计划条款 | 现状 | 计划动作 |
| --- | --- | --- | --- |
| 1 | §4 标记三态（橙待确认/蓝待练/绿本轮已练） | 只有 `confirmedSentenceId` 有/无 | **M1 补状态字段** |
| 2 | §4 「可能是这句」提示 | 有 `suggestionSource` | 前端展示即可 |
| 3 | §4 回溯窗口可配置 | 固定 `reactionDelaySeconds=0.6` | M1 改成可配置 |
| 4 | §5 两类反馈不同帮助 | 已有映射 | ✅ 满足 |
| 5 | §5 等待 3 秒提示 / 30 秒超时 | `CET_AI_TIMEOUT_SECONDS` | M2 补前端提示 |
| 6 | §5 旧响应隔离 | 已有 `stepId` 校验 | ✅ 满足 |
| 7 | §7 M1 「保存并重开」 | 有 `GET /practices/{id}` | 前端实现即可 |
| 8 | §7 M3 有效用时排除 AI 等待 | 有 `effectiveMs` | 需验证口径 |
| 9 | §1 四级样例 | **只有六级一份** | **M1 前补四级样例** |
| 10 | §1 至少两条换句原声 | 未验证 | M2 处理 |
| 11 | §1 无候选测试样例 | 未验证 | M2 处理 |

---

## 5. 数据与隔离

| 项 | 值 |
| --- | --- |
| 隔离数据目录 | `.cet-qa/data/`（`ELT_USER_DB` 指向此处） |
| 数据库 | `.cet-qa/data/vocab.db`（60 张表） |
| 音频 | `.cet-qa/media/cet6-202512-conv2.mp3` |
| 服务可见音频 | `output/v2_assets/2181/cet6-202512-conv2.mp3` |
| 正式学习数据 | **不触碰**（`resources/vocab.db` 保持原样） |

---

## 6. 前端接入位置（M1 用）

| 需求 | 接入点 | 说明 |
| --- | --- | --- |
| 课程列表 | `frontend/templates/index.html` `lesson-grid` | 已有卡片，加四六级标签 |
| 精听工作区 | `frontend/templates/workspace.html` | 5080 行，播放器/字幕/侧栏都在 |
| 标记控件 | workspace 播放器区 | 加空格监听 + 时间线标记 |
| 题目面板 | workspace 侧栏折叠区 | 复用现有侧栏样式 |
| 反馈按钮 | workspace 侧栏 | 三态按钮 |
| 恢复入口 | workspace 顶部或侧栏 | 读 `GET /practices/{id}` |

**重要约束**（来自 `CONTRIBUTING.md`）：
> 改动前端模板必须同步更新测试——有几个测试断言了 DOM id 和 JS 函数名。

改 `workspace.html` 前先查相关测试。
