# 包 D · 数据与离线兜底 交付说明

分支：`codex/cet-drill-d`（worktree `/tmp/wt-drill-d`，端口 5305）
基线：`c4a480d`
范围：`scripts/cet/import_materials.py`、`backend/webapp/services/cet_ai.py`（只改无 key 降级分支）、`backend/webapp/services/cet_materials.py`（只读辅助）。
**未改** `cet_service.py`、`tests/**`、任何前端文件。

---

## 一、结论速览

| 编号 | 内容 | 状态 | 关键证据 |
| --- | --- | --- | --- |
| D1 | 导入演示材料时跳过「answer 未确认 + 题干含（样例占位）」的题；测试夹具原样保留 | ✅ | 两个材料各跳过 1 题；`GET questions` 六级为 `[5,6,7,8]`、四级为 `[1,2,3,4]`，无占位题；夹具文件 0 改动 |
| D2 | `feedback=meaning` 无 key 时 200 + `{mode:'offline', words:[{word,ipa,meaning}], translation?, hint:'配置模型后可获得讲解'}` | ✅ | 真实 5305 服务：HTTP 200，`currentStep.data.mode='offline'`，10 个词均有 word/ipa/meaning，hint 精确；有译文时包含 translation，无译文时省略 |
| D3 | `feedback=heard` 无 key 时不出现任何「模型未配置」类错误 | ✅ | 真实服务：HTTP 200，`currentStep.data={noCandidate:true,reason:'offline'}`，`transferStatus='no_candidate'`；响应全文无「模型未配置/模型尚未配置/尚未配置/请配置模型」等字样 |

---

## 二、D1：演示材料不显示（样例占位）题

### 改了什么

文件：`scripts/cet/import_materials.py`

| 位置 | 内容 |
| --- | --- |
| L127–L147 | 新增窄规则 `_PLACEHOLDER_MARKER='样例占位'`、`_answer_is_unconfirmed()`、`_is_sample_placeholder()`：**题干含「样例占位」且 answer 未确认**才算占位题；`0` 是合法答案（选项 A），不按 falsy 误判 |
| L254–L263 | `import_one()` 在构造题目 payload 前先过滤占位题，并打印跳过数量 |
| L300–L308 | 导入摘要增加「跳过 N 道（样例占位）题（answer 未确认）」说明 |

未改动 `tests/fixtures/cet/source/*.questions.json` 任何一个字节；未知答案路径仍由 fixtures 覆盖。

### 导入实测（真实脚本 + 全新临时库）

命令：

```bash
TMP=$(mktemp -d /tmp/cet-d-run-XXXX); mkdir -p "$TMP/config"
ELT_USER_DB="$TMP/vocab.db" ELT_CONFIG_DIR="$TMP/config" \
  CET_QA_MEDIA=/Users/a1111/Documents/ChatGPT/EchoLingo-cet/.cet-qa/media \
  .venv/bin/python scripts/cet/import_materials.py --all
```

输出原文（完整见 `docs/cet-base/卡点融合/evidence/D/d1-import-evidence.txt`）：

```text
  · 清洗 1 处说话人标记（/ W:）
  · 跳过 1 道（样例占位）题（answer 未确认，仅夹具保留）
新建 cet6-202512-conv2 → v2-1｜32 句｜105.5s｜跳过 1 道（样例占位）题（answer 未确认）
  · 跳过 1 道（样例占位）题（answer 未确认，仅夹具保留）
新建 cet4-sample-hope-wish → v2-2｜32 句｜80.5s｜跳过 1 道（样例占位）题（answer 未确认）

完成。启动后用 GET /api/cet/materials 确认。
```

服务实测（5305）：

```text
cet6-202512-conv2 questionCount= 4
cet4-sample-hope-wish questionCount= 4
-- cet6-202512-conv2
numbers= [5, 6, 7, 8]
placeholders= []
-- cet4-sample-hope-wish
numbers= [1, 2, 3, 4]
placeholders= []
```

对应夹具原题：`cet6-202512-conv2.questions.json` 第 9 题 `(样例占位)`（answer=null）、`cet4-sample-hope-wish.questions.json` 第 5 题 `（样例占位）`（answer=null）均只在夹具中保留，不进入实际导入。

---

## 三、D2：`feedback=meaning` 无 key 离线释义

### 根因

`feedback=meaning` 在 `cet_service.next_practice_step()` 中映射到 `action=explain`，最终调用 `cet_ai.generate()` → `cet_ai._call()`。基线 `_call()` 第一行：

```python
if not ai_config.AI_API_KEY:
    raise CETAIError("AI_NOT_CONFIGURED", "模型尚未配置；仍可使用本地回听、慢放、原文和保存。", 503)
```

因此无 key 时直接 503，前端只能走「模型尚未配置」分支。

### 改了什么

文件：`backend/webapp/services/cet_ai.py`

| 位置 | 内容 |
| --- | --- |
| L27–L58 | 新增 `_offline_explain()`：逐词生成 `{word, ipa, meaning}`；`ipa` 取 `dicts.lookup_ecdict_phonetic()`（缺省回退 `dicts.eng_ipa()`）；`meaning` 取 `dicts.lookup_ecdict_translation()`（原版离线 ECDICT，查不到返回空字符串，不抛错）；`hint` 固定「配置模型后可获得讲解」；译文来自 `cet_materials.sentence_translation()`，没有就不加该键 |
| L61–L71 | `_call()` 无 key 分支：`action='explain'` 返回 `_offline_explain(...), 'offline'`；`action='transfer'` 返回 `{noCandidate: True, reason: 'offline'}, 'offline'`；其它动作保持原 `AI_NOT_CONFIGURED` 503 |
| L153–L155 | `generate()` 遇到 `model='offline'`（且当前确实无 key）直接返回离线 payload，不再走模型输出格式校验 |

文件：`backend/webapp/services/cet_materials.py`（只读辅助，**无表/无列变更**）

| 位置 | 内容 |
| --- | --- |
| L572–L597 | 新增 `sentence_translation(text)`：只读查询 `v2_sentences.translation`（按 normalized_text），没有/异常返回空串；用独立 sqlite 只读连接，避免 `db.get_v2_sentence()` 在空库上初始化 schema 的写副作用 |

### 真实服务实测（5305，无 AI key）

`curl /health` 确认 `AI_API_KEY: missing`。`POST /api/cet/practices/<id>/next`，body `{feedback:'meaning', stepId, materialVersion}`：

```json
{
  "status": 200,
  "currentStep.action": "explain",
  "currentStep.data": {
    "mode": "offline",
    "words": [
      {"word": "It's", "ipa": "its", "meaning": "it is的缩写, it has的缩写"},
      {"word": "getting", "ipa": "'^etiŋ", "meaning": "n. 采煤, 采掘；获得"},
      {"word": "late", "ipa": "leit", "meaning": "a. 迟的, 晚的, 已故的"}
    ],
    "translation": "天色渐晚，我们都显出疲惫的迹象。",
    "hint": "配置模型后可获得讲解"
  },
  "aiRequest.model": "offline"
}
```

完整探针：`docs/cet-base/卡点融合/evidence/D/d2-http-probe-5305.json`（该次库中已预置该句译文，故 translation 存在）。另在空译文库验证：`translation` 键**不存在**（省略），其余结构不变。

### 浏览器实测（真实服务 + 真实点击 + 真实媒体状态）

截图（1440 / 375）：

- `docs/cet-base/卡点融合/evidence/D/d2-meaning-offline-1440.png`
- `docs/cet-base/卡点融合/evidence/D/d2-meaning-offline-375.png`

操作：真实打开 `/workspace/1` → 点击「继续精练 →」进入精练 → 真实点击「不懂意思」按钮 → 读取 `POST /api/cet/practices/<id>/next` 响应与页面 DOM。
实测数据（`d2-meaning-offline-probe.json`）：

```text
1440: status=200, mode=offline, hint=配置模型后可获得讲解, words=10, offline_dom=true, word_rows=10,
      before={t:3.78,paused:false,rate:1}, after={t:1.34,paused:false,rate:1}, pageerror=[]
375:  status=200, mode=offline, hint=配置模型后可获得讲解, words=10, offline_dom=true, word_rows=10,
      before={t:2.35,paused:false,rate:1}, after={t:1.35,paused:false,rate:1}, pageerror=[]
```

截图中的词卡由包 K 的 K7 `renderExplainContent` 渲染（`data.mode==='offline'` 分支，见 K 的 `cet-listening.js` L1296–L1338）。**联调借用 K 未提交版本、未提交到 D 分支**；D 分支本身不包含任何前端改动（缓存串无变化）。

---

## 四、D3：`feedback=heard` 无 key 不出现配置类错误

### 改了什么

同 `backend/webapp/services/cet_ai.py` L61–L71：无 key 且 `action='transfer'` 时返回 `{"noCandidate": True, "reason": "offline"}`，`cet_service` 按既有「无候选」分支收敛为 200 + `finish` 步骤（`source='local'`、`transferStatus='no_candidate'`），不抛 503，不出现任何「模型未配置」文案。

### 真实服务实测（HTTP，候选存在时）

```text
heard status 200
step action finish, source local, data {'noCandidate': True, 'reason': 'offline'}
transferStatus no_candidate
aiRequest.model = offline
bad words: []   # 扫描「模型未配置/模型尚未配置/尚未配置/请配置模型/AI_NOT_CONFIGURED/模型暂不可用」
```

### 浏览器实测（真实服务 + 真实点击）

截图（1440 / 375）：

- `docs/cet-base/卡点融合/evidence/D/d3-heard-offline-1440.png`
- `docs/cet-base/卡点融合/evidence/D/d3-heard-offline-375.png`

实测（`d3-heard-offline-probe.json`）：

```text
1440: status=200, step_action=finish, step_source=local, step_data={noCandidate:true,reason:'offline'},
      transferStatus=no_candidate, transfer_box=true, transfer_state=no_candidate, bad_words=[],
      before={t:2.28,paused:false,rate:1}, after={t:4.15,paused:true,rate:1}, pageerror=[]
375:  status=200, step_action=finish, step_source=local, step_data={noCandidate:true,reason:'offline'},
      transferStatus=no_candidate, transfer_box=true, transfer_state=no_candidate, bad_words=[],
      before={t:4.99,paused:false,rate:1}, after={t:4.14,paused:true,rate:1}, pageerror=[]
```

页面显示「换句检验（本地完成）/ 当前环境未启用智能换句检验，本轮按本地完成处理」，无「模型未配置」类错误。

---

## 五、两种回归

- `ELT_CET_FEATURE_CET_LISTENING_AGENT=0`：本次 D 分支未改任何模板/前端，功能关闭时页面与 `c4a480d` 一致；`tests/test_cet_base_mode.py`（含 `test_base05_listening_agent_env_override_can_also_disable`、`test_base05_workspace_does_not_mount_assets_when_disabled`）全部通过。
- `ELT_CET_MODE=0`（通用模式）：D 分支未改 `cet_mode.py`/页面；`tests/test_cet_base_mode.py`（含 `test_generic_mode_keeps_everything_for_original_regression`、`test_generic_mode_still_renders_every_legacy_entry`、`test_base05_generic_mode_mounts_teammate_module`）全部通过。

## 六、测试命令与输出原文

### 1) 目标单文件（CET 后端）

```bash
TMPCFG=$(mktemp -d /tmp/cet-d-pytest-XXXX)
ELT_CONFIG_DIR="$TMPCFG" .venv/bin/python -m pytest -q tests/test_cet_backend.py
```

输出原文（节选末尾）：

```text
.............F                                                           [100%]
=================================== FAILURES ===================================
...
>       assert response.status_code == 503
E       assert 200 == 503
...
FAILED tests/test_cet_backend.py::test_model_output_validation_and_missing_key
1 failed, 13 passed in 2.06s
```

说明：该失败是**旧契约断言**（`test_model_output_validation_and_missing_key` 写死了 `feedback=meaning` 无 key 必须 503）与 D2 新契约（200 离线）冲突；按任务书“不改 tests/”，本包未改测试，需主代理在集成时更新该断言。同文件其余 13 项（含模型输出校验、并发、幂等、AI 迟到响应等）全部通过。

### 2) CET 底座相关后端回归

```bash
TMPCFG=$(mktemp -d /tmp/cet-d-pytest-XXXX)
ELT_CONFIG_DIR="$TMPCFG" .venv/bin/python -m pytest -q \
  tests/test_cet_base_import.py tests/test_cet_base_materials.py tests/test_cet_base_mode.py \
  tests/test_cet_base_routes_registered.py tests/test_cet_base_startup.py
```

输出原文：

```text
........................................................................ [ 39%]
........................................................................ [ 79%]
......................................                                   [100%]
182 passed in 404.08s (0:06:44)
```

### 3) 全量 pytest（按 conftest 推荐口径，排除需真实浏览器/契约 mock 的两组）

```bash
TMPCFG=$(mktemp -d /tmp/cet-d-pytest-full-XXXX)
ELT_CONFIG_DIR="$TMPCFG" .venv/bin/python -m pytest -q \
  --ignore=tests/test_cet_listening_ui.py --ignore=tests/test_cet_contract.py
```

输出原文（末尾）：

```text
FAILED tests/test_baidu_pan.py::test_download_file_verifies_artifact - Assert...
FAILED tests/test_cet_backend.py::test_model_output_validation_and_missing_key
FAILED tests/test_health_planning_enabled.py::test_health_reports_planning_enabled_when_module_present
3 failed, 1053 passed, 104 skipped in 658.84s (0:10:58)
```

说明：
- 列表第 1、3 项（`test_baidu_pan` 的 `bdpan 未安装`、`test_health_planning_enabled` 的 planning 私有模块缺失）是 conftest 文档记录的本机基线环境失败，与 D 无关；
- 列表第 2 项 `test_model_output_validation_and_missing_key` 是 D2 新契约与旧断言冲突（见 §九.1）；
- 其余 1053 passed。

### 4) py_compile

```bash
.venv/bin/python -m py_compile \
  scripts/cet/import_materials.py backend/webapp/services/cet_ai.py backend/webapp/services/cet_materials.py
# → 无输出，exit 0
```

---

## 七、缓存串

D 包**未改任何 JS / CSS**（D 分支 `git diff --name-only` 只有 3 个 Python 文件 + 本文档/证据），因此没有新旧缓存串变化。截图中的前端来自 K/W 未提交联调版本（K 的缓存串为 `20261107-drill-k`，W 为 `20261107-drill-w`），**未提交到 D 分支**。

## 八、改动文件清单

```text
backend/webapp/services/cet_ai.py        +45
backend/webapp/services/cet_materials.py +29
scripts/cet/import_materials.py          +42
docs/cet-base/卡点融合/包D-交付说明.md    （本文件）
docs/cet-base/卡点融合/evidence/D/*      （截图 / 探针 JSON / 导入日志）
```

## 九、未完成项 / 风险 / 需要配合

1. **`tests/test_cet_backend.py::test_model_output_validation_and_missing_key` 旧断言需更新**：该测试写死 `feedback=meaning` 无 key → 503；D2 按任务书改为 200 离线。按“不改 tests/”本包未动它，主代理集成时请改为检查 200 + `mode='offline'`。
2. **`scripts/cet/verify_fresh_db.py` 的题目数断言需同步**：它写死每个材料 `questionCount == 5`（L127），D1 后实际导入 4 道（两个夹具各含 1 道样例占位题）。该脚本不在 D 的文件所有权内，未改；请主代理/脚本维护者改成 4，或改为“排除样例占位题后的数量”。
3. **`tests/test_cet_listening_ui.py` 的 `qs == 5` 断言**（L123 六级、L321 四级）同样基于旧导入数据；若用 D1 后的新库跑该浏览器用例会失败，需同步更新为 4（同样不属于 D 的文件所有权）。
4. **D2 的响应结构落在 `currentStep.data`**：`cet_service.next_practice_step()` 会把 `generate()` 返回值放进 `_new_step(payload=...)`，最终 `currentStep.data` 为 offline 结构。前端渲染由包 K 的 K7（`renderExplainContent` 已支持 `data.mode==='offline'`）负责；若 K 未合入，基线 `renderExplain` 会显示「模型未返回可用解释」，这是显示层问题、不是 D2 接口失败。
5. **D3 的本地行为是“未检验”收口**：无 key 时无法真正生成换句题，返回 `noCandidate=true/reason='offline'`，练习以 `finish` 步骤结束、`transferStatus='no_candidate'`；这是诚实的中性 200，不伪装成通过。K 前端显示「换句检验（本地完成）」。
6. **对已有交卷记录的旧库**：`import_materials.py` 原有规则是“已有交卷记录不重建题目”（保护历史作答），因此这类库跑 D1 过滤不会删除已有占位题；需要全新库或清理后重建。演示库建议用全新临时库重导（本包验证即如此）。
7. **范围外行为**：`feedback=sound` 第二次点击会走 `action=chunk` 且无 key 时仍返回 `AI_NOT_CONFIGURED` 503（D2/D3 只覆盖 meaning/heard，未越权改 chunk）；如需离线分块，另立任务。

## 十、证据文件

```text
docs/cet-base/卡点融合/evidence/D/
  d1-import-evidence.txt          导入日志 + API questionCount/题目号
  d2-http-probe-5305.json         D2 真实 5305 服务探针（含 translation）
  d2-meaning-offline-probe.json   D2 浏览器实测数据（1440/375）
  d2-meaning-offline-1440.png
  d2-meaning-offline-375.png
  d3-heard-offline-probe.json     D3 浏览器实测数据（1440/375）
  d3-heard-offline-1440.png
  d3-heard-offline-375.png
```

> 截图说明：为验证 D2/D3 的最终 UI 效果，截图在 `/tmp/cet-d-preview` 集成预览环境拍摄（后端为 D 分支代码，前端临时借用 K 的 `cet-listening.js/css` 与 W 的 `workspace.html/cet-workspace.css/cet-player.js` **未提交版本**）；这些前端文件没有、也不会进入 D 分支提交。基线单跑 D 分支时，D2/D3 的接口返回仍是本文档实测的 200 + offline 结构。
