"""Host-independent input snapshots and validation for an optional AI director.

No model, ComfyUI, network or persistence imports belong here. The caller owns
approval checks, locking and atomic project writes. ``complete`` is an injected
adapter accepting messages, max_tokens and enable_thinking keyword arguments.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import time
import uuid


SCHEMA_VERSION = 1
FILL_CONTRACT = "director-fill-zh-CN-v3-motion-lock"
MOTION_HOLD = "hold"
MOTION_MINIMAL_GESTURE = "minimal_gesture"
MOTION_VALUES = (MOTION_HOLD, MOTION_MINIMAL_GESTURE)
MOTION_LABELS = {
    MOTION_HOLD: "保持姿态",
    MOTION_MINIMAL_GESTURE: "单次轻微手势",
}
HOLD_SHOT_DESCRIPTION = (
    "保持当前构图与人物姿态，仅随确认语音进行口型、呼吸和微表情变化。"
)
HOLD_POSE = "人物居中，手臂自然放松，保持当前姿态与平稳表情。"
_MOTION_WORDS = re.compile(
    r"挥手|招手|指向|指着|手指|摊开|拍手|鼓掌|抬手|举手|挥动|触胸|按住胸|"
    r"双手|手臂.*摆|推向|拉近|点头|摇头|耸肩|前倾|后仰|转身|迈步|走向|身体.*转|"
    r"模拟上传|模拟提取|麦克风|道具|"
    r"\b(?:wave|point|pointing|clap|clapping|raise(?:s)? (?:the|her|his) hand|"
    r"open(?:s)? both hands|touch(?:es)? (?:the|her|his) chest|lean(?:s)? forward|"
    r"turn(?:s)? around|walk(?:s)?|step(?:s)?|nod(?:s|ded|ding)?|shake(?:s)? (?:the|her|his) head|"
    r"shrug(?:s|ged|ging)?|move(?:s)? the (?:arm|hand)|microphone|prop)\b",
    re.IGNORECASE,
)
_DISALLOWED_MINIMAL_GESTURE = re.compile(
    r"挥手|招手|指向|指着|拍手|鼓掌|双手.*摊|摊开.*双手|触胸|按住胸|推送|"
    r"模拟上传|模拟提取|麦克风|道具|推镜|拉镜|变焦|摇镜|平移|横移|环绕|跟拍|背景.*(?:变化|切换)|"
    r"\b(?:wave|point|clap|open(?:s|ed|ing)? both hands|touch(?:es|ed|ing)? (?:the|her|his) chest|"
    r"push(?:es|ed|ing)? (?:forward|out)|upload|extract|microphone|prop|dolly|zoom|pan|truck|"
    r"orbit|arc camera|camera (?:moves?|push(?:es)? in|pulls? back)|background (?:changes?|switches?))\b",
    re.IGNORECASE,
)
_MINIMAL_GESTURE_CATEGORIES = (
    re.compile(r"抬手|举手|\braise(?:s|d|ing)?\s+(?:(?:the|her|his)\s+)?(?:(?:right|left)\s+)?hand\b", re.IGNORECASE),
    re.compile(r"点头|\bnod(?:s|ded|ding)?\b", re.IGNORECASE),
    re.compile(r"摇头|\bshake(?:s|d|n|ing)?\s+(?:the|her|his)\s+head\b", re.IGNORECASE),
    re.compile(r"耸肩|\bshrug(?:s|ged|ging)?\b", re.IGNORECASE),
    re.compile(r"手掌|手指|手腕|手臂|\b(?:palm|finger|wrist|forearm|arm)\b", re.IGNORECASE),
    re.compile(r"前倾|后仰|转身|迈步|走向|\b(?:lean|turn|walk|step)(?:s|ed|ing)?\b", re.IGNORECASE),
)
DEFAULT_CONSTRAINTS = (
    "人物保持居中，参考背景不变，全程采用固定的中近景或中景。"
    "禁止推拉、变焦、摇移或环绕运镜。根据已确认音频的语义和节奏，"
    "口播和居中演讲默认由音频驱动口型，不把台词文字写入提示词；"
    "只允许口型、呼吸和微表情，整片只使用少量单次轻微手势。"
    "不修改时间线、参考素材和生成参数，不添加字幕或文字叠加。"
)
SYSTEM_PROMPT = """你是长视频全片分镜导演，不是最终 H3 提示词扩写器。
将全部分段作为一部连续长片理解，只填写固定槽位中的创作内容。
所有创作字段使用简体中文，品牌名和专有名词可保留原文。
只返回一个 JSON 对象，不要 Markdown：
{"global_contract":"全片连续性约束",
"segments":{"S1":{"intent":"本段意图","brief":"镜头简报",
"entry_state":"入段姿态","exit_state":"结束姿态","next_transition":"下一段衔接",
"cues":{"C0":{"shot_description":"可见画面与动作","pose":"姿态与表情","motion":"hold"}}}}}
S1 表示第 1 段，C0 表示该段提供的语音短句 C0。
包含全部分段及各段确切的短句槽位，没有短句时返回 cues:{}。
每个字段写一句简洁描述，不重复全片约束。保持跨段连续性，表演克制并贴合语义。
motion 只能填写 hold 或 minimal_gesture。口播和居中演讲默认全部使用 hold；
只有确实需要强调时才使用一个 minimal_gesture，每段最多一个，整片约每 20 秒最多一个。
hold 只允许口型、呼吸和微表情，不得写挥手、指向、摊手、拍手、触胸、移动身体、道具或镜头动作。
minimal_gesture 只能描述一个短暂、低幅度的手势，不得叠加多个动作。
不要输出时间戳、时长、台词、短句原文、camera 字段或 H3 标签。
时间、台词绑定、相机规则和短句之间的保持区间全部由本地程序管理；
后续 H3 提示词不写台词、对白、<d> 标签、字幕或其它文字叠加，口型只由音频参考驱动。
口播和居中演讲保持一个连续固定镜头，不新增切镜、运镜、参考素材或背景。
唱歌遵守提供的镜头约束。每段结束状态必须能自然衔接下一段。"""

_MODE_LABELS = {
    "singing": "唱歌",
    "speaking": "口播",
    "speech_light": "居中演讲",
}


class DirectorValidationError(ValueError):
    """An incomplete or unsafe model response that may receive one repair."""

    def __init__(self, message, *, attempts=None):
        super().__init__(message)
        self.attempts = copy.deepcopy(attempts or [])


def _json_copy(value):
    """Reject non-JSON values and non-finite numbers before a model call."""
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def snapshot_fingerprint(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def director_source_fingerprint(source):
    """Exclude replaceable AI/rule briefs so applying a plan cannot stale itself.

    Locked authored briefs and explicit final prompts remain source constraints;
    the full model input, including unlocked briefs, has a separate fingerprint.
    """
    stable_source = copy.deepcopy(source)
    for row in stable_source["segments"]:
        if not row.get("locked") and row.get("prompt_kind", "brief") != "final":
            row.pop("existing_brief", None)
    return snapshot_fingerprint(stable_source)


def _integer(value, label, minimum=0):
    if type(value) is not int or value < minimum:
        raise DirectorValidationError(f"{label} must be an integer >= {minimum}.")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise DirectorValidationError(f"{label} must be a non-empty string.")
    return value.strip()


def _seconds_text(value):
    return f"{float(value):.3f}s"


def _display_text(value):
    """Flatten authored text for the single-line storyboard message."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def format_model_message(source, camera_plan=None):
    """Format the exact human-readable timeline sent as the Qwen user message."""
    mode = _MODE_LABELS.get(source.get("mode"), _display_text(source.get("mode")) or "未指定")
    constraints = _display_text(source.get("constraints")) or "无额外要求"
    lines = [f"全片模式：{mode}", f"全片要求：{constraints}"]
    if source.get("mode") in {"speaking", "speech_light"}:
        lines.append(
            "动作策略：默认保持姿态；整片约每 20 秒最多一个单次轻微手势，"
            "每段最多一个，其余只保留口型、呼吸和微表情。"
        )
    lines.extend(["", "分段："])
    for row in source["segments"]:
        start = row["start_sample"] / source["sample_rate"]
        end = row["end_sample"] / source["sample_rate"]
        duration = row["content_duration_seconds"]
        content = _display_text(row.get("text")) or "无已确认文字"
        lines.extend([
            f"第 {row['segment_index'] + 1} 段 · {start:.3f}–{end:.3f} 秒 · {duration:.3f} 秒",
            f"内容：{content}",
        ])
        cues = row.get("speech_cues") or []
        if cues:
            lines.append("语音时间线（段内，相对本段起点）：")
            lines.extend(
                f"C{cue['cue_index']} · {cue['start_seconds']:.3f}–{cue['end_seconds']:.3f} 秒：{cue['text']}"
                for cue in cues)
        else:
            lines.append("语音时间线（段内，相对本段起点）：无已确认文字（由音频参考驱动口型，保持自然停顿）")
        material = _display_text(row.get("material_note"))
        if material:
            lines.append(f"素材说明：{material}")
        camera = (camera_plan or {}).get(f"S{row['segment_index']+1}")
        if source["mode"] == "singing" and camera:
            lines.append(f"镜头约束：{_display_text(camera)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def compact_model_input(source):
    """Return only the human-readable timeline needed by the director model."""
    segments = []
    for row in source["segments"]:
        start = row["start_sample"] / source["sample_rate"]
        end = row["end_sample"] / source["sample_rate"]
        item = {
            "segment_index": row["segment_index"],
            "segment_label": f"Segment {row['segment_index'] + 1}",
            "time_range": f"{start:.3f}-{end:.3f}s",
            "duration": _seconds_text(row["content_duration_seconds"]),
            "content": row["text"],
        }
        item["speech_cues"] = [
            {"cue_index": int(cue["cue_index"]),
             "time_range": f"{float(cue['start_seconds']):.3f}-{float(cue['end_seconds']):.3f}s",
             "text": str(cue["text"])}
            for cue in (row.get("speech_cues") or [])]
        if row["material_note"]:
            item["material_note"] = row["material_note"]
        if row["refs"]:
            item["reference_count"] = len(row["refs"])
        if row["locked"] or row["prompt_kind"] == "final":
            item["fixed_brief"] = row["existing_brief"]
            item["fixed"] = True
        segments.append(item)
    return _json_copy({
        "mode": source["mode"],
        "constraints": source["constraints"],
        "segments": segments,
    })


def build_input_snapshot(plan, *, transcript=None, analysis=None,
                         constraints=None, source_fingerprint=None,
                         reference_fingerprints=None):
    """Copy all confirmed source data, never truncate the later segments.

    ``source_fingerprint`` may be the caller's independent director-source
    fingerprint, never core.fingerprint/the video approval fingerprint (which
    includes the generated prompt). By default unlocked existing briefs are
    excluded so applying AI output cannot invalidate its own source. The full
    input fingerprint still includes every field supplied to the model.
    Raw transcript/analysis are fingerprinted locally, never sent to the model.
    Files are not read here: the caller supplies them under its project lock.
    """
    sr = _integer(plan.get("sample_rate"), "sample_rate", 1)
    total = _integer(plan.get("samples"), "samples", 1)
    fps = plan.get("fps", 24)
    if type(fps) not in (int, float) or not math.isfinite(fps) or fps <= 0:
        raise DirectorValidationError("fps must be a finite positive number.")
    rows = plan.get("segments")
    if not isinstance(rows, list) or not rows:
        raise DirectorValidationError("The confirmed timeline has no segments.")
    segments, previous = [], 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise DirectorValidationError(f"Segment {index} must be an object.")
        if row.get("index", index) != index:
            raise DirectorValidationError("Segment indices must match timeline order.")
        start = _integer(row.get("start_sample"), f"segment {index} start_sample")
        end = _integer(row.get("end_sample"), f"segment {index} end_sample", 1)
        if start != previous or end <= start or end > total:
            raise DirectorValidationError("Segments must continuously cover the source audio.")
        frames = _integer(row.get("generation_frames"), f"segment {index} generation_frames", 1)
        kind = row.get("prompt_kind", "brief")
        if kind not in {"brief", "final"}:
            raise DirectorValidationError(f"Unknown prompt_kind for segment {index}.")
        cues = []
        for cue in row.get("speech_cues") or []:
            if not isinstance(cue, dict):
                continue
            try:
                cue_start = float(cue.get("start_seconds"))
                cue_end = float(cue.get("end_seconds"))
            except (TypeError, ValueError):
                continue
            cue_text = str(cue.get("text") or "").strip()
            if (not math.isfinite(cue_start) or not math.isfinite(cue_end)
                    or cue_end <= cue_start or not cue_text):
                continue
            cues.append({"cue_index": len(cues),
                         "start_seconds": cue_start,
                         "end_seconds": cue_end,
                         "text": cue_text})
        segments.append({
            "segment_index": index, "start_sample": start, "end_sample": end,
            "content_duration_seconds": (end-start)/sr,
            "generation_duration_seconds": frames/fps,
            "generation_frames": frames, "text": str(row.get("text") or ""),
            "existing_brief": str(row.get("prompt") or ""),
            "prompt_kind": kind, "locked": bool(row.get("prompt_locked", False)),
            "material_note": str(row.get("material_note") or ""),
            "speech_cues": cues,
            "refs": list(row.get("refs") or []),
        })
        previous = end
    if previous != total:
        raise DirectorValidationError("The confirmed timeline does not cover the source audio.")
    source = _json_copy({
        "project_id": str(plan.get("id") or ""), "sample_rate": sr,
        "samples": total, "fps": fps, "mode": str(plan.get("mode") or ""),
        "constraints": DEFAULT_CONSTRAINTS if constraints is None else constraints,
        "reference_default_count": plan.get("reference_default_count"),
        "reference_fingerprints": reference_fingerprints or {},
        "segments": segments,
    })
    model_input = compact_model_input(source)
    camera_plan = {
        f"S{index+1}": str(row.get("camera") or
            "遵循已批准的唱歌运镜与景别。")
        for index, row in enumerate(rows) if source["mode"] == "singing"}
    model_message = format_model_message(source, camera_plan)
    local_hashes = {name: snapshot_fingerprint(value if value is not None else {})
                    for name, value in (("transcript", transcript), ("analysis", analysis))}
    signature = (snapshot_fingerprint({"source": director_source_fingerprint(source),
                                      "local_source_hashes": local_hashes})
                 if source_fingerprint is None
                 else _text(source_fingerprint, "source_fingerprint"))
    return {"schema_version": SCHEMA_VERSION, "source_fingerprint": signature,
             "output_contract": FILL_CONTRACT,
             "camera_plan": camera_plan,
             "input_fingerprint": snapshot_fingerprint(
                 {"source": source, "local_source_hashes": local_hashes,
                  "camera_plan": camera_plan, "output_contract": FILL_CONTRACT}),
             "model_input_fingerprint": snapshot_fingerprint(model_input),
             "model_message_fingerprint": snapshot_fingerprint(model_message),
             "local_source_hashes": local_hashes, "source": source,
             "model_input": model_input, "model_message": model_message}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DirectorValidationError(f"Duplicate JSON key: {key}.")
        result[key] = value
    return result


def _bad_constant(value):
    raise DirectorValidationError(f"Non-finite JSON value: {value}.")


def _keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise DirectorValidationError(f"{label} must contain exactly: {', '.join(expected)}.")


_SEGMENT_BASE_FIELDS = (
    "segment_index", "intent", "brief", "entry_state",
    "exit_state", "next_transition",
)
_MODEL_BEAT_FIELDS = (*_SEGMENT_BASE_FIELDS, "time_range", "beats")
_MODEL_ACTION_FIELDS = (*_SEGMENT_BASE_FIELDS, "time_range", "actions")
_CANONICAL_BEAT_FIELDS = (
    "segment_index", "start_sample", "end_sample", "intent", "brief",
    "entry_state", "exit_state", "next_transition", "beats",
)
_CANONICAL_ACTION_FIELDS = (
    "segment_index", "start_sample", "end_sample", "intent", "brief",
    "entry_state", "exit_state", "next_transition", "actions",
)
_CANONICAL_BOTH_FIELDS = (*_CANONICAL_BEAT_FIELDS, "actions")
_BEAT_FIELDS = (
    "start_seconds", "end_seconds", "speech_cue_indices",
    "shot_description", "pose", "camera", "motion",
)
_LEGACY_BEAT_FIELDS = _BEAT_FIELDS[:-1]
_TIME_RANGE_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*[-\u2013\u2014]\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*s?\s*$"
)
_ACTION_TIME_ROUNDING_TOLERANCE = 0.0005 + 1e-9


def _validate_model_time_range(value, source, index):
    if not isinstance(value, str):
        raise DirectorValidationError(f"segment {index} time_range must be text.")
    match = _TIME_RANGE_RE.fullmatch(value)
    if not match:
        raise DirectorValidationError(
            f"segment {index} time_range must look like 0.000-10.000s.")
    start, end = (float(item) for item in match.groups())
    expected_start = source["start_sample"] / source["sample_rate"]
    expected_end = source["end_sample"] / source["sample_rate"]
    if (abs(start - expected_start) > 0.002
            or abs(end - expected_end) > 0.002):
        raise DirectorValidationError(
            f"Segment {index} changed the confirmed time_range.")


def _source_cues(source):
    cues = source.get("speech_cues") or []
    return cues if isinstance(cues, list) else []


def _cue_indices(value, source, index):
    if not isinstance(value, list):
        raise DirectorValidationError(
            f"Segment {index} beat speech_cue_indices must be a list.")
    result = []
    cues = _source_cues(source)
    for cue in value:
        if type(cue) is not int or cue < 0 or cue >= len(cues):
            raise DirectorValidationError(
                f"Segment {index} references an unknown speech cue.")
        if cue in result:
            raise DirectorValidationError(
                f"Segment {index} repeats a speech cue in one beat.")
        result.append(cue)
    return result


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise DirectorValidationError(f"{label} must be a finite number.")
    return float(value)


def _beat_cues_overlap(beat, source, index):
    cues = _source_cues(source)
    for cue_index in beat["speech_cue_indices"]:
        cue = cues[cue_index]
        if (cue["end_seconds"] < beat["start_seconds"] - _ACTION_TIME_ROUNDING_TOLERANCE
                or cue["start_seconds"] > beat["end_seconds"] + _ACTION_TIME_ROUNDING_TOLERANCE):
            raise DirectorValidationError(
                f"Segment {index} beat references a cue outside its time range.")


def _motion(value, label):
    if value not in MOTION_VALUES:
        raise DirectorValidationError(
            f"{label} must be one of: {', '.join(MOTION_VALUES)}.")
    return value


def _contains_explicit_motion(value):
    return bool(_MOTION_WORDS.search(str(value or "")))


def _hold_beat(beat):
    beat["motion"] = MOTION_HOLD
    beat["shot_description"] = HOLD_SHOT_DESCRIPTION
    if _contains_explicit_motion(beat.get("pose")):
        beat["pose"] = HOLD_POSE
    return beat


def _valid_minimal_gesture(beat):
    text = f"{beat.get('shot_description', '')} {beat.get('pose', '')}"
    if _DISALLOWED_MINIMAL_GESTURE.search(text):
        return False
    return sum(bool(pattern.search(text)) for pattern in _MINIMAL_GESTURE_CATEGORIES) <= 1


def motion_budget(mode, total_duration_seconds):
    """Return the conservative visible-gesture budget for speech modes."""
    if mode not in {"speaking", "speech_light"}:
        return None
    duration = max(0.0, float(total_duration_seconds or 0.0))
    return max(1, min(4, int(duration // 20)))


def motion_policy_text(mode, total_duration_seconds=None):
    if mode not in {"speaking", "speech_light"}:
        return "遵循已批准的唱歌动作与运镜规则。"
    budget = motion_budget(mode, total_duration_seconds or 0)
    return (
        f"默认保持姿态；本段最多 1 个单次轻微手势，整片最多 {budget} 个。"
        "保持姿态时只允许口型、呼吸和微表情，不得新增手势、镜头运动、道具或背景变化。"
    )


def _apply_motion_budget(rows, mode, total_duration_seconds, enabled_segments):
    """Downgrade excess speech gestures locally instead of rerunning Qwen."""
    if mode not in {"speaking", "speech_light"}:
        return
    budget = motion_budget(mode, total_duration_seconds)
    used = 0
    for index, row in enumerate(rows):
        if index not in enabled_segments:
            continue
        segment_used = 0
        for beat in row["beats"]:
            if beat.get("motion") != MOTION_MINIMAL_GESTURE:
                continue
            if segment_used >= 1 or used >= budget:
                _hold_beat(beat)
                continue
            used += 1
            segment_used += 1


def assert_motion_policy(rows, mode, total_duration_seconds):
    """Reject authored motion edits that exceed the approved speech budget."""
    if mode not in {"speaking", "speech_light"}:
        return
    total = 0
    for index, row in enumerate(rows):
        beats = row.get("beats") if isinstance(row, dict) else None
        if not isinstance(beats, list) or not beats or not all(
                isinstance(beat, dict) and "motion" in beat for beat in beats):
            continue  # Preserve legacy drafts until they are explicitly converted.
        segment_total = 0
        for beat in beats:
            motion = _motion(beat.get("motion"), f"Segment {index} beat motion")
            if motion == MOTION_HOLD:
                if (_contains_explicit_motion(beat.get("shot_description"))
                        or _contains_explicit_motion(beat.get("pose"))):
                    raise DirectorValidationError("保持姿态的时间段不得包含新增动作。")
                continue
            if not _valid_minimal_gesture(beat):
                raise DirectorValidationError("单次轻微手势只能包含一个低幅度动作，且不得使用禁用动作、运镜或道具。")
            segment_total += 1
        if segment_total > 1:
            raise DirectorValidationError(f"第 {index + 1} 段最多只能有一个单次轻微手势。")
        total += segment_total
    budget = motion_budget(mode, total_duration_seconds)
    if total > budget:
        raise DirectorValidationError(f"全片最多只能有 {budget} 个单次轻微手势。")


def _validate_beats(beats, source, index, mode=None):
    if not isinstance(beats, list) or not beats:
        raise DirectorValidationError(f"Segment {index} needs at least one beat.")
    duration = _timeline_duration(source)
    cleaned, previous = [], 0.0
    for beat in beats:
        beat_keys = set(beat) if isinstance(beat, dict) else set()
        if beat_keys == set(_BEAT_FIELDS):
            has_motion = True
        elif beat_keys == set(_LEGACY_BEAT_FIELDS):
            has_motion = False
        else:
            raise DirectorValidationError(
                f"Segment {index} beat must contain exactly the approved timeline fields.")
        start = _number(beat["start_seconds"], f"Segment {index} beat start_seconds")
        end = _number(beat["end_seconds"], f"Segment {index} beat end_seconds")
        if start < -_ACTION_TIME_ROUNDING_TOLERANCE:
            raise DirectorValidationError(f"Segment {index} beat starts before the content timeline.")
        if abs(start - previous) <= _ACTION_TIME_ROUNDING_TOLERANCE:
            start = previous
        elif start < previous or start > previous:
            raise DirectorValidationError(
                f"Segment {index} beats must be continuous and non-overlapping.")
        if end > duration:
            if end - duration <= _ACTION_TIME_ROUNDING_TOLERANCE:
                end = duration
            else:
                raise DirectorValidationError(f"Segment {index} beat exceeds the content timeline.")
        if end <= start:
            raise DirectorValidationError(f"Segment {index} beat has an empty time range.")
        cue_indices = _cue_indices(beat["speech_cue_indices"], source, index)
        cleaned_beat = {
            "start_seconds": start,
            "end_seconds": end,
            "speech_cue_indices": cue_indices,
            "shot_description": _text(beat["shot_description"], "shot_description"),
            "pose": _text(beat["pose"], "pose"),
            "camera": _text(beat["camera"], "camera"),
        }
        if has_motion or mode in {"speaking", "speech_light"}:
            cleaned_beat["motion"] = _motion(
                beat.get("motion", MOTION_HOLD),
                f"Segment {index} beat motion")
        if has_motion and mode in {"speaking", "speech_light"}:
            if cleaned_beat["motion"] == MOTION_HOLD:
                _hold_beat(cleaned_beat)
            elif not _valid_minimal_gesture(cleaned_beat):
                _hold_beat(cleaned_beat)
        _beat_cues_overlap(cleaned_beat, source, index)
        cleaned.append(cleaned_beat)
        previous = end
    if abs(previous - duration) > _ACTION_TIME_ROUNDING_TOLERANCE:
        raise DirectorValidationError(
            f"Segment {index} beats must cover the full content duration.")
    if cleaned:
        cleaned[-1]["end_seconds"] = duration
    return cleaned


def _validate_actions(actions, source, index):
    """Keep the old action contract readable while new drafts use beats."""
    if not isinstance(actions, list) or not actions:
        raise DirectorValidationError(
            f"Segment {index} needs at least one action or natural hold.")
    cleaned, previous = [], 0.0
    duration = _timeline_duration(source)
    for action in actions:
        _keys(action, ("start_seconds", "end_seconds", "description"),
              f"Segment {index} action")
        start = _number(action["start_seconds"], f"Segment {index} action start_seconds")
        end = _number(action["end_seconds"], f"Segment {index} action end_seconds")
        if start < 0:
            raise DirectorValidationError(
                f"Segment {index} action starts before the content timeline.")
        if start < previous:
            if previous - start <= _ACTION_TIME_ROUNDING_TOLERANCE:
                start = previous
            else:
                raise DirectorValidationError(
                    f"Segment {index} action exceeds or overlaps the content timeline.")
        if end > duration:
            if end - duration <= _ACTION_TIME_ROUNDING_TOLERANCE:
                end = duration
            else:
                raise DirectorValidationError(
                    f"Segment {index} action exceeds or overlaps the content timeline.")
        if end <= start:
            raise DirectorValidationError(
                f"Segment {index} action exceeds or overlaps the content timeline.")
        cleaned.append({"start_seconds": start, "end_seconds": end,
                        "description": _text(action["description"], "action description")})
        previous = end
    return cleaned


def _cue_indices_for_interval(start, end, source):
    return [int(cue["cue_index"]) for cue in _source_cues(source)
            if cue["end_seconds"] > start + _ACTION_TIME_ROUNDING_TOLERANCE
            and cue["start_seconds"] < end - _ACTION_TIME_ROUNDING_TOLERANCE]


def _actions_to_beats(actions, source):
    """Convert a legacy action list into a full hold-covered beat timeline."""
    duration = _timeline_duration(source)
    result, cursor = [], 0.0

    def add(start, end, description):
        if end <= start:
            return
        beat = {
            "start_seconds": start, "end_seconds": end,
            "speech_cue_indices": _cue_indices_for_interval(start, end, source),
            "shot_description": description,
            "pose": "保持已批准的姿态与表情。",
            "camera": "locked-off",
        }
        if source.get("mode") in {"speaking", "speech_light"}:
            beat["motion"] = (MOTION_MINIMAL_GESTURE
                               if _contains_explicit_motion(description)
                               else MOTION_HOLD)
        result.append(beat)

    for action in actions:
        if action["start_seconds"] > cursor + _ACTION_TIME_ROUNDING_TOLERANCE:
            add(cursor, action["start_seconds"], "保持已批准的姿态与连续性状态。")
        add(action["start_seconds"], action["end_seconds"], action["description"])
        cursor = action["end_seconds"]
    if cursor < duration - _ACTION_TIME_ROUNDING_TOLERANCE:
        add(cursor, duration, "保持已批准的姿态与连续性状态。")
    elif result:
        result[-1]["end_seconds"] = duration
    return result


def _actions_from_beats(beats):
    return [{"start_seconds": beat["start_seconds"],
             "end_seconds": beat["end_seconds"],
             "description": beat["shot_description"]}
            for beat in beats]


_TIMELINE_TIME_RE = re.compile(
    r"^At\s+(\d{2}):(\d{2}\.\d{3})\s*[-\u2013\u2014]\s*"
    r"(\d{2}):(\d{2}\.\d{3})\s*$")
_TIMELINE_DIALOGUE_LINE_RE = re.compile(
    r"(?im)^\s*(?:台词|对白|dialogue)\s*[:：].*(?:\r?\n|$)")
_TIMELINE_DIALOGUE_TAG_RE = re.compile(r"<d>.*?</d>", re.IGNORECASE | re.DOTALL)


def _timeline_clock(value):
    milliseconds = max(0, int(round(float(value) * 1000)))
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds = remainder / 1000
    return f"{minutes:02d}:{seconds:06.3f}"


def _timeline_duration(source):
    if source.get("content_duration_seconds") is not None:
        return float(source["content_duration_seconds"])
    return ((int(source["end_sample"]) - int(source["start_sample"])) /
            float(source["sample_rate"]))


def strip_timeline_dialogue(value):
    """Remove spoken-text rows and H3 dialogue tags from authored timelines."""
    text = str(value or "").replace("\r\n", "\n")
    text = _TIMELINE_DIALOGUE_LINE_RE.sub("", text)
    text = _TIMELINE_DIALOGUE_TAG_RE.sub("", text)
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def render_h3_timeline(segment, source, mode="speaking"):
    """Render an approved beat plan as an editable, dialogue-free timeline."""
    if "timeline_override" in segment:
        return strip_timeline_dialogue(segment.get("timeline_override") or "")
    source = {**source, "sample_rate": source.get("sample_rate", 1)}
    if segment.get("beats"):
        raw_beats = segment["beats"]
        beats = _validate_beats(raw_beats, source, segment.get("segment_index", 0), mode)
        legacy = (mode in {"speaking", "speech_light"}
                  and not all(isinstance(item, dict) and "motion" in item
                              for item in raw_beats))
    else:
        actions = _validate_actions(segment.get("actions") or [], source,
                                    segment.get("segment_index", 0))
        beats = _actions_to_beats(actions, source)
        legacy = True
    lines = ["[Shot 1] 连续镜头"]
    if mode in {"speaking", "speech_light"}:
        lines.append("时间锚点表示表演节奏，不代表真实切镜。")
    if legacy:
        lines.append("[兼容方案] 旧版动作已转换为 H3 时间线。")
    for beat in beats:
        lines.extend([
            "",
            f"At {_timeline_clock(beat['start_seconds'])}–{_timeline_clock(beat['end_seconds'])}",
        ])
        lines.append(f"画面：{beat['shot_description']}")
        lines.append(f"姿态：{beat['pose']}")
        lines.append(f"镜头：{'固定机位' if beat['camera'] == 'locked-off' else beat['camera']}")
        if mode in {"speaking", "speech_light"}:
            lines.append(f"动作策略：{MOTION_LABELS.get(beat.get('motion'), '保持姿态')}")
    return "\n".join(lines)


def parse_h3_timeline(value, source, mode="speaking"):
    """Parse a dialogue-free timeline; legacy dialogue rows are ignored."""
    if not isinstance(value, str) or not value.strip():
        raise DirectorValidationError("H3 时间线不能为空。")
    source = {**source, "sample_rate": source.get("sample_rate", 1)}
    duration = _timeline_duration(source)
    lines = [line.strip() for line in value.replace("\r\n", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    if not lines or not re.fullmatch(r"\[Shot 1\].*", lines[0]):
        raise DirectorValidationError("H3 时间线必须从 [Shot 1] 连续镜头开始。")
    if any(re.search(r"\[Shot\s+(?!1\b)\d+\]", line, re.IGNORECASE)
           for line in lines):
        raise DirectorValidationError("口播分段只能使用一个连续 [Shot 1]。")
    at_positions = [index for index, line in enumerate(lines)
                    if _TIMELINE_TIME_RE.fullmatch(line)]
    if not at_positions:
        raise DirectorValidationError("H3 时间线缺少 At 时间区间。")
    if at_positions[0] <= 0:
        raise DirectorValidationError("H3 时间线缺少 [Shot 1] 标题。")
    allowed_header = {"[Shot 1] 连续镜头",
                      "时间锚点表示表演节奏，不代表真实切镜。",
                      "[兼容方案] 旧版动作已转换为 H3 时间线。"}
    if any(line and line not in allowed_header for line in lines[:at_positions[0]]):
        raise DirectorValidationError("H3 时间线标题格式无效。")

    beats = []
    for position, start_line in enumerate(at_positions):
        end_line = at_positions[position + 1] if position + 1 < len(at_positions) else len(lines)
        match = _TIMELINE_TIME_RE.fullmatch(lines[start_line])
        start = float(match.group(1)) * 60 + float(match.group(2))
        end = float(match.group(3)) * 60 + float(match.group(4))
        fields = {}
        for line in lines[start_line + 1:end_line]:
            if not line:
                continue
            if "：" not in line:
                raise DirectorValidationError("H3 时间线只能编辑画面、姿态、镜头和动作策略字段。")
            name, text = line.split("：", 1)
            if name in fields or name not in {"台词", "画面", "姿态", "镜头", "动作策略"}:
                raise DirectorValidationError("H3 时间线字段重复或未知。")
            if name == "台词":
                continue
            fields[name] = text.strip()
        missing = [name for name in ("画面", "姿态", "镜头") if not fields.get(name)]
        if missing:
            raise DirectorValidationError("H3 时间线缺少字段：" + "、".join(missing))
        beat = {"start_seconds": start, "end_seconds": end,
                "speech_cue_indices": [],
                "shot_description": fields["画面"],
                "pose": fields["姿态"],
                "camera": "locked-off" if fields["镜头"] == "固定机位" else fields["镜头"]}
        if mode in {"speaking", "speech_light"}:
            motion_text = fields.get("动作策略", "保持姿态")
            if motion_text in {"保持姿态", MOTION_HOLD}:
                beat["motion"] = MOTION_HOLD
            elif motion_text in {"单次轻微手势", MOTION_MINIMAL_GESTURE}:
                beat["motion"] = MOTION_MINIMAL_GESTURE
            else:
                raise DirectorValidationError("动作策略只能填写“保持姿态”或“单次轻微手势”。")
        beats.append(beat)
    return {"beats": _validate_beats(beats, source, source.get("segment_index", 0), mode),
            "actions": _actions_from_beats(beats)}


def validate_director_result(result, snapshot):
    """Validate model or legacy canonical output into one local result shape."""
    _keys(result, ("schema_version", "global_contract", "segments"), "Director result")
    if type(result["schema_version"]) is not int or result["schema_version"] != SCHEMA_VERSION:
        raise DirectorValidationError("Unsupported director schema_version.")
    contract = _text(result["global_contract"], "global_contract")
    expected = snapshot["source"]["segments"]
    rows = result["segments"]
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise DirectorValidationError(f"Expected exactly {len(expected)} segments.")
    ordered = {}
    motion_enabled = set()
    sr = snapshot["source"]["sample_rate"]
    mode = snapshot["source"].get("mode")
    for row in rows:
        is_model = "time_range" in row
        has_beats = "beats" in row
        has_actions = "actions" in row
        if is_model and has_beats:
            fields = _MODEL_BEAT_FIELDS
        elif is_model and has_actions:
            fields = _MODEL_ACTION_FIELDS
        elif not is_model and has_beats and has_actions:
            fields = _CANONICAL_BOTH_FIELDS
        elif not is_model and has_beats:
            fields = _CANONICAL_BEAT_FIELDS
        else:
            fields = _CANONICAL_ACTION_FIELDS
        _keys(row, fields, "Director segment")
        index = _integer(row["segment_index"], "segment_index")
        if index >= len(expected):
            raise DirectorValidationError(f"Unknown segment_index: {index}.")
        if index in ordered:
            raise DirectorValidationError(f"Duplicate segment_index: {index}.")
        source = expected[index]
        source_with_context = {**source, "sample_rate": sr, "mode": mode}
        if "time_range" in row:
            _validate_model_time_range(row["time_range"], source_with_context, index)
        else:
            for name in ("start_sample", "end_sample"):
                if _integer(row[name], f"segment {index} {name}") != source[name]:
                    raise DirectorValidationError(f"Segment {index} changed confirmed {name}.")
        cleaned = {name: source[name] for name in
                   ("segment_index", "start_sample", "end_sample")}
        for name in ("intent", "brief", "entry_state", "exit_state", "next_transition"):
            cleaned[name] = _text(row[name], f"segment {index} {name}")
        if has_beats:
            raw_motion = all(isinstance(item, dict) and "motion" in item
                              for item in row["beats"])
            beats = _validate_beats(row["beats"], source_with_context, index, mode)
            if has_actions:
                # Validate the legacy alias too so old approval payloads cannot
                # smuggle a different timeline beside the visible beats.
                _validate_actions(row["actions"], source_with_context, index)
            actions = _actions_from_beats(beats)
            if raw_motion:
                motion_enabled.add(index)
        else:
            actions = _validate_actions(row["actions"], source_with_context, index)
            beats = _actions_to_beats(actions, source_with_context)
        cleaned["beats"] = beats
        cleaned["actions"] = actions
        ordered[index] = cleaned
    result_rows = [ordered[index] for index in range(len(expected))]
    _apply_motion_budget(
        result_rows, mode,
        sum(_timeline_duration({**row, "sample_rate": sr}) for row in expected),
        motion_enabled,
    )
    for row in result_rows:
        row["actions"] = _actions_from_beats(row["beats"])
    return {"schema_version": SCHEMA_VERSION, "global_contract": contract,
            "segments": result_rows}


def parse_director_response(response, snapshot):
    """Parse strict JSON, rejecting fences, trailing prose and duplicate keys."""
    if not isinstance(response, str):
        raise DirectorValidationError("Director response must be JSON text.")
    try:
        result = json.loads(response, object_pairs_hook=_unique_object,
                            parse_constant=_bad_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DirectorValidationError(f"Director response is not valid JSON: {exc}") from exc
    return validate_director_result(result, snapshot)


def draft_for_storage(snapshot, result, *, attempts=None, model_config=None,
                      current_source_fingerprint=None, created_at=None):
    """Return persistable draft fields without applying prompts or approving them.

    The caller writes this under its own project lock. A source conflict remains
    a retained ``stale`` draft, never an auto-applied partial director result.
    """
    validated = validate_director_result(result, snapshot)
    source_signature = snapshot["source_fingerprint"]
    stale = (current_source_fingerprint is not None
             and current_source_fingerprint != source_signature)
    return _json_copy({
        "schema_version": SCHEMA_VERSION, "id": uuid.uuid4().hex,
        "status": "stale" if stale else "draft", "approved": False,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
        "source_fingerprint": source_signature,
        "input_fingerprint": snapshot["input_fingerprint"],
        "input_snapshot": snapshot, "result": validated,
        "model_config": model_config or {}, "attempts": attempts or [],
    })


_FILL_SEGMENT_FIELDS = ("intent", "brief", "entry_state", "exit_state", "next_transition")


def _object_schema(properties):
    return {"type": "object", "properties": properties,
            "required": list(properties), "additionalProperties": False}


def director_fill_schema(snapshot):
    """Constrain keys, not model-authored timing; empty cue maps stay empty."""
    text = {"type": "string", "minLength": 1}
    motion = {"type": "string", "enum": list(MOTION_VALUES)}
    speech_mode = snapshot["source"].get("mode") in {"speaking", "speech_light"}
    segments = {}
    for row in snapshot["source"]["segments"]:
        cue_fields = {"shot_description": dict(text), "pose": dict(text)}
        if speech_mode:
            cue_fields["motion"] = dict(motion)
        cues = {f"C{cue['cue_index']}": _object_schema(cue_fields)
                for cue in _source_cues(row)}
        segments[f"S{row['segment_index']+1}"] = _object_schema({
            **{name: dict(text) for name in _FILL_SEGMENT_FIELDS},
            "cues": _object_schema(cues),
        })
    return _object_schema({"global_contract": dict(text),
                           "segments": _object_schema(segments)})


def fixed_director_slots(snapshot):
    """Build exact cue intervals and local holds before spending any inference."""
    result = {}
    for row in snapshot["source"]["segments"]:
        duration = _timeline_duration({**row, "sample_rate": snapshot["source"]["sample_rate"]})
        cursor, slots = 0.0, []
        for index, cue in enumerate(_source_cues(row)):
            start = _number(cue["start_seconds"], "cue start")
            end = _number(cue["end_seconds"], "cue end")
            if (cue["cue_index"] != index or start < -_ACTION_TIME_ROUNDING_TOLERANCE
                    or start < cursor - _ACTION_TIME_ROUNDING_TOLERANCE
                    or end > duration + _ACTION_TIME_ROUNDING_TOLERANCE):
                raise DirectorValidationError("Confirmed speech cues overlap or exceed the segment.")
            start = cursor if abs(start-cursor) <= _ACTION_TIME_ROUNDING_TOLERANCE else start
            end = min(end, duration)
            if end <= start:
                raise DirectorValidationError("Confirmed speech cue has an empty interval.")
            if start > cursor:
                slots.append({"key": None, "start_seconds": cursor, "end_seconds": start,
                              "speech_cue_indices": []})
            slots.append({"key": f"C{index}", "start_seconds": start, "end_seconds": end,
                          "speech_cue_indices": [index]})
            cursor = end
        if cursor < duration:
            if slots and duration-cursor <= _ACTION_TIME_ROUNDING_TOLERANCE:
                slots[-1]["end_seconds"] = duration
            else:
                slots.append({"key": None, "start_seconds": cursor, "end_seconds": duration,
                              "speech_cue_indices": []})
        result[f"S{row['segment_index']+1}"] = slots
    return result


def _parse_fill_json(response):
    if not isinstance(response, str):
        raise DirectorValidationError("Director response must be JSON text.")
    try:
        return json.loads(response, object_pairs_hook=_unique_object, parse_constant=_bad_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DirectorValidationError(
            f"Director JSON is incomplete or invalid; no whole-film retry: {exc}") from exc


def has_chinese_description(value):
    """Require Chinese prose, while allowing embedded names and protocol tokens."""
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", str(value)))


def _fill_fields(value, schema, path=()):
    """Collect only known string slots; structural corruption is not a rewrite request."""
    valid, invalid = {}, []
    if schema.get("type") == "string" and schema.get("enum"):
        if isinstance(value, str) and value in schema["enum"]:
            valid[path] = value
        else:
            invalid.append(path)
        return valid, invalid
    if schema["type"] == "string":
        if isinstance(value, str) and has_chinese_description(value) and not re.search(
                r"<\/?d>|\[Shot\s+\d", value, re.IGNORECASE):
            valid[path] = value.strip()
        else:
            invalid.append(path)
        return valid, invalid
    if (not isinstance(value, dict) or set(value) - set(schema["properties"])
            or any(child["type"] == "object" and key not in value
                   for key, child in schema["properties"].items())):
        raise DirectorValidationError(
            f"Invalid fixed-slot structure at {'.'.join(path) or 'root'}; no whole-film retry.")
    for key, child in schema["properties"].items():
        found, missing = _fill_fields(value.get(key), child, (*path, key))
        valid.update(found)
        invalid.extend(missing)
    return valid, invalid


def _schema_at_path(schema, path):
    current = schema
    for key in path:
        if current.get("type") != "object":
            return {"type": "string", "minLength": 1}
        current = current.get("properties", {}).get(key)
        if not isinstance(current, dict):
            return {"type": "string", "minLength": 1}
    return copy.deepcopy(current)


def _assemble_fill(snapshot, fields, slots):
    rows = []
    for source in snapshot["source"]["segments"]:
        segment_key = f"S{source['segment_index']+1}"
        prefix = ("segments", segment_key)
        row = {name: source[name] for name in ("segment_index", "start_sample", "end_sample")}
        row.update({name: fields[(*prefix, name)] for name in _FILL_SEGMENT_FIELDS})
        camera = (snapshot.get("camera_plan", {}).get(segment_key) or
                  "遵循已批准的唱歌运镜与景别。"
                  if snapshot["source"]["mode"] == "singing" else "locked-off")
        speech_mode = snapshot["source"].get("mode") in {"speaking", "speech_light"}
        beats = []
        for slot in slots[segment_key]:
            beat = {key: copy.deepcopy(value) for key, value in slot.items() if key != "key"}
            if slot["key"] is None:
                beat.update(
                    shot_description="保持当前姿态与画面连续性，继续由音频参考驱动口型，不虚构台词。",
                    pose=beats[-1]["pose"] if beats else row["entry_state"])
            else:
                field_names = (("shot_description", "pose", "motion") if speech_mode
                               else ("shot_description", "pose"))
                beat.update({name: fields[(*prefix, "cues", slot["key"], name)]
                             for name in field_names})
            if speech_mode:
                beat.setdefault("motion", MOTION_HOLD)
            beat["camera"] = camera
            beats.append(beat)
        row["beats"] = beats
        rows.append(row)
    return validate_director_result({
        "schema_version": SCHEMA_VERSION, "global_contract": fields[("global_contract",)],
        "segments": rows,
    }, snapshot)


def generate_director_draft(snapshot, complete, *, model_config=None,
                            max_output_tokens=8192, created_at=None, on_phase=None,
                            enable_thinking=False):
    """Fill a host-owned skeleton once; only missing creative fields may be retried."""
    _integer(max_output_tokens, "max_output_tokens", 1)
    if max_output_tokens > 32768:
        raise DirectorValidationError("max_output_tokens must be <= 32768.")
    snapshot = _json_copy(snapshot)
    slots = fixed_director_slots(snapshot)
    schema = director_fill_schema(snapshot)
    snapshot["output_contract"] = FILL_CONTRACT
    model_message = snapshot.get("model_message")
    if not isinstance(model_message, str) or not model_message.strip():
        model_message = format_model_message(snapshot["source"], snapshot.get("camera_plan"))
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": model_message},
    ]
    attempts = []
    def call(messages, response_schema, kind):
        if on_phase:
            on_phase("repairing" if kind == "repair_fields" else "preparing")
        response = complete(messages=copy.deepcopy(messages),
                            max_tokens=max_output_tokens, enable_thinking=enable_thinking,
                            response_schema=response_schema)
        record = {"kind": kind, "messages": copy.deepcopy(messages), "response": response,
                  "response_schema": copy.deepcopy(response_schema)}
        attempts.append(record)
        if on_phase:
            on_phase("validating")
        return record

    try:
        record = call(messages, schema, "fill")
        started = time.monotonic()
        try:
            fields, missing = _fill_fields(_parse_fill_json(record["response"]), schema)
        finally:
            record["validation_seconds"] = time.monotonic() - started
        if missing:
            record["validation_error"] = "创作字段为空、含禁用标签或缺少中文叙述：" + ", ".join(map(".".join, missing))
            if not fields:
                raise DirectorValidationError("没有可用的中文创作字段；保留旧方案，不自动重写全片。")
            repair_schema = _object_schema({
                ".".join(path): _schema_at_path(schema, path) for path in missing})
            repair_messages = [
                *messages,
                {"role": "assistant", "content": record["response"]},
                {"role": "user", "content": (
                    "仅补填下列无效创作字段，返回扁平 JSON 对象，键为下列完整点分路径。"
                    "文字值使用简体中文，简洁描述，不包含台词或 H3 标签；"
                    "动作策略字段只能填写 hold 或 minimal_gesture。"
                    "不得返回全片或修改任何已通过字段：\n"
                    + "\n".join(repair_schema["required"]))},
            ]
            record = call(repair_messages, repair_schema, "repair_fields")
            started = time.monotonic()
            try:
                repaired, remaining = _fill_fields(
                    _parse_fill_json(record["response"]), repair_schema)
                if remaining:
                    raise DirectorValidationError("Director local field repair failed.")
                fields.update({path: repaired[(".".join(path),)] for path in missing})
            finally:
                record["validation_seconds"] = time.monotonic() - started
        started = time.monotonic()
        try:
            result = _assemble_fill(snapshot, fields, slots)
            draft = draft_for_storage(snapshot, result, model_config=model_config,
                                      created_at=created_at)
        finally:
            record["validation_seconds"] += time.monotonic() - started
        draft["attempts"] = copy.deepcopy(attempts)
        return draft
    except DirectorValidationError as exc:
        if attempts:
            attempts[-1]["validation_error"] = str(exc)
        exc.attempts = copy.deepcopy(attempts)
        raise
