"""受控模型 fixture：**必须能通过生产校验器**的模型响应。

为什么单独成文件：
    D02 的核心教训是「fixture 非法 → 受控成功路径从未跑通」。
    上一版把所有句子固定返回两个跨句短语
    （`displaying signs of weariness` / `call it a day`），
    而 `cet_ai._validate_phrase()` 要求每个短语是**当前原句的连续子串**，
    于是六级材料 32 句全部 `AI_INVALID_OUTPUT`，
    所谓「受控模型 33/39 与无 key 一致」只是把一种失败换成了另一种失败。

本模块的 fixture 一律**从输入原句派生**，因此天然满足：

    explain   keyPhrases 每个都是原句连续子串且 ≤8 词（或返回空数组）
    chunk     2–4 段、每段 ≤8 词、按顺序完整覆盖原句、不改写
    transfer  options 为字符串数组、correctAnswer 为字母

同时提供 `assert_valid_*()` 供脚本**主动断言**「确实成功」，
而不是只看整套测试通过数——那正是上一版掩盖问题的方式。
"""
from __future__ import annotations

import re
from typing import Any

# 与生产校验器 `cet_ai._WORD_RE` 保持一致，否则词数判定会出现偏差。
_WORD_RE = re.compile(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)*")

MAX_PHRASE_WORDS = 8
MIN_CHUNKS = 2
MAX_CHUNKS = 4

CONTROLLED_EXPLANATION = "受控解释：本句在谈工作进度，先抓主干再看修饰。"


def word_count(text: str) -> int:
    """与生产校验器同一口径的词数。"""
    return len(_WORD_RE.findall(text))


def extract_phrase(sentence: str, words: int = 3) -> str:
    """从原句取前 N 个词作为连续子串。

    必须是连续子串，否则生产校验器 `text not in sentence` 直接拒绝。
    """
    matches = list(_WORD_RE.finditer(sentence))
    if not matches:
        return ""
    take = matches[:words]
    return sentence[take[0].start():take[-1].end()]


def build_key_phrases(sentence: str) -> list[str]:
    """explain 的 keyPhrases：从原句派生，保证合法。

    单句只有 1 个词时返回空数组 —— 契约明确允许空数组，
    这比伪造一个跨句短语（上一版的错误）诚实。
    """
    if word_count(sentence) < 2:
        return []
    phrase = extract_phrase(sentence, words=min(3, word_count(sentence)))
    return [phrase] if phrase and phrase in sentence and word_count(phrase) <= MAX_PHRASE_WORDS else []


def build_chunks(sentence: str) -> list[str] | None:
    """chunk 的合法分段：2–4 段、每段 ≤8 词、完整覆盖、不改写。

    返回 None 表示**该句在生产约束下无法合法分段**（例如 `"What?"` 只有 1 个词，
    而校验器要求 2–4 段、每段 ≤8 词 —— 任何切法都会得到 1 段）。
    调用方必须据此抛 `AI_INVALID_OUTPUT`，而不是伪造一个非法响应，
    否则又会回到「fixture 非法、成功路径没跑通」的老问题。
    """
    tokens = [(m.start(), m.end()) for m in re.finditer(r"\S+", sentence)]
    if not tokens:
        return None

    # 贪心按词数上限切分
    groups: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    current_words = 0
    for start, end in tokens:
        words = word_count(sentence[start:end])
        if current and current_words + words > MAX_PHRASE_WORDS:
            groups.append(current)
            current, current_words = [], 0
        current.append((start, end))
        current_words += words
    if current:
        groups.append(current)

    # 只切出 1 段时，按 token 对半分成 2 段（仍是连续子串）
    if len(groups) == 1:
        only = groups[0]
        if len(only) < 2:
            return None
        middle = max(1, len(only) // 2)
        groups = [only[:middle], only[middle:]]

    if not MIN_CHUNKS <= len(groups) <= MAX_CHUNKS:
        return None

    chunks = [sentence[g[0][0]:g[-1][1]] for g in groups]
    # 自检：完整覆盖、无遗漏、每段 ≤8 词
    if "".join(chunks) != sentence and not _covers_in_order(chunks, sentence):
        return None
    if any(word_count(chunk) > MAX_PHRASE_WORDS for chunk in chunks):
        return None
    return chunks


def _covers_in_order(chunks: list[str], sentence: str) -> bool:
    """按顺序拼接后除空白外应等于原句（原句可能有多空格）。"""
    return re.sub(r"\s+", "", "".join(chunks)) == re.sub(r"\s+", "", sentence)


def build_transfer(candidate: dict[str, Any] | None) -> dict[str, Any]:
    """transfer：options 为字符串数组、correctAnswer 为字母。"""
    return {
        "question": "受控换句题：这句表达了什么？",
        "options": ["受控选项甲", "受控选项乙"],
        "correctAnswer": "A",
        "explanation": "受控解释：与原句意思最接近的是甲。",
    }


# ────────────────────── 主动断言「确实成功」 ──────────────────────


class FixtureAssertionError(AssertionError):
    """fixture 未通过生产校验规则。"""


def assert_valid_explanation(payload: dict[str, Any], sentence: str) -> None:
    """断言 explain 响应**确实成功**，而不是「没报错」。

    D02 的要求原文：必须检查 `explanation` 非空且 `keyPhrases` 合法。
    """
    if not isinstance(payload, dict):
        raise FixtureAssertionError(f"explain 响应不是对象：{payload!r}")
    explanation = str(payload.get("explanation") or "").strip()
    if not explanation:
        raise FixtureAssertionError("explain 响应 explanation 为空 —— 成功路径没有真正走通")
    phrases = payload.get("keyPhrases")
    if not isinstance(phrases, list):
        raise FixtureAssertionError(f"explain 响应 keyPhrases 不是数组：{phrases!r}")
    for phrase in phrases:
        text = str(phrase or "").strip()
        if not text:
            raise FixtureAssertionError("explain 响应含空 keyPhrase")
        if text not in sentence:
            raise FixtureAssertionError(
                f"keyPhrase {text!r} 不是原句的连续子串 —— 生产校验器会拒绝"
            )
        if word_count(text) > MAX_PHRASE_WORDS:
            raise FixtureAssertionError(
                f"keyPhrase {text!r} 超过 {MAX_PHRASE_WORDS} 词 —— 生产校验器会拒绝"
            )


def assert_valid_chunks(payload: dict[str, Any], sentence: str) -> None:
    """断言 chunk 响应满足生产校验器全部规则。"""
    chunks = payload.get("chunks")
    if not isinstance(chunks, list):
        raise FixtureAssertionError(f"chunk 响应 chunks 不是数组：{chunks!r}")
    if not MIN_CHUNKS <= len(chunks) <= MAX_CHUNKS:
        raise FixtureAssertionError(
            f"chunk 段数 {len(chunks)} 不在 {MIN_CHUNKS}–{MAX_CHUNKS} 内"
        )
    texts = []
    for item in chunks:
        text = str(item or "").strip()
        if not text:
            raise FixtureAssertionError("chunk 含空段")
        if word_count(text) > MAX_PHRASE_WORDS:
            raise FixtureAssertionError(
                f"chunk 段 {text!r} 超过 {MAX_PHRASE_WORDS} 词"
            )
        texts.append(text)
    if not _covers_in_order(texts, sentence):
        raise FixtureAssertionError(
            f"chunk 未按顺序完整覆盖原句：{texts!r} vs {sentence!r}"
        )


def assert_valid_transfer(payload: dict[str, Any]) -> None:
    """断言 transfer 响应满足生产校验器规则。"""
    question = str(payload.get("question") or "").strip()
    options = payload.get("options")
    answer = str(payload.get("correctAnswer") or "").strip().upper()
    if not question:
        raise FixtureAssertionError("transfer 响应 question 为空")
    if not isinstance(options, list) or not 2 <= len(options) <= 4:
        raise FixtureAssertionError(f"transfer options 必须是 2–4 项数组：{options!r}")
    letters = {chr(65 + index) for index in range(len(options))}
    if answer not in letters:
        raise FixtureAssertionError(
            f"transfer correctAnswer {answer!r} 不在选项字母 {sorted(letters)} 内"
        )
