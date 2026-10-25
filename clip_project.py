"""Create an independent, frame-aligned Shot Planner project without inference."""
import copy
import json
import math
from pathlib import Path
import shutil
import tempfile
import time
import uuid

import numpy as np
import soundfile as sf

from .core import (LOCK, audio_file, collect_reference_library, decorate, inside,
                   project_path, read_plan, read_project_transcript,
                   rebuild_speech_timeline, segmentation, waveform_peaks, write_plan)
from .controller import command, media_tool, probe_video
from .nodes import probe_source_video, source_video_path


def source_clip_bounds(duration, payload):
    try:
        start, end = float(payload["start_seconds"]), float(payload["end_seconds"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("请输入有效的截取起止时间。") from None
    if not all(math.isfinite(t) for t in (start, end)) or not 0 <= start < end <= duration:
        raise ValueError("截取范围必须在视频内，且终点大于起点。")
    total = math.floor(duration * 24 + 1e-7)
    first, last = min(total, math.floor(start * 24 + .5)), min(total, math.floor(end * 24 + .5))
    if last <= first:
        raise ValueError("截取范围对齐后不足一帧。")
    return first, last


def clip_bounds(plan, payload):
    if plan.get("project_kind") != "shot_planner":
        raise ValueError("只有 TY Shot Planner 视频项目支持截取片段。")
    if type(payload.get("revision")) is not int or payload["revision"] != plan["revision"]:
        raise ValueError("项目已变化，请刷新后重新选择截取范围。")
    return source_clip_bounds(plan["duration"], payload)


def preview_source(payload):
    source = source_video_path(payload.get("source_video"))
    metadata = probe_source_video(source)
    if math.floor(metadata["duration"] * 24 + 1e-7) < 1:
        raise ValueError("源视频不足一帧，无法截取。")
    ffmpeg = media_tool("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 FFmpeg，无法读取原音轨。")
    with tempfile.TemporaryDirectory(prefix="ty-source-wave-") as temporary:
        wav = Path(temporary) / "wave.wav"
        command([ffmpeg, "-v", "error", "-n", "-i", str(source), "-map", "0:a:0", "-vn",
                 "-ac", "1", "-ar", "8000", "-af", "apad",
                 "-t", f"{metadata['duration']:.9f}", "-c:a", "pcm_f32le", str(wav)])
        audio, _ = sf.read(wav, dtype="float32")
    return {**metadata, "waveform": {"original": waveform_peaks(
        audio, bins=min(24000, max(1, math.ceil(metadata["duration"] * 50))))}}


def clip_source(payload):
    source = source_video_path(payload.get("source_video"))
    metadata = probe_source_video(source)
    first, last = source_clip_bounds(metadata["duration"], payload)
    start, end = first / 24, last / 24
    ffmpeg = media_tool("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 FFmpeg，无法截取视频。")
    import folder_paths
    input_root = Path(folder_paths.get_input_directory()).resolve()
    clips = inside(input_root, input_root / "TYH3LongVideo" / "clips")
    clips.mkdir(parents=True, exist_ok=True)
    destination = inside(input_root, clips / uuid.uuid4().hex)
    with tempfile.TemporaryDirectory(prefix=".clip-", dir=clips) as staging:
        stored = Path(staging) / "clip.mp4"
        command([ffmpeg, "-v", "error", "-n", "-i", str(source),
                 "-map", "0:v:0", "-map", "0:a:0", "-vf",
                 f"fps=24:round=near,trim=start_frame={first}:end_frame={last},setpts=N/(24*TB)",
                 "-af", f"apad,atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS",
                 "-frames:v", str(last-first), "-r", "24", "-fps_mode", "cfr",
                 "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "320k",
                 "-t", f"{end-start:.9f}", "-movflags", "+faststart", str(stored)])
        info = probe_video(stored)
        checked = probe_source_video(stored)
        if int(info.get("nb_read_frames") or 0) != last-first or checked["source_fps"] != 24:
            raise ValueError("截取视频帧数或帧率异常，未应用到节点。")
        if abs(checked["duration"] - (end-start)) > 1/48000:
            raise ValueError("截取视频时长异常，未应用到节点。")
        Path(staging).rename(destination)
    return {"source_video": (destination / "clip.mp4").relative_to(input_root).as_posix(),
            "start_seconds": start, "end_seconds": end, "duration": end-start,
            "frames": last-first, "fps": 24}


def shifted_transcript(transcript, start, end):
    result = {}
    for key in ("segments", "words"):
        items = []
        for original in transcript.get(key) or []:
            a, b = max(start, float(original["start"])), min(end, float(original["end"]))
            if b <= a:
                continue
            item = {k: copy.deepcopy(v) for k, v in original.items() if k != "words"}
            item.update(start=a-start, end=b-start)
            if "words" in original:
                item["words"] = shifted_transcript({"words": original["words"]}, start, end)["words"]
            items.append(item)
        result[key] = items
    return result


def create_clip_project(root, project_id, payload):
    with LOCK:
        parent = read_plan(root, project_id)
        first, last = clip_bounds(parent, payload)
        original = project_path(root, project_id)
        source = inside(original, original / parent["source_video"])
        if not source.is_file():
            raise ValueError("项目源视频已丢失，无法截取。")
        library = collect_reference_library(parent, original)
        try:
            transcript, _ = read_project_transcript(original, parent)
        except ValueError:
            transcript = {"segments": [], "words": []}
    ffmpeg = media_tool("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 FFmpeg，无法截取视频。")
    start, end = first / 24, last / 24
    sr = int(parent["sample_rate"])
    begin, finish = round(start * sr), round(end * sr)
    count = finish - begin
    new_id = uuid.uuid4().hex
    # Publish segments.json only after every media file has been verified.
    with tempfile.TemporaryDirectory(prefix=".clip-", dir=root) as staging:
        build_root = Path(staging)
        directory = project_path(build_root, new_id)
        for name in ("source", "audio", "state", "refs", "takes", "cache", "work"):
            (directory / name).mkdir(parents=True, exist_ok=True)
        audio = {}
        for name in ("source.wav", "vocals.wav"):
            data, rate = sf.read(audio_file(original, name), start=begin, stop=finish,
                                 dtype="float32", always_2d=True)
            if rate != sr or len(data) != count:
                raise ValueError("项目音轨长度或采样率异常，无法精确截取。")
            sf.write(audio_file(directory, name), data, sr, subtype="FLOAT")
            audio[name] = data
        stored = directory / "source" / "source.mp4"
        command([ffmpeg, "-v", "error", "-n", "-i", str(source),
                 "-i", str(audio_file(directory, "source.wav")),
                 "-map", "0:v:0", "-map", "1:a:0", "-vf",
                 f"fps=24:round=near,trim=start_frame={first}:end_frame={last},setpts=N/(24*TB)",
                 "-frames:v", str(last-first), "-r", "24", "-fps_mode", "cfr",
                 "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "320k", "-t", f"{end-start:.9f}",
                 "-movflags", "+faststart", str(stored)])
        info = probe_video(stored)
        if int(info.get("nb_read_frames") or 0) != last-first:
            raise ValueError("截取视频帧数不足，未创建新项目。")
        for name in library:
            shutil.copy2(inside(original / "refs", original / "refs" / name), directory / "refs" / name)
        shifted = shifted_transcript(transcript, start, end)
        rows = []
        for row in parent["segments"]:
            a, b = max(begin, row["start_sample"]), min(finish, row["end_sample"])
            if b <= a or round((b-begin)/sr*24) <= round((a-begin)/sr*24):
                continue
            # Copy authored content only; jobs, old takes and approvals belong to the parent.
            item = {key: copy.deepcopy(row[key]) for key in
                    ("prompt", "prompt_kind", "material_note", "refs", "reference_slots",
                     "energy_db", "reason", "warnings", "boundary_kind", "boundary_confidence",
                     "vocal_state") if key in row}
            item.update(start_sample=a-begin, end_sample=b-begin)
            rows.append(item)
        if not rows:
            raise ValueError("截取范围没有可生成的视频帧。")
        # Absorb sub-frame boundary slivers into their adjacent retained segment.
        rows[0]["start_sample"] = 0
        for previous, current in zip(rows, rows[1:]):
            current["start_sample"] = previous["end_sample"]
        rows[-1]["end_sample"] = count
        child = {key: copy.deepcopy(parent[key]) for key in
                 ("schema", "analysis_schema", "mode", "max_seconds", "target_seconds", "director",
                  "generation_megapixels", "fixed_prompt", "source_video_input",
                  "reference_default_count", "reference_defaults") if key in parent}
        child.update(id=new_id, revision=1, created=time.time(), project_kind="shot_planner",
                     planning_method="rules", fps=24, sample_rate=sr, samples=count,
                     duration=count/sr, segments=rows, approved=False, run_status="draft",
                     reference_library=library, sample_coverage_verified=True,
                     source_video="source/source.mp4",
                     source_video_metadata={**parent["source_video_metadata"],
                                            "source_fps": 24, "duration": count/sr},
                     clip_origin={"project_id": project_id, "start_frame": first,
                                  "end_frame": last, "fps": 24,
                                  "start_seconds": start, "end_seconds": end},
                     warnings=["截取首尾为人工边界，请试听确认；不会自动开始生成。"])
        decorate(child, regenerate_prompts=False)
        rebuild_speech_timeline(rows, shifted, sr)
        _, analysis = segmentation(audio["vocals.wav"], sr, shifted, child["mode"],
                                   child["max_seconds"], child["target_seconds"],
                                   mix_audio=audio["source.wav"], return_analysis=True)
        selected = {round(row["end"], 3) for row in rows}
        for candidate in analysis["candidates"]:
            candidate["selected"] = candidate["time"] in selected or candidate["time"] == 0
        child["audio_structure"] = {"tempo_bpm": analysis["rhythm"].get("tempo_bpm"),
                                    "tempo_confidence": analysis["rhythm"].get("confidence"),
                                    "sections": analysis["sections"]}
        if not np.array_equal(np.concatenate([
                audio["source.wav"][row["start_sample"]:row["end_sample"]] for row in rows]),
                audio["source.wav"]):
            raise ValueError("截取音轨覆盖校验失败。")
        for name, value in (("analysis.json", analysis), ("transcript.json", shifted)):
            (directory / "state" / name).write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with LOCK:
            if read_plan(root, project_id)["revision"] != parent["revision"]:
                raise ValueError("截取期间原项目已修改，请刷新后重试。")
            write_plan(build_root, child)
            directory.rename(project_path(root, new_id))
    return child
