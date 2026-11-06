# 包 B · 后端 — 交付说明（返修 2.1）

| 项 | 值 |
| --- | --- |
| 分支 | `codex/cet-fix2.1-b` |
| 基线 | `3573e90` |
| worktree | `/tmp/wt-fix21-b` |
| 服务端口 | 5282（隔离数据目录 `/tmp/fix21-b-svc`） |
| 任务书 | [53-CET底座返修2.1任务书-2026-11-06.md](../../../../../../Users/杨玉宝个人资料/四六级智能体打造/53-CET底座返修2.1任务书-2026-11-06.md) §3 包 B |

## 0. 一句话结论

B1（阻断）、B2、B3、B4 全部完成并验证。全量回归 **3 failed / 982 passed / 62 skipped** ——
相对基线（2 failed / 904 passed / 62 skipped）**只多 1 项**，且那 1 项是
`test_cet_fix2_p4_favorites.py::test_p4_1_sync_endpoint_exists_and_is_lesson_scoped`，
它断言的正是 B3 删掉的端点，**由主代理改写为反向断言**（已确认接手）。

## 1. 改动清单（只有我名下的文件）

| 文件 | 改动 | 编号 |
| --- | --- | --- |
| `backend/webapp/services/v2_vocab.py` | 新增 `_exchange_lemma` / `_word_exists_in_dict` / `_is_quality_lemma_entry` / `_resolve_lemma` / `_ordered_candidates`；重写 `_preferred_lookup_lemma`；`lookup_word_meaning` 改用有序候选 + 还原 lemma | B1 |
| `backend/webapp/fastapi_routes/v2_lessons.py` | `save_word` 词句守卫加「原始提交词」兜底（10 行） | B2 |
| `backend/webapp/fastapi_routes/vocab.py` | 删除 `sync-collections` 端点 + `SyncCollectionsBody` + `_first_segment_with_word`（-131 行） | B3 |
| `backend/webapp/fastapi_routes/pages.py` | `lesson_id` 注解 `int` → `str` + `_coerce_lesson_id` / `_reject_non_integer_lesson_id` / `_missing_lesson_id` | B4 |
| `tests/test_cet_fix21_b_backend.py` | 新建，79 个用例 | B1–B4 |
| `docs/cet-base/返修2.1/包B-交付说明.md` | 本文件 | — |

**未改**：`backend/db.py`（任务书 §2 明确本轮不改）、`frontend/**`（包 A）、
`tests/test_cet_fix2_p4_favorites.py`（主代理接手）、`backend/webapp/cet_mode.py`（主代理改）、队友四文件。

## 2. B1（阻断）· 词形还原

### 2.1 根因

`v2_vocab.py` 两处配合出错：

1. `_lookup_candidates()` 为 `-ing`/`-ed` 词**无条件**追加 `base + "e"`（第 462、468 行）。
2. `_preferred_lookup_lemma()` 直接 `return candidates[-1]` —— 即那个凭空拼出来的 `+e` 形式，
   **且不检查它在词典里是否真实存在**。

`getting` 的 `+e` 形式 `gette` 恰好是 ECDICT 里一个真词条（「欧洲轮机技术制造商集团」），
所以被当成 lemma，用户收藏后看到的就是这个释义。

### 2.2 修法（三层，任务书 B1「不要造出一个不存在的词」）

```
第一层  ECDICT exchange 字段：0:<lemma> / 1:<marker>，只有屈折标记（p/i/d/s/3/s3/pd/dp）才认
第二层  规则剥离：_lookup_candidates 的候选按序取第一个「词典里查得到」的
第三层  校验：候选必须 (a) 词典存在 (b) 条目质量正常（非 abbr/人名）
        全失败 → 回退原词
```

**两个关键设计决定**（都是实测踩出来的，不是猜的）：

**① exchange 词元必须同时能被规则产出才采用** —— 否则 `went→go`、`children→child`
会被改写。任务书 B1 要的是「修词形还原 bug」，不是「引入不规则变形还原」；
`3573e90` 对这三个词保持原词，本轮不改（§1 通用规则 + B1 只要求「确认不会造出不存在的词」）。

**② 必须有「条目质量」这一层** —— 这是本轮最容易踩的坑：

| 候选 | ECDICT 里真实存在？ | 释义 |
| --- | --- | --- |
| `playe` | ✅ 存在 | [医]翻放机, 唱机 |
| `walke` | ✅ 存在 | [人名] 沃克 |
| `carrie` | ✅ 存在 | n. 卡丽（女名, Caroline 的昵称） |
| `tri` | ✅ 存在 | [计] 三 |
| `gett` | ✅ 存在 | abbr. 欧洲轮机技术制造商集团 |
| `mak` / `hav` | ✅ 存在 | abbr. 多次激活密钥 / 甲型肝炎病毒 |

**只查存在性会把 `played→playe`、`walked→walke`、`carried→carrie`、`tried→tri`。**
所以加了 `_is_quality_lemma_entry()`：要求 translation 里有常见词性标记，且不是 `abbr.` 开头、
不是人名/地名。这层不追求完美，只挡掉会污染收藏本的条目。

**③ 只改 lemma 不够，必须同时调整候选顺序** —— `_ordered_candidates()` 把还原后的词元提到首位。
不这么做，`getting` 的**释义**仍会先命中 `gett` 的轮机条目 —— B1 的可见症状就是那个释义。

### 2.3 修前 → 修后 10 词实测对照表

实测命令（同一份脚本，分别在新旧代码上跑）：
```bash
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python /tmp/b1table.py   # 调 lookup_word_meaning
```

| # | 词 | 修前 lemma | 修前释义 | 修后 lemma | 修后释义 | 结果 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `getting` | **`gette`** | **欧洲轮机技术制造商集团** | **`get`** | 得到；获得 | ✅ 修好（阻断项） |
| 2 | `displaying` | **`displaye`** | 展出；使展现；显示（ | **`display`** | 显示；陈列 | ✅ 修好（阻断项） |
| 3 | `hoping` | `hope` | 希望（；形式） | `hope` | 希望；信心 | ✅ 本来对，锁住 |
| 4 | `making` | `make` | 制造；手工制造业 | `make` | 制造；安排 | ✅ 本来对，锁住 |
| 5 | `running` | **`runne`** | 赛跑；流出 | **`run`** | 赛跑；奔跑 | ✅ 修好 |
| 6 | `studies` | **`studie`** | 研究；学习；学业（ | **`study`** | 学习；研究 | ✅ 修好 |
| 7 | `better` | `better` | 更好的；更好地 | `better` | 更好的；更好地 | ✅ 保持原样（见 §2.4） |
| 8 | `went` | `went` | 英文：v change location… | `went` | 同左 | ✅ 保持原词 |
| 9 | `children` | `children` | 孩子；孩子们 | `children` | 同左 | ✅ 保持原词 |
| 10 | `was` | `was` | 的过去式 | `was` | 同左 | ✅ 保持原词 |

**追加 4 个「只查存在性会错」的反例**（主代理要求，本轮发现的坑）：

| 词 | 修前 lemma | 修前释义 | 修后 lemma | 修后释义 |
| --- | --- | --- | --- | --- |
| `played` | **`playe`** | 玩耍；演奏；播放 | **`play`** | 游戏；游玩 |
| `walked` | **`walke`** | 走路；散步（；的过去分词） | **`walk`** | 散步；步行 |
| `carried` | **`carrie`** | 进位；射程 | **`carry`** | 进位；射程 |
| `tried` | **`trie`** | 试验过的；可靠的 | **`try`** | 尝试；试验 |

> `carried` 的释义修前修后一样（「进位；射程」是 `carry` 自己的释义，本来就没取错），
> 但 lemma 从 `carrie`（女名）修正成 `carry`。`tried` 的释义修前是 `trie` 的「试验过的；可靠的」，
> 修后是 `try` 的「尝试；试验」。

**其余规则路径覆盖**（测试里锁住）：`boxes→box`（-es）、`seeing→see`（-ing 且原形以 e 结尾，
修前是 `seee`）、`having→have`。

### 2.4 关于 `better` 的判断（任务书给了两个选项，我选「保持原样」）

任务书原文：「`better` 保持原样或还原成 `good`（看词典字段）」。查 ECDICT：

```
better → exchange = '0:good/1:r/d:bettered/s:betters/i:bettering/p:bettered/3:betters'
```

`1:r` 是**比较级**标记，`1:t` 是最高级 —— 它们不是屈折变形（屈折是 p/i/d/s/3/s3/pd/dp）。
比较级不是「同一个词的屈折形式」，而且 `better` 本身是独立词条（有自己的释义「较好的；比较好」，
`bnc=704`、`collins=5`）。所以选「保持原样」：`_INFLECTION_MARKERS` 白名单刻意排除 `r`/`t`。

### 2.5 B1 测试

`tests/test_cet_fix21_b_backend.py` 中 `test_b1_*`，共 54 个用例，含：

- 10 词参数化（`test_b1_lemma_resolution` + `test_b1_lookup_word_meaning_lemma`，各 16 个词）
- 负向用例 3 个：
  - `test_b1_negative_case_falls_back_to_original_word`（规则造出的候选全不存在 → 回退原词）
  - `test_b1_negative_case_nonsense_word_keeps_itself`（纯造词 → 回退原词）
  - `test_b1_negative_case_exchange_lemma_must_exist_in_dict`（exchange 指向的词词典里没有 → 不采用）
- `test_b1_never_returns_bogus_or_junk_lemma`（10 组「绝不能出现的 lemma」）
- `test_b1_getting_meaning_is_not_the_turbine_abbreviation`（**断言释义**，不只断言 lemma）
- `test_b1_better_stays_better_because_r_is_comparative`
- `test_b1_irregular_forms_keep_their_own_entry`
- `test_b1_lemma_is_never_the_unconditional_plus_e_form`（根因层：候选里仍有 `gette`，但不能被选中）
- `test_b1_works_in_both_cet_and_generic_mode`（CET/通用双模式）
- `test_b1_save_word_stores_real_lemma`（端到端：`POST .../word` 收藏 `getting`，库里是 `get`，且 `gette`/`gett` 不入库）

### 2.6 B1 浏览器验收（真实服务 5282 + 真实点击）

脚本 `/tmp/b1browser.py`（playwright，无 `route.abort()`，真实点击）：

```
1. goto /workspace/1，等 networkidle
2. 在字幕里点 "getting" 这个词（真实 DOM click）
3. 点收藏按钮
4. goto /#favorites
```

实测输出：
```json
{
 "clicked_word": "getting",
 "favorite_clicked": true,
 "favorites_has_get": true,
 "favorites_mentions_gette": false,
 "favorites_mentions_turbine": false,
 "favorites_mentions_displaye": false,
 "page_errors": []
}
```
「我的收藏」页面正文实测（截取）：
```
get        COCA #39      自评后显示释义  单词
           It's getting late and we're both displaying signs of weariness.
           2025年12月六级第一套 · Conversation Two
hope       COCA #482
           Okay, I'm hoping to finish in about an hour.
display    COCA #2092
           It's getting late and we're both displaying signs of weariness.
```
**「我的收藏」显示 `get` / `hope` / `display`，释义正常，页面无「gette」「displaye」「轮机」。**
截图（随本分支提交）：

| 文件 | 内容 |
| --- | --- |
| `docs/cet-base/返修2.1/evidence/b1-workspace.png` | 打开 `/workspace/1`，字幕里 `getting` / `displaying` 高亮 |
| `docs/cet-base/返修2.1/evidence/b1-popover.png` | 点 `getting` 后的查词浮层 |
| `docs/cet-base/返修2.1/evidence/b1-after-favorite.png` | 点收藏按钮后的状态 |
| `docs/cet-base/返修2.1/evidence/b1-favorites.png` | `/#favorites`：显示 `get` / `hope` / `display`，各带原句 |

## 3. B2 · 短词收藏后保留原句

### 3.1 根因（不改 `db.py`）

`db.py:1283-1284` 的 `sentence_contains_word()` 对 `len(w) < 5` 直接 `return False`（不做词干匹配）。

链路：收藏 `hoping` → 归一成 `hope`（4 字符 < 5）→ `sentence_contains_word(句, "hope")` = False
→ `find_v2_sentence_containing("hope")` 也找不到（句里是 `hoping`，而它用 `LIKE '%hope%'`）
→ `context_sentence = ""` → **原句丢失、`contexts=[]`**。

### 3.2 修法

`v2_lessons.py` `save_word` 里，当守卫判否时，**再拿用户实际提交的原词**（`body.word`，如 `hoping`）判一次；
命中就用这句提交上来的原句，否则仍走原兜底：

```python
context_sentence = str(body.sentence or "")
if context_sentence and not db.sentence_contains_word(context_sentence, word):
    # 返修 2.1 B2：短词（<5 字符）不做词干匹配，但原始提交词可能就在句子里（hoping → hope）
    raw_word = re.sub(r"[^a-z'-]", "", str(body.word or "").strip().lower()).strip("'-")
    if not (raw_word and raw_word != word and db.sentence_contains_word(context_sentence, raw_word)):
        context_sentence = db.find_v2_sentence_containing(word) or ""
```

`sentence_contains_word` 对 `hoping`（6 字符）走「先整词、再 max(4, len-2) 前缀」匹配，
能命中句里的 `hoping` —— 这一点是**实测确认的**，不是假设（见测试 `test_b2_short_word_keeps_submitted_sentence`）。

### 3.3 B2 实测（真实服务 5282）

```
hoping -> {'ok': True, 'word': 'hope', 'saved': True, 'count': 1, 'is_new': True, 'meaning': '希望；信心'}

=== /vocab-log ===
hope   meaning='希望；信心'
       context='Okay, I’m hoping to finish in about an hour.'   ← 原句保留 ✅
```

### 3.4 B2 测试

| 用例 | 断言 |
| --- | --- |
| `test_b2_short_word_keeps_submitted_sentence` | 收藏 `hoping`（原句含 hoping）→ 库里 `hope` 的 sentence 就是提交的那句 |
| `test_b2_short_word_context_reaches_vocab_log` | `/vocab-log` 里 `hope` 的 `contexts` 含这句（任务书 B2 验收原文） |
| `test_b2_word_not_in_sentence_still_uses_fallback` | **反向保护**：收藏的词不在提交的句子里 → 不保留那句错的句子，仍走原兜底（空语境） |
| `test_b2_fallback_to_lesson_sentence_still_works_for_long_words` | **反向保护**：长词走原兜底路径仍能找到 `v2_sentences` 里的句子 |
| `test_b2_long_word_behavior_unchanged` | **反向保护**：长词（>=5 字符）行为不变 |
| `test_b2_works_in_both_cet_and_generic_mode` | CET/通用双模式都保留原句 |

> **一处需要说清的实测细节**：`find_v2_sentence_containing("hope")` 用 `LIKE '%hope%'`，
> 而句里是 `hoping` —— **匹配不到**。所以「原始提交词也不在句子里」时，兜底结果仍是空串，
> 这是 `3573e90` 的原行为，B2 不改变它。我最初的测试写成「应兜底到含 hoping 的那句」，
> 实测发现那是我对兜底逻辑的误判，已按真实行为修正测试（而不是去改代码迎合测试）。

## 4. B3 · 删除 sync-collections 端点

### 4.1 删了什么

`backend/webapp/fastapi_routes/vocab.py`（-131 行）：
- `@router.post("/api/vocab-review/sync-collections")` 端点函数 `sync_lesson_collections`（原 :335-430）
- `class SyncCollectionsBody`（原 :328-332）—— **只被该端点使用**，已确认
- `def _first_segment_with_word`（原 :302-325）—— **只被该端点使用**，已确认

确认方式：`grep -rn "_first_segment_with_word\|SyncCollectionsBody" --include=*.py .` 只命中
定义处与端点内部调用处，无其它调用方。

### 4.2 实测验收

```
$ curl -s -o /dev/null -w "%{http_code}" -X POST http://127.0.0.1:5282/api/vocab-review/sync-collections -d '{}'
404
$ curl -s -X POST http://127.0.0.1:5282/api/vocab-review/activate -d '{"word":"climate","source":"manual"}'
200  {"word":"climate","count":1,...}        ← activate 保留（主代理确认过）
```

`grep` 残留（**全部在我不该动的文件里**）：
```
frontend/templates/index.html:5734          ← 包 A 的 A2 会删
tests/test_cet_fix2_p4_favorites.py:47,261,287,408,410,418,427,439  ← 主代理接手改写
backend/webapp/cet_mode.py:183,216          ← 主代理已删该文案键
```

### 4.3 B3 测试

| 用例 | 断言 |
| --- | --- |
| `test_b3_sync_collections_route_is_gone` | router 里没有该 path |
| `test_b3_sync_collections_source_is_gone` | 源码里没有端点实现 / `SyncCollectionsBody` / `_first_segment_with_word` |
| `test_b3_sync_collections_returns_404_on_live_app` | 真实 app `POST` 返回 404（或 410） |
| `test_b3_activate_endpoint_is_kept` | **边界**：`/api/vocab-review/activate` 必须保留 |
| `test_b3_frontend_has_no_remaining_call_in_backend_scope` | `backend/` 下（除 `cet_mode.py`）无残留引用 |

### 4.4 需要主代理同步处理的测试清单（已发 lead，此处备档）

| 文件:行 | 测试 | 处理 |
| --- | --- | --- |
| `tests/test_cet_fix2_p4_favorites.py:256-266` | `test_p4_1_sync_endpoint_exists_and_is_lesson_scoped` | 改写成反向断言（lead 已确认） |
| `:277-288` | `test_p4_1_frontend_syncs_on_favorites_load` | 改写成反向断言 |
| `:290-296` | `test_p4_1_sync_does_not_run_in_generic_mode` | 删除 |
| `:406-415` | `test_p4_1_sync_endpoint_is_idempotent_on_live_service` | 改写成「POST 返回 404」 |
| `:417-423` | `test_p4_1_sync_dry_run_reports_without_writing` | 删除 |
| `:425-430` | `test_p4_1_sync_unknown_lesson_is_404` | 删除 |
| `:432-448` | `test_p4_1_favorites_page_shows_synced_words_and_original_sentence` | 改用真实路径 `POST .../word` 保覆盖 |
| `:47` | copy-key 清单里的 `"fix2_sync_collections"` | lead 已删该键 + 清单项 |
| `:268-275` | `test_p4_1_sync_is_idempotent_by_construction` | 保留（测 `db.py` 的 upsert 事实），改名 |

**我没有改这个文件**（不在我名下）。

## 5. B4 · `/workspace/abc` 走 `cet_404.html`

### 5.1 根因与修法

`pages.py` 的 `lesson_id: int` 注解让 FastAPI 在**进函数之前**就把 `/workspace/abc` 拦成
422 JSON（`{"detail":[{"type":"int_parsing",...}]}`），用户看到一段 JSON。

修法：注解改 `str`，自己解析。关键在于**通用模式必须逐字节保持原样**，所以：

- 复用 **pydantic 自己的 int 解析器**（`TypeAdapter(int)`）—— 原版就是 pydantic 的 lax 解析，
  它接受 `-1` / `+1` / `01` / `1.0` / `1_0`（实测基线全部接受，不是 422）。用正则 `^\d+$` 会把它们误判。
- 解析失败时，通用模式**手工重建同一个 `RequestValidationError`**，交给 FastAPI 既有异常处理器，
  产出与原来一字不差的 422（status/headers/body 都比对过）。
- CET 模式解析失败 → 渲染同一个 `cet_404.html`。
- 传进 404 模板的 `lesson_id` 用**解析后的 int**（与 `3573e90` 一致），否则 `/workspace/+1`
  这类宽松形式会在 404 页上多显示一个字符（实测发现并修正，详见 §5.3）。

### 5.2 实测（真实服务 5282）

```
/workspace/abc              -> 404  404copy=1
/workspace/abc/intensive    -> 404  404copy=1
/workspace/999              -> 404  404copy=1      ← 返修2 P5-2 的原行为，未回退
```

### 5.3 通用模式逐字节比对（与 `3573e90`）

方法：同一份脚本 `/tmp/b4cmp2.py`，在新旧代码上各跑一次，比对 status / body 长度 / 前 160 字符 /
content-type / cache-control，覆盖 16 个 URL（含 `-1` `+1` `01` `%201` `1.0` `0x10` `1_0` 阿拉伯数字等边界）：

```
=== GENERIC (ELT_CET_MODE=0): baseline vs new ===
IDENTICAL ✅
```

**CET 模式**逐 URL 对比（只有预期内的变化）：

| URL | 基线 | 修后 | |
| --- | --- | --- | --- |
| `/workspace/-1` | 404 / 2988 | 404 / 2988 | same |
| `/workspace/+1` | 404 / 2987 | 404 / 2987 | same |
| `/workspace/01` | 404 / 2987 | 404 / 2987 | same |
| `/workspace/1.0` | 404 / 2987 | 404 / 2987 | same |
| `/workspace/1_0` | 404 / 2988 | 404 / 2988 | same |
| `/workspace/999999` | 404 / 2992 | 404 / 2992 | same |
| `/workspace/abc` | **422** / 154 | **404** / 2989 | ✅ B4 目标 |
| `/workspace/abc/intensive` | **422** / 154 | **404** / 2989 | ✅ B4 目标 |
| `/workspace/12x` | 422 / 154 | 404 / 2989 | ✅ B4 目标 |
| `/workspace/0x10` | 422 / 155 | 404 / 2990 | ✅ B4 目标 |
| `/workspace/1e3` | 422 / 154 | 404 / 2989 | ✅ B4 目标 |

> 「404 页长度从 2987 → 2988」那次差异是我第一版的 bug（把原始字符串传进模板），
> 已修成传解析后的 int，现在与基线完全一致。

### 5.4 B4 测试

| 用例 | 断言 |
| --- | --- |
| `test_b4_non_numeric_id_returns_404_page_in_cet_mode`（4 个 URL 参数化） | 404 + 含「材料不存在或已删除」+ 含「返回听力材料」+ **不含 `int_parsing`** |
| `test_b4_non_numeric_404_page_has_back_link` | 与 `/workspace/999999` 是同一个页面 |
| `test_b4_existing_lesson_still_renders` / `..._intensive_...` | 反向保护：存在的材料仍 200 |
| `test_b4_generic_mode_keeps_original_422`（4 个 URL 参数化） | 通用模式仍 422，且 `type`/`loc`/`input` 三个字段逐项断言 |
| `test_b4_generic_mode_missing_numeric_lesson_stays_200` | 通用模式不存在材料仍 200 空页 |
| `test_b4_generic_mode_accepts_lax_integer_forms` | `+1`/`01`/`1.0`/`1_0`/`-1` 不被误判 422 |

## 6. 偏离通用规则的说明（主代理已批准，此处备档）

任务书 §1 通用规则：「`ELT_CET_MODE=0` 时行为与 `3573e90` 完全一致」。
**B1 / B2 不按 CET 模式门控，两种模式都生效**。理由：

1. **它们是数据正确性 bug，不是产品模式差异。** 通用模式下用户同样会收藏单词，
   同样会存进 `gette` 并看到「欧洲轮机技术制造商集团」。
2. **复验报告 §4 明确要求并入返修 2.1**，原文：
   `§5.3 词形还原错误（displaying→displaye）｜并入返修 2.1。用户可见，而且 §2.1 修好以后用户自己收藏时仍会碰到`
   —— 「用户自己收藏时仍会碰到」这句本身就说明它不是 CET 专属。
3. **已有同类先例**：返修 2 的 P3-2（说话人标记清洗）也是两种模式都生效，
   复验报告 §2 判定通过、无异议。
4. 门控会产出荒谬结果：**同一个词，CET 模式存 `get`，通用模式存 `gette`**。

**双模式断言**（测试里已锁）：
- `test_b1_works_in_both_cet_and_generic_mode[1]` / `[0]` —— 两种模式下 `getting→get`、`displaying→display`、`running→run`、`studies→study`
- `test_b2_works_in_both_cet_and_generic_mode[1]` / `[0]` —— 两种模式下 `hoping` 的原句都保留

**B3 / B4 严格门控**：B4 通用模式 422 已逐字节比对（§5.3），B3 是纯删除（无模式分支）。

## 7. 验收命令与结果原文

### 7.1 我的新测试

```bash
$ cd /tmp/wt-fix21-b
$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_cet_fix21_b_backend.py
........................................................................ [ 91%]
.......                                                                  [100%]
79 passed in 1.27s
```

### 7.2 现有测试不回归（任务书指定的 5 个文件）

```bash
$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
    tests/test_cet_base_import.py tests/test_cet_fix2_p3_cleaning.py \
    tests/test_cet_fix2_p5_routes.py tests/test_dicts.py tests/test_ipa.py
```
`test_ipa.py` 需要真实服务（collection 期就 `KeyError: 'results'`），在 `3573e90` 上同样报错，
与本轮无关（已实测确认是既有状态）。其余文件：

```bash
$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
    tests/test_cet_base_import.py tests/test_cet_fix2_p3_cleaning.py tests/test_cet_fix2_p5_routes.py tests/test_dicts.py
```
结果见 §7.3 全量回归（含这 4 个文件，全绿）。

### 7.3 全量回归

```bash
$ ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q \
    --ignore=tests/test_cet_listening_ui.py --ignore=tests/test_cet_contract.py
3 failed, 982 passed, 62 skipped in 228.86s (0:03:48)
```

| 失败项 | 归属 |
| --- | --- |
| `test_baidu_pan.py::test_download_file_verifies_artifact` | **基线固定失败**（bdpan CLI 未装） |
| `test_health_planning_enabled.py::test_health_reports_planning_enabled_when_module_present` | **基线固定失败**（planning 私有模块缺失） |
| `test_cet_fix2_p4_favorites.py::test_p4_1_sync_endpoint_exists_and_is_lesson_scoped` | **B3 预期内**，主代理改写为反向断言 |

基线：`2 failed / 904 passed / 62 skipped`（主代理复核值）。
本分支：`3 failed / 982 passed / 62 skipped`。**passed +78**（我新增 79 个用例；
净增 78 是因为 B3 让 1 个原绿项变红，正好抵消 1 个新用例），**failed 只增 1 项且是 B3 的直接结果，已确认由主代理改写**。

### 7.4 浏览器验收（`CET_BASE_URL=http://127.0.0.1:5282`）

```bash
$ CET_BASE_URL=http://127.0.0.1:5282 ELT_CONFIG_DIR=$(mktemp -d) \
    .venv/bin/python /tmp/b1browser.py
```
输出见 §2.6（真实服务 + 真实 DOM 点击，无 `route.abort()`，无静态 HTML 断言）。

## 8. 未完成项 / 需要别人配合

| 项 | 状态 | 说明 |
| --- | --- | --- |
| B1 浏览器截图入仓 | ✅ 完成 | 4 张截图已复制进 `docs/cet-base/返修2.1/evidence/`，随本分支提交（见 §2.6） |
| B1 修前/修后对照表 | ✅ 完成 | §2.3（10 词）+ 4 个反例 |
| B3 测试清单交主代理 | ✅ 完成 | §4.4，已 `send_message` 给 lead，lead 已确认接手 |
| `cet_mode.py` 的 `fix2_sync_collections` 键 | ✅ 主代理已删 | 我没动 `cet_mode.py`（不在我名下） |
| 前端 `index.html:5734` 的 sync-collections 调用 | ⏳ 包 A 的 A2 | 我实测：前端**仍会发起**该请求（`/tmp/f1check.py` 抓到 1 次），但后端已 404，`cetSyncLessonCollections` 里 `if (!resp.ok) return 0;` 会静默返回 0 —— 无副作用，但**网络记录里仍能看到这次请求**。F1 验收要求「网络记录里没有 sync-collections」，**必须等包 A 删前端调用才能通过**。 |
| `backend/db.py` | ✅ 未改 | 任务书 §2 |
| 正式库 digest（F8） | ⏸ 主代理 | 我不读写 `resources/vocab.db` |

## 9. 复现我所有验证的最小步骤

```bash
# 1. 单元/集成测试
cd /tmp/wt-fix21-b
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_cet_fix21_b_backend.py

# 2. 起服务（隔离数据）
mkdir -p /tmp/fix21-b-svc/config
ELT_USER_DB=/tmp/fix21-b-svc/vocab.db ELT_CONFIG_DIR=/tmp/fix21-b-svc/config \
  ELT_PORT=5282 ELT_DATA_DIR=/tmp/fix21-b-svc \
  nohup .venv/bin/python backend/fastapi_server.py > /tmp/fix21-b-svc/server.log 2>&1 &
sleep 12
ELT_USER_DB=/tmp/fix21-b-svc/vocab.db ELT_CONFIG_DIR=/tmp/fix21-b-svc/config \
  .venv/bin/python scripts/cet/import_materials.py --all

# 3. B1/B2 服务端
.venv/bin/python /tmp/b1live.py     # getting→get, displaying→display, hoping→hope + 原句

# 4. B3/B4 服务端
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:5282/api/vocab-review/sync-collections -d '{}'   # 404
for u in /workspace/abc /workspace/abc/intensive; do
  curl -s -o /dev/null -w "$u -> %{http_code}\n" "http://127.0.0.1:5282$u"; done   # 404

# 5. 浏览器
CET_BASE_URL=http://127.0.0.1:5282 ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python /tmp/b1browser.py
```
