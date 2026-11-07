# 推送说明 · codex/cet-fix2.1-integration

> 推送时间：2026-11-06｜推送人：施工方主代理
> 远端分支：`origin/codex/cet-fix2.1-integration`
> 远端 SHA：`0dec592081b91ae2f146ebbdb53717c8d75fc7e4`（与本地一致，已核 `git ls-remote`）
> 基于：`cet-base-delivery` = `6f07433`（**该分支本身未动，也未推送任何改动到它**）

---

## 一、这次推的是什么

一个分支、两轮返修、一次队友合并。**41 个提交，80 个文件，+13926 / −180 行。**

| 轮次 | 范围 | SHA 锚点 | 说明文档 |
| --- | --- | --- | --- |
| 返修 2 | 5 个包（工作区 / 快捷键 / 导入切句 / 收藏文案 / 测试路由文档） | `6f07433` → `3573e90` | `docs/cet-base/返修2/总验收报告.md` |
| 返修 2.1 | 2 个包（前端 / 后端）+ 3 项文档 | `3573e90` → `398f30b` | `docs/cet-base/返修2.1/总验收报告.md` |
| 队友合并 | 卡点标记删除功能 | `398f30b` → `0dec592` | 本文 §五 |

`cet-base-delivery` 分支**没有**被改动，仍是 `6f07433`。是否合入该分支、何时推送，等审核方复验后再定。

---

## 二、返修 2（`6f07433` → `3573e90`）

按任务书 [51-CET底座返修任务书-并行版] 施工：1 主代理 + 5 子代理并行，各自独立 worktree / 分支 / 端口。

### 2.1 做了什么

| 包 | 编号 | 内容 |
| --- | --- | --- |
| 包1 工作区 | P1-1…P1-6 | 「逐句精听 →」入口（P0，原先精听页是全站孤岛）、「本课收藏」文案、收藏回流、导出功能收束、接口重复请求去重（`words` 4→1 次等）、三个共享脚本按 feature 条件加载 |
| 包2 快捷键 | P2-1…P2-4 | 底座在「队友模块关闭」时接管 Space/←/→/R/L（含句循环）；队友开启时全让出；「⌨ 快捷键」提示；重复 mount 幂等 |
| 包3 导入切句 | P3-1…P3-3 | 可选「本地模型自动切句」（默认不勾、有字幕时后端 422 硬拒、有 TXT 优先文本对齐）、说话人标记清洗、百度网盘初始化定位 |
| 包4 收藏文案 | P4-1…P4-3 | 统一「收藏」口径（词=收藏单词/句=收藏句子）、去英文残留、首页脚本条件加载 |
| 包5 测试文档 | P5-1…P5-5 | `CET_UI_BASE` 改必填（原先默认连 5173 会污染演示库）、不存在材料 404、SHA 与快照脚本文档修正 |

### 2.2 回归

```
2 failed / 904 passed / 62 skipped
```
基线 `2 failed / 769 passed / 28 skipped` —— failed 持平，passed +135。
两项失败始终是 `bdpan` CLI 未装、`planning` 私有模块缺失，与本项目无关。

### 2.3 合并后复验发现的 6 处补漏

全部由主代理在合并后复验中发现，**没有一个来自各包自述**：

1. 包1：`cet-favorites.js` 行为已改但缓存串未 bump（真实用户会吃到旧 JS）
2. 包4：包1 所需 4 个 `cet_copy` 键缺失；`fix2_lesson_collections` 通用值误写，改变了通用模式标题
3. 包1：P1-3 浏览器用例挑词竞态（在进度恢复前挑词），在非零进度库上必红
4. 包5：`功能收束清单.md` 按新口径过时
5. **包2：§0.6 回归** —— 通用模式下「本句循环」指示条泄漏可见。用「基线 vs 集成逐页 diff 可见内容」才抓到
6. 包5：合并后需更新的数字清单（6 文件 15 处）

---

## 三、返修 2.1（`3573e90` → `398f30b`）

依据 [52-返修2复验报告]。**这一轮的核心是「拆掉多余通道」，不是「补通道」** —— 因为返修 2 的前提是错的。

### 3.1 前提更正（最重要）

返修 2 的 P1-3 / P4-1 建立在「工作区收藏的词进不了『我的收藏』」这个判断上。复验发现该判断**不成立**：

- **用户主动收藏本来就能进「我的收藏」** —— `save_word`（`v2_lessons.py:1700`）末尾会调 `db.activate_word_review(...)`
- 当初看到的「本课 10 词、我的收藏 0 词」，那 10 个词是打开工作区时 `POST /highlighted-words/sync` **自动写入**的四六级高亮词，不是用户收藏
- 返修 2 为此新增的 `sync-collections` 把整张 `v2_lesson_words` 表激活进复习本 → 自动高亮词全部冒充成「收藏」，且**打开一个查看页面就写库**

### 3.2 做了什么

| 包 | 编号 | 内容 |
| --- | --- | --- |
| A 前端 | A1 | CET 模式不再自动同步高亮词（**先确认**字幕高亮 / `★` 标记 / 精听页「重点词汇」都不依赖它，结论：无依赖） |
| A 前端 | A1-附加 | **精听页存在第二条同根因通道**（`intensive.html:2300`），用户点一下「逐句精听 →」再返回，本课收藏就变 10。一并门控 |
| A 前端 | A2 / A3 | 去掉「我的收藏」页面加载时的写库调用；去掉收藏后多余的第二次 `activate` |
| A 前端 | A4 / A5 | 查词卡片改「☆ 收藏单词 / ✓ 已收藏」、去掉「＋ 加入复习本」；修 CSS `[hidden]` 被作者样式压过的问题 |
| B 后端 | B1 | **词形还原修复**（见 §3.3） |
| B 后端 | B2 | 短词（<5 字符）收藏后保留原句 |
| B 后端 | B3 | 删除 `sync-collections` 端点 |
| B 后端 | B4 | `/workspace/abc` 走 404 页面（通用模式逐字节一致） |
| 主代理 | M1–M3 | 演示库需 `--fresh` 重建；更正返修 2 文档里的错误说法；§5.2/5.3/5.4 标为已修 |

### 3.3 词形还原修复（阻断项）

用户可见的严重 bug：收藏 `getting` 存成 `gette`，释义是「欧洲轮机技术制造商集团」。

**根因**：`v2_vocab.py` 的 `_lookup_candidates()` 对 `-ing`/`-ed` 无条件追加 `base+"e"`（`:462`/`:468`），
`_preferred_lookup_lemma()` 直接取 `candidates[-1]`（`:487`）**且不检查该词是否真实存在**。

**修法**：三层 —— ECDICT `exchange` 字段优先 → 规则剥离兜底 → 条目质量校验（存在性 + 词性标记 + 排除 abbr/人名地名）。

| 修前 | 修后 |
| --- | --- |
| `getting → gette`（欧洲轮机技术制造商集团） | `getting → get`（得到；获得） |
| `displaying → displaye` | `displaying → display` |
| `running → runne` | `running → run` |
| `studies → studie` | `studies → study` |
| `played → playe` | `played → play` |
| `walked → walke` | `walked → walk` |
| `carried → carrie` | `carried → carry` |
| `tried → trie` | `tried → try` |

后 4 个是本轮**新发现**的：`playe`/`walke`/`carrie`/`trie` 在 ECDICT 里**都是真实词条**
（翻放机 / 人名沃克 / 女名卡丽 / [计]三），所以「只查词典存在性」不够，必须有条目质量层。

### 3.4 回归

```
2 failed / 1007 passed / 72 skipped
```
基线 `2 failed / 904 passed / 62 skipped` —— failed 持平，passed +103。

### 3.5 F1–F9 总验收（全新隔离库实跑）

| 场景 | 结果 |
| --- | --- |
| F1 不冒充收藏 | ✅ 全新库打开工作区：本课收藏 **0**、`highlighted-words/sync` **0 次**；打开「我的收藏」：空态、`sync-collections` **0 次**；字幕高亮仍 10 个 |
| F1 变体（主代理追加） | ✅ 经过精听页再返回，本课收藏仍 **0** |
| F2 真收藏 | ✅ 收藏 `getting`/`hoping` → 我的收藏显示 `get`/`hope`，释义正常，**两词都带原句** |
| F3 文案 | ✅ 可见文本「生词」「复习本」「生词本」各 **0 次** |
| F4 隐藏 | ✅ 设 `hidden` → `display: none` |
| F5 路由 | ✅ `/workspace/abc`、`/workspace/abc/intensive` 均 404 + 返回链接 |
| F6 通用模式 | ✅ 高亮同步保留、本课收藏 10、词卡「☆ 加入生词 / ＋ 加入复习本」= 原版 |
| F7 回归 | ✅ 2 failed（不新增） |
| F8 正式库 | ✅ digest `92b86dba…f9dc4` 未变 |
| F9 返修2 不回退 | ✅ 精听入口、Space/←→/L、自动切句、SRT 冲突拒绝、404 全部仍通过 |

---

## 四、红线自查（两轮全程）

| 红线 | 状态 |
| --- | --- |
| `cet-base-delivery` 未动 | ✅ 仍为 `6f07433` |
| 正式库 `resources/vocab.db` | ✅ digest 全程 `92b86dbaceb711b0a244d6e0b447d8fdb042c9c905dfae0894c095f24a3f9dc4` |
| 5173 演示实例（PID 55798） | ✅ 全程存活未重启 |
| `backend/db.py` | ✅ 零改动（返修 2.1 任务书要求） |
| pytest 只设 `ELT_CONFIG_DIR` | ✅ 从未用 `ELT_USER_DB` 跑测试 |

**一处如实记录**：返修 2 施工**早期**，演示库被写入 2 条学习进度
（`lesson 1 @ 0.844s`、`lesson 2 @ 0.08s`）。根因是当时 p1–p4 分支仍带 `127.0.0.1:5173` 默认值，
正是 P5-1 要修的事故本身；P5-1 合并后已零写入。演示库未回滚，如需干净演示库用 `run.sh --fresh` 重建。

---

## 五、队友合并（`398f30b` → `0dec592`）

### 5.1 来源与谱系

队友推送在 `origin/codex/cet-listening-marks`（`b88d067`），含 2 个提交：

```
b88d067 feat(cet): 卡点标记列表增加删除按钮
ec9b293 feat(cet): 卡点标记支持删除
```

**谱系注意**：该分支的父是 `e3e64ee`（`origin/codex/cet-listening`），
它是我们基线 `6f07433` 的**祖先**，落后 85 个提交。所以「拿 `6f07433` 直接 diff 队友分支」
会显示「删除 3 万行、删掉全部 `test_cet_base_*.py`」—— 那是**落后造成的假象**，不是队友删了东西。
正确的比较基准是 merge-base `e3e64ee`，实际改动只有 **+62 行**。

### 5.2 改动内容

| 文件 | 内容 | 合并方式 |
| --- | --- | --- |
| `backend/webapp/fastapi_routes/cet.py` | `DELETE /marks/{mark_id}` 路由 | `git apply` 自动适配（偏移 +11 行） |
| `backend/webapp/services/cet_service.py` | `delete_mark()`：404 `MARK_NOT_FOUND` / 409 `MARK_IN_PRACTICE` | 自动适配（偏移 +177 行） |
| `frontend/static/cet-listening.css` | `.cet-btn-danger` 样式 | 直接应用 |
| `frontend/static/cet-listening.js` | 列表「删除」按钮 + `deleteMark()` | **手工适配**（见 §5.3） |
| `tests/test_cet_backend.py` | 2 条新测试 | 直接应用 |

### 5.3 手工适配说明（`cet-listening.js`）

我们这边该文件相对队友基线已演进 `+581/-55`，`renderMarks()` 的 `acts` 块多了「改一下」按钮，
队友的 hunk 上下文对不上（`git apply --check` 明确报错）。

手工插入位置：`if (st === 'ready')` 块之后、`row.appendChild(acts)` 之前 —— 与队友原意一致。
已核对 `api()` 支持任意 method、`flashHint(text, isError)` 签名一致。

**后端代码与队友逐行 diff 无差异**；JS 逻辑一致，唯一多出的 1 行是说明注释。

### 5.4 验证

- 真实服务端到端：创建 `201` → 列表 `count=1` → 删除 `200` → 列表 `count=0` → 重复删除 `404 MARK_NOT_FOUND`
- 前端加载无 JS 报错，`cet-btn-danger` 样式生效
- 409 `MARK_IN_PRACTICE` 由队友测试覆盖（真实环境确认标记需先交卷，属业务前置条件）
- 全量回归 **2 failed / 1009 passed / 72 skipped**（合并前 2/1007，+2 即队友的两个新测试）

### 5.5 给队友的建议

队友分支落后我们 85 个提交。若继续在 `e3e64ee` 这条老基线上开发，下次合并的适配成本会越来越大。
**建议 rebase 到 `codex/cet-fix2.1-integration` 再开工。**

---

## 六、最终数字

| 项 | 值 |
| --- | --- |
| 分支 | `codex/cet-fix2.1-integration` |
| SHA | `0dec592081b91ae2f146ebbdb53717c8d75fc7e4` |
| 提交数（相对 `6f07433`） | 41 |
| 文件改动 | 80 files, +13926 / −180 |
| 全量回归 | **2 failed / 1009 passed / 72 skipped** |
| 正式库 digest | `92b86dbaceb711b0a244d6e0b447d8fdb042c9c905dfae0894c095f24a3f9dc4` |
| 新增测试文件 | 12 个（`tests/test_cet_fix2_*.py` × 9 + `tests/test_cet_fix21_*.py` × 3） |

### 复验入口

```bash
git fetch origin codex/cet-fix2.1-integration
git checkout codex/cet-fix2.1-integration

# 全量回归（只设 ELT_CONFIG_DIR，绝不设 ELT_USER_DB）
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
  --ignore=tests/test_cet_listening_ui.py --ignore=tests/test_cet_contract.py
# 期望：2 failed, 1009 passed, 72 skipped
```

### 详细验收文档

| 文档 | 内容 |
| --- | --- |
| [`docs/cet-base/返修2/总验收报告.md`](docs/cet-base/返修2/总验收报告.md) | 返修 2：21 个编号逐项证据、E1–E10 实测、6 处补漏、3 处偏离、5 项范围外发现 |
| [`docs/cet-base/返修2.1/总验收报告.md`](docs/cet-base/返修2.1/总验收报告.md) | 返修 2.1：12 个编号、F1–F9 实测、3 处偏离、3 项未完成如实记录 |
| `docs/cet-base/返修2/包1…包5-交付说明.md` | 各包改了什么、行号、命令输出原文 |
| `docs/cet-base/返修2.1/包A-交付说明.md`、`包B-交付说明.md` | 同上 |

---

## 七、需要审核方裁决的三件事

1. **`natural-tts.js` 在 CET 模式保留加载**（返修 2 §4.1）—— 它服务收藏词卡发音与句子兜底朗读，
   属 CET 保留功能；任务书字面写「三个脚本都移除」，实际只移除了 `elt_credits.js` 与 `voice-input.js`。
2. **返修 2.1 的 B1/B2 未按 CET 模式门控**（返修 2.1 §5.1）—— 词形还原与短词原句是**数据正确性**修复，
   两种模式都生效。若门控，会出现「CET 存 `get`、通用模式存 `gette`」的荒谬结果。
3. **返修 2.1 改了 `intensive.html`**（返修 2.1 §5.2）—— 不在任务书文件所有权表内，
   但它是 A1 的同根因第二条通道，不堵则 A1 在真实使用中等于没修完。
