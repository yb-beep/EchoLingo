"""返修 2.1 包 B（后端）· B1–B4 回归测试。

对应任务书 §3 包 B 的四个编号：

| 编号 | 问题 | 本文件覆盖 |
| --- | --- | --- |
| B1 | 词形还原出错（getting→gette、displaying→displaye，释义变成「欧洲轮机技术制造商集团」） | `test_b1_*` |
| B2 | 短词收藏后没有原句（收藏 hoping 存成 hope，contexts=[]） | `test_b2_*` |
| B3 | `sync-collections` 端点已无调用方且语义错误 | `test_b3_*` |
| B4 | `/workspace/abc` 返回 422 JSON，不是友好 404 页 | `test_b4_*` |

**B1/B2 不按 CET 模式门控**（两种模式都生效）：它们是词典/写库的**数据正确性** bug，
不是产品模式差异。复验报告 §4 明确要求并入返修 2.1，且原文写了「用户自己收藏时仍会碰到」。
本文件因此对 B1/B2 同时断言 CET 与 `ELT_CET_MODE=0` 两种模式。
**B3/B4 严格门控**：通用模式必须与 `3573e90` 完全一致。

跑法（只设 ELT_CONFIG_DIR，绝不设 ELT_USER_DB）：
```bash
ELT_CONFIG_DIR=$(mktemp -d) .venv/bin/python -m pytest -q tests/test_cet_fix21_b_backend.py
```
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_BACKEND = REPO / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# ══════════════════════════════════════════════════════════════════════
# B1 · 词形还原
# ══════════════════════════════════════════════════════════════════════
#
# 修前（3573e90）实测：
#   getting    candidates=['getting','gett','get','gette']     → lemma='gette'   ❌
#   displaying candidates=['displaying','display','displaye']  → lemma='displaye' ❌
#   running    candidates=['running','runn','run','runne']     → lemma='runne'   ❌
#   studies    candidates=['studies','study','studi','studie'] → lemma='studie'   ❌
#   hoping     candidates=['hoping','hop','hope']              → lemma='hope'    ✅
#   making     candidates=['making','mak','make']              → lemma='make'    ✅
# 根因：`_lookup_candidates` 无条件追加 `base+"e"`，`_preferred_lookup_lemma` 取
# `candidates[-1]` 且不检查该候选在词典里是否真实存在。
#
# 修后：三层还原（ECDICT exchange 优先 → 规则剥离兜底 → 存在性/质量校验），
# 全失败回退原词（任务书 B1「不要造出一个不存在的词」）。

# (输入词, 期望 lemma, 说明)
B1_LEMMA_CASES = [
    # ── 任务书 B1 点名的 7 个词 ──
    ("getting", "get", "阻断项原症状：修前 gette（释义「欧洲轮机技术制造商集团」）"),
    ("displaying", "display", "阻断项原症状：修前 displaye"),
    ("hoping", "hope", "修前本来就对，锁住不回归"),
    ("making", "make", "修前本来就对，锁住不回归"),
    ("running", "run", "修前 runne"),
    ("studies", "study", "修前 studie"),
    ("better", "better", "比较级 1:r 不还原，保持原样（见 test_b1_better_* 说明）"),
    # ── 3 个不规则变化词：确认不会造出不存在的词 ──
    ("went", "went", "不规则过去式；规则剥不出 go，保持原词"),
    ("children", "children", "不规则复数；规则剥不出 child，保持原词"),
    ("was", "was", "不规则过去式；ECDICT exchange 为空，保持原词"),
    # ── 4 个「只查存在性会错」的反例（主代理要求的追加用例）──
    # 这 4 个词正是 `_is_quality_lemma_entry` 质量层存在的理由：
    #   playe  → ECDICT 真有词条：[医]翻放机, 唱机
    #   walke  → ECDICT 真有词条：[人名] 沃克
    #   carrie → ECDICT 真有词条：n. 卡丽（女名, Caroline 的昵称）
    #   tri    → ECDICT 真有词条：[计] 三
    # 只靠「在词典里查得到」会把 played→playe、walked→walke、carried→carrie、tried→tri。
    ("played", "play", "反例：playe 在 ECDICT 是真词条（[医]翻放机）"),
    ("walked", "walk", "反例：walke 在 ECDICT 是真词条（[人名] 沃克）"),
    ("carried", "carry", "反例：carrie 在 ECDICT 是真词条（女名 卡丽）"),
    ("tried", "try", "反例：tri 在 ECDICT 是真词条（[计] 三）"),
    # ── 其余规则路径覆盖（-es / -ies / -ing 双写 / 去 e 加 ing）──
    ("boxes", "box", "-es 路径"),
    ("seeing", "see", "-ing 且原形以 e 结尾（seee 是拼错的候选）"),
    ("having", "have", "-ing 且原形以 e 结尾"),
]


@pytest.mark.parametrize("word,expected,note", B1_LEMMA_CASES, ids=[c[0] for c in B1_LEMMA_CASES])
def test_b1_lemma_resolution(word, expected, note):
    """B1：`_resolve_lemma` 必须给出真实存在的词元，不得造词。"""
    from webapp.services import v2_vocab

    assert v2_vocab._resolve_lemma(word) == expected, (
        f"{word} 应还原成 {expected}（{note}），实际 {v2_vocab._resolve_lemma(word)!r}"
    )


@pytest.mark.parametrize("word,expected,note", B1_LEMMA_CASES, ids=[c[0] for c in B1_LEMMA_CASES])
def test_b1_lookup_word_meaning_lemma(word, expected, note):
    """B1：端到端 `lookup_word_meaning` 返回的 lemma 必须是真实词元。

    这是 `save_word` 真正消费的字段（`v2_lessons.py` 用 `lookup.get("lemma")` 归一写库词）。
    """
    from webapp.services import v2_vocab

    v2_vocab._LOOKUP_CACHE.clear()
    result = v2_vocab.lookup_word_meaning(word)
    assert result.get("lemma") == expected, (
        f"{word} 的 lemma 应为 {expected}（{note}），实际 {result.get('lemma')!r}"
    )


@pytest.mark.parametrize(
    "word,forbidden",
    [
        # 修前会产出的「凭空拼出来的词」——这些都不是正常英文词，绝不能成为 lemma
        ("getting", "gette"),
        ("displaying", "displaye"),
        ("running", "runne"),
        ("studies", "studie"),
        ("seeing", "seee"),
        # 修前会产出的「词典里是缩写/人名」的候选
        ("getting", "gett"),
        ("played", "playe"),
        ("walked", "walke"),
        ("carried", "carrie"),
        ("tried", "tri"),
    ],
    ids=lambda v: str(v),
)
def test_b1_never_returns_bogus_or_junk_lemma(word, forbidden):
    """B1：不得把「造出来的词」或「缩写/人名条目」当 lemma。"""
    from webapp.services import v2_vocab

    assert v2_vocab._resolve_lemma(word) != forbidden, (
        f"{word} 的 lemma 不能是 {forbidden!r}（造词 / 缩写 / 人名条目）"
    )


def test_b1_getting_meaning_is_not_the_turbine_abbreviation():
    """B1 可见症状回归：`getting` 的释义不能是「欧洲轮机技术制造商集团」。

    修前 `_lookup_local_dict_meaning` 先命中 `gett`（ECDICT 里的缩写条目），
    用户收藏 `getting` 后看到的就是这个释义。只改 lemma 不改候选顺序修不好它，
    所以这里断言的是**释义**，不是 lemma。
    """
    from webapp.services import v2_vocab

    v2_vocab._LOOKUP_CACHE.clear()
    result = v2_vocab.lookup_word_meaning("getting")

    meaning = str(result.get("meaning") or "")
    assert "轮机" not in meaning, f"getting 的释义仍是轮机缩写条目：{meaning!r}"
    assert "欧洲" not in meaning, f"getting 的释义仍是欧洲轮机条目：{meaning!r}"
    assert result.get("lemma") == "get"


def test_b1_better_stays_better_because_r_is_comparative():
    """B1：`better` 保持原样，**不**还原成 `good`。

    任务书写「`better` 保持原样或还原成 `good`（看词典字段）」。查 ECDICT：
        better → exchange = '0:good/1:r/d:bettered/...'
    `1:r` 是**比较级**标记（不是屈折变形 p/i/d/s/3），`1:t` 同理是最高级。
    比较级不是「同一个词的屈折形式」，且 `better` 本身是独立词条（有自己的释义
    「较好的；比较好」）。所以这里选「保持原样」。
    """
    from webapp.services import v2_vocab

    assert v2_vocab._exchange_lemma("better") == "", (
        "better 的 exchange 是 1:r（比较级），不应被当成屈折词元还原"
    )
    assert v2_vocab._resolve_lemma("better") == "better"


def test_b1_irregular_forms_keep_their_own_entry():
    """B1：went / children / was 保持原词。

    ECDICT 的 exchange 确实给了 went→go、children→child（was 为空），但任务书 B1
    要求的是「修词形还原 bug」，不是「引入不规则变形还原」。`3573e90` 对这三个词
    保持原词，本轮不改这个行为（任务书 §1「`ELT_CET_MODE=0` 时行为与 3573e90 完全一致」，
    且 B1 只点名要「确认不会造出不存在的词」）。

    实现上靠 `_resolve_lemma` 的约束保证：exchange 词元必须**同时**能被常规剥离规则
    产出才会被采用。`went`/`children` 的规则候选只有自己，所以 exchange 的 go/child 不生效。
    """
    from webapp.services import v2_vocab

    for word in ("went", "children", "was"):
        assert v2_vocab._resolve_lemma(word) == word, f"{word} 必须保持原词"
        # 且 exchange 里确实有 / 没有词元，说明这是刻意的规则约束而非碰巧
    assert v2_vocab._exchange_lemma("went") == "go"
    assert v2_vocab._exchange_lemma("children") == "child"
    assert v2_vocab._exchange_lemma("was") == ""


def test_b1_negative_case_falls_back_to_original_word():
    """B1 负向用例 1：规则造出来的候选在词典里都不存在 → 必须回退原词。

    `test-ifying` 不在 ECDICT 里，规则会剥出 test-ify / testif / testifie 之类，
    全都不存在，最终必须回退到原词本身，不能返回任何拼出来的形式。
    """
    from webapp.services import v2_vocab

    word = "testifyinglyz"  # 词典里不存在，规则能剥出 testifyinglyz→… 一堆假候选
    result = v2_vocab._resolve_lemma(word)
    assert result == word, f"还原失败必须回退原词 {word!r}，实际 {result!r}"


def test_b1_negative_case_nonsense_word_keeps_itself():
    """B1 负向用例 2：纯造词（exchange 与规则都还原不出真词）→ 回退原词。"""
    from webapp.services import v2_vocab

    for word in ("zzzgetting", "florbing", "blorpted"):
        assert v2_vocab._resolve_lemma(word) == word, f"{word} 必须回退原词"


def test_b1_negative_case_exchange_lemma_must_exist_in_dict(monkeypatch):
    """B1 负向用例 3：exchange 指向的词元在词典里查不到 → 不采用，回退。

    用 monkeypatch 造一个「exchange 说有词元、但词典里没有」的情形，
    确认校验层生效（任务书 B1「还原出来的词在 ECDICT 里查不到时，回退成原词」）。
    """
    from webapp.services import v2_vocab

    monkeypatch.setattr(v2_vocab, "_exchange_lemma", lambda w: "nonexistentlemma")
    monkeypatch.setattr(v2_vocab, "_word_exists_in_dict", lambda w: False)
    monkeypatch.setattr(v2_vocab, "_is_quality_lemma_entry", lambda w: False)

    assert v2_vocab._resolve_lemma("getting") == "getting"


def test_b1_lemma_is_never_the_unconditional_plus_e_form():
    """B1 根因回归：`+e` 形式只能在词典里真实存在时才被采用。

    这是根因层面的断言 —— `_lookup_candidates` 仍然会产出 `base+"e"`
    （别处依赖它的产出，不改它的顺序），但它绝不能无条件成为 lemma。
    """
    from webapp.services import v2_vocab

    # 确认根因仍在候选里（说明我们修的是「选择」而不是「把候选删掉」）
    assert "gette" in v2_vocab._lookup_candidates("getting")
    assert "displaye" in v2_vocab._lookup_candidates("displaying")
    # 但绝不能被选中
    assert v2_vocab._preferred_lookup_lemma(v2_vocab._lookup_candidates("getting")) == "get"
    assert (
        v2_vocab._preferred_lookup_lemma(v2_vocab._lookup_candidates("displaying"))
        == "display"
    )


@pytest.mark.parametrize("cet_mode", ["1", "0"])
def test_b1_works_in_both_cet_and_generic_mode(monkeypatch, cet_mode):
    """B1 不门控：CET 模式与通用模式都必须 `getting→get`。

    理由（交付说明「偏离通用规则说明」一节）：B1 是词典数据正确性 bug，
    不是产品模式差异。复验报告 §4 明确要求并入返修 2.1。
    """
    monkeypatch.setenv("ELT_CET_MODE", cet_mode)
    from webapp.services import v2_vocab

    v2_vocab._LOOKUP_CACHE.clear()
    for word, expected in (
        ("getting", "get"),
        ("displaying", "display"),
        ("running", "run"),
        ("studies", "study"),
    ):
        assert v2_vocab.lookup_word_meaning(word)["lemma"] == expected, (
            f"ELT_CET_MODE={cet_mode} 下 {word} 应还原成 {expected}"
        )


def test_b1_save_word_stores_real_lemma(tmp_path, monkeypatch):
    """B1 端到端：`POST /api/v2/lessons/{id}/word` 收藏 `getting`，库里存的是 `get`。

    这是任务书 B1 验收「浏览器里收藏 getting，『我的收藏』显示 get」的服务端等价证据。
    """
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    lesson = db.create_v2_lesson(
        source_type="youtube",
        source_url="https://www.youtube.com/watch?v=b1getting01",
        video_id="b1getting01",
        title="B1 getting",
    )

    resp = client.post(
        f"/api/v2/lessons/{lesson['id']}/word",
        json={"word": "getting", "sentence": "I am getting better at this."},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["word"] == "get", (
        f"收藏 getting 必须存成 get，实际 {resp.json()['word']!r}（B1 阻断项回归）"
    )
    assert "get" in db.get_all_words()
    assert "gette" not in db.get_all_words(), "gette 是被造出来的词，绝不能入库"
    assert "gett" not in db.get_all_words(), "gett 是缩写条目，绝不能入库"


# ══════════════════════════════════════════════════════════════════════
# B2 · 短词收藏后保留原句
# ══════════════════════════════════════════════════════════════════════
#
# 根因（不改 db.py）：
#   db.sentence_contains_word() 对 len(w) < 5 直接 return False（db.py:1283-1284 不做词干匹配）
#   → 收藏 hoping 归一成 hope（4 字符）→ 守卫判否 → find_v2_sentence_containing("hope")
#     也找不到（句里是 hoping）→ context_sentence="" → 原句丢失、contexts=[]。
# 修法：在 save_word 里当守卫判否、但**用户实际提交的原词**（body.word）确实出现在句子里时，
# 仍用这句提交上来的原句。


def _make_lesson(db_module, title: str, sentences: list[str]) -> int:
    lesson = db_module.create_v2_lesson(
        source_type="youtube",
        source_url=f"https://www.youtube.com/watch?v=b2{abs(hash(title)) % 10**8}",
        video_id=f"b2{abs(hash(title)) % 10**8}",
        title=title,
    )
    db_module.replace_v2_subtitle_segments(
        lesson["id"],
        [{"index": i + 1, "start": i, "end": i + 1, "text": t} for i, t in enumerate(sentences)],
    )
    # find_v2_sentence_containing 查的是 v2_sentences 表，字幕段是另一张表 —— 两处都要写
    for sentence in sentences:
        db_module.upsert_v2_sentence(sentence)
    return lesson["id"]


def test_b2_short_word_keeps_submitted_sentence(tmp_path, monkeypatch):
    """B2 主用例：收藏 `hoping`（存成 `hope`），原句必须保留。"""
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    sentence = "She was hoping to see him again."
    lesson_id = _make_lesson(db, "B2 hoping", [sentence])

    resp = client.post(
        f"/api/v2/lessons/{lesson_id}/word",
        json={"word": "hoping", "sentence": sentence},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["word"] == "hope", "hoping 归一成 hope"
    stored = db.get_v2_lesson_word(lesson_id, "hope")
    assert stored is not None
    assert stored.get("sentence") == sentence, (
        f"B2：hope 的原句必须保留提交上来的那句，实际 {stored.get('sentence')!r}"
    )


def test_b2_short_word_context_reaches_vocab_log(tmp_path, monkeypatch):
    """B2 验收：`/vocab-log` 里 `hope` 的 `contexts` 有这句原句。

    任务书 B2 验收原文：「收藏 hoping（原句含 hoping），`/vocab-log` 里 `hope` 的
    `contexts` 有这句」。
    """
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    sentence = "She was hoping to see him again."
    lesson_id = _make_lesson(db, "B2 contexts", [sentence])

    client.post(f"/api/v2/lessons/{lesson_id}/word", json={"word": "hoping", "sentence": sentence})

    log = client.get("/vocab-log").json()
    assert "hope" in log, f"/vocab-log 里应有 hope，实际 keys={list(log)[:10]}"
    contexts = log["hope"].get("contexts") or []
    assert contexts, "B2：hope 的 contexts 不能为空（原句丢失就是这个症状）"
    assert any(c.get("sentence") == sentence for c in contexts), (
        f"contexts 里必须含提交的原句，实际 {contexts}"
    )


def test_b2_word_not_in_sentence_still_uses_fallback(tmp_path, monkeypatch):
    """B2 反向保护：收藏的词**不在**提交的句子里时，仍按原逻辑走兜底。

    任务书 B2 验收原文：「收藏一个不在句子里的词，仍按原逻辑走兜底」。
    """
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    other_sentence = "The weather is nice today."
    real_sentence = "She was hoping to see him again."
    lesson_id = _make_lesson(db, "B2 fallback", [real_sentence])

    # 提交一个不含 hoping 的句子 → 守卫判否 → 走 find_v2_sentence_containing("hope")
    resp = client.post(
        f"/api/v2/lessons/{lesson_id}/word",
        json={"word": "hoping", "sentence": other_sentence},
    )

    assert resp.status_code == 200, resp.text
    stored = db.get_v2_lesson_word(lesson_id, "hope")
    assert stored is not None
    # 不能保留那句错的（不含词的）句子 —— 原守卫语义必须保持
    assert stored.get("sentence") != other_sentence, (
        "B2：不含词的句子不能被当成 hope 的语境（原守卫语义必须保持）"
    )
    # 兜底也找不到时保持原行为：语境为空串，不写脏数据
    # （`find_v2_sentence_containing("hope")` 用 `LIKE '%hope%'`，而句里是 hoping，匹配不到。
    #  这是 3573e90 的原行为，B2 只额外放行「原始提交词确实在句子里」的情形，不改兜底。）
    assert stored.get("sentence") == "", (
        f"B2：原始提交词不在句子里时应保持原兜底行为（空语境），实际 {stored.get('sentence')!r}"
    )
    assert real_sentence  # 该句确实存在于 v2_sentences，只是 LIKE 口径匹配不到


def test_b2_fallback_to_lesson_sentence_still_works_for_long_words(tmp_path, monkeypatch):
    """B2 反向保护：长词走原兜底路径仍能找到 v2_sentences 里的句子。

    确保 B2 的改动没有破坏「守卫判否 → 去 v2_sentences 找真正含词的句子」这条原路径。
    """
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    wrong_sentence = "The weather is nice today."
    real_sentence = "A complex system needs careful design."
    lesson_id = _make_lesson(db, "B2 long fallback", [real_sentence])

    resp = client.post(
        f"/api/v2/lessons/{lesson_id}/word",
        json={"word": "complex", "sentence": wrong_sentence},
    )

    assert resp.status_code == 200, resp.text
    stored = db.get_v2_lesson_word(lesson_id, "complex")
    assert stored is not None
    assert stored.get("sentence") == real_sentence, (
        f"B2：长词应兜底到 v2_sentences 里真正含该词的句子，实际 {stored.get('sentence')!r}"
    )


def test_b2_long_word_behavior_unchanged(tmp_path, monkeypatch):
    """B2 反向保护：长词（>=5 字符）走原逻辑，不受 B2 改动影响。"""
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    sentence = "A complex system needs careful design."
    lesson_id = _make_lesson(db, "B2 long", [sentence])

    resp = client.post(
        f"/api/v2/lessons/{lesson_id}/word",
        json={"word": "complex", "sentence": sentence},
    )

    assert resp.status_code == 200, resp.text
    stored = db.get_v2_lesson_word(lesson_id, "complex")
    assert stored is not None
    assert stored.get("sentence") == sentence


@pytest.mark.parametrize("cet_mode", ["1", "0"])
def test_b2_works_in_both_cet_and_generic_mode(tmp_path, monkeypatch, cet_mode):
    """B2 不门控：CET 与通用模式都必须保留原句（理由同 B1）。"""
    monkeypatch.setenv("ELT_CET_MODE", cet_mode)
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())
    sentence = "She was hoping to see him again."
    lesson_id = _make_lesson(db, f"B2 mode {cet_mode}", [sentence])

    client.post(f"/api/v2/lessons/{lesson_id}/word", json={"word": "hoping", "sentence": sentence})

    stored = db.get_v2_lesson_word(lesson_id, "hope")
    assert stored is not None
    assert stored.get("sentence") == sentence, f"ELT_CET_MODE={cet_mode} 下原句必须保留"


# ══════════════════════════════════════════════════════════════════════
# B3 · 删除 sync-collections 端点
# ══════════════════════════════════════════════════════════════════════
#
# 背景（任务书 §0）：返修 2 的 P4-1 建立在错误前提上。
# 用户主动收藏本来就能进「我的收藏」（save_word 会调 activate_word_review）；
# 当初看到的「本课 10 词」是打开工作区时自动写入的四六级高亮词，不是用户收藏。
# `sync-collections` 把 v2_lesson_words 全部激活进复习本 → 自动高亮词全变成「收藏」。
# 所以本轮删掉这条多余通道。
#
# 注意：本包只删后端端点。前端调用点（index.html）由包 A 删，
# 返修 2 为它加的测试（tests/test_cet_fix2_p4_favorites.py）由主代理改写为反向断言。


def test_b3_sync_collections_route_is_gone():
    """B3：`vocab.py` 的 router 里不能再有 sync-collections 这条路由。"""
    from webapp.fastapi_routes import vocab as vocab_routes

    paths = {getattr(route, "path", "") for route in vocab_routes.router.routes}
    assert "/api/vocab-review/sync-collections" not in paths, (
        "sync-collections 端点必须已删除（语义错误：会把自动高亮词当成收藏）"
    )


def test_b3_sync_collections_source_is_gone():
    """B3：源码里不能再有该端点的实现（防止被悄悄加回来）。"""
    source = (REPO / "backend" / "webapp" / "fastapi_routes" / "vocab.py").read_text(
        encoding="utf-8"
    )
    assert "def sync_lesson_collections(" not in source, "端点实现必须已删除"
    assert "@router.post(\"/api/vocab-review/sync-collections\")" not in source
    # 只被该端点使用的两个辅助符号也一并清理
    assert "class SyncCollectionsBody" not in source, "SyncCollectionsBody 只被该端点使用，应一并删除"
    assert "def _first_segment_with_word(" not in source, (
        "_first_segment_with_word 只被该端点使用，应一并删除"
    )


def test_b3_sync_collections_returns_404_on_live_app(tmp_path, monkeypatch):
    """B3 验收：`POST /api/vocab-review/sync-collections` 返回 404。"""
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    client = TestClient(create_app())

    resp = client.post("/api/vocab-review/sync-collections", json={})

    assert resp.status_code in (404, 410), (
        f"sync-collections 必须 404（已删除），实际 {resp.status_code}"
    )


def test_b3_activate_endpoint_is_kept():
    """B3 边界：`/api/vocab-review/activate` **必须保留**。

    主代理明确确认：A3 删的是「前端收藏后多余的第二次 activate 调用」，
    不是这个端点本身（`save_word` 内部用的 `db.activate_word_review` 也依赖同一路径）。
    """
    from webapp.fastapi_routes import vocab as vocab_routes

    paths = {getattr(route, "path", "") for route in vocab_routes.router.routes}
    assert "/api/vocab-review/activate" in paths, "activate 端点必须保留（B3 只删 sync-collections）"


def test_b3_frontend_has_no_remaining_call_in_backend_scope():
    """B3：后端范围内无残留调用（前端 index.html 那处由包 A 删）。"""
    # cet_mode.py 的文案键由主代理统一处理（不在包 B 名下），这里排除它
    hits = []
    for path in (REPO / "backend").rglob("*.py"):
        if path.name == "cet_mode.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "sync-collections" in text or "sync_collections" in text:
            hits.append(str(path.relative_to(REPO)))
    assert not hits, f"backend/ 下不应再有 sync-collections 引用，实际命中：{hits}"


# ══════════════════════════════════════════════════════════════════════
# B4 · /workspace/abc 走 cet_404.html
# ══════════════════════════════════════════════════════════════════════
#
# 修前：`pages.py` 的 `lesson_id: int` 注解让 FastAPI 在进函数前就把 `/workspace/abc`
# 拦成 422 JSON（`{"detail":[{"type":"int_parsing",...}]}`），用户看到一段 JSON。
# 修后：注解改 `str` 自己解析；非数字在 CET 模式渲染同一个 `cet_404.html`（404 页），
# 通用模式保持原 422（逐字节比对过 3573e90）。

MISSING_COPY = "材料不存在或已删除"
BACK_LINK_COPY = "返回听力材料"


def _client(tmp_path, monkeypatch):
    import db
    from fastapi.testclient import TestClient
    from fastapi_server import create_app

    monkeypatch.setattr(db, "DB_PATH", tmp_path / "vocab.db")
    return TestClient(create_app()), db


@pytest.mark.parametrize(
    "url",
    [
        "/workspace/abc",
        "/workspace/abc/intensive",
        "/workspace/12x",
        "/workspace/12x/intensive",
    ],
)
def test_b4_non_numeric_id_returns_404_page_in_cet_mode(tmp_path, monkeypatch, url):
    """B4 验收：`/workspace/abc` 与 `/workspace/abc/intensive` 返回 404 页面。"""
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    client, _ = _client(tmp_path, monkeypatch)

    resp = client.get(url)

    assert resp.status_code == 404, f"{url} 必须返回 404 页面，实际 {resp.status_code}"
    assert MISSING_COPY in resp.text, f"{url} 应渲染 cet_404.html 的文案"
    assert BACK_LINK_COPY in resp.text, f"{url} 应有返回入口"
    assert "int_parsing" not in resp.text, f"{url} 不能再返回 422 JSON 错误体"


def test_b4_non_numeric_404_page_has_back_link(tmp_path, monkeypatch):
    """B4：非数字 id 的 404 页与 `/workspace/999` 是同一个页面（含返回链接）。"""
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    client, _ = _client(tmp_path, monkeypatch)

    non_numeric = client.get("/workspace/abc")
    missing_numeric = client.get("/workspace/999999")

    assert non_numeric.status_code == missing_numeric.status_code == 404
    assert 'href="/"' in non_numeric.text
    assert MISSING_COPY in non_numeric.text and MISSING_COPY in missing_numeric.text


def test_b4_existing_lesson_still_renders(tmp_path, monkeypatch):
    """B4 反向保护：存在的材料不能被 404 误伤（数字 id 解析路径没坏）。"""
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    client, db = _client(tmp_path, monkeypatch)
    lesson = db.create_v2_lesson(
        source_type="local", source_url="b4-existing", title="B4 正常材料"
    )

    resp = client.get(f"/workspace/{lesson['id']}")

    assert resp.status_code == 200, f"存在的材料必须 200，实际 {resp.status_code}"
    assert MISSING_COPY not in resp.text


def test_b4_intensive_existing_lesson_still_renders(tmp_path, monkeypatch):
    """B4 反向保护：存在的材料精听页仍 200。"""
    monkeypatch.delenv("ELT_CET_MODE", raising=False)
    client, db = _client(tmp_path, monkeypatch)
    lesson = db.create_v2_lesson(
        source_type="local", source_url="b4-existing-intensive", title="B4 正常材料精听"
    )

    resp = client.get(f"/workspace/{lesson['id']}/intensive")

    assert resp.status_code == 200, f"存在的材料精听页必须 200，实际 {resp.status_code}"


@pytest.mark.parametrize(
    "url",
    [
        "/workspace/abc",
        "/workspace/abc/intensive",
        "/workspace/0x10",
        "/workspace/1e3",
    ],
)
def test_b4_generic_mode_keeps_original_422(tmp_path, monkeypatch, url):
    """B4 通用模式回归：`ELT_CET_MODE=0` 时非数字 id 仍是原版 422（逐字节口径）。

    任务书 §1：「`ELT_CET_MODE=0` 时行为与 3573e90 完全一致」。
    原版对 `/workspace/abc` 返回 422 且 body 是 pydantic 的 int_parsing 错误，
    这里连 `input` 字段的值一起断言，确保不是「另一个 422」。
    """
    monkeypatch.setenv("ELT_CET_MODE", "0")
    client, _ = _client(tmp_path, monkeypatch)

    resp = client.get(url)

    assert resp.status_code == 422, f"通用模式 {url} 必须仍是 422，实际 {resp.status_code}"
    detail = resp.json()["detail"]
    assert detail[0]["type"] == "int_parsing"
    assert detail[0]["loc"] == ["path", "lesson_id"]
    assert detail[0]["input"] == url.split("/")[2], (
        f"通用模式 422 的 input 必须是原始路径片段，实际 {detail[0]['input']!r}"
    )
    assert MISSING_COPY not in resp.text, "通用模式不应出现 CET 模式的 404 文案"


def test_b4_generic_mode_missing_numeric_lesson_stays_200(tmp_path, monkeypatch):
    """B4 通用模式回归：不存在的数字 lesson 仍是原版 200 空页。"""
    monkeypatch.setenv("ELT_CET_MODE", "0")
    client, _ = _client(tmp_path, monkeypatch)

    resp = client.get("/workspace/999999")

    assert resp.status_code == 200, f"通用模式必须保持原行为 200，实际 {resp.status_code}"
    assert MISSING_COPY not in resp.text


def test_b4_generic_mode_accepts_lax_integer_forms(tmp_path, monkeypatch):
    """B4 通用模式：`+1` / `01` / `1.0` / `1_0` 这些宽松整数形式仍按原版解析。

    原版用 pydantic 的 lax int 解析，接受这些形式（实测 3573e90：全部 200/404 而非 422）。
    实现复用了 `TypeAdapter(int)`，本用例锁住这个等价性。
    """
    monkeypatch.setenv("ELT_CET_MODE", "0")
    client, _ = _client(tmp_path, monkeypatch)

    for url in ("/workspace/+1", "/workspace/01", "/workspace/1.0", "/workspace/1_0", "/workspace/-1"):
        resp = client.get(url)
        assert resp.status_code != 422, f"通用模式 {url} 应被原版 lax int 接受，不该 422"
