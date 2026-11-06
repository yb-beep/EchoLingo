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

import hashlib
import io
import json
import re
import shutil
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
    """字幕/文本校验失败：携带逐条错误，供前端逐行显示。"""

    def __init__(self, errors: list[dict[str, Any]], message: str = "字幕校验未通过"):
        super().__init__(message)
        self.errors = errors
        self.message = message


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


def _json_sentences(raw: Any) -> list[dict[str, Any]]:
    """从仓库句子 JSON 结构里取出句子数组（兼容多种包装）。"""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    for key in ("sentences", "items", "segments", "units"):
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def parse_sentence_json(raw_text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float | None]:
    """解析仓库句子 JSON（`.cet-qa/media/*.sentences.source.json` 同结构）。

    返回 `(entries, errors, declared_duration)`。字段兼容
    `start` / `startSec` / `start_seconds` 与 `end` / `endSec` / `end_seconds`。
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

    items = _json_sentences(parsed)
    if not items:
        return [], [{
            "index": 0,
            "line": 0,
            "reason": "no_sentences",
            "message": "句子 JSON 里没有 sentences/items/segments 数组",
        }], declared_duration

    entries: list[dict[str, Any]] = []
    for position, item in enumerate(items, start=1):
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
    }

    if fmt == "text":
        paragraphs = split_plain_text(raw_text)
        if not paragraphs:
            result["errors"] = [{
                "index": 0, "line": 0, "reason": "empty_text",
                "message": "文本文件没有可导入的内容",
            }]
            return result
        result["paragraphs"] = paragraphs
        result["sentenceCount"] = len(paragraphs)
        return result

    if fmt == "json":
        entries, errors, declared = parse_sentence_json(raw_text)
        result["durationSec"] = declared
    else:
        entries, errors = parse_srt(raw_text)

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
    """
    import tempfile

    suffix = Path(str(filename or "")).suffix.lower() or ".audio"
    handle = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    temp_path = Path(handle.name)
    try:
        handle.write(audio_bytes)
        handle.close()
        from webapp.services.v2_lessons import _probe_uploaded_media  # noqa: SLF001

        duration, _kind = _probe_uploaded_media(temp_path)
        return float(duration)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


# ── 去重 ────────────────────────────────────────────────────


def find_duplicate(*, audio_sha256: str, title: str) -> dict[str, Any] | None:
    """同一音频 SHA-256 + 同标题 → 已有材料。

    只匹配未归档、且媒体文件可解析的课程；音频哈希读不到（文件缺失）
    不参与匹配，避免把「哈希为空」的两份材料误判成重复。
    """
    from webapp.services import cet_materials  # 延迟导入，避免循环依赖

    if not audio_sha256:
        return None
    wanted_title = str(title or "").strip().lower()
    for lesson in db.list_v2_lessons(include_archived=False):
        existing_hash = cet_materials.audio_sha256(lesson)
        if not existing_hash or existing_hash != audio_sha256:
            continue
        if str(lesson.get("title") or "").strip().lower() != wanted_title:
            continue
        return cet_materials.material_item(lesson)
    return None


# ── 导入主流程 ──────────────────────────────────────────────


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

    # ── 2. 音频哈希去重（上传前就判，省一次落盘）──
    digest = sha256_bytes(audio_bytes)
    if not allow_duplicate:
        existing = find_duplicate(audio_sha256=digest, title=clean_title)
        if existing:
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

    # ── 3. 探测真实音频时长（ffprobe，临时文件，不留任何记录）──
    # 先探时长再校验，好处是**校验失败时还没有任何上传暂存**，
    # 不需要事后清理，也不会留下孤儿记录。
    duration = probe_audio_duration(audio_bytes, audio_filename) or 0.0

    # ── 4. 校验字幕（用真实音频时长做上界）──
    subtitle_info: dict[str, Any] = {
        "format": "", "timed": False, "sentenceCount": 0, "errors": [],
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
            }

    if subtitle_info["errors"]:
        # 校验失败：此时还没上传任何东西，直接拒绝，不留痕迹
        raise ImportValidationError(
            subtitle_info["errors"], message="字幕校验未通过，未导入任何内容"
        )

    # ── 5. 上传音频（复用现有上传设施；此处起才产生记录）──
    upload = lesson_service.save_media_upload(
        audio_filename, io.BytesIO(audio_bytes)
    )
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

        # ── 6. 写字幕 / 原文 ──
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
    except ImportValidationError:
        raise
    except Exception as exc:
        if lesson_id is not None:
            _rollback_lesson(lesson_id)
        raise ImportValidationError([{
            "index": 0, "line": 0, "reason": "import_failed",
            "message": f"导入失败：{exc}",
        }], message="导入失败") from exc

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
