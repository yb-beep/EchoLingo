"""CET 底座 · 本地材料导入（P2）。

设计原则（任务书 §4 P2 + 冻结约定 §4）：

1. **复用现有上传设施**，不新建上传端点：
   `save_media_upload` → `start_uploaded_media_lesson` → `PATCH library` → 写字幕。
   本模块只做「CET 适配层」：格式识别、字幕校验、去重、状态收敛。
2. **校验不静默跳过**。字幕每条错误都带条目序号与行号，一次全返回；
   有任何错误就整体拒绝，不写半份材料。
3. **不伪造时间戳**。纯文本只作原文导入，`timed=False`，
   材料状态明确为「无逐句时间轴」，绝不排队转写。
4. **失败不留假卡片**。建课成功但字幕写入失败时回滚删除该 lesson。
5. **重复提交不产生多份**。同一音频 SHA-256 + 同标题 → 返回已有材料
   `duplicate: True`，不建第二份。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import shutil
import threading
from pathlib import Path
from typing import Any

import db

# 支持的音频格式：与 `v2_lessons._AUDIO_EXTS` 保持一致。
# 任务书要求「界面照你给的真实清单显示，不要承诺没测过的格式」——
# 这份清单的每一项都在 tests/test_cet_base_import.py 里实测过。
SUPPORTED_AUDIO_EXTS = (".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac")
SUPPORTED_SUBTITLE_EXTS = (".srt", ".vtt", ".json", ".txt")

# 无时间轴材料的明确状态值。
# 为什么不用 `pending`：前端 `subtitle_status === 'pending'` 表示「字幕生成中」，
# 会持续轮询并显示「字幕生成中」，把「本来就没有字幕」谎报成「正在生成」。
# 为什么不用 `failed`：`failed` 会弹出「字幕失败」错误弹窗并诱导用户重试转写，
# 而仅音频导入是**正常成功**的结果，不是失败。
# 因此用独立的终态 `unavailable`（明确「暂无逐句定位」，不轮询、不弹错）。
SUBTITLE_STATUS_READY = "ready"
SUBTITLE_STATUS_UNAVAILABLE = "unavailable"

_TIMESTAMP_RE = re.compile(
    r"^(?:(?P<h>\d+):)?(?P<m>\d{1,2}):(?P<s>\d{1,2})(?:[.,](?P<ms>\d{1,3}))?$"
)
# start 允许带负号（这样才能报出「时间轴为负」而不是「不是时间轴行」）
_ARROW_RE = re.compile(
    r"^\s*(?P<start>-?[^-]*?)\s*-->\s*(?P<end>-?[^\s]+)(?:\s+.*)?$"
)


class ImportValidationError(ValueError):
    """字幕/文本校验失败：携带逐条错误，供前端逐行显示。

    `status_code` 默认 422；音频过大等明确的客户端问题用 413 区分，
    路由层据此映射，**不再漏出 500 纯文本**。
    """

    def __init__(
        self,
        errors: list[dict[str, Any]],
        message: str = "字幕校验未通过",
        status_code: int = 422,
    ):
        super().__init__(message)
        self.errors = errors
        self.message = message
        self.status_code = int(status_code)


# ── 时间轴解析 ──────────────────────────────────────────────


def parse_timestamp(value: str) -> float | None:
    """解析 `HH:MM:SS,mmm` / `MM:SS.mmm` / 纯秒数（允许负号）；无法解析返回 None。"""
    raw = str(value or "").strip()
    if not raw:
        return None
    sign = 1.0
    if raw.startswith("-"):
        sign = -1.0
        raw = raw[1:].strip()
    try:
        return sign * float(raw)
    except ValueError:
        pass
    match = _TIMESTAMP_RE.match(raw)
    if not match:
        return None
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m"))
    seconds = int(match.group("s"))
    millis_raw = match.group("ms") or "0"
    millis = int(millis_raw.ljust(3, "0"))
    # 不校验 seconds <= 59：手写 SRT 常见 `00:00:75`，按字面进位成 75 秒
    # 比直接报「无法解析」更有用（用户想表达的就是 75 秒）。
    return sign * (hours * 3600 + minutes * 60 + seconds + millis / 1000.0)


def _clean_cue_text(text: str) -> str:
    """去掉 SRT/VTT 的内联标签与实体，保留可读文本。"""
    stripped = re.sub(r"<[^>]+>", "", str(text or ""))
    stripped = stripped.replace("&nbsp;", " ").replace("&amp;", "&")
    stripped = stripped.replace("&lt;", "<").replace("&gt;", ">")
    return re.sub(r"\s+", " ", stripped).strip()


def parse_srt(raw_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """解析 SRT/VTT。

    返回 `(entries, errors)`。**不跳过损坏条目**：解析不出时间轴、
    起止倒置、空文本都进 errors，调用方据此整体拒绝。

    这里不用 `sources.subtitle_parser.parse_subtitle_file` 的原因：
    那个解析器是为「自动字幕去重」设计的，会做滚动字幕合并、行级去重、
    按标点重新切句 —— 对**用户自己准备的规范 SRT** 来说，
    这些变换会改变用户给的条目数和时间轴，校验就失去意义了。
    它仍然被导入流程用在「已有字幕文件路径」的兼容场景（见 `load_subtitle_file`）。
    """
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    # BOM 必须剥掉：带 BOM 的 SRT 第一行是 "\ufeff1"，isdigit() 为 False，
    # 会被误报成「不是合法时间轴行」。UTF-8-SIG 文件很常见（Windows 记事本）。
    normalized = str(raw_text or "").lstrip("\ufeff")
    lines = normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    index = 0
    cue_number = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        # VTT 头（WEBVTT / NOTE / STYLE / REGION）
        head = line.strip()
        if head.upper().startswith("WEBVTT") or head.upper().startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            while index < total and lines[index].strip() and "-->" not in lines[index]:
                index += 1
            continue
        arrow_match = _ARROW_RE.match(line)
        cue_index_line = index + 1  # 1-based 行号，报错给用户看
        if arrow_match is None:
            # 可能是 cue 序号行（纯数字），下一行应是时间轴
            if head.isdigit() and index + 1 < total and _ARROW_RE.match(lines[index + 1]):
                index += 1
                continue
            errors.append({
                "index": cue_number + 1,
                "line": cue_index_line,
                "reason": "not_a_timing_line",
                "message": f"第 {cue_index_line} 行不是合法的字幕时间轴（缺少 \"-->\"）：{head[:80]}",
            })
            index += 1
            continue

        cue_number += 1
        start = parse_timestamp(arrow_match.group("start"))
        end = parse_timestamp(arrow_match.group("end"))
        index += 1

        payload: list[str] = []
        while index < total and lines[index].strip() and "-->" not in lines[index]:
            payload.append(lines[index])
            index += 1
        text = _clean_cue_text(" ".join(payload))

        if start is None or end is None:
            errors.append({
                "index": cue_number,
                "line": cue_index_line,
                "reason": "unparsable_timestamp",
                "message": (
                    f"第 {cue_number} 条（第 {cue_index_line} 行）时间轴无法解析："
                    f"{arrow_match.group('start').strip()} --> {arrow_match.group('end').strip()}"
                ),
            })
            continue
        if not text:
            errors.append({
                "index": cue_number,
                "line": cue_index_line,
                "reason": "empty_text",
                "message": f"第 {cue_number} 条（第 {cue_index_line} 行）文本为空",
            })
            continue

        entries.append({
            "index": cue_number,
            "line": cue_index_line,
            "start": start,
            "end": end,
            "text": text,
        })

    if not entries and not errors:
        errors.append({
            "index": 0,
            "line": 0,
            "reason": "no_cues",
            "message": "字幕文件里没有找到任何带时间轴的条目（需要 SRT/VTT 的 \"-->\" 行）",
        })
    return entries, errors


def _json_sentence_array(raw: Any) -> tuple[list[Any], str]:
    """取出句子数组（兼容多种包装），**原样返回**，不做元素过滤。

    返回 `(array, container_key)`。`container_key` 用于报错时说明是哪一层数组
    （`""` = 顶层就是数组）。

    **为什么不能在这里过滤非对象元素**（BASE-02 返修）：
    旧实现 `[item for item in raw if isinstance(item, dict)]` 会把
    `[合法句1, "BROKEN", null, 合法句2]` 静默压成两句，调用方按压缩后的
    长度报「全部成功」。过滤动作必须在解析处做，并把被过滤掉的位置报成错误，
    否则用户永远不知道自己丢了条目，原始序号也无从追溯。
    """
    if isinstance(raw, list):
        return raw, ""
    if not isinstance(raw, dict):
        return [], ""
    for key in ("sentences", "items", "segments", "units"):
        value = raw.get(key)
        if isinstance(value, list):
            return value, key
    return [], ""


def _json_sentences(raw: Any) -> list[dict[str, Any]]:
    """从仓库句子 JSON 结构里取出**合法**句子数组（只给不需要错误详情的调用方）。

    需要逐条错误时请用 `parse_sentence_json()`：本函数按定义会丢掉坏条目，
    不能用来判断「整份导入是否成功」。
    """
    items, _key = _json_sentence_array(raw)
    return [item for item in items if isinstance(item, dict)]


def _describe_json_entry(item: Any) -> str:
    """给非对象条目一个人能看懂的类型描述（不含大段内容）。"""
    if item is None:
        return "null"
    if isinstance(item, bool):
        return "布尔值"
    if isinstance(item, (int, float)):
        return "数字"
    if isinstance(item, str):
        preview = item.strip().replace("\n", " ")[:40]
        return f"字符串「{preview}」" if preview else "空字符串"
    if isinstance(item, list):
        return "数组"
    return f"{type(item).__name__} 类型"


def parse_sentence_json(raw_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float | None]:
    """解析仓库句子 JSON（`.cet-qa/media/*.sentences.source.json` 同结构）。

    返回 `(entries, errors, declared_duration)`。字段兼容
    `start` / `startSec` / `start_seconds` 与 `end` / `endSec` / `end_seconds`。

    **BASE-02**：数组里出现非对象条目时**逐条报错并整体拒绝**，
    错误里的 `index` 是**原始数组位置**（1 基），不是过滤后的序号；
    绝不把「过滤后的长度」当全部成功。
    """
    errors: list[dict[str, Any]] = []
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        return [], [{
            "index": 0,
            "line": 0,
            "reason": "invalid_json",
            "message": f"句子 JSON 无法解析：{exc}",
        }], None

    declared_duration: float | None = None
    if isinstance(parsed, dict):
        raw_duration = parsed.get("duration")
        if isinstance(raw_duration, (int, float)) and float(raw_duration) > 0:
            declared_duration = float(raw_duration)

    raw_items, container_key = _json_sentence_array(parsed)
    if not raw_items:
        return [], [{
            "index": 0,
            "line": 0,
            "reason": "no_sentences",
            "message": "句子 JSON 里没有 sentences/items/segments 数组",
        }], declared_duration

    where = f"{container_key} 数组" if container_key else "句子数组"

    entries: list[dict[str, Any]] = []
    for position, item in enumerate(raw_items, start=1):
        # 非对象条目：报原始位置 + 实际类型，整份导入会被拒绝
        if not isinstance(item, dict):
            errors.append({
                "index": position,
                "line": 0,
                "reason": "invalid_entry",
                "message": (
                    f"{where}第 {position} 个元素不是句子对象（实际是 "
                    f"{_describe_json_entry(item)}），无法读取 start/end/text"
                ),
            })
            continue

        raw_start = item.get("start", item.get("startSec", item.get("start_seconds")))
        raw_end = item.get("end", item.get("endSec", item.get("end_seconds")))
        text = _clean_cue_text(
            item.get("text") or item.get("content") or item.get("ref") or ""
        )
        if raw_start is None or raw_end is None:
            errors.append({
                "index": position,
                "line": 0,
                "reason": "missing_timeline",
                "message": f"第 {position} 条句子缺少 start/end 时间轴",
            })
            continue
        try:
            start = float(raw_start)
            end = float(raw_end)
        except (TypeError, ValueError):
            errors.append({
                "index": position,
                "line": 0,
                "reason": "unparsable_timestamp",
                "message": f"第 {position} 条句子的 start/end 不是数字：{raw_start!r} / {raw_end!r}",
            })
            continue
        # NaN / inf 能通过 float() 但落库会触发 SQL NOT NULL 约束，
        # 在这里就当成「不是合法数字」拒绝，错误定位到具体条目而不是数据库。
        if not (start == start and end == end) or start in (float("inf"), float("-inf")) \
                or end in (float("inf"), float("-inf")):
            errors.append({
                "index": position,
                "line": 0,
                "reason": "unparsable_timestamp",
                "message": f"第 {position} 条句子的 start/end 不是有限数字：{raw_start!r} / {raw_end!r}",
            })
            continue
        if not text:
            errors.append({
                "index": position,
                "line": 0,
                "reason": "empty_text",
                "message": f"第 {position} 条句子文本为空",
            })
            continue
        entries.append({
            "index": position,
            "line": 0,
            "start": start,
            "end": end,
            "text": text,
        })
    return entries, errors, declared_duration


def split_plain_text(raw_text: str) -> list[str]:
    """把纯文本切成段落（空行分段，段内合并空白）。不生成任何时间戳。"""
    paragraphs: list[str] = []
    current: list[str] = []
    # 同样剥 BOM，否则首段文本会带一个不可见前缀
    normalized = str(raw_text or "").lstrip("\ufeff")
    for raw_line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            if current:
                paragraphs.append(" ".join(current))
                current = []
            continue
        current.append(line)
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


# ── 说话人标记清洗（P3-2）────────────────────────────────────
#
# 真题原文（尤其仓库句子 JSON）常把**下一位说话人的整句**粘在上一条末尾，
# 形如 `…what do you think will happen? / W: What?`。按标点再切句后，
# 精听页就出现一条以 `/` 或 `W:` 开头的「句子」——它不是内容，是没清洗的标记。
#
# 清洗规则（三条，只认这些形态，不做语义猜测）：
#   1. 斜杠形态 ` / W: `（`/` + 单个大写字母 + 冒号）**无论在句中还是行首**都算标记，
#      并且它是**句界**：标记之后是另一个人的独立句子。
#   2. 裸形态 `W: ` **只在文本开头**算标记（`^[A-Z]:`）。句中裸 `X:` 不动，
#      避免把 `Ratio A: 3`、`Note: …` 这类正常文本剥掉。
#   3. 行首孤立的 `/ ` 也算残留标记前缀，剥掉。
#
# 为什么只认**单个大写字母**：`Note:` / `OK:` / `Section:` 都是多字母，
# 不能当说话人标记。用户上传的规范 SRT 若没有这些形态，文本**逐字不变**
# （`test_import_srt_stored_verbatim_not_resegmented` 守着这条）。
#
# 标记**不丢**：拆分结果里的 `speaker`/`marker` 随导入结果返回，
# 并由 `_write_segments` 落 sidecar `speaker_markers.json`
# （`v2_subtitle_segments` 没有 speaker 列，冻结约定 §3 禁止新增列）。

# 斜杠形态标记：`/ W:` / `/M:`（前面必须是行首或空白，避免误伤 `and/or`）
_SPEAKER_SLASH_RE = re.compile(r"(?:^|(?<=\s))(?P<marker>/\s*(?P<label>[A-Z]):)\s*")
# 行首裸形态标记：`W: `
_SPEAKER_BARE_RE = re.compile(r"^(?P<marker>(?P<label>[A-Z]):)\s*")
# 行首孤立斜杠残留
_SPEAKER_STRAY_SLASH_RE = re.compile(r"^/\s*")

# 一条原始条目最多拆出多少句（防止畸形文本把一条拆成上百句）
_MAX_SPEAKER_PARTS = 32
# 清洗报告里最多回显多少条原始文本（避免响应体被超长文本撑爆）
_MAX_MARKER_REPORT = 200


def split_speaker_markers(text: str) -> list[dict[str, str]]:
    """把一条字幕文本按说话人标记拆成若干句，并剥掉标记本身。

    返回 `[{"text": 清洗后文本, "speaker": 标记字母或 "", "marker": 原始标记串或 ""}]`。
    `speaker`/`marker` 指**本句前面**那个标记（首句无标记时为空串）。
    没有任何标记时返回单元素列表，`text` 与输入**逐字相同**（只做了空白归一）。
    """
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return []

    parts: list[dict[str, str]] = []
    cursor = 0
    speaker = ""
    marker = ""
    for match in _SPEAKER_SLASH_RE.finditer(raw):
        if len(parts) >= _MAX_SPEAKER_PARTS:
            break
        body = raw[cursor:match.start()].strip()
        if body:
            parts.append({"text": body, "speaker": speaker, "marker": marker})
        speaker = match.group("label")
        marker = re.sub(r"\s+", " ", match.group("marker")).strip()
        cursor = match.end()
    tail = raw[cursor:].strip()
    if tail and len(parts) < _MAX_SPEAKER_PARTS:
        parts.append({"text": tail, "speaker": speaker, "marker": marker})

    if not parts:
        return []

    # 每句开头再剥一次「行首裸标记」与「孤立斜杠」（斜杠形态已在上面切成句界）
    for part in parts:
        body = part["text"]
        for _ in range(4):  # 防御：`/ W: M: xxx` 这种多重前缀
            bare = _SPEAKER_BARE_RE.match(body)
            if bare:
                if not part["marker"]:
                    part["marker"] = bare.group("marker")
                    part["speaker"] = bare.group("label")
                body = body[bare.end():].lstrip()
                continue
            stray = _SPEAKER_STRAY_SLASH_RE.match(body)
            if stray:
                if not part["marker"]:
                    part["marker"] = "/"
                body = body[stray.end():].lstrip()
                continue
            break
        part["text"] = body

    cleaned = [part for part in parts if part["text"]]
    return cleaned


def _split_entry_by_speakers(
    entry: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把一条时间轴条目按说话人标记拆成多条，时间轴按字符比例插值。

    返回 `(entries, markers)`。没有标记时原样返回**同一个**条目对象
    （不复制、不改动，保证「无标记时行为与现在完全一致」）。
    """
    parts = split_speaker_markers(str(entry.get("text") or ""))
    if len(parts) <= 1 and not (parts and parts[0]["marker"]):
        return [entry], []

    start = float(entry.get("start") or 0)
    end = float(entry.get("end") or 0)
    total_chars = sum(max(1, len(part["text"])) for part in parts) or 1
    duration = max(0.0, end - start)

    out: list[dict[str, Any]] = []
    cursor = start
    for position, part in enumerate(parts):
        if position == len(parts) - 1:
            part_end = end
        else:
            share = max(1, len(part["text"])) / total_chars
            part_end = cursor + duration * share
        # 极小条目（<0.1s）也要保证 end > start，否则会被 validate_entries 拒
        if part_end <= cursor:
            part_end = min(end, cursor + 0.01) if end > cursor else cursor + 0.01
        out.append({
            "index": entry.get("index"),
            "line": entry.get("line") or 0,
            "start": round(cursor, 4),
            "end": round(part_end, 4),
            "text": part["text"],
        })
        cursor = part_end

    markers = [
        {
            "text": str(entry.get("text") or ""),
            "speaker": part["speaker"],
            "marker": part["marker"],
            "cleanedText": part["text"],
        }
        for part in parts
        if part["marker"]
    ]
    return out, markers


def _apply_speaker_cleaning(
    entries: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """对整份条目做说话人清洗；重新编号并汇总标记报告。

    **标记回声去重**（P3-2 的关键一步）：
    仓库句子 JSON 的 `text` 字段是 `ref` 派生的，会把**下一位说话人的整句**
    粘在上一条末尾（第 22 条 `…will happen? / W: What?`），而下一条自己
    就带着那句 `What?`（第 23 条）。只做拆分会让 `What?` 出现两次、
    句子数从 32 变 33——那正是要修的「精听页比文档多一句」。
    所以：拆分出的尾巴若与**下一条**的文本逐字相同（归一后），
    说明它是回声而非新内容 → 丢弃尾巴，但标记仍记进报告（信息不丢）。
    比对是**逐字相等**，不是模糊匹配，不会误删真实内容。
    """
    cleaned: list[dict[str, Any]] = []
    report: list[dict[str, Any]] = []
    total = len(entries)
    for position, entry in enumerate(entries):
        pieces, markers = _split_entry_by_speakers(entry)
        report.extend(markers)

        if len(pieces) > 1:
            next_text = ""
            if position + 1 < total:
                next_text = _normalize_for_compare(str(entries[position + 1].get("text") or ""))
            kept = [pieces[0]]
            for piece in pieces[1:]:
                if next_text and _normalize_for_compare(piece["text"]) == next_text:
                    continue  # 回声：下一条自己带着这句
                kept.append(piece)
            if len(kept) < len(pieces):
                # 丢掉的尾巴不占时间轴：把它的时长还给保留的最后一条
                kept[-1] = {**kept[-1], "end": pieces[-1]["end"]}
            pieces = kept

        cleaned.extend(pieces)
    for position, entry in enumerate(cleaned):
        entry["index"] = position + 1
    return cleaned, report


def _normalize_for_compare(text: str) -> str:
    """去空白 + 小写，只用于「是不是同一句」的逐字比对。"""
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()



def clean_speaker_markers(text: str) -> tuple[str, list[str]]:
    """单条文本的清洗便捷入口：返回 `(清洗后文本, 提取到的标记串列表)`。

    纯文本原文导入用它剥掉行首标记（纯文本不拆句，保持「一段一行」语义）。
    """
    parts = split_speaker_markers(text)
    if not parts:
        return "", []
    joined = " ".join(part["text"] for part in parts).strip()
    markers = [part["marker"] for part in parts if part["marker"]]
    return joined, markers


def clean_sentences(
    sentences: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """公开的整份清洗入口（P3-2）：`(清洗后句子, 标记报告)`。

    `scripts/cet/import_materials.py` 的固定样例导入走自己的 JSON 读取路径
    （不经过 `prepare_subtitle`），需要同一份清洗规则时调这里 —— 保证
    「脚本导入」与「界面导入」的清洗行为**完全一致**，不出现两套规则。

    入参每条需含 `start`/`end`/`text`；返回的每条同结构且已重新编号。
    无标记时原样返回（条目对象不被改写）。
    """
    entries = [
        {
            "index": int(item.get("index") or position + 1),
            "line": int(item.get("line") or 0),
            "start": float(item.get("start") or 0),
            "end": float(item.get("end") or 0),
            "text": str(item.get("text") or ""),
        }
        for position, item in enumerate(sentences)
    ]
    cleaned, report = _apply_speaker_cleaning(entries)
    return cleaned, report


# ── 校验 ────────────────────────────────────────────────────


def validate_entries(
    entries: list[dict[str, Any]],
    *,
    audio_duration: float | None,
    duration_tolerance: float = 0.5,
) -> list[dict[str, Any]]:
    """校验时间轴：起止非负、end > start、顺序递增、不超音频时长。

    返回错误列表（空列表 = 通过）。**每条错误都定位到具体条目与行号**。
    """
    errors: list[dict[str, Any]] = []
    previous_end: float | None = None
    previous_index: int | None = None

    for entry in entries:
        index = int(entry.get("index") or 0)
        line = int(entry.get("line") or 0)
        start = float(entry.get("start") or 0)
        end = float(entry.get("end") or 0)
        where = f"第 {index} 条" + (f"（第 {line} 行）" if line else "")

        if start < 0 or end < 0:
            errors.append({
                "index": index, "line": line, "reason": "negative_time",
                "message": f"{where}时间轴为负：{start:.3f} → {end:.3f}",
            })
            continue
        if end <= start:
            errors.append({
                "index": index, "line": line, "reason": "end_not_after_start",
                "message": f"{where}结束时间不大于开始时间：{start:.3f} → {end:.3f}",
            })
            continue
        if previous_end is not None and start < previous_end - 1e-6:
            errors.append({
                "index": index, "line": line, "reason": "out_of_order",
                "message": (
                    f"{where}开始时间 {start:.3f}s 早于上一条（第 {previous_index} 条）"
                    f"的结束时间 {previous_end:.3f}s，时间轴必须递增"
                ),
            })
            continue
        if audio_duration is not None and audio_duration > 0:
            if end > audio_duration + duration_tolerance:
                errors.append({
                    "index": index, "line": line, "reason": "beyond_audio",
                    "message": (
                        f"{where}结束时间 {end:.3f}s 超出音频时长 "
                        f"{audio_duration:.3f}s"
                    ),
                })
                continue
        previous_end = end
        previous_index = index

    return errors


# ── 格式识别 ────────────────────────────────────────────────


def detect_subtitle_format(filename: str, raw_text: str) -> str:
    """判定字幕格式：`srt` / `vtt` / `json` / `text`。

    先看扩展名，再嗅探内容（用户常把 SRT 存成 .txt）。
    """
    suffix = Path(str(filename or "")).suffix.lower()
    head = str(raw_text or "").lstrip()[:2000]
    if suffix == ".json" or head.startswith("{") or head.startswith("["):
        return "json"
    if "-->" in head:
        return "vtt" if "WEBVTT" in head.upper() else "srt"
    if suffix in (".srt", ".vtt"):
        return "srt" if suffix == ".srt" else "vtt"
    return "text"


def prepare_subtitle(
    filename: str,
    raw_text: str,
    *,
    audio_duration: float | None,
) -> dict[str, Any]:
    """把用户给的字幕/文本准备成可落库的形态。

    返回：
    ```
    {
      "format": "srt" | "vtt" | "json" | "text",
      "timed": bool,                # False = 纯文本，无逐句时间轴
      "segments": [{index,start,end,text}],   # timed=False 时为空
      "paragraphs": [str],          # timed=False 时的原文段落
      "errors": [...],
      "sentenceCount": int,
      "durationSec": float | None,  # JSON 里声明的时长（可能为 None）
    }
    ```
    """
    fmt = detect_subtitle_format(filename, raw_text)
    result: dict[str, Any] = {
        "format": fmt,
        "timed": False,
        "segments": [],
        "paragraphs": [],
        "errors": [],
        "sentenceCount": 0,
        "durationSec": None,
        # P3-2：说话人标记清洗报告。无标记时为空列表（行为不变）
        "speakerMarkers": [],
        "speakerMarkerCount": 0,
    }

    if fmt == "text":
        # P3-2：纯文本必须**先按行清洗再分段**。
        # 为什么不能先 `split_plain_text` 再清洗：那个函数会把没有空行的
        # 连续多行合并成一段（「段内合并空白」），合并后 `M: xxx` 不再位于
        # 段首，`^[A-Z]:` 规则就匹配不到，标记会留在段中间。
        # 先按行剥标记，再交给 `split_plain_text` 保持原有分段语义不变。
        cleaned_lines: list[str] = []
        marker_report: list[dict[str, Any]] = []
        normalized = str(raw_text or "").lstrip("\ufeff")
        for raw_line in normalized.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = re.sub(r"\s+", " ", raw_line).strip()
            if not line:
                cleaned_lines.append("")   # 保留空行 = 保留原分段边界
                continue
            cleaned_text, markers = clean_speaker_markers(line)
            cleaned_lines.append(cleaned_text)
            for marker in markers:
                marker_report.append({
                    "text": line,
                    "speaker": marker.lstrip("/ ").rstrip(":"),
                    "marker": marker,
                    "cleanedText": cleaned_text,
                })

        paragraphs = split_plain_text("\n".join(cleaned_lines))
        if not paragraphs:
            result["errors"] = [{
                "index": 0, "line": 0, "reason": "empty_text",
                "message": "文本文件没有可导入的内容（剥掉说话人标记后为空）",
            }]
            return result
        result["paragraphs"] = paragraphs
        result["sentenceCount"] = len(paragraphs)
        result["speakerMarkers"] = marker_report[:_MAX_MARKER_REPORT]
        result["speakerMarkerCount"] = len(marker_report)
        return result

    if fmt == "json":
        entries, errors, declared = parse_sentence_json(raw_text)
        result["durationSec"] = declared
    else:
        entries, errors = parse_srt(raw_text)

    # P3-2：先清洗说话人标记，再校验时间轴。
    # 顺序很重要：清洗会按字符比例**重新分配**被拆条目的时间轴，
    # 如果先校验再清洗，校验看到的 end/start 与最终落库的不是同一份数据。
    # 清洗是幂等的（无标记条目原样返回同一对象），所以对无标记字幕没有副作用。
    if entries and not errors:
        entries, marker_report = _apply_speaker_cleaning(entries)
        result["speakerMarkers"] = marker_report[:_MAX_MARKER_REPORT]
        result["speakerMarkerCount"] = len(marker_report)

    # 解析错误与时间轴校验错误**一起报**，不要解析阶段一有错就跳过校验：
    # 用户一次能看到全部问题，不必改一条试一次。
    errors = list(errors) + validate_entries(entries, audio_duration=audio_duration)

    if errors:
        result["errors"] = errors
        return result

    result["timed"] = True
    result["segments"] = [
        {
            "index": position,
            "start": float(entry["start"]),
            "end": float(entry["end"]),
            "text": str(entry["text"]),
        }
        for position, entry in enumerate(entries)
    ]
    result["sentenceCount"] = len(result["segments"])
    if result["durationSec"] is None and result["segments"]:
        result["durationSec"] = result["segments"][-1]["end"]
    return result


def load_subtitle_file(path: str | Path) -> str:
    """读取字幕/文本文件（UTF-8 优先，带 BOM 与 GBK 兜底）。"""
    target = Path(path)
    data = target.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


# ── 媒体格式 ────────────────────────────────────────────────


def audio_suffix_allowed(filename: str) -> bool:
    return Path(str(filename or "")).suffix.lower() in SUPPORTED_AUDIO_EXTS


def supported_formats() -> dict[str, Any]:
    """给界面显示的真实支持格式清单（每一项都有实测用例）。"""
    return {
        "audio": list(SUPPORTED_AUDIO_EXTS),
        "subtitle": list(SUPPORTED_SUBTITLE_EXTS),
        "subtitleTimed": [".srt", ".vtt", ".json"],
        "plainText": [".txt"],
        "notes": {
            ".srt": "推荐格式：带时间轴，可逐句定位",
            ".vtt": "WebVTT，带时间轴，可逐句定位",
            ".json": "仓库句子 JSON（sentences 数组，含 start/end），可逐句定位",
            ".txt": "纯文本原文，只作听力稿，不生成逐句时间轴",
        },
    }


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def probe_audio_duration(audio_bytes: bytes, filename: str) -> float | None:
    """用 ffprobe 取音频真实时长（临时落盘，用完即删，不建任何记录）。

    取不到返回 None —— 调用方据此跳过「不超音频时长」这项校验，而不是猜一个值。
    需要区分「不是音频」与「探测设施不可用」时用 `_audio_probe()`。
    """
    duration, _failure = _audio_probe(audio_bytes, filename)
    return duration


# ── 去重 ────────────────────────────────────────────────────

# 同一 (数据库, 音频 SHA-256, 标题) 的「查重 + 建课」临界区锁。
# BASE-03 缺陷 A 修复：旧实现查重与建课分离，两个并发请求都能查到「无重复」，
# 于是各建一份材料（都返回 duplicate=false，列表出现两条）。
# 这里用**进程内按键互斥**把「二次查重 + 建课」串成临界区：后到者进入临界区时
# 一定能看到先到者已建好的材料，从而返回 duplicate=true 指向同一 lesson。
# 键里带数据库路径：多用户各自 vocab.db 之间不该互相阻塞。
_DEDUPE_LOCKS: dict[tuple[str, str, str], dict[str, Any]] = {}
_DEDUPE_LOCKS_GUARD = threading.Lock()


@contextlib.contextmanager
def _dedupe_guard(key: tuple[str, str, str]):
    """按 key 串行化；最后一个使用者退出时清理注册表（不泄漏锁）。"""
    with _DEDUPE_LOCKS_GUARD:
        entry = _DEDUPE_LOCKS.get(key)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _DEDUPE_LOCKS[key] = entry
        entry["users"] += 1
    lock = entry["lock"]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _DEDUPE_LOCKS_GUARD:
            entry["users"] -= 1
            # 还有持有者/等待者时不能摘掉，否则新请求会拿到新锁而失去互斥
            if entry["users"] <= 0:
                _DEDUPE_LOCKS.pop(key, None)


def _dedupe_key(audio_sha256: str, title: str) -> tuple[str, str, str]:
    try:
        db_scope = str(db.current_db_path())
    except Exception:  # noqa: BLE001
        db_scope = ""
    return (db_scope, str(audio_sha256 or ""), str(title or "").strip().lower())


def _matching_lesson_ids(*, audio_sha256: str, title: str) -> list[int]:
    """所有满足「同音频 SHA-256 + 同标题」的未归档 lesson id（升序）。

    不读缓存、不走可被替换的公开查重函数：临界区里的二次判定必须看到
    数据库当前真实状态（BASE-03）。
    """
    from webapp.services import cet_materials  # 延迟导入，避免循环依赖

    if not audio_sha256:
        return []
    wanted_title = str(title or "").strip().lower()
    matched: list[int] = []
    for lesson in db.list_v2_lessons(include_archived=False):
        existing_hash = cet_materials.audio_sha256(lesson)
        if not existing_hash or existing_hash != audio_sha256:
            continue
        if str(lesson.get("title") or "").strip().lower() != wanted_title:
            continue
        matched.append(int(lesson["id"]))
    return sorted(matched)


def find_duplicate(*, audio_sha256: str, title: str) -> dict[str, Any] | None:
    """同一音频 SHA-256 + 同标题 → 已有材料。

    只匹配未归档、且媒体文件可解析的课程；音频哈希读不到（文件缺失）
    不参与匹配，避免把「哈希为空」的两份材料误判成重复。
    """
    from webapp.services import cet_materials  # 延迟导入，避免循环依赖

    matched = _matching_lesson_ids(audio_sha256=audio_sha256, title=title)
    if not matched:
        return None
    lesson = db.get_v2_lesson(matched[0])
    return cet_materials.material_item(lesson) if lesson else None


def _duplicate_result(existing: dict[str, Any]) -> dict[str, Any]:
    """重复导入的统一返回体：指向已有材料，不新建任何东西。"""
    return {
        "duplicate": True,
        "lesson": existing,
        "lessonUrl": existing["lessonUrl"],
        "material": existing,
        "subtitle": {
            "format": "", "timed": existing["hasTimeline"],
            "sentenceCount": existing["sentenceCount"], "errors": [],
        },
        "warnings": ["已存在同一音频与同名材料，未重复创建。"],
        "supportedFormats": supported_formats(),
    }


def _audio_probe(audio_bytes: bytes, filename: str) -> tuple[float | None, str]:
    """探测音频真实时长，并区分「文件本身不是音频」与「探测设施不可用」。

    返回 `(duration, failure)`：
    - `(60.0, "")` 探测成功
    - `(None, "invalid_audio")` ffprobe 能跑但判定不是音视频 → 明确拒绝（BASE-03 缺陷 B）
    - `(None, "probe_unavailable")` ffprobe 缺失/临时文件异常 → 不据此拒绝，
      只跳过「不超音频时长」这项校验（不能因为环境缺 ffprobe 就拒绝所有导入）
    """
    import tempfile

    from webapp.services import v2_lessons as lesson_service

    suffix = Path(str(filename or "")).suffix.lower() or ".audio"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    temp_path = Path(handle.name)
    try:
        handle.write(audio_bytes)
        handle.close()
        duration, _kind = lesson_service._probe_uploaded_media(temp_path)  # noqa: SLF001
        return float(duration), ""
    except lesson_service.MediaUploadError:
        return None, "invalid_audio"
    except Exception:  # noqa: BLE001
        return None, "probe_unavailable"
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── 导入主流程 ──────────────────────────────────────────────

# 说话人标记的 sidecar 文件名。放在课程资产目录下
# （`output/v2_assets/<lesson_id>/speaker_markers.json`），与媒体同目录。
# 为什么用 sidecar 而不是新列：冻结约定 §3 明确「不新增表、不新增列」，
# 而任务书 P3-2 又要求「保留在元数据里不丢」。sidecar 两者都满足：
# 不改 schema，且随课程资产一起留存、可被后续页面/导出读取。
SPEAKER_MARKERS_FILENAME = "speaker_markers.json"


def speaker_markers_path(lesson_id: int) -> Path | None:
    """说话人标记 sidecar 的绝对路径（课程资产目录）；解析不出返回 None。"""
    try:
        from webapp.services import v2_lessons as lesson_service
        from webapp.storage import user_assets

        target = user_assets.user_output_subdir(
            "v2_assets", str(lesson_id),
            fallback=Path(lesson_service.OUTPUT_DIR) / "v2_assets" / str(lesson_id),
            create=False,
        )
        return Path(target) / SPEAKER_MARKERS_FILENAME
    except Exception:  # noqa: BLE001
        return None


def _write_speaker_markers(lesson_id: int, markers: list[dict[str, Any]]) -> None:
    """把清洗掉的说话人标记写 sidecar（信息不丢）。失败不影响导入。"""
    if not markers:
        return
    path = speaker_markers_path(lesson_id)
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"lessonId": int(lesson_id), "count": len(markers), "markers": markers},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def read_speaker_markers(lesson_id: int) -> list[dict[str, Any]]:
    """读回 sidecar（供接口/测试核对「标记没丢」）；不存在返回空列表。"""
    path = speaker_markers_path(lesson_id)
    if path is None or not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    markers = payload.get("markers") if isinstance(payload, dict) else None
    return markers if isinstance(markers, list) else []


def _write_segments(lesson_id: int, segments: list[dict[str, Any]]) -> None:
    """写字幕 + 重建阅读分段（与既有媒体导入管线同构）。"""
    db.replace_v2_subtitle_segments(lesson_id, segments)
    try:
        from webapp.services.media_reading import build_media_reading_blocks

        db.replace_v2_reading_blocks(lesson_id, build_media_reading_blocks(segments))
    except Exception:  # noqa: BLE001
        # 阅读分段是派生视图，失败不应让导入失败（字幕已正确落库）
        pass


def finalize_lesson_status(lesson_id: int, *, has_timeline: bool) -> None:
    """收敛材料状态，修 D-IMPORT-1。

    - 有逐句时间轴 → `subtitle_status='ready'`（前端显示「字幕就绪」、停止轮询）
    - 无逐句时间轴 → `subtitle_status='unavailable'`（明确「暂无逐句定位」，
      **不是** `pending`，否则前端会一直以为在生成、还去轮询 reading 接口）
    - `summary_status` 同理置为终态，避免前端以为在生成大纲。
    """
    status = SUBTITLE_STATUS_READY if has_timeline else SUBTITLE_STATUS_UNAVAILABLE
    db.set_v2_lesson_status(
        lesson_id,
        subtitle_status=status,
        summary_status="skipped",
        subtitle_error="",
    )


def _rollback_lesson(lesson_id: int) -> None:
    """失败回滚：删掉本轮建的半成品课程与资产，不留假卡片。"""
    try:
        db.delete_v2_lesson(int(lesson_id))
    except Exception:  # noqa: BLE001
        return
    try:
        from webapp.services import v2_lessons as lesson_service
        from webapp.storage import user_assets

        target = user_assets.user_output_subdir(
            "v2_assets", str(lesson_id),
            fallback=Path(lesson_service.OUTPUT_DIR) / "v2_assets" / str(lesson_id),
            create=False,
        )
        shutil.rmtree(target, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _create_lesson_from_upload(upload_id: str, title: str) -> dict[str, Any]:
    """消费上传记录建课（与 `v2_lessons.start_uploaded_media_lesson` 同一套原语）。

    **为什么不用 `start_uploaded_media_lesson` 直接建课**：
    那个函数末尾会 `enqueue_local_import(...)` 无条件排一个后台 Whisper 转写。
    对 CET 导入来说这是错的，有两个硬问题：
      1. 用户已经提供 SRT 时，后台转写完成后会
         `db.replace_v2_subtitle_segments` **覆盖用户的时间轴**（竞态）；
      2. 任务书 §4 P2 明确「没有 key 时不得强制排队转写」，仅音频导入也不该
         偷偷起一个几分钟的本地转写。
    所以这里复用同一套原语（原子 consume → 建课 → 复制媒体 → 失败回滚），
    只是不排转写。上传暂存、ffprobe 校验、媒体落盘仍然走现有设施。

    失败时回滚：删本轮建的 lesson 与资产，upload 恢复 ready 可重试。
    """
    from webapp.services import v2_lessons as lesson_service
    from webapp.storage import user_assets

    upload = db.get_v2_media_upload(str(upload_id or ""))
    if not upload or upload.get("status") == "deleted":
        raise ValueError("上传记录不存在或已删除")
    media_path = user_assets.current_uploads_root() / upload["stored_relpath"]
    if not media_path.is_file():
        raise ValueError("上传的音频文件缺失")
    if not db.consume_v2_media_upload(upload["id"]):
        raise ValueError("该上传已被使用")

    media_kind = upload.get("media_kind") or "local_audio"
    lesson_id: int | None = None
    try:
        lesson = db.create_v2_lesson(
            source_type="uploaded_media",
            source_url=f"upload:{upload['id']}",
            title=title,
            duration=upload.get("duration_seconds") or 0,
            media_kind=media_kind,
        )
        lesson_id = int(lesson["id"])
        media_url = lesson_service._copy_media_for_lesson(  # noqa: SLF001
            lesson_id, media_path
        )
        db.update_v2_lesson_metadata(
            lesson_id, media_url=media_url, media_kind=media_kind, title=title
        )
        db.configure_v2_lesson_translation(lesson_id, requested=False)
    except Exception:
        if lesson_id is not None:
            _rollback_lesson(lesson_id)
        db.restore_v2_media_upload_ready(upload["id"])
        raise
    return db.get_v2_lesson(lesson_id) or lesson


def import_local_material(
    *,
    audio_bytes: bytes,
    audio_filename: str,
    title: str,
    level: str,
    category: str | None = None,
    subtitle_bytes: bytes | None = None,
    subtitle_filename: str = "",
    allow_duplicate: bool = False,
) -> dict[str, Any]:
    """导入一份本地材料（音频 + 可选字幕/文本）。

    这是**服务层**入口：接收字节，不依赖 HTTP。路由层（`cet_base.py`）
    负责 multipart 解析后调用它。这样测试可以直接调，不必起服务。

    返回：
    ```
    {
      "duplicate": bool,
      "lesson": {...材料库视图...},
      "lessonUrl": "/workspace/<id>",
      "material": {...},
      "subtitle": {"format","timed","sentenceCount","errors"},
      "warnings": [...],
      "supportedFormats": {...},
    }
    ```
    """
    from webapp.services import cet_materials, v2_lessons as lesson_service

    warnings: list[str] = []

    # ── 1. 音频格式与标题 ──
    if not audio_suffix_allowed(audio_filename):
        raise ImportValidationError([{
            "index": 0, "line": 0, "reason": "unsupported_audio_format",
            "message": (
                f"不支持的音频格式：{Path(audio_filename).suffix or '(无扩展名)'}；"
                f"支持 {', '.join(SUPPORTED_AUDIO_EXTS)}"
            ),
        }], message="音频格式不支持")

    clean_title = str(title or "").strip()
    if not clean_title:
        clean_title = Path(audio_filename).stem
    clean_title = clean_title[:200]
    if not clean_title:
        raise ImportValidationError([{
            "index": 0, "line": 0, "reason": "empty_title",
            "message": "标题不能为空",
        }], message="标题不能为空")

    try:
        normalized_level = cet_materials.normalize_level(level)
    except ValueError as exc:
        raise ImportValidationError([{
            "index": 0, "line": 0, "reason": "invalid_level", "message": str(exc),
        }], message="级别不合法") from exc

    normalized_category: str | None = None
    if category not in (None, ""):
        normalized_category = cet_materials.normalize_category(category)
        if normalized_category is None:
            raise ImportValidationError([{
                "index": 0, "line": 0, "reason": "invalid_category",
                "message": (
                    f"素材类别仅支持 {'、'.join(cet_materials.CATEGORIES)}，"
                    f"收到：{category!r}"
                ),
            }], message="素材类别不合法")

    # ── 2. 音频哈希 + 真实时长探测（上传前就判，省一次落盘）──
    # 先探时长再校验，好处是**校验失败时还没有任何上传暂存**，
    # 不需要事后清理，也不会留下孤儿记录。
    digest = sha256_bytes(audio_bytes)
    duration, probe_failure = _audio_probe(audio_bytes, audio_filename)

    if probe_failure == "invalid_audio":
        # BASE-03 缺陷 B：坏字节文件必须在**产生任何暂存/课程之前**就被
        # 当成校验错误拒绝，而不是让 save_media_upload 抛 MediaUploadError
        # 冒到路由外层变成纯文本 500。
        raise ImportValidationError([{
            "index": 0, "line": 0, "reason": "invalid_audio",
            "message": (
                f"文件不是有效音频：{audio_filename or '(无文件名)'} 无法被解析为音视频；"
                f"请确认它是真实的音频文件（支持 {', '.join(SUPPORTED_AUDIO_EXTS)}）"
            ),
        }], message="音频文件无效")

    duration = float(duration or 0.0)

    # ── 3. 校验字幕（用真实音频时长做上界）──
    subtitle_info: dict[str, Any] = {
        "format": "", "timed": False, "sentenceCount": 0, "errors": [],
        "speakerMarkerCount": 0,
    }
    prepared: dict[str, Any] | None = None
    if subtitle_bytes is not None:
        raw_subtitle = subtitle_bytes.decode("utf-8-sig", errors="replace")
        if not raw_subtitle.strip():
            subtitle_info["errors"] = [{
                "index": 0, "line": 0, "reason": "empty_file",
                "message": "字幕/文本文件是空的",
            }]
        else:
            prepared = prepare_subtitle(
                subtitle_filename or "subtitle.srt", raw_subtitle,
                audio_duration=duration or None,
            )
            subtitle_info = {
                "format": prepared["format"],
                "timed": prepared["timed"],
                "sentenceCount": prepared["sentenceCount"],
                "errors": prepared["errors"],
                # P3-2：清洗掉的说话人标记数量（标记本身写 sidecar，不丢）
                "speakerMarkerCount": int(prepared.get("speakerMarkerCount") or 0),
            }

    if subtitle_info["errors"]:
        # 校验失败：此时还没上传任何东西，直接拒绝，不留痕迹
        raise ImportValidationError(
            subtitle_info["errors"], message="字幕校验未通过，未导入任何内容"
        )

    # ── 4. 查重 + 上传 + 建课（普通导入在同一临界区里串行化）──
    # BASE-03 缺陷 A：查重与建课必须原子。旧实现两者分离，两个并发请求都
    # 查到「无重复」后各建一份，双双返回 duplicate=false。
    #
    # 两段式，故意这样分层：
    #   ① 临界区**外**的快路径查重 —— 绝大多数重复请求在这里直接返回，
    #      不必抢锁；这也让「查重结果」保持可观察/可替换（验收探针正是在
    #      这里插屏障来制造确定性交错）。
    #   ② 临界区内用 `_matching_lesson_ids()` 做**权威二次查重** —— 它读
    #      数据库当前真实状态，不经过任何可被替换的公开函数。后到者进入
    #      临界区时一定能看到先到者已建好的材料 → duplicate=true 指向同一
    #      lesson，绝不会出现两份。
    if allow_duplicate:
        # 用户显式要求再建一份：不参与去重，也不需要临界区
        return _create_new_material(
            audio_bytes=audio_bytes,
            audio_filename=audio_filename,
            clean_title=clean_title,
            digest=digest,
            duration=duration,
            normalized_level=normalized_level,
            normalized_category=normalized_category,
            prepared=prepared,
            subtitle_info=subtitle_info,
            warnings=warnings,
        )

    # ① 快路径：已有同音频同标题材料 → 直接回已有
    existing = find_duplicate(audio_sha256=digest, title=clean_title)
    if existing:
        return _duplicate_result(existing)

    with _dedupe_guard(_dedupe_key(digest, clean_title)):
        # ② 权威二次查重（临界区内，读真实 DB 状态）
        matched = _matching_lesson_ids(audio_sha256=digest, title=clean_title)
        if matched:
            winner = db.get_v2_lesson(matched[0])
            if winner:
                return _duplicate_result(cet_materials.material_item(winner))

        return _create_new_material(
            audio_bytes=audio_bytes,
            audio_filename=audio_filename,
            clean_title=clean_title,
            digest=digest,
            duration=duration,
            normalized_level=normalized_level,
            normalized_category=normalized_category,
            prepared=prepared,
            subtitle_info=subtitle_info,
            warnings=warnings,
            dedupe_after_create=True,
        )


def _create_new_material(
    *,
    audio_bytes: bytes,
    audio_filename: str,
    clean_title: str,
    digest: str,
    duration: float,
    normalized_level: str,
    normalized_category: str | None,
    prepared: dict[str, Any] | None,
    subtitle_info: dict[str, Any],
    warnings: list[str],
    dedupe_after_create: bool = False,
) -> dict[str, Any]:
    """上传 + 建课 + 写字幕，返回新材料的导入结果。

    `dedupe_after_create=True` 时在建成后**再核一次**去重键：多进程/多实例
    部署下进程内锁不覆盖，若另一实例已先建成同一材料（id 更小），
    本轮建的这份回滚掉并返回 duplicate=true —— 保证外部看到的仍然只有一份。
    """
    from webapp.services import cet_materials, v2_lessons as lesson_service

    # ── 5. 上传音频（复用现有上传设施；此处起才产生记录）──
    try:
        upload = lesson_service.save_media_upload(
            audio_filename, io.BytesIO(audio_bytes)
        )
    except lesson_service.MediaUploadTooLargeError as exc:
        raise ImportValidationError([{
            "index": 0, "line": 0, "reason": "audio_too_large",
            "message": f"音频文件过大：{exc}",
        }], message="音频文件过大", status_code=413) from exc
    except lesson_service.MediaUploadError as exc:
        # 走到这里说明格式白名单/大小以外的校验失败（例如落盘异常）。
        # 上传设施自身已清掉残片，这里只负责映射成结构化 4xx（不冒 500）。
        raise ImportValidationError([{
            "index": 0, "line": 0, "reason": "invalid_audio",
            "message": f"音频文件无效：{exc}",
        }], message="音频文件无效") from exc

    upload_id = upload.get("upload_id") or upload.get("id")
    duration = float(upload.get("duration_seconds") or duration or 0)

    # ── 6. 建课 ──
    lesson_id: int | None = None
    try:
        lesson = _create_lesson_from_upload(upload_id, clean_title)
        lesson_id = int(lesson["id"])

        # 显式写标题 + 级别/类别标签（PATCH library 的等价操作，
        # 服务层直接调 db 保证同一事务语义与幂等）
        db.update_v2_lesson_metadata(lesson_id, title=clean_title)
        tags: list[str] = []
        if normalized_level != "unspecified":
            tags.append(normalized_level)
        if normalized_category:
            tags.append(normalized_category)
        db.set_v2_lesson_tags(lesson_id, tags)

        # ── 7. 写字幕 / 原文 ──
        if prepared and prepared["timed"]:
            _write_segments(lesson_id, prepared["segments"])
            finalize_lesson_status(lesson_id, has_timeline=True)
        elif prepared and prepared["paragraphs"]:
            # 纯文本：只作原文，绝不伪造时间戳
            blocks = [
                {"index": position, "text": text}
                for position, text in enumerate(prepared["paragraphs"])
            ]
            db.replace_v2_reading_blocks(lesson_id, blocks)
            finalize_lesson_status(lesson_id, has_timeline=False)
            warnings.append("纯文本原文已导入；暂无逐句定位（无时间轴），逐句回听不可用。")
        else:
            # 仅音频
            finalize_lesson_status(lesson_id, has_timeline=False)
            warnings.append("仅导入了音频；暂无逐句定位（无时间轴），逐句回听不可用。")

        # P3-2：清洗掉的说话人标记写 sidecar（不新增列，信息不丢）
        if prepared:
            markers = prepared.get("speakerMarkers") or []
            _write_speaker_markers(lesson_id, markers)
            if markers:
                warnings.append(
                    f"已清洗 {len(markers)} 处行首说话人标记（如「/ W:」），"
                    f"标记原文保留在课程元数据 speaker_markers.json。"
                )
    except ImportValidationError:
        if lesson_id is not None:
            _rollback_lesson(lesson_id)
        raise
    except Exception as exc:
        if lesson_id is not None:
            _rollback_lesson(lesson_id)
        raise ImportValidationError([{
            "index": 0, "line": 0, "reason": "import_failed",
            "message": f"导入失败：{exc}",
        }], message="导入失败") from exc

    # ── 8. 建成后复核去重键（多进程兜底，不是伪造返回）──
    if dedupe_after_create:
        matched = _matching_lesson_ids(audio_sha256=digest, title=clean_title)
        earlier = [item for item in matched if item < int(lesson_id)]
        if earlier:
            # 另一实例已先建成同一材料：本轮这份作废，返回先到者
            _rollback_lesson(int(lesson_id))
            winner = db.get_v2_lesson(earlier[0])
            if winner:
                return _duplicate_result(cet_materials.material_item(winner))

    refreshed = db.get_v2_lesson(lesson_id) or lesson
    material = cet_materials.material_item(refreshed)
    return {
        "duplicate": False,
        "lesson": material,
        "lessonUrl": material["lessonUrl"],
        "material": material,
        "subtitle": subtitle_info,
        "warnings": warnings,
        "supportedFormats": supported_formats(),
    }
