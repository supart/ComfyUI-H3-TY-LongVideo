"""LongVideo-owned multimodal Qwen H3 prompt generation.

This module deliberately owns only the H3 prompt pass.  Model discovery and
the llama.cpp child process stay in ``qwen_adapter`` and ``runtime`` so the
storyboard and per-segment paths share one lock and one cleanup policy.
"""
from __future__ import annotations

import gc
import logging
import math
from pathlib import Path
import re
import sys
import time

from . import ai_director
from . import qwen_adapter
from .qwen_h3_media import (
    image_content,
    image_grid_content,
    sample_indices_per_second,
    text_content,
)


LOGGER = logging.getLogger(__name__)
REFERENCE_VIDEO_FPS = 24.0
LONG_VIDEO_SYSTEM_PROMPT = """你是 LongVideo 的连续性导演和 H3 提示词格式化器。
当前请求是已确认长篇口播或音乐视频中的一个分段。根据全片设计本段，
保留已批准的主体身份、场景、景别、表演连续性、入段状态、结束状态和衔接。
时间、素材说明和分镜是必须保留的原始资料，不得改写；本模块不提供台词文字。
不要新增人物、地点、服装、道具、台词、对白、字幕、文字叠加、运镜或情节。口播或居中演讲模式中，服装和可见穿戴配饰必须逐帧保持参考图的款式、主色、色相、纹样、材质和层次；禁止换装、改色、重染、渐变色偏或新增、删除配饰。
只返回一份当前分段可直接使用的完整 MiniMax H3 提示词。
自然语言描述全部使用简体中文，品牌名与专有名词可保留原文。
H3 官方字段名、参考标签和时间语法保持原样，按要求排列字段，
不得输出台词、对白或 <d> 标签；不要解释、提问或使用 Markdown、JSON、代码块。
结果将直接写入 H3 节点的 literal prompt。"""


_BASE_H3_SECTIONS = (
    "integrated_multimodal_description", "overall_soundscape", "non_diegetic_music")
_REF_H3_SECTIONS = (
    "subject_definitions", "summary", "retention_analysis", "detailed_description",
    "overall_soundscape", "non_diegetic_music")
_H3_DIALOGUE_RE = re.compile(r"<d>\[Chinese\](.*?)</d>", re.DOTALL)
_H3_TIME_RE = re.compile(
    r"At\s+(\d{2}:\d{2}\.\d{3})\s*[-\u2013\u2014]\s*"
    r"(\d{2}:\d{2}\.\d{3})")


def _timeline_contract(prompt):
    """Extract only the approved readable timeline from the bridge request."""
    text = str(prompt or "")
    match = re.search(r"(?ms)^\[Shot 1\].*?(?=^(?:Reference material notes:|素材说明：)|\Z)", text)
    return match.group(0).strip() if match else ""


def _h3_expected_dialogue(prompt):
    return _H3_DIALOGUE_RE.findall(_timeline_contract(prompt))


def _h3_expected_times(prompt):
    return _H3_TIME_RE.findall(_timeline_contract(prompt))


def _h3_output_instruction(mode, image_count, video_count, prompt):
    expected = _h3_expected_dialogue(prompt)
    dialogue_rule = (
        "按原顺序逐字复制全部已批准台词标签，每条仅出现一次："
        + " ".join(f"<d>[Chinese]{text}</d>" for text in expected)
        if expected else
        "不要输出台词、对白或 <d> 标签；说明可见口型由参考音频驱动。")
    if mode == "ref2va":
        fields = ", ".join(f"{field}:" for field in _REF_H3_SECTIONS)
        reference_rule = (
            f"严格按此顺序使用六个字段：{fields}。"
            "保持已连接的 <Picture N> 和 <Video N> 标签对应关系。")
    else:
        fields = ", ".join(f"{field}:" for field in _BASE_H3_SECTIONS)
        alignment = {
            "i2va": "开头说明视频首帧与 <Picture 1> 对齐。",
            "fl2va": "开头说明视频首帧与 <Picture 1>、尾帧与 <Picture 2> 分别对齐。",
            "l2va": "开头说明视频尾帧与 <Picture 1> 对齐。",
        }.get(mode, "")
        reference_rule = f"严格按此顺序使用三个字段：{fields}。{alignment}"
    times = _h3_expected_times(prompt)
    time_rule = ("按原顺序保留以下段内表演时间锚点，每个仅出现一次："
                 + ", ".join(f"At {start}–{end}" for start, end in times)) if times else ""
    return (
        "H3 输出约束：只返回简体中文自然语言描述的最终提示词，协议字段与标签不得翻译。"
        f"{reference_rule} 仅使用一个连续 [Shot 1]；时间锚点表示镜头内表演节奏，不是切镜。"
        + time_rule + " " + dialogue_rule + " "
        "说话者编号如 (S1)、姿态、动作、相机和声音行为必须放在 <d> 标签外。"
        "保留确切的目标时长与参考连续性。"
    ).strip()


def _validate_h3_output(value, prompt, mode, image_count, video_count):
    """Return all contract violations so the caller can perform one repair."""
    text = str(value or "").strip()
    issues = []
    if not text:
        return ["输出为空。"]
    if not ai_director.has_chinese_description(_H3_DIALOGUE_RE.sub("", text)):
        issues.append("台词以外的自然语言描述必须使用简体中文。")
    if "```" in text:
        issues.append("不要返回 Markdown 代码块。")
    sections = _REF_H3_SECTIONS if mode == "ref2va" else _BASE_H3_SECTIONS
    positions = [text.find(section + ":") for section in sections]
    if any(position < 0 for position in positions) or positions != sorted(positions):
        issues.append("严格按要求的顺序使用 H3 字段。")
    detailed_start = (text.find("detailed_description:") if mode == "ref2va"
                      else text.find("integrated_multimodal_description:"))
    detailed = text[detailed_start:] if detailed_start >= 0 else text
    if detailed.count("[Shot 1]") != 1:
        issues.append("画面描述必须且只能包含一个 [Shot 1]。")
    if re.search(r"\[Shot\s+(?:[2-9]|[1-9]\d+)\]", detailed, re.IGNORECASE):
        issues.append("本段内不得新增镜头或切镜。")
    expected_dialogue = _h3_expected_dialogue(prompt)
    actual_dialogue = _H3_DIALOGUE_RE.findall(text)
    if actual_dialogue != expected_dialogue:
        issues.append("按原顺序逐字复制已批准中文台词标签，每条仅出现一次。")
    expected_times = _h3_expected_times(prompt)
    actual_times = _H3_TIME_RE.findall(detailed)
    if expected_times and actual_times != expected_times:
        issues.append("按原顺序保留全部已批准的段内时间锚点，每个仅出现一次。")
    if image_count or video_count:
        for index in range(1, image_count + 1):
            if f"<Picture {index}>" not in text:
                issues.append(f"保留已连接的 <Picture {index}> 参考标签。")
        for index in range(1, video_count + 1):
            if f"<Video {index}>" not in text:
                issues.append(f"保留已连接的 <Video {index}> 参考标签。")
    elif re.search(r"<(?:Picture|Video)\s+\d+>", text):
        issues.append("T2VA 输出不得虚构参考标签。")
    return list(dict.fromkeys(issues))


def _autogrow_values(inputs):
    if not inputs:
        return []

    def index(item):
        value = item[0]
        digits = ""
        for character in reversed(str(value)):
            if not character.isdigit():
                break
            digits = character + digits
        return int(digits or 0)

    return [value for _, value in sorted(inputs.items(), key=index) if value is not None]


def _validate_reference_images(images):
    if len(images) > 9:
        raise ValueError(f"At most 9 reference images are supported; got {len(images)}.")
    for index, image in enumerate(images, 1):
        count = int(image.shape[0])
        if count != 1:
            raise ValueError(
                f"Reference image {index} must contain exactly one image; got a batch of {count}."
            )


def _reference_video_details(videos, samples_per_second):
    if len(videos) > 3:
        raise ValueError(f"At most 3 reference videos are supported; got {len(videos)}.")
    details = []
    total_duration = 0.0
    for index, frames in enumerate(videos, 1):
        frame_count = int(frames.shape[0])
        if frame_count < 1:
            raise ValueError(f"Reference video {index} must contain at least one frame.")
        duration = frame_count / REFERENCE_VIDEO_FPS
        if duration > 15.0:
            raise ValueError(
                f"Reference video {index} must be at most 15 seconds at 24 fps; "
                f"got {duration:.2f}s."
            )
        total_duration += duration
        details.append((
            frames,
            frame_count,
            duration,
            sample_indices_per_second(
                frame_count, REFERENCE_VIDEO_FPS, samples_per_second),
        ))
    if total_duration > 15.0:
        raise ValueError(
            f"Reference videos may total at most 15 seconds; got {total_duration:.2f}s."
        )
    return details


def _detect_mode(image_count, video_count):
    if not 0 <= image_count <= 9:
        raise ValueError("Reference image count must be between 0 and 9.")
    if not 0 <= video_count <= 3:
        raise ValueError("Reference video count must be between 0 and 3.")
    if video_count or image_count > 2:
        return "ref2va"
    if image_count == 0:
        return "t2va"
    return None


def _mode_router_messages(prompt, image_count):
    if image_count == 1:
        choices = "i2va, l2va, ref2va"
        rules = (
            "仅在 Picture 1 明确为开场首帧时选择 i2va，明确为结束尾帧时选择 l2va。"
            "普通人物、场景、风格或构图参考选择 ref2va。"
        )
    elif image_count == 2:
        choices = "fl2va, ref2va"
        rules = (
            "Choose fl2va only when Picture 1 is clearly the opening frame and Picture 2 "
            "is clearly the ending frame, or the request explicitly asks to transition between them. "
            "Otherwise choose ref2va."
        )
    else:
        raise ValueError("Automatic image mode routing requires one or two images.")
    return [
        {
            "role": "system",
            "content": (
                "判断当前长视频分段的 MiniMax H3 图像输入模式。"
                f"仅返回一个小写标识：{choices}。不要解释。{rules}"
            ),
        },
        {"role": "user", "content": prompt},
    ]


def _parse_mode_selection(text, image_count):
    allowed = {
        1: ("i2va", "l2va", "ref2va"),
        2: ("fl2va", "ref2va"),
    }.get(image_count)
    if not allowed:
        raise ValueError("Automatic image mode routing requires one or two images.")
    normalized = str(text or "").strip().lower()
    if normalized in allowed:
        return normalized
    matches = [mode for mode in allowed
               if re.search(rf"\b{re.escape(mode)}\b", normalized)]
    return matches[0] if len(matches) == 1 else "ref2va"


def _picture_role(mode, picture_index):
    if mode == "i2va":
        return "目标视频的首帧"
    if mode == "l2va":
        return "目标视频的尾帧"
    if mode == "fl2va":
        return ("目标视频的首帧" if picture_index == 1 else "目标视频的尾帧")
    return (
        "按顺序提供的视觉参考；保留主体身份、构图、服装、场景和光照，"
        "除非已批准分镜明确要求变化"
    )


def _sampling_settings(think_mode):
    if think_mode:
        return {
            "temperature": 1.0,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0,
            "repetition_penalty": 1.0,
        }
    return {
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    }


def _user_content(prompt, duration, images, video_details, mode):
    content = [
        text_content(
            "LongVideo 单段请求：\n"
            f"{prompt}\n\n实际生成时长：{duration:.2f} 秒。\n"
            "本段属于已批准的长视频序列，优先保持全片连续性，不另行设计孤立的 15 秒镜头。"
        ),
        text_content(_h3_output_instruction(
            mode, len(images), len(video_details), prompt)),
    ]
    assets = []
    for index, image in enumerate(images, 1):
        role = _picture_role(mode, index)
        content.append(text_content(f"<Picture {index}>: {role}."))
        content.append(image_content(image))
        assets.append(f"Picture {index}={role}")

    for index, details in enumerate(video_details, 1):
        frames, frame_count, source_duration, sample_groups = details
        sampled_count = sum(len(group) for group in sample_groups)
        content.append(text_content(
            f"<Video {index}>：{frame_count} 帧，{REFERENCE_VIDEO_FPS:.3f} fps，"
            f"时长 {source_duration:.2f} 秒；已采样 {sampled_count} 帧，"
            f"按时间顺序组成 {len(sample_groups)} 张每秒拼图。"
        ))
        for second, frame_indices in enumerate(sample_groups, 1):
            timestamps = ", ".join(
                f"{frame_index / REFERENCE_VIDEO_FPS:.3f}s"
                for frame_index in frame_indices
            )
            content.append(text_content(
                f"<Video {index}> 第 {second}/{len(sample_groups)} 秒拼图。"
                f"逐行从左到右按时间顺序读取，时间点：{timestamps}。"
            ))
            content.append(image_grid_content(frames, frame_indices))
        assets.append(
            f"Video {index}={source_duration:.2f}s/{sampled_count} sampled frames")
    return content, ", ".join(assets) if assets else "none"


def _check_interrupted():
    management = sys.modules.get("comfy.model_management")
    check = getattr(management, "throw_exception_if_processing_interrupted", None)
    if callable(check):
        check()


def _unload_comfy_models():
    qwen_adapter._cleanup_comfy_memory()


def _release(binding):
    release = getattr(binding.module, "_release_node_resources", None)
    try:
        if callable(release):
            release()
        else:
            binding.server_manager.release()
    finally:
        gc.collect()
        _unload_comfy_models()
        gc.collect()


def _node(prompt, node_id):
    node = prompt.get(str(node_id))
    return node if isinstance(node, dict) else None


def _link(value):
    return value if isinstance(value, (list, tuple)) and len(value) == 2 else None


def _source_node(prompt, node_id):
    source = _node(prompt, node_id)
    if source is None:
        raise ValueError(f"参考素材上游节点 {node_id} 不存在。")
    return source


def _resolve_input_file(value):
    import folder_paths

    raw = str(value or "").strip()
    if not raw:
        raise ValueError("参考素材节点没有文件名。")
    candidate = Path(raw)
    if candidate.is_absolute() and candidate.is_file():
        return candidate.resolve()
    getter = getattr(folder_paths, "get_annotated_filepath", None)
    if callable(getter):
        try:
            path = Path(getter(raw, default_path=False)).resolve()
            if path.is_file():
                return path
        except (OSError, TypeError, ValueError):
            pass
    path = (Path(folder_paths.get_input_directory()) / raw).resolve()
    input_root = Path(folder_paths.get_input_directory()).resolve()
    if path.is_relative_to(input_root) and path.is_file():
        return path
    raise FileNotFoundError(f"参考素材文件不存在：{raw}")


def _find_file_value(prompt, node_id, *, video=False, seen=None):
    seen = set() if seen is None else seen
    node_id = str(node_id)
    if node_id in seen:
        return None
    seen.add(node_id)
    node = _source_node(prompt, node_id)
    class_type = str(node.get("class_type") or "")
    inputs = node.get("inputs") or {}
    preferred = ("video", "filename", "path", "file") if video else ("image", "filename", "path", "file")
    if class_type in {
        "LoadImage", "LoadImagePath", "VHS_LoadVideo", "VHS_LoadVideoFFmpeg",
        "VHS_LoadVideoPath", "VHS_LoadVideoFFmpegPath",
    }:
        for key in preferred:
            value = inputs.get(key)
            if isinstance(value, str) and value.strip():
                return value
    for value in inputs.values():
        link = _link(value)
        if link:
            found = _find_file_value(prompt, link[0], video=video, seen=seen)
            if found:
                return found
    return None


def _load_image(path):
    import numpy as np
    import torch
    from PIL import Image

    with Image.open(path) as source:
        image = source.convert("RGB")
        pixels = np.asarray(image, dtype=np.float32) / 255.0
    return torch.from_numpy(pixels.copy()).unsqueeze(0)


def _load_video(path):
    import numpy as np
    import torch
    from PIL import Image

    try:
        import imageio.v3 as imageio

        frames = []
        for index, frame in enumerate(imageio.imiter(path)):
            if index >= 360:
                break
            array = np.asarray(frame)[..., :3]
            if array.ndim == 3:
                frames.append(array)
        if frames:
            height, width = frames[0].shape[:2]
            normalized = []
            for frame in frames:
                if frame.shape[:2] != (height, width):
                    frame = np.asarray(Image.fromarray(frame).resize((width, height)))
                normalized.append(frame.astype(np.float32) / 255.0)
            return torch.from_numpy(np.stack(normalized, axis=0).copy())
    except Exception as error:
        LOGGER.debug("imageio video read failed for %s: %s", path, error)
    return _load_image(path)


def reference_media(prompt, h3_id):
    """Load the image/video files feeding one H3 node from the Comfy input dir."""
    h3 = _node(prompt, h3_id)
    if h3 is None:
        raise ValueError("当前工作流没有有效的 MiniMax H3 参考生成节点。")
    images, videos = [], []
    for key, value in sorted((h3.get("inputs") or {}).items()):
        link = _link(value)
        if not link:
            continue
        if str(key).startswith("ref_images.ref_image_"):
            raw = _find_file_value(prompt, link[0], video=False)
            if not raw:
                raise ValueError(f"{key} 上游不是可读取的图像文件节点。")
            images.append(_load_image(_resolve_input_file(raw)))
        elif str(key).startswith("ref_videos.ref_video_"):
            raw = _find_file_value(prompt, link[0], video=True)
            if not raw:
                raise ValueError(f"{key} 上游不是可读取的视频文件节点。")
            videos.append(_load_video(_resolve_input_file(raw)))
    return images, videos


def _chat(server, messages, config, *, max_tokens, think_mode, settings):
    return server.chat(
        messages,
        seed=config["seed"],
        max_tokens=max_tokens,
        think_mode=think_mode,
        reasoning_effort=config["reasoning_effort"],
        **settings,
    )


class QwenH3Engine:
    """One local H3 inference pass with optional resident-model reuse."""

    def __init__(self, config):
        if not isinstance(config, dict):
            raise ValueError("Qwen H3 配置必须是对象。")
        try:
            self.config = qwen_adapter.normalize_qwen_config(config)
        except qwen_adapter.QwenAdapterConfigurationError as exc:
            raise ValueError(str(exc)) from exc
        self.last_metadata = {}

    def generate(self, prompt, *, images=None, videos=None, duration=None):
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("H3 提示词不能为空。")
        duration = float(self.config["duration"] if duration is None else duration)
        if not 1 <= duration <= 60:
            raise ValueError("本段实际生成时长必须在 1 到 60 秒之间。")
        binding = qwen_adapter.discover_qwen_runtime()
        model = qwen_adapter._model_path(binding.module, self.config["llm_model"], "llm_model")
        projector = qwen_adapter._model_path(binding.module, self.config["vision_model"], "vision_model")
        if images is None or videos is None:
            raise ValueError("Qwen H3 需要由执行桥提供当前 H3 节点的参考素材。")
        _validate_reference_images(images)
        video_details = _reference_video_details(
            videos, self.config["video_sample_frames_per_sec"])
        mode = _detect_mode(len(images), len(videos))
        started = time.monotonic()
        settings = _sampling_settings(self.config["think_mode"])
        server = None
        success = False
        mode_route_usage = {}
        try:
            with binding.inference_lock:
                _check_interrupted()
                _unload_comfy_models()
                server, _loaded_new = binding.server_manager.acquire(
                    model, projector, qwen_adapter.DEFAULT_CONTEXT_SIZE)
                _check_interrupted()
                if mode is None:
                    route_config = {**self.config, "reasoning_effort": "low"}
                    mode_selection, mode_route_usage = _chat(
                        server, _mode_router_messages(prompt, len(images)),
                        route_config, max_tokens=16, think_mode=False,
                        settings=_sampling_settings(False))
                    mode = _parse_mode_selection(mode_selection, len(images))
                content, assets = _user_content(
                    prompt, duration, images, video_details, mode)
                messages = [
                    {"role": "system", "content": LONG_VIDEO_SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ]
                context = qwen_adapter._check_context(
                    server, messages, self.config["max_tokens"], self.config["think_mode"])
                result, usage = _chat(
                    server, messages, self.config,
                    max_tokens=self.config["max_tokens"],
                    think_mode=self.config["think_mode"],
                    settings=settings,
                )
                issues = _validate_h3_output(
                    result, prompt, mode, len(images), len(videos))
                repaired = False
                if issues:
                    repaired = True
                    repair_messages = [
                        *messages,
                        {"role": "assistant", "content": result},
                        {"role": "user", "content": (
                            "修复输出，仅返回完整 H3 提示词，自然语言描述使用简体中文。"
                            "保留字段顺序、一个连续 [Shot 1] 和全部已批准段内时间锚点；不要输出台词、对白、<d> 标签或字幕。"
                            "不要添加 Markdown、JSON 或解释。待修复问题："
                            + "; ".join(issues)
                        )},
                    ]
                    qwen_adapter._check_context(
                        server, repair_messages,
                        self.config["max_tokens"], self.config["think_mode"])
                    result, repair_usage = _chat(
                        server, repair_messages, self.config,
                        max_tokens=self.config["max_tokens"],
                        think_mode=self.config["think_mode"],
                        settings=settings,
                    )
                    usage = repair_usage or usage
                    remaining = _validate_h3_output(
                        result, prompt, mode, len(images), len(videos))
                    if remaining:
                        raise RuntimeError("Qwen H3 输出校验失败：" + "; ".join(remaining))
                if not isinstance(result, str) or not result.strip():
                    raise RuntimeError("Qwen 没有返回有效 H3 提示词。")
                self.last_metadata = {
                    "mode": mode,
                    "skill": "long_video_director",
                    "duration": duration,
                    "assets": assets,
                    "mode_route_usage": dict(mode_route_usage or {}),
                    "usage": dict(usage or {}),
                    "context": context,
                    "repaired": repaired,
                    "elapsed_seconds": time.monotonic() - started,
                }
                success = True
                return result.strip()
        finally:
            server = None
            if not success or self.config["force_unload_model"]:
                _release(binding)
            self.last_metadata.setdefault("elapsed_seconds", time.monotonic() - started)


def generate_h3_prompt(prompt, config, *, images, videos, duration):
    """Small functional entry point used by the execution bridge and tests."""
    engine = QwenH3Engine({**config, "duration": duration})
    result = engine.generate(
        prompt, images=images, videos=videos, duration=duration)
    generate_h3_prompt.last_metadata = engine.last_metadata
    return result
