"""V2 vocabulary highlighting service."""
import html
import json
import re
import sqlite3
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import db
from webapp.services import dicts as dict_service

# Common noise words to exclude from highlighting (articles, pronouns, short function words)
_NOISE_WORDS: set[str] | None = None
_INTERMEDIATE_WORDS: set[str] | None = None
_WORD_MEANINGS: dict[str, str] | None = None
_V2_WORDLIST_INDEX: dict[str, dict] | None = None
_LOOKUP_CACHE: dict[str, dict] = {}
_WORD_MEANING_PLACEHOLDERS = {
    "查询中",
    "查询中...",
    "查询中…",
    "正在查询",
    "正在查询...",
    "正在查询…",
    "暂无释义",
    "查询失败，请稍后重试",
    "本地与在线查询均未命中",
}

_COMPILED_DIR = Path(__file__).resolve().parents[3] / "resources" / "wordlists" / "wordlists" / "compiled"
WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*")

# ── B1：词形还原（返修 2.1）────────────────────────────────────────────
# ECDICT 的 `exchange` 字段形如 `0:get/1:i`：
#   `0:<lemma>`  —— 该词的词元（原形）
#   `1:<marker>` —— 该词自身属于哪种屈折形式
# 只有**屈折**标记才做还原；比较级 `r` / 最高级 `t` 不还原
# （`better` 的 exchange 是 `0:good/1:r`，`1:r` 是比较级不是屈折变形，
#  且 `better` 本身是独立词条，任务书 B1 允许「保持原样」，这里选保持原样）。
_INFLECTION_MARKERS = frozenset({"p", "i", "d", "s", "3", "s3", "3s", "pd", "dp"})
_EXCHANGE_LEMMA_CACHE: dict[str, str] = {}
_EXISTS_CACHE: dict[str, bool] = {}
_QUALITY_CACHE: dict[str, bool] = {}
_EXCHANGE_CONN = None
_EXCHANGE_LOCK = threading.Lock()
_ECDICT_PATH = Path(__file__).resolve().parents[3] / "resources" / "ecdict" / "ecdict.db"

_COMMON_MEANINGS = {
    "randomly": "随机地；任意地",
    "a": "一个；某一",
    "an": "一个；某一",
    "and": "和；并且",
    "are": "是；在",
    "as": "作为；像；当...时",
    "be": "是；成为",
    "became": "变成；成为",
    "become": "成为；变得",
    "better": "更好的；更好地",
    "can": "能够；可以",
    "complex": "复杂的；复合的",
    "content": "内容",
    "do": "做；进行",
    "for": "为了；给",
    "from": "来自；从",
    "have": "有；已经",
    "help": "帮助",
    "how": "如何；怎样",
    "in": "在...里",
    "into": "进入；变成",
    "is": "是",
    "it": "它",
    "learn": "学习",
    "method": "方法",
    "not": "不",
    "of": "...的",
    "on": "在...上；关于",
    "or": "或者",
    "practical": "实用的；实际的",
    "read": "阅读；读",
    "remember": "记住；记得",
    "sentence": "句子",
    "system": "系统",
    "that": "那；那个；引导从句",
    "the": "这个；那个",
    "this": "这；这个",
    "to": "到；向；用于不定式",
    "use": "使用",
    "what": "什么；所...的事",
    "with": "和；带有",
    "word": "单词；词",
    "you": "你；你们",
}


def _get_noise_words() -> set[str]:
    global _NOISE_WORDS
    if _NOISE_WORDS is None:
        _NOISE_WORDS = {
            "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
            "have", "has", "had", "do", "does", "did", "will", "would", "shall",
            "should", "may", "might", "can", "could", "must", "i", "you", "he",
            "she", "it", "we", "they", "me", "him", "her", "us", "them", "my",
            "your", "his", "its", "our", "their", "mine", "yours", "hers", "ours",
            "theirs", "this", "that", "these", "those", "am", "not", "no", "to",
            "of", "in", "for", "on", "with", "at", "by", "from", "as", "into",
            "about", "like", "after", "between", "through", "over", "before",
            "under", "again", "further", "then", "once", "here", "there", "when",
            "where", "why", "how", "all", "both", "each", "few", "more", "most",
            "other", "some", "such", "only", "own", "same", "so", "than", "too",
            "very", "and", "but", "or", "nor", "just", "because", "if", "while",
            "up", "out", "off", "down", "also", "even", "still", "well", "now",
        }
    return _NOISE_WORDS


def tokenize_for_lookup(text: str) -> list[str]:
    raw_words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", text)
    noise = _get_noise_words()
    seen: set[str] = set()
    result: list[str] = []
    for w in raw_words:
        lower = w.lower()
        if len(lower) <= 2 or lower in noise or w[0].isupper():
            continue
        if lower not in seen:
            seen.add(lower)
            result.append(lower)
    return result


def load_default_intermediate_words() -> set[str]:
    global _INTERMEDIATE_WORDS
    if _INTERMEDIATE_WORDS is not None:
        return _INTERMEDIATE_WORDS

    # 默认高亮中频词：优先用户上传的 BNC 4k-6k，其次内置 COCA 2001-5000，再退牛津3000。
    candidates = ["user_bnc_4k_6k.json", "builtin_coca_5000.json", "builtin_oxford3000.json"]
    _INTERMEDIATE_WORDS = set()
    for filename in candidates:
        path = _COMPILED_DIR / filename
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            _INTERMEDIATE_WORDS = {w.lower() for w in data.get("words", [])}
            break
        except Exception:
            continue
    return _INTERMEDIATE_WORDS


def _load_compiled_word_set(filename: str) -> set[str]:
    path = _COMPILED_DIR / filename
    if filename.startswith("user_"):
        # 多用户模式下上传词表按用户隔离存储
        try:
            from webapp.storage import wordlists as _wl
            path = _wl.resolve_compiled_path(filename[:-5] if filename.endswith(".json") else filename)
        except Exception:
            pass
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    words = data.get("words") if isinstance(data, dict) else data
    if isinstance(words, dict):
        return {str(word).lower() for word in words}
    if isinstance(words, list):
        return {str(word).lower() for word in words if isinstance(word, str)}
    return set()


def load_lists_for_keys(keys: list[str]) -> list[tuple[str, set[str]]]:
    """按给定顺序返回 (词表 key, 词集)，顺序即高亮取色优先级。"""
    lists: list[tuple[str, set[str]]] = []
    for key in keys:
        if key == "my_vocab":
            try:
                lists.append((key, {str(w).lower() for w in db.get_review_word_set()}))
            except Exception:
                lists.append((key, set()))
            continue
        if key == "my_mastered":
            continue  # 已掌握是 exclude 语义，不作为高亮源
        lists.append((key, _load_compiled_word_set(f"{key}.json")))
    return lists


def load_words_for_list_keys(keys: list[str]) -> set[str]:
    """用户所选词表的词集并集（compiled user_*/builtin_* + 虚拟词表 my_vocab）。"""
    words: set[str] = set()
    for _, word_set in load_lists_for_keys(keys):
        words |= word_set
    return words


def load_exclude_words() -> set[str]:
    """所有 exclude 类型 compiled 词表的词集并集（基础词过滤，始终生效）。"""
    words: set[str] = set()
    if not _COMPILED_DIR.exists():
        return words
    for path in sorted(_COMPILED_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("metadata") if isinstance(data, dict) else None
        if not isinstance(meta, dict) or meta.get("type") != "exclude":
            continue
        words |= {str(w).lower() for w in data.get("words", []) if isinstance(w, str)}
    return words


def load_v2_wordlist_index() -> dict[str, dict]:
    global _V2_WORDLIST_INDEX
    if _V2_WORDLIST_INDEX is not None:
        return _V2_WORDLIST_INDEX

    words: dict[str, dict] = {}
    for word in load_default_intermediate_words():
        words[word] = {"word": word, "level": "ielts"}
    _V2_WORDLIST_INDEX = words
    return _V2_WORDLIST_INDEX


def clear_vocab_caches() -> None:
    global _INTERMEDIATE_WORDS, _WORD_MEANINGS, _V2_WORDLIST_INDEX
    _INTERMEDIATE_WORDS = None
    _WORD_MEANINGS = None
    _V2_WORDLIST_INDEX = None
    _LOOKUP_CACHE.clear()


def _meaning_from_analysis(analysis: Any) -> str:
    if not isinstance(analysis, dict):
        return ""
    basic = str(analysis.get("basic_meaning") or "").strip()
    if basic:
        return basic
    vocab = analysis.get("vocabulary")
    if isinstance(vocab, list):
        for item in vocab:
            if isinstance(item, dict):
                meaning = str(item.get("meaning") or "").strip()
                if meaning:
                    return meaning
    return ""


def load_word_meanings() -> dict[str, str]:
    global _WORD_MEANINGS
    if _WORD_MEANINGS is not None:
        return _WORD_MEANINGS

    meanings: dict[str, str] = {}
    for path in sorted(_COMPILED_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        words = data.get("words") if isinstance(data, dict) else data
        if isinstance(words, dict):
            for word, payload in words.items():
                meaning = ""
                if isinstance(payload, dict):
                    meaning = str(payload.get("basic_meaning") or payload.get("meaning") or "").strip()
                elif isinstance(payload, str):
                    meaning = payload.strip()
                if meaning and not meanings.get(word.lower()):
                    meanings[word.lower()] = meaning
        elif isinstance(words, list):
            for word in words:
                if isinstance(word, str):
                    meanings.setdefault(word.lower(), "")

    try:
        for word, entry in db.get_all_words().items():
            meaning = _meaning_from_analysis(entry.get("cached_analysis"))
            if meaning:
                meanings[word.lower()] = meaning
    except Exception:
        pass

    _WORD_MEANINGS = meanings
    return _WORD_MEANINGS


def remember_word_meaning(word: str, meaning: str) -> None:
    normalized = re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")
    if not normalized:
        return
    meanings = load_word_meanings()
    if meaning and not is_word_meaning_placeholder(meaning):
        meanings[normalized] = meaning
    for key in (f"{normalized}:0", f"{normalized}:1"):
        _LOOKUP_CACHE.pop(key, None)


def is_word_meaning_placeholder(meaning: str) -> bool:
    return re.sub(r"\s+", "", str(meaning or "")) in _WORD_MEANING_PLACEHOLDERS


def forget_word_meaning_cache(word: str) -> None:
    normalized = re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")
    if not normalized:
        return
    for key in (f"{normalized}:0", f"{normalized}:1"):
        _LOOKUP_CACHE.pop(key, None)


def _get_ipa(word: str) -> str:
    try:
        from analyzer import _to_canonical_ipa
        ipa = _to_canonical_ipa(word)
        cleaned = ipa.rstrip("*").strip() if ipa else ""
        if re.sub(r"[^a-z]", "", cleaned.lower()) == re.sub(r"[^a-z]", "", word.lower()):
            return ""
        return cleaned
    except Exception:
        return ""


def lookup_word_meaning(word: str, *, allow_external_fallback: bool = False) -> dict:
    surface = re.sub(r"[^a-zA-Z'-]", "", word or "").lower().strip("'-")
    normalized = surface.replace("-", "")
    if not normalized:
        return {"word": "", "meaning": "", "phonetic": "", "found": False}
    full_external_fallback = bool(
        allow_external_fallback and _external_fallback_billing_allowed()
    )
    fallback_scope = (
        "full" if full_external_fallback else "free" if allow_external_fallback else "local"
    )
    cache_key = f"{normalized}:{fallback_scope}"
    if cache_key in _LOOKUP_CACHE:
        return dict(_LOOKUP_CACHE[cache_key])
    meanings = load_word_meanings()
    # B1：候选按「还原后的词元优先」排序。只改 lemma 不改候选顺序的话，
    # `getting` 的释义仍会先命中 `gett` 的「欧洲轮机技术制造商集团」——B1 的可见症状就是这个。
    candidates = _ordered_candidates(_lookup_candidates(surface))
    for candidate in candidates:
        for meaning in (meanings.get(candidate, ""), _COMMON_MEANINGS.get(candidate, "")):
            if meaning and not is_word_meaning_placeholder(meaning) and _concise_gloss(meaning):
                phonetic = _get_ipa(normalized)
                result = {
                    "word": normalized,
                    # B1：报出去的 lemma 一律走还原，不能直接用命中释义的那个候选。
                    # 反例（实测）：`meanings` 是模块级可变缓存，`getting` 一旦被查过就会
                    # 被写进 `meanings["getting"]`；下次再查时若 `get` 在该缓存里没有释义，
                    # 循环会落到 `getting` 并把它当 lemma 返回 —— 修好的 B1 会这样"复发"。
                    "lemma": _resolve_lemma(candidate) or candidate,
                    "meaning": meaning,
                    "phonetic": phonetic,
                    "found": True,
                    "source": "compiled",
                }
                _LOOKUP_CACHE[cache_key] = result
                return dict(result)
    dict_meaning = _lookup_local_dict_meaning(candidates)
    local_result = None
    if dict_meaning:
        concise_meaning = _concise_gloss(dict_meaning, limit=20) or dict_meaning
        local_result = {
            "word": normalized,
            "lemma": _preferred_lookup_lemma(candidates),
            "meaning": concise_meaning,
            "phonetic": _get_ipa(normalized),
            "found": True,
            "source": "local",
        }
        if not allow_external_fallback or _concise_gloss(concise_meaning, limit=20):
            meanings[normalized] = concise_meaning
            _LOOKUP_CACHE[cache_key] = local_result
            return dict(local_result)
    if not allow_external_fallback:
        phonetic = _get_ipa(normalized)
        result = {"word": normalized, "lemma": normalized, "meaning": "", "phonetic": phonetic, "found": False}
        _LOOKUP_CACHE[cache_key] = result
        return dict(result)
    # 计费激活且无父 operation 时仍允许免费的 MyMemory 查询，但不静默调用 AI。
    # 用户主动查词因此能兜底；建课等父 operation 内仍使用完整 AI + 免费翻译链。
    fallback = (
        _translate_word_fallback(surface)
        if full_external_fallback
        else _translate_word_free_fallback(surface)
    )
    if fallback:
        meanings[normalized] = fallback
        result = {
            "word": normalized,
            "lemma": _preferred_lookup_lemma(candidates),
            "meaning": fallback,
            "phonetic": _get_ipa(normalized),
            "found": True,
            "source": "external",
        }
        _LOOKUP_CACHE[cache_key] = result
        return dict(result)
    if local_result:
        meanings[normalized] = local_result["meaning"]
        _LOOKUP_CACHE[cache_key] = local_result
        return dict(local_result)
    result = {"word": normalized, "lemma": normalized, "meaning": "", "found": False}
    _LOOKUP_CACHE[cache_key] = result
    return dict(result)


def _lookup_local_dict_meaning(candidates: list[str]) -> str:
    english_fallback = ""
    for candidate in candidates:
        meaning = str(dict_service.lookup_ecdict(candidate) or "").strip()
        if meaning:
            summary = _summarize_local_dict_meaning(meaning)
            if _concise_gloss(summary):
                return summary
            if summary and not english_fallback:
                english_fallback = summary
    return english_fallback


def _summarize_local_dict_meaning(raw: str) -> str:
    text = html.unescape(str(raw or ""))
    text = re.sub(r"@@@LINK=[^\s]+", " ", text)
    text = re.sub(r"/[^/]{1,80}/", " ", text)
    text = re.sub(r"\b(?:BrE|NAmE|especially|synonym|opposite)\b", " ", text, flags=re.I)
    text = re.sub(r"\s+", " ", text).strip(" ;,")
    if not text:
        return ""

    zh_parts = []
    zh_pattern = r"[\u4e00-\u9fff][\u4e00-\u9fff，；、（）()的地得之一二三四五六七八九十0-9-]{1,80}"
    for match in re.finditer(zh_pattern, text):
        part = match.group(0).strip(" ，,；;、")
        if len(part) <= 4 and part.endswith("等"):
            continue
        if part and part not in zh_parts:
            zh_parts.append(part)
        if len(zh_parts) >= 2:
            break

    english_source = re.sub(zh_pattern, " ", text)
    chunks = [
        chunk.strip(" -;:,.")
        for chunk in re.split(r"(?<=[.!?])\s+|\s+[0-9]+\s+", english_source)
    ]
    en_part = ""
    for chunk in chunks:
        if not chunk or len(chunk) < 18:
            continue
        if not re.search(r"[A-Za-z]", chunk):
            continue
        if re.search(r"\b(?:word origin|example bank|see also|grammar point)\b", chunk, flags=re.I):
            continue
        en_part = chunk[:180].strip(" -;:,.")
        break

    parts = []
    if zh_parts:
        parts.append("中文：" + "；".join(zh_parts))
    if en_part:
        parts.append("英文：" + en_part)
    if parts:
        return "\n".join(parts)[:360]
    return text[:260]


def _exchange_lemma(word: str) -> str:
    """B1：读 ECDICT 的 `exchange` 字段，返回**权威屈折词元**；不适用时返回 ""。

    `exchange` 形如 `0:get/1:i`：
      - `0:<lemma>`  该词的词元（原形）
      - `1:<marker>` 该词自身属于哪种屈折形式
    只有 `1:` 标记属于屈折（p/i/d/s/3/s3/pd/dp…）时才认 `0:` 那个词元。
    `1:r`（比较级，如 `better` → `0:good/1:r`）与 `1:t`（最高级）**不还原**：
    它们不是屈折变形，且 better/best 本身就是独立词条（任务书 B1 允许保持原样）。
    """
    key = (word or "").strip().lower()
    if not key:
        return ""
    if key in _EXCHANGE_LEMMA_CACHE:
        return _EXCHANGE_LEMMA_CACHE[key]
    global _EXCHANGE_CONN
    lemma = ""
    try:
        if _EXCHANGE_CONN is None and _ECDICT_PATH.exists():
            with _EXCHANGE_LOCK:
                if _EXCHANGE_CONN is None:
                    _EXCHANGE_CONN = sqlite3.connect(
                        f"file:{_ECDICT_PATH}?mode=ro", uri=True, check_same_thread=False
                    )
        if _EXCHANGE_CONN is not None:
            with _EXCHANGE_LOCK:
                row = _EXCHANGE_CONN.execute(
                    "SELECT exchange FROM words WHERE word = ?", (key,)
                ).fetchone()
            if row and row[0]:
                base = ""
                markers: set[str] = set()
                for part in str(row[0]).split("/"):
                    part = part.strip()
                    if part.startswith("0:"):
                        base = part[2:].strip().lower()
                    elif part.startswith("1:"):
                        markers.add(part[2:].strip().lower())
                # 只有「纯屈折」标记才认这个词元；带 r/t（比较级/最高级）不还原
                if base and base != key and markers and markers <= _INFLECTION_MARKERS:
                    lemma = base
    except Exception:
        lemma = ""
    _EXCHANGE_LEMMA_CACHE[key] = lemma
    return lemma


def _word_exists_in_dict(word: str) -> bool:
    """B1 校验层：词在词典里是否查得到（ECDICT 优先，其次随库分发的 compact 释义库）。"""
    key = (word or "").strip().lower()
    if not key:
        return False
    cached = _EXISTS_CACHE.get(key)
    if cached is not None:
        return cached
    exists = False
    try:
        exists = bool(dict_service.word_in_dict(key))
    except Exception:
        exists = False
    if not exists:
        try:
            exists = bool(str(dict_service.lookup_ecdict(key) or "").strip())
        except Exception:
            exists = False
    _EXISTS_CACHE[key] = exists
    return exists


# ECDICT 里存在大量「形似还原词、实为缩写/人名/专业词」的条目，例如
#   gett → abbr. 欧洲轮机技术制造商集团      playe → [医]翻放机, 唱机
#   walke → [人名] 沃克                      carrie → n. 卡丽（女名…）
#   tri  → [计] 三                           mak/hav → abbr. …
# 只靠「在词典里查得到」会把 played→playe、getting→gett。这里额外做条目质量判定。
_QUALITY_POS_RE = re.compile(
    r"(?:^|[\s;,])(?:n|v|vt|vi|a|ad|adv|adj|prep|conj|pron|num|int|art|aux|pl|abbr)\."
)
_NAME_HINTS = ("人名", "女名", "男名", "姓氏", "地名")


def _is_quality_lemma_entry(word: str) -> bool:
    """B1 质量层：候选词条是否是「正常词条」（不是缩写 / 人名 / 纯专业词标签）。

    判定依据只有一条：ECDICT 的 translation 里出现常见词性标记，且不是 `abbr.` 开头。
    这层不追求完美，只挡掉 `gett`/`playe`/`walke`/`carrie`/`tri` 这类会污染收藏本的条目。
    """
    key = (word or "").strip().lower()
    if not key:
        return False
    cached = _QUALITY_CACHE.get(key)
    if cached is not None:
        return cached
    ok = False
    try:
        raw = str(dict_service.lookup_ecdict(key) or "")
    except Exception:
        raw = ""
    if raw:
        # lookup_ecdict 的返回形如 "/phonetic/ [pos] definition translation"
        text = re.sub(r"^/[^/]*/\s*", "", raw)
        text = re.sub(r"^\[[^\]]*\]\s*", "", text)
        head = text[:120]
        lowered = head.lower()
        is_abbr = lowered.startswith("abbr") or "[abbr" in lowered[:16]
        has_pos = bool(_QUALITY_POS_RE.search(head))
        is_name = any(hint in head[:40] for hint in _NAME_HINTS)
        ok = bool(has_pos and not is_abbr and not is_name)
    _QUALITY_CACHE[key] = ok
    return ok


def _rule_lemma_candidates(word: str) -> list[str]:
    """B1 第二层：规则剥离候选（保持 `_lookup_candidates` 的顺序，交给存在性+质量双层筛选）。"""
    return _lookup_candidates(word)


def _resolve_lemma(word: str) -> str:
    """B1：三层还原（exchange 优先 → 规则兜底 → 存在性/质量校验），全失败回退原词。

    任务书 B1 明确：「还原出来的词在 ECDICT 里查不到时，回退成原词，不要造出一个不存在的词」。

    关键约束：exchange 给出的词元**必须也能被常规剥离规则产出**才会被采用。
    这样 `getting→get`（规则能产 `get`）成立，而 `went→go`、`children→child`、`was→be`
    这类不规则变化不会被改写（规则产不出 `go`/`child`/`be`），保持 3573e90 的原行为。
    """
    surface = re.sub(r"[^a-zA-Z'-]", "", word or "").lower().strip("'-")
    if not surface:
        return ""
    normalized = surface.replace("-", "")
    candidates = _rule_lemma_candidates(surface)
    candidate_set = set(candidates)

    # 第一层：ECDICT exchange 字段（权威屈折词元），但必须同时满足
    #   ① 是常规规则能产出的形式（排除不规则变化，保住 went/children/was 原样）
    #   ② 词典里查得到  ③ 不是缩写/人名条目
    for probe in (surface, normalized):
        lemma = _exchange_lemma(probe)
        if (
            lemma
            and lemma in candidate_set
            and _word_exists_in_dict(lemma)
            and _is_quality_lemma_entry(lemma)
        ):
            return lemma

    # 第二层：规则剥离。按候选顺序找第一个「真实存在于词典且条目正常」的还原形式。
    # 顺序即优先级：`_lookup_candidates` 把 base 放在 base+"e" 之前，
    # 所以 played→play 会先于 playe 命中；+e 形式只作为最后兜底。
    for candidate in candidates:
        if candidate == surface:
            continue
        if _word_exists_in_dict(candidate) and _is_quality_lemma_entry(candidate):
            return candidate

    # 第三层：全都还原不出真词 → 回退原词（不造不存在的词）
    return surface


def _ordered_candidates(candidates: list[str]) -> list[str]:
    """B1：把还原后的词元提到候选首位，让释义也取自正确的词。

    不这么做的话，`getting` 的释义仍会先命中 `gett` 的「欧洲轮机技术制造商集团」——
    B1 的可见症状就是这个释义，只改 lemma 不改释义顺序等于没修好。
    """
    if not candidates:
        return []
    lemma = _resolve_lemma(candidates[0])
    if not lemma or lemma == candidates[0]:
        return list(candidates)
    ordered = [lemma]
    ordered.extend(item for item in candidates if item != lemma)
    return ordered


def _lookup_candidates(word: str) -> list[str]:
    candidates = [word]
    if word.endswith("'s") and len(word) > 3:
        candidates.append(word[:-2])
    if word.endswith("ies") and len(word) > 4:
        candidates.append(word[:-3] + "y")
    if word.endswith("ied") and len(word) > 4:
        candidates.append(word[:-3] + "y")
    if word.endswith("ing") and len(word) > 5:
        base = word[:-3]
        candidates.append(base)
        if len(base) > 1 and base[-1] == base[-2]:
            candidates.append(base[:-1])
        candidates.append(base + "e")
    if word.endswith("ed") and len(word) > 4:
        base = word[:-2]
        candidates.append(base)
        if len(base) > 1 and base[-1] == base[-2]:
            candidates.append(base[:-1])
        candidates.append(base + "e")
    if word.endswith("es") and len(word) > 4:
        candidates.append(word[:-2])
    if word.endswith("s") and len(word) > 3:
        candidates.append(word[:-1])

    seen = set()
    result = []
    for candidate in [*candidates, *(item.replace("-", "") for item in candidates)]:
        if candidate and candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _preferred_lookup_lemma(candidates: list[str]) -> str:
    """B1：返回候选里**真实存在**的还原词元；都还原不出就回退原词。

    原实现 `return candidates[-1]` 直接取最后一个候选 —— 而 `_lookup_candidates` 把
    `base+"e"` 无条件追加在最后，于是 `getting → gette`、`displaying → displaye`。
    改法：交给 `_resolve_lemma` 走「exchange → 规则 → 存在性/质量校验」，全失败回退原词。
    """
    if not candidates:
        return ""
    # 带连字符的复合词（non-negotiables）保持原逻辑：连字符形式优先
    for candidate in reversed(candidates):
        if "-" in candidate:
            return candidate
    return _resolve_lemma(candidates[0]) or candidates[0]


def _normalize_reading_word(word: str) -> str:
    normalized = re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")
    candidates = _lookup_candidates(normalized)
    return candidates[0] if candidates else normalized


def _reading_highlight_match(word: str, word_index: dict[str, dict]) -> tuple[str, dict] | None:
    normalized = re.sub(r"[^a-zA-Z']", "", word or "").lower().strip("'")
    if len(normalized) <= 2 or normalized in _get_noise_words():
        return None
    for candidate in _lookup_candidates(normalized):
        meta = word_index.get(candidate)
        if meta:
            return candidate, meta
    return None


def highlight_reading_blocks(
    blocks: list[dict],
    hidden_words: set[str] | None = None,
    *,
    include_meanings: bool = True,
    source_words: set[str] | None = None,
    source_lists: list[tuple[str, set[str]]] | None = None,
) -> dict:
    if source_words is not None:
        word_index = {w: {"word": w, "level": "selected"} for w in source_words}
    else:
        word_index = load_v2_wordlist_index()
    hidden = {word.lower() for word in (hidden_words or set())}
    meanings = load_word_meanings() if include_meanings else {}
    gloss_cache: dict[str, str] = {}
    result: list[dict] = []
    candidate_count = 0
    for block in blocks:
        text = str(block.get("text", ""))
        highlights: list[dict] = []
        for match in WORD_RE.finditer(text):
            word = match.group(0)
            highlighted = _reading_highlight_match(word, word_index)
            if not highlighted:
                continue
            matched_word, meta = highlighted
            surface = re.sub(r"[^a-zA-Z']", "", word).lower().strip("'")
            if surface in hidden or matched_word in hidden:
                continue
            if matched_word not in gloss_cache:
                if not include_meanings:
                    gloss_cache[matched_word] = ""
                else:
                    candidates = _lookup_candidates(matched_word)
                    gloss = ""
                    for candidate in candidates:
                        for meaning in (meanings.get(candidate, ""), _COMMON_MEANINGS.get(candidate, "")):
                            gloss = _concise_gloss(meaning)
                            if gloss:
                                break
                        if gloss:
                            break
                    if not gloss:
                        gloss = _concise_gloss(_lookup_local_dict_meaning(candidates))
                    gloss_cache[matched_word] = gloss
            item = {
                "word": word,
                "normalized": matched_word,
                "level": meta.get("level", "ielts"),
                "meaning": gloss_cache[matched_word],
                "start": match.start(),
                "end": match.end(),
            }
            if source_lists is not None:
                # 记录最高优先级来源词表，前端据此着色
                item["list_key"] = next(
                    (
                        key
                        for key, list_words in source_lists
                        if matched_word in list_words
                    ),
                    "",
                )
            highlights.append(item)
        candidate_count += len(highlights)
        block_copy = dict(block)
        block_copy["highlights"] = highlights
        result.append(block_copy)
    return {"blocks": result, "candidate_count": candidate_count}


def _concise_gloss(meaning: str, limit: int = 14) -> str:
    """词下注释只需要短释义：去掉词典长文中的英文段、前缀，按分号截取前几个义项。"""
    text = str(meaning or "").strip()
    if not text:
        return ""
    text = text.split("英文：", 1)[0]
    text = text.removeprefix("中文：").strip()
    text = text.split("\n", 1)[0].strip()
    text = re.sub(r"\s+[A-Za-z][A-Za-z\s'()./=-]*.*$", "", text).strip()
    if "（" not in text and "(" not in text:
        text = text.replace("）", "").replace(")", "")
    senses = []
    for sense in re.split(r"[；;]", text):
        sense = sense.strip()
        if not sense:
            continue
        is_example = bool(re.search(r"[。！？!?]\s*$", sense))
        is_example = is_example or (
            len(sense) >= 9
            and (
                re.search(r"(?:是由|需要|已经|正在|应该|好像|似乎|我们|他们|我的|你的|他的)", sense)
                or sense.endswith(("的", "了"))
            )
        )
        if not is_example:
            senses.append(sense)
    if not senses:
        return ""
    text = "；".join(senses)
    if len(text) <= limit and len(senses) <= 3:
        return text
    out = ""
    for sense in senses[:3]:
        candidate = f"{out}；{sense}" if out else sense
        if len(candidate) > limit:
            break
        out = candidate
    if out:
        return out
    return text[: limit - 1] + "…"


def _translate_word_free_fallback(word: str) -> str:
    if len(word) <= 2:
        return ""
    query = urllib.parse.urlencode({"q": word, "langpair": "en|zh-CN"})
    url = f"https://api.mymemory.translated.net/get?{query}"
    translated = ""
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except Exception:
        data = {}
    translated = str(data.get("responseData", {}).get("translatedText") or "").strip()
    if translated and translated.lower() != word.lower() and re.search(r"[\u4e00-\u9fff]", translated):
        return translated
    return ""


def _translate_word_fallback(word: str) -> str:
    if len(word) <= 2:
        return ""
    ai_meaning = _ai_word_gloss_fallback(word)
    if ai_meaning:
        return ai_meaning
    translated = _translate_word_free_fallback(word)
    if translated:
        return translated
    return _ai_word_gloss_fallback(word)


def _external_fallback_billing_allowed() -> bool:
    """外部词义 fallback（翻译 API + AI 补义）是否允许触发。

    单用户/公开库/积分 off：允许（本来就不计费）。
    计费激活时：只有处于父 bundle operation 上下文（建课等）才允许——
    页面加载、被动轮询、保存流程里的 fallback 绝不静默触发可计费 AI。"""
    from webapp.runtime import credit_meter

    if not credit_meter.billing_active():
        return True
    return credit_meter.current_operation() is not None


def _ai_word_gloss_fallback(word: str) -> str:
    try:
        from prompts import WORD_GLOSS_FALLBACK_PROMPT
        from webapp.runtime import ai_config

        if not ai_config.AI_API_KEY:
            return ""
        response = ai_config.client.chat.completions.create(
            model=ai_config.AI_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": WORD_GLOSS_FALLBACK_PROMPT.format(word=word),
                }
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
            max_tokens=80,
            extra_body={"thinking": {"type": "disabled"}},
        )
        payload = json.loads(response.choices[0].message.content or "{}")
        meaning = str(payload.get("meaning") or "").strip()
    except Exception:
        return ""
    if not re.search(r"[\u4e00-\u9fff]", meaning):
        return ""
    return meaning[:80]


def should_highlight_token(token: str) -> bool:
    words = load_default_intermediate_words()
    return token.lower() in words


def highlight_segments(
    segments: list[dict],
    hidden_words: set[str] | None = None,
    *,
    include_meanings: bool = True,
    source_words: set[str] | None = None,
    source_lists: list[tuple[str, set[str]]] | None = None,
) -> list[dict]:
    intermediate = source_words if source_words is not None else load_default_intermediate_words()
    meanings = load_word_meanings() if include_meanings else {}
    noise = _get_noise_words()
    hidden = {word.lower() for word in (hidden_words or set())}
    result: list[dict] = []
    for seg in segments:
        text = seg.get("text", "")
        words = re.findall(r"[a-zA-Z]+(?:'[a-zA-Z]+)?", text.lower())
        highlighted: list[str] = []
        seen: set[str] = set()
        for w in words:
            if len(w) <= 2 or w in noise:
                continue
            if w in hidden:
                continue
            if w in intermediate and w not in seen:
                seen.add(w)
                highlighted.append(w)
        seg_copy = dict(seg)
        seg_copy["highlighted_words"] = highlighted
        seg_copy["word_meanings"] = {w: meanings.get(w, "") for w in highlighted if meanings.get(w, "")}
        if source_lists is not None:
            # 每个高亮词记录其最高优先级来源词表，前端据此着色
            seg_copy["highlighted_word_lists"] = {
                w: next((key for key, list_words in source_lists if w in list_words), "")
                for w in highlighted
            }
        result.append(seg_copy)
    return result
