# 给 A 和 B 的对接说明（C 的工具怎么用）

> C 已交付的测试工具都在 `scripts/qa/`、`tests/`。这份说明只讲**你需要做什么才能对上**，
> 详细环境与排错见 [启动与测试说明](./启动与测试说明.md)。
>
> 更新：2026-11-03（D1）｜底座基线 `1ea5b35`

---

## 先说结论：现在就能开始

```bash
cd EchoLingo-cet
scripts/qa/start_cet.sh --mock --seed
```

- 底座：<http://127.0.0.1:5173>
- **Mock API：<http://127.0.0.1:5199>**（A 用它做前端，不用等 B）
- 一条命令起底座 + Mock + 预置一节样例课

---

## 给 A（前端）

### 1. 把 API base 指向 Mock 就能开发

```
http://127.0.0.1:5199
```

契约样例（`tests/fixtures/cet/`）里的字段就是 Mock 返回的字段。**直接照着写，不用猜。**

字段级规格见 [`接口规格.md`](./接口规格.md)：每个字段的类型、可空性、示例都列出来了，
由脚本从运行中的 Mock 实测生成，不会与实际行为脱节。

### 2. 四种场景随时切

```bash
# 单次请求
curl "http://127.0.0.1:5199/api/cet/materials?scenario=empty"
# 请求头
curl -H "X-CET-Scenario: slow" http://127.0.0.1:5199/api/cet/materials
# 或重启时设环境变量
CET_MOCK_SCENARIO=error .venv/bin/python tests/mock_cet_api.py
```

| 场景 | 效果 | 用来验 |
| --- | --- | --- |
| `normal` | 正常返回 | 主流程 |
| `empty` | 材料列表返回空数组 | 空态页面 |
| `slow` | 每个业务请求延迟 2.5s | 等待提示、加载态 |
| `error` | 返回 503 + `error_info` | 错误态、重试按钮 |

切回真实后端只改 base，**其余代码不用动**。

### 3. 请按约定标 `data-cet-*`（这条影响 T02/T03/T14 能否自动验收）

纯前端行为（长按不连发、输入框空格不产生标记、切页面停旧播放）C 走 HTTP 验不了，
必须靠浏览器自动化。所以需要一个稳定选择器约定。

```bash
.venv/bin/python scripts/qa/check_frontend_contract.py
```

现在会报告 12 项全部未出现（属预期）。按下面标上即可：

| 属性 | 标在哪 |
| --- | --- |
| `data-cet-app` | 四六级界面根容器 |
| `data-cet-material-id="cet6-202512-conv2"` | 同上，当前材料 ID |
| `data-cet-player` | `<audio>` 元素 |
| `data-cet-mark-layer` / `data-cet-mark` / `data-cet-mark-sec="36.9"` | 时间线图层、单个标记、原始按键秒数 |
| `data-cet-feedback="sound\|meaning\|heard"` | 三个反馈按钮 |
| `data-cet-practice-step` / `data-cet-step-action="slow"` | 带练步骤容器与动作 |
| `data-cet-review-item` / `data-cet-review-status="pending"` | 复习条目与状态 |
| `data-cet-state="loading\|empty\|error\|ready"` | 页面主状态 |

标完 C 就能补键盘事件测试，你也不用陪我反复改选择器。

### 4. 错误响应有两种形状 —— 必须都处理（A 最容易踩）

C 实测所有 CET 端点，错误响应会出现**两种不同结构**：

| 来源 | 形状 | 例子 |
| --- | --- | --- |
| 业务校验失败 | `{"error": "...", "error_info": {code, title, action, ...}}` | `{"error":"materialId 必填","error_info":{"code":"URL_REQUIRED",...}}` |
| 请求体解析失败 | `{"detail": [...]}` | `{"detail":[{"type":"dict_type","msg":"Input should be a valid dictionary"}]}` |

**只处理一种，另一种就会白屏。** 底座既有前端用的是：

```js
const detail = payload && (payload.detail || payload.error_info || payload);
```

建议照这个来。`error_info.code` 是给前端分支用的（如 `MARK_NOT_CONFIRMED` 提示去确认句子），
`error_info.action` 是可以直接展示给用户的一句话。

C 已加契约测试 `test_error_response_is_parseable_by_one_rule` 保证：
**所有错误响应都能被这一条规则解析出可显示信息**，不会出现三种字段都没有的裸错误体。

### 5. 几个已经替你验过的点

- **交卷前绝不返回答案**：`GET /materials/{id}/questions` 的样例里没有 `answer`/`evidenceRef`/`explanation`，契约测试双向断言。你按这个写就不会泄题。
- **未知答案要保留 unknown**：有道题 `answerSource=unknown`，判题返回 `correct: null`。**不要在页面上推断或填充。**
- **重复操作幂等**：交卷/建标记/上报事件都带 `eventId` 或会话 ID 去重。重复点击不会产生重复记录。
- **过期结果会被拒**：`POST /practices/{id}/next` 带旧 `stepId` 会返回 409。你收到 409 时不要覆盖当前界面状态。

---

## 给 B（后端）

### 1. 真实接口就绪后，用同一套断言打你的服务

```bash
CET_API_BASE=http://127.0.0.1:5173 .venv/bin/python -m pytest tests/test_cet_contract.py -v
```

**这是最重要的一条**：同一套断言既能打 Mock 也能打真实服务，你不需要另写测试。

当前真实服务是 `41 failed / 7 passed / 8 skipped` —— 全部因为 `/api/cet/*` 还没实现。
每实现一批，对应用例就会转绿。

### 2. 先看「还差哪些接口」，照着实现

```bash
.venv/bin/python scripts/qa/check_backend_gap.py
```

它把契约测试的失败**倒推成接口清单**，按 5 个批次排好：

| 批次 | 内容 | 转绿用例 |
| --- | --- | --- |
| 1 | 材料与句子 | 6 |
| 2 | 交卷与判题 | 5 |
| 3 | 标记与确认 | 8 |
| 4 | 练习与带练 | 13 |
| 5 | 卡点与复习 | 4 |

现在跑，会明确告诉你批次 1 的 4 个接口还没做，做完了再跑一次就能确认转绿。
**不用对着一堆红字猜。**

### 3. 字段规格直接查，不用翻代码

```bash
.venv/bin/python scripts/qa/gen_api_spec.py
```

生成 [`接口规格.md`](./接口规格.md)：24 个接口、每个字段的类型/可空性/示例。
注意它标出的可空字段（如 `answer: integer|null`）—— 那是「未知答案」路径，
**不要在实现里填默认值**。

### 4. 分层跑，先攻契约层

```bash
# 纯形状断言，不改状态，最快
.venv/bin/python -m pytest tests/test_cet_contract.py -m contract -v

# 会话流转（交卷→标记→带练→复习）
.venv/bin/python -m pytest tests/test_cet_contract.py -m flow -v

# 并发一致性
.venv/bin/python -m pytest tests/test_cet_contract.py -m concurrent -v
```

建议顺序：`contract` 全绿 → 再攻 `flow`。

### 5. 特别提醒：并发不是可选项

C 在 Mock 里就抓到了一个真实并发 bug（BUG-006b）：`submit_attempt` 用「先查再写」，
并发下两个请求同时通过检查，产生两个 attemptId。修法是加锁。

**你的实现如果也用「先查后写」而没有事务或唯一约束，会遇到同样问题。**
四条并发用例专门测这个：

| 用例 | 测什么 |
| --- | --- |
| `test_concurrent_duplicate_events_count_once` | 同一 eventId 并发上报，时长只算一次 |
| `test_concurrent_attempts_create_one_record` | 同一会话并发交卷，只有一个 attemptId |
| `test_concurrent_marks_from_different_events_all_recorded` | 不同标记并发建，都落库且 ID 唯一 |
| `test_concurrent_stale_and_fresh_next_do_not_corrupt_state` | 并发 next 后状态仍自洽 |

### 6. 契约里需要你确认的 4 个点（T10 换句检查）

第 7 节列了 `transfer` 动作和 `candidateRef` 字段，但**没定义候选对象结构**。
C 先给了可运行形状（`tests/fixtures/cet/transfer_candidates.json`），需要你定：

1. 候选检索的数据来源：只在本材料内检索，还是跨材料？
2. 候选数量上限与排序依据？
3. 无候选时返回空 `items` 还是错误码？（C 按空 items 设计，便于页面展示空态）
4. 换句题目与参考答案是否需独立生成并要求来源？

你定了之后 C 改样例，**不要让前端照 C 的提议硬编码**。

### 7. 另外两个待你处理的

- **BUG-001**：`requirements.txt` 未锁 FastAPI 版本，装到 0.162 后 `app.routes` 只有 5/17 有 `.path`，底座一个测试因此失败。属版本漂移，**运行时正常**。锁版本会改你的开发环境，需三人同意，所以 C 没动。**写路由自检时请用 `app.openapi()["paths"]`，不要遍历 `app.routes`。**
- **BUG-002**：无 AI 密钥时所有 AI 接口返回 500 而非优雅降级。根因在 `ai_config.py` —— 注释说会前置拦截，实际直接用占位符 `"missing-api-key"` 发请求。建议补前置检查，返回可展示的「未配置」错误。

### 8. 验收场景与契约对照

`docs/qa/验收场景覆盖矩阵.md` 列了 T01—T16 每个场景现在靠哪条测试验证。
你要实现哪些接口，对照它看最清楚。

---

## 数据隔离（两人都需注意）

| 用途 | 位置 |
| --- | --- |
| C 的隔离测试数据 | `.cet-qa/data/vocab.db` |
| 默认数据（**别动**） | `resources/vocab.db` |
| 共同联调音频 | `.cet-qa/media/cet6-202512-conv2.mp3`（不入库） |

启动脚本已把 `ELT_USER_DB` 指到隔离目录，**不会碰用户原有学习记录**。
若你想用自己的数据目录，设环境变量即可。

---

## 一句话总结

| 你是谁 | 现在第一步 | 命令 |
| --- | --- | --- |
| A | 把 API base 指到 5199 开始做页面 | `scripts/qa/start_cet.sh --mock` |
| B | 实现 `/api/cet/*`，用 contract 层逐个转绿 | `CET_API_BASE=http://127.0.0.1:5173 pytest -m contract` |

有对不上的地方，先看契约样例（`tests/fixtures/cet/`）——那是三人共同的准绳。
样例与实际不符时，**先对齐契约再改代码**，不要各自迁就。
