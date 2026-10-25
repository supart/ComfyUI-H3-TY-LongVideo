"""Optional director review and text-only queueing, with no inference imports."""

import copy
from datetime import datetime, timezone
import json
import re
import sys
import time
import uuid

from . import ai_director, director_task, qwen_adapter
from .core import (LOCK, brief_text, fingerprint, normalize_reference_names,
                   project_path, read_plan, rebuild_speech_timeline,
                   reference_directory, request_regeneration, state_file, write_plan)


DIRECTOR_QWEN_FIELDS = (
    "llm_model", "vision_model", "seed", "max_tokens",
    "think_mode", "reasoning_effort",
)
EXTERNAL_QWEN_FIELDS = (
    "intent", "brief", "entry_state", "exit_state", "next_transition", "timeline",
)
DEFAULT_EXTERNAL_QWEN_FIELDS = (
    "brief", "entry_state", "exit_state", "timeline",
)


def external_qwen_fields(row):
    value = row.get("external_qwen_fields")
    if value is None:
        return list(DEFAULT_EXTERNAL_QWEN_FIELDS)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("外部 Qwen 输出项必须是字符串列表。")
    if len(value) != len(set(value)):
        raise ValueError("外部 Qwen 输出项不能重复。")
    unknown = set(value) - set(EXTERNAL_QWEN_FIELDS)
    if unknown:
        raise ValueError("未知的外部 Qwen 输出项：" + "、".join(sorted(unknown)))
    return [name for name in EXTERNAL_QWEN_FIELDS if name in value]


def _director_qwen_config(config):
    """Keep only the Qwen settings that affect whole-film storyboard output."""
    try:
        normalized = qwen_adapter.normalize_qwen_config(config)
    except qwen_adapter.QwenAdapterConfigurationError as exc:
        raise ValueError(str(exc)) from exc
    return {key: copy.deepcopy(normalized[key]) for key in DIRECTOR_QWEN_FIELDS}


def _config_from_legacy_source(prompt, source_id, max_tokens):
    selected = prompt.get(source_id) if isinstance(prompt, dict) else None
    if not isinstance(selected, dict) or selected.get("class_type") != qwen_adapter.NODE_ID:
        raise ValueError("旧项目指定的 Qwen 来源节点无效，请重新打开分段审核面板。")
    inputs = selected.get("inputs", {})
    if any(not isinstance(inputs.get(field), str) or not inputs[field].strip()
           for field in ("llm_model", "vision_model")):
        raise ValueError("旧项目 Qwen 模型必须为明确文件名，暂不支持未解析的模型连线。")
    config = {
        "skill": "long_video_director",
        "duration": 10.0,
        "llm_model": inputs["llm_model"].strip(),
        "vision_model": inputs["vision_model"].strip(),
        "think_mode": False,
        "reasoning_effort": "medium",
        "seed": inputs.get("seed", 0),
        "max_tokens": max_tokens,
        "video_sample_frames_per_sec": 2,
        "force_unload_model": True,
    }
    return _director_qwen_config(config)


def _qwen_config_for_preview(plan, payload, source_id, prompt, max_tokens):
    if source_id:
        return _config_from_legacy_source(prompt, source_id, max_tokens)
    raw = payload.get("qwen_config")
    if raw is None:
        stored = (plan.get("director_base_qwen_config")
                  or plan.get("qwen_config")
                  or plan.get("director_qwen_config"))
        raw = stored if isinstance(stored, dict) and stored.get("llm_model") else None
    if raw is None:
        try:
            raw = qwen_adapter.default_model_config()
        except qwen_adapter.QwenAdapterUnavailable as exc:
            raise ValueError(
                "LongVideo 独立 Qwen 分镜运行时或共享模型不可用；"
                "请运行 install_runtime.py，并检查 ComfyUI/models/LLM/Qwen3.8。"
            ) from exc
    merged = dict(raw)
    merged["max_tokens"] = max_tokens
    return _director_qwen_config(merged)


def source_snapshot(root, plan):
    """Read the same confirmed source as the task, also while a video is running."""
    directory = project_path(root, plan["id"])
    candidate = copy.deepcopy(plan)
    transcript = director_task._read_state(directory, "transcript.json")
    analysis = director_task._read_state(directory, "analysis.json")
    rebuild_speech_timeline(candidate["segments"], transcript, candidate["sample_rate"])
    refs = {}
    for row in candidate["segments"]:
        for name in normalize_reference_names(row.get("refs"), directory, allow_blank=True):
            if not name:
                continue
            if name not in refs:
                refs[name] = director_task._file_digest(reference_directory(directory)/name)
    return ai_director.build_input_snapshot(
        candidate, transcript=transcript, analysis=analysis,
        constraints=candidate.get("director_constraints"), reference_fingerprints=refs)


def _speech_source_rows(root, plan):
    """Return current segment rows with local speech cues for execution/review."""
    candidate = copy.deepcopy(plan)
    directory = project_path(root, plan["id"])
    try:
        transcript = director_task._read_state(directory, "transcript.json")
    except (OSError, ValueError, json.JSONDecodeError):
        transcript = {"segments": [], "words": []}
    rebuild_speech_timeline(candidate["segments"], transcript, candidate["sample_rate"])
    return candidate["segments"]


def _project_duration_seconds(plan):
    """Use the full source duration when describing the global motion budget."""
    try:
        sample_rate = float(plan["sample_rate"])
        samples = float(plan["samples"])
        if sample_rate > 0 and samples >= 0:
            return samples / sample_rate
    except (KeyError, TypeError, ValueError):
        pass
    try:
        sample_rate = float(plan["sample_rate"])
        end_sample = max(float(row["end_sample"]) for row in plan.get("segments", []))
        if sample_rate > 0 and end_sample >= 0:
            return end_sample / sample_rate
    except (KeyError, TypeError, ValueError):
        pass
    return max(0.0, float(plan.get("duration") or 0.0))


def _server():
    return getattr(getattr(sys.modules.get("server"), "PromptServer", None), "instance", None)


def _job_status(plan, server=None):
    job = copy.deepcopy(plan.get("director_job"))
    if not job:
        return None
    server = server or _server()
    if server is None:
        return job
    running, waiting = server.prompt_queue.get_current_queue()
    pid = job.get("prompt_id")
    terminal = job.get("status") in {"completed", "failed", "interrupted"}
    if terminal and job.get("finished_at"):
        pass  # A persisted terminal result survives history eviction/restarts.
    elif any(item[1] == pid for item in running) and not terminal:
        job["status"] = "running"
        if job.get("phase") in {None, "queued"}:
            job["phase"] = "running"
    elif any(item[1] == pid for item in waiting) and not terminal:
        job["status"] = "queued"
    else:
        history = server.prompt_queue.get_history(prompt_id=pid).get(pid)
        if history:
            succeeded = history.get("status", {}).get("status_str") == "success"
            job["status"] = "completed" if succeeded else "failed"
            for name, data in history.get("status", {}).get("messages", []):
                stamp = data.get("timestamp")
                if isinstance(stamp, (int, float)):
                    at = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
                    if name == "execution_start":
                        job.setdefault("started_at", at)
                    elif name in {"execution_error", "execution_success", "execution_interrupted"}:
                        job.setdefault("finished_at", at)
                if name == "execution_error":
                    job["error"] = str(data.get("exception_message") or "导演任务失败。").strip()
                    job["error_type"] = str(data.get("exception_type") or "")
                elif name == "execution_interrupted":
                    job.update(status="interrupted", error="导演任务已中断；原方案已保留。")
            if not succeeded:
                job.setdefault("error", "导演任务失败；详细记录已不可用，原方案已保留。")
            job["phase"] = job["status"]
        elif not terminal:
            job["status"] = "interrupted"
            job["phase"] = "interrupted"
            job["error"] = "导演任务已不在队列或历史中，状态不可恢复；原方案已保留，请重新规划。"
        if job.get("status") in {"completed", "failed", "interrupted"}:
            job.setdefault("recovered_at", datetime.now(timezone.utc).isoformat())
    try:
        start = datetime.fromisoformat(job.get("started_at") or job["queued_at"]).timestamp()
        end = (datetime.fromisoformat(job["finished_at"]).timestamp()
               if job.get("finished_at") else time.time())
        if job.get("status") not in {"completed", "failed", "interrupted"} or job.get("finished_at"):
            job["elapsed_seconds"] = max(0, end - start)
    except (KeyError, ValueError, TypeError):
        pass
    return job


def _assert_idle(plan, server=None, *, allow_video_running=False):
    from . import controller
    video_busy = plan.get("run_status") in director_task.BUSY_STATUSES or plan["id"] in controller.TASKS
    if video_busy and not allow_video_running:
        raise ValueError("当前项目仍在生成或合成，请停止后再修改导演方案。")
    job = _job_status(plan, server)
    if job and job.get("status") in {"queued", "running", "validating"}:
        raise ValueError("当前项目的导演任务仍在排队或运行，请等待完成。")
    server = server or _server()
    if server is not None:
        running, waiting = server.prompt_queue.get_current_queue()
        for item in running + waiting:
            if any(node.get("class_type") == "TY_H3LVDirectorTask"
                   and node.get("inputs", {}).get("project_id") == plan["id"]
                   for node in item[2].values()):
                raise ValueError("当前项目已有导演任务，不能重复提交。")


def _revision(plan, payload):
    if type(payload.get("revision")) is not int or payload["revision"] != plan["revision"]:
        raise ValueError("方案已被另一个窗口修改，请刷新后重试。")


def _cut_approval(plan):
    if not plan.get("approved") or plan.get("approved_fingerprint") != fingerprint(plan):
        raise ValueError("请先保存并确认当前切分，再审核导演方案。")


def _draft(directory):
    path = state_file(directory, "director_draft.json")
    return director_task._read_state(directory, path.name) if path.is_file() else None


def _approval_current(root, plan):
    approval = plan.get("director_approval")
    if not isinstance(approval, dict) or not isinstance(plan.get("approved_director"), dict):
        return False
    draft = _draft(project_path(root, plan["id"]))
    if not draft or draft.get("id") != approval.get("draft_id") or draft.get("status") != "draft":
        return False
    if approval.get("review_fingerprint") != fingerprint(plan):
        return False
    if approval.get("result_fingerprint") != ai_director.snapshot_fingerprint(plan["approved_director"]):
        return False
    if approval.get("qwen_config") != plan.get("director_qwen_config"):
        return False
    return approval.get("source_fingerprint") == source_snapshot(root, plan)["source_fingerprint"]


def adopt_manual_prompt_edits(root, plan):
    """Keep a Qwen approval usable after an authored brief-only edit.

    Unlocked segment briefs are intentionally excluded from the director source
    fingerprint.  Once the user saves and confirms that edit, refresh only the
    review fingerprint so video generation can use the new authored prompt
    without pretending the Qwen draft itself changed.
    """
    if plan.get("planning_method", "rules") != "qwen":
        return False
    approval = plan.get("director_approval")
    approved = plan.get("approved_director")
    if not isinstance(approval, dict) or not isinstance(approved, dict):
        return False
    draft = _draft(project_path(root, plan["id"]))
    if (not draft or draft.get("status") != "draft"
            or draft.get("id") != approval.get("draft_id")
            or approval.get("result_fingerprint") != ai_director.snapshot_fingerprint(approved)
            or approval.get("qwen_config") != plan.get("director_qwen_config")):
        return False
    if approval.get("source_fingerprint") != source_snapshot(root, plan)["source_fingerprint"]:
        return False
    current = fingerprint(plan)
    if approval.get("review_fingerprint") == current:
        return False
    approval["review_fingerprint"] = current
    return True


def director_state(root, project_id, server=None):
    with LOCK:
        plan = read_plan(root, project_id)
        draft = _draft(project_path(root, project_id))
        job = _job_status(plan, server)
        stored_job = {key: value for key, value in job.items()
                      if key != "elapsed_seconds"} if job else None
        if stored_job and stored_job != plan.get("director_job"):
            # Status persistence is independent of the content/approval revision.
            plan["director_job"] = stored_job
            write_plan(root, plan)
    # Runtime discovery can briefly use the Qwen lock. Never hold core.LOCK
    # while doing so: the worker reports phases by acquiring core.LOCK.
    result = {
        "planning_method": plan.get("planning_method", "rules"),
        "constraints": plan.get("director_constraints", ai_director.DEFAULT_CONSTRAINTS),
        "draft": draft, "approval": copy.deepcopy(plan.get("director_approval")),
        "stale": False, "qwen_available": False, "revision": plan["revision"],
        "job": job,
        "reference_hash_scope": "project_refs_only",
    }
    try:
        result["qwen_available"] = qwen_adapter.discover_qwen_runtime() is not None
    except qwen_adapter.QwenAdapterUnavailable as exc:
        result["error"] = str(exc)
    if plan.get("planning_method", "rules") == "qwen":
        try:
            current = source_snapshot(root, plan)["source_fingerprint"]
            current_approval = result["approval"] and _approval_current(root, plan)
            adopted = (current_approval and draft
                       and result["approval"].get("draft_id") == draft.get("id"))
            result["stale"] = bool(not adopted and (
                (draft.get("status") != "draft" or draft.get("source_fingerprint") != current)
                if draft else (result["approval"] and not current_approval)))
        except (ValueError, OSError) as exc:
            result.update(stale=True, error=str(exc))
    return result


def configure_director(root, project_id, payload, server=None):
    method = payload.get("planning_method")
    constraints = payload.get("constraints")
    if method not in {"rules", "qwen"}:
        raise ValueError("planning_method 必须是 rules 或 qwen。")
    if not isinstance(constraints, str) or not constraints.strip():
        raise ValueError("请填写完整的全片约束。")
    constraints = constraints.strip()
    with LOCK:
        plan = read_plan(root, project_id)
        _revision(plan, payload)
        _assert_idle(plan, server)
        changed = (plan.get("planning_method", "rules") != method
                   or plan.get("director_constraints", "") != constraints)
        if changed:
            reason = "全片导演规划设置已修改"
            for row in plan["segments"]:
                request_regeneration(row, reason)
            plan["full_regeneration_pending"] = True
            plan["full_regeneration_reason"] = reason
            if plan.get("final_video"):
                plan["final_stale"] = True
        plan.update(planning_method=method, director_constraints=constraints,
                    revision=plan["revision"]+1)
        write_plan(root, plan)
    return plan


def _legacy_regeneration_ready(plan):
    """Allow an idle legacy project to reuse its last approved Qwen input."""
    rows = [row for row in (plan.get("segments") or [])
            if row.get("needs_regeneration")]
    return bool(rows) and all(
        row.get("needs_regeneration")
        and row.get("job", {}).get("status") == "completed"
        and row.get("job", {}).get("video")
        and isinstance(row.get("job", {}).get("director_execution", {}).get("qwen_input"), str)
        and row["job"]["director_execution"]["qwen_input"].strip()
        for row in rows
    )


def require_director_approval(root, plan, allow_legacy_regeneration=False):
    """Shared gate for queue/controller and direct canvas execution."""
    if plan.get("planning_method", "rules") == "rules":
        return
    if plan.get("planning_method") != "qwen":
        raise ValueError("未知的导演规划方式。")
    _cut_approval(plan)
    job = _job_status(plan)
    if job and job.get("status") in {"queued", "running", "validating"}:
        raise ValueError("全片导演任务尚未完成，不能开始视频生成。")
    if not _approval_current(root, plan):
        if allow_legacy_regeneration and _legacy_regeneration_ready(plan):
            return
        raise ValueError("请先审核并确认当前 AI 导演方案；旧方案缺失或已过期。")


def effective_segment_brief(root, plan, row, allow_legacy_regeneration=False):
    if row.get("prompt_kind") == "final":
        return row["prompt"]
    if (allow_legacy_regeneration
            and plan.get("planning_method", "rules") == "qwen"
            and not _approval_current(root, plan)):
        previous = row.get("replacement_previous_job") or row.get("job") or {}
        request = (previous.get("director_execution") or {}).get("qwen_input")
        if isinstance(request, str) and request.strip():
            return request
    require_director_approval(root, plan, allow_legacy_regeneration=allow_legacy_regeneration)
    if plan.get("planning_method", "rules") == "rules":
        # The external Qwen pass needs the declared picture roles as well as
        # the camera brief. Keep legacy prompt bytes unchanged when there is
        # no note, but preserve the optional material note for ordinary briefs.
        return brief_text(row) if str(row.get("material_note") or "").strip() else row["prompt"]
    result = plan["approved_director"]
    segment = result["segments"][row["index"]]
    source_row = _speech_source_rows(root, plan)[row["index"]]
    source_row.update(segment_index=row["index"], sample_rate=plan["sample_rate"])
    duration = (row["end_sample"]-row["start_sample"])/plan["sample_rate"]
    timeline = ai_director.render_h3_timeline(
        segment, source_row, plan.get("mode", "speaking"))
    selected = set(external_qwen_fields(row))
    mode = plan.get("mode", "speaking")
    total_duration = _project_duration_seconds(plan)
    lines = [
        "这是一个已批准的单段镜头 brief。",
        "时间只从本段起点 00:00.000 计算；不要输出或重复全片绝对时间区间。",
        f"模式：{ai_director._MODE_LABELS.get(mode, mode)}",
        f"全片连续性约束：{plan.get('director_constraints', ai_director.DEFAULT_CONSTRAINTS)}",
        f"全片视觉契约：{result['global_contract']}",
        f"本段有效内容时长：{duration:.3f} 秒",
        f"动作策略（强约束）：{ai_director.motion_policy_text(mode, total_duration)}",
    ]
    if "timeline" in selected:
        lines.extend(["本段 H3 时间线（相对本段起点）：", timeline])
    material = str(row.get("material_note") or "").strip()
    if material:
        lines.append(f"素材说明：{material}")
    optional = {
        "intent": f"本段意图：{segment['intent']}",
        "brief": f"批准镜头简报（不得覆盖动作策略）：{row['prompt']}",
        "entry_state": f"入段状态：{segment['entry_state']}",
        "exit_state": f"结束状态：{segment['exit_state']}",
        "next_transition": f"下一段衔接：{segment['next_transition']}",
    }
    lines.extend(optional[name] for name in EXTERNAL_QWEN_FIELDS
                 if name != "timeline" and name in selected)
    lines.extend([
        "外部 Qwen 只负责结合图1中的唯一表演主体、图2背景/PPT和本段 H3 结构；不得新增替代主体、手势、镜头运动、道具、背景物体、台词、对白、字幕或文字叠加。",
        "保持姿态的时间段只允许口型、呼吸和微表情；minimal_gesture 只能保留本地批准的单个轻微手势。",
        "不得新增台词、对白、字幕、动作或镜头变化；口型只由音频参考驱动。",
        "生成时长由外部 Qwen/H3 节点单独提供；有效内容结束后的补帧保持结束状态。",
    ])
    return "\n".join(lines)


def save_external_qwen_fields(root, project_id, payload, server=None):
    with LOCK:
        plan = read_plan(root, project_id)
        _revision(plan, payload)
        _assert_idle(plan, server, allow_video_running=True)
        if plan.get("planning_method") != "qwen":
            raise ValueError("只有 Qwen 全片导演项目可以设置外部输出项。")
        draft = _draft(project_path(root, project_id))
        if not draft or draft.get("status") != "draft":
            raise ValueError("请先完成 Qwen 全片分镜设计。")
        index = payload.get("segment_index")
        if type(index) is not int or not 0 <= index < len(plan["segments"]):
            raise ValueError("分段编号无效。")
        if "fields" not in payload:
            raise ValueError("缺少外部 Qwen 输出项。")
        row = plan["segments"][index]
        fields = external_qwen_fields({"external_qwen_fields": payload.get("fields")})
        if fields == external_qwen_fields(row):
            return plan
        row["external_qwen_fields"] = fields
        if row.get("job"):
            request_regeneration(row, "外部 Qwen 输出项已修改")
            row["director_video_stale"] = True
            if plan.get("final_video"):
                plan["final_stale"] = True
        plan["revision"] += 1
        write_plan(root, plan)
        return plan


def approve_director(root, project_id, payload, server=None):
    with LOCK:
        plan = read_plan(root, project_id)
        _revision(plan, payload)
        _assert_idle(plan, server, allow_video_running=True)
        _cut_approval(plan)
        if plan.get("planning_method", "rules") != "qwen":
            raise ValueError("请先选择 Qwen 全片导演。")
        directory = project_path(root, project_id)
        draft = _draft(directory)
        snapshot = source_snapshot(root, plan)
        adopted = (draft and plan.get("director_approval", {}).get("draft_id") == draft.get("id")
                   and _approval_current(root, plan))
        if (not draft or draft.get("id") != payload.get("draft_id")
                or draft.get("status") != "draft"
                or (not adopted
                    and draft.get("source_fingerprint") != snapshot["source_fingerprint"])):
            raise ValueError("导演草稿已变化或过期，请重新规划并审核。")
        configured = plan.get("director_qwen_config")
        if configured and any(draft.get("model_config", {}).get(key) != value
                              for key, value in configured.items()):
            raise ValueError("导演草稿的 Qwen 模型配置已变化，请重新规划并审核。")
        # New direct-Qwen planning has no canvas source ID. Legacy drafts may
        # still carry one, but model configuration is the only binding now.
        submitted, locks, timeline_overrides = copy.deepcopy(payload.get("segments")), {}, {}
        if not isinstance(submitted, list):
            raise ValueError("必须提交全片所有导演分段。")
        source_rows = {item["segment_index"]: item
                       for item in snapshot["source"]["segments"]}
        for row in submitted:
            if not isinstance(row, dict):
                raise ValueError("导演分段必须是对象。")
            index = row.get("segment_index")
            if type(index) is not int or index not in source_rows:
                raise ValueError("导演分段编号无效。")
            row.pop("timeline_override", None)
            timeline = row.pop("timeline", None)
            if timeline is not None:
                if not isinstance(timeline, str):
                    raise ValueError("H3 时间线必须是文本。")
                timeline_overrides[index] = ai_director.strip_timeline_dialogue(timeline)
                if "beats" not in row and "actions" not in row:
                    original = draft["result"]["segments"][index]
                    row["beats"] = copy.deepcopy(original.get("beats"))
            locked = row.pop("prompt_locked", None)
            if locked is not None and type(locked) is not bool:
                raise ValueError("prompt_locked 必须是布尔值。")
            if locked is not None:
                locks[row.get("segment_index")] = locked
        total_duration = sum(
            ai_director._timeline_duration({**row, "sample_rate": plan["sample_rate"]})
            for row in snapshot["source"]["segments"])
        ai_director.assert_motion_policy(
            submitted, plan.get("mode", "speaking"), total_duration)
        approved = ai_director.validate_director_result({
            "schema_version": 1, "global_contract": payload.get("global_contract"),
            "segments": submitted,
        }, snapshot)
        for index, timeline in timeline_overrides.items():
            approved["segments"][index]["timeline_override"] = timeline
        previous = plan.get("approved_director", {})
        old_rows = {item["segment_index"]: item for item in previous.get("segments", [])}
        changed = []
        for row, designed in zip(plan["segments"], approved["segments"]):
            index = row["index"]
            old_brief = row["prompt"]
            was_locked = bool(row.get("prompt_locked"))
            if was_locked and index in old_rows:
                old = old_rows[index]
                if (old["start_sample"], old["end_sample"]) == (row["start_sample"], row["end_sample"]):
                    approved["segments"][index] = designed = copy.deepcopy(old)
            if not was_locked and row.get("prompt_kind", "brief") == "brief":
                row["prompt"] = designed["brief"]
            else:
                # The prompt always remains authored; an AI draft cannot unlock it.
                designed["brief"] = old_brief
            row["prompt_locked"] = locks.get(index, was_locked)
            design_changed = (old_brief != row["prompt"] or old_rows.get(index) != designed
                              or previous.get("global_contract") != approved["global_contract"])
            if design_changed and row.get("job"):
                row["director_video_stale"] = True
                # Active prompts finish unchanged; their newly authored
                # replacement is retained for the next batch run.
                request_regeneration(row, "导演方案已修改")
                changed.append(index)
        plan["approved_director"] = approved
        # Locked edits become the new source; adoption cannot stale its own approval.
        signature = source_snapshot(root, plan)["source_fingerprint"]
        plan["director_approval"] = {
            "id": uuid.uuid4().hex, "draft_id": draft["id"],
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "source_fingerprint": signature, "review_fingerprint": fingerprint(plan),
            "result_fingerprint": ai_director.snapshot_fingerprint(approved),
            "reference_hash_scope": "project_refs_only",
            "qwen_source_id": "",
            "qwen_config": copy.deepcopy(plan.get("director_qwen_config")),
        }
        plan.update(approved_fingerprint=fingerprint(plan), revision=plan["revision"]+1)
        if changed:
            plan["final_stale"] = True
        # Approval only marks changed completed takes for regeneration; it never
        # starts video work itself.
        director_task._atomic_json(
            state_file(directory, f"director_approved_{plan['director_approval']['id']}.json"),
            {"approval": plan["director_approval"], "result": approved})
        write_plan(root, plan)
        return plan


def preview_director(root, project_id, payload, server=None):
    """Read-only snapshot; new requests use Qwen's registered defaults."""
    source_id = str(payload.get("qwen_source_id") or "").strip()
    prompt = payload.get("prompt")
    requested_config = payload.get("qwen_config")
    requested_tokens = (
        requested_config.get("max_tokens")
        if isinstance(requested_config, dict) else None)
    tokens = payload.get("max_tokens", requested_tokens if requested_tokens is not None else 8192)
    if type(tokens) is not int or not 0 < tokens <= 32768:
        raise ValueError("max_tokens 必须为 1 到 32768 之间的整数。")
    with LOCK:
        plan = read_plan(root, project_id)
        _revision(plan, payload)
        _assert_idle(plan, server)
        _cut_approval(plan)
        config = _qwen_config_for_preview(plan, payload, source_id, prompt, tokens)
        base_config = copy.deepcopy(config)
        if "run_seed" in payload:
            seed = payload["run_seed"]
            if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFFFFFFFFFF:
                raise ValueError("run_seed 必须是非负 64 位整数。")
            config["seed"] = seed
        candidate = copy.deepcopy(plan)
        constraints = payload.get("constraints", plan.get(
            "director_constraints", ai_director.DEFAULT_CONSTRAINTS))
        if not isinstance(constraints, str) or not constraints.strip():
            raise ValueError("请填写完整的全片约束。")
        candidate["director_constraints"] = constraints.strip()
        source = source_snapshot(root, candidate)
        rerun_of = payload.get("rerun_of")
        if rerun_of:
            job = _job_status(plan, server) or {}
            if job.get("prompt_id") != rerun_of or job.get("status") != "completed":
                raise ValueError("原任务已变化或尚未完成，请重新预览。")
            previous = director_task._read_state(
                project_path(root, project_id), "director_queue_snapshot.json")
            captured = previous.get("confirmed_preview", {})
            if (previous.get("prompt_id") != rerun_of
                    or captured.get("input_snapshot", {}).get("source_fingerprint")
                    != source["source_fingerprint"]
                    or captured.get("input_snapshot", {}).get("camera_plan", {})
                    != source.get("camera_plan", {})
                    or captured.get("base_model_config", captured.get("model_config")) != base_config):
                raise ValueError("分段、全片要求或模型设置已变化，请重新预览并确认。")
            if config["seed"] % 0xFFFFFFFF == captured.get("model_config", {}).get("seed", 0) % 0xFFFFFFFF:
                raise ValueError("重新运行必须使用新的内部分镜随机种子。")
    content = {"project_id": project_id, "revision": plan["revision"],
        "qwen_source_id": source_id, "model_config": config,
        "base_model_config": base_config, "rerun_of": payload.get("rerun_of") or "",
        "input_snapshot": source, "model_input": copy.deepcopy(source["model_input"]),
        "model_message": source["model_message"],
        "model_message_fingerprint": source["model_message_fingerprint"],
        "system_prompt": ai_director.SYSTEM_PROMPT,
               "skill_policy": "LongVideo storyboard only; no Qwen/H3 skills"}
    return {**content, "preview_fingerprint": ai_director.snapshot_fingerprint(content)}


def _director_request(root, project_id, payload):
    request_id = payload.get("request_id")
    if request_id is None:
        return None, None
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
        raise ValueError("分镜请求标识无效。")
    path = state_file(project_path(root, project_id), f"director_request_{request_id}.json")
    digest = ai_director.snapshot_fingerprint(payload)
    if not path.is_file():
        return path, {"fingerprint": digest}
    receipt = json.loads(path.read_text(encoding="utf-8"))
    if receipt["fingerprint"] != digest:
        raise ValueError("重复请求的内容已变化，请重新预览。")
    if receipt.get("error"):
        raise ValueError(receipt["error"])
    return path, receipt


async def queue_director(root, project_id, payload, server, validate_prompt=None):
    """Only a confirmed, unchanged preview may enqueue the isolated text task."""
    if validate_prompt is None:
        import execution
        validate_prompt = execution.validate_prompt
    with LOCK:
        _, receipt = _director_request(root, project_id, payload)
        if receipt and "response" in receipt:
            return copy.deepcopy(receipt["response"])
    preview = preview_director(root, project_id, payload, server)
    if payload.get("preview_fingerprint") != preview["preview_fingerprint"]:
        raise ValueError("确认预览缺失或输入已经变化，请重新预览并确认运行。")
    source = preview["input_snapshot"]
    config = preview["model_config"]
    source_id = preview["qwen_source_id"]
    prompt = payload.get("prompt") or {}
    prompt_id, node_id = str(uuid.uuid4()), "director"
    task_inputs = {
        "project_id": project_id, "model": config["llm_model"],
        "mmproj": config["vision_model"], "seed": config["seed"],
        "max_tokens": config["max_tokens"],
        "expected_input_fingerprint": source["input_fingerprint"],
        "director_prompt_id": prompt_id,
    }
    if not source_id:
        task_inputs.update(
            think_mode=config["think_mode"],
            reasoning_effort=config["reasoning_effort"])
    if source_id:
        task_inputs["qwen_source_id"] = source_id
    queued_prompt = {node_id: {"class_type": "TY_H3LVDirectorTask", "inputs": task_inputs}}
    # Prompt validation may await host work. Never hold core.LOCK across it.
    valid = await validate_prompt(prompt_id, queued_prompt, [node_id])
    if not valid[0]:
        raise ValueError("导演任务校验失败："+str(valid[1]))
    with LOCK:
        receipt_path, receipt = _director_request(root, project_id, payload)
        if receipt and "response" in receipt:
            return copy.deepcopy(receipt["response"])
        current = read_plan(root, project_id)
        _revision(current, payload)
        _assert_idle(current, server)
        _cut_approval(current)
        checked = preview_director(root, project_id, payload, server)
        if checked["preview_fingerprint"] != preview["preview_fingerprint"]:
            raise ValueError("提交期间项目输入发生变化，未加入队列。")
        directory = project_path(root, project_id)
        stored_config = {
            "llm_model": config["llm_model"],
            "vision_model": config["vision_model"],
            "seed": config["seed"],
        }
        if not source_id:
            stored_config.update(
                max_tokens=config["max_tokens"],
                think_mode=config["think_mode"],
                reasoning_effort=config["reasoning_effort"])
        snapshot = {
            "prompt": copy.deepcopy(prompt), "workflow": copy.deepcopy(payload.get("workflow", {})),
            "qwen_source_id": source_id, "client_id": str(payload.get("client_id") or ""),
            "source_fingerprint": source["source_fingerprint"],
            "input_fingerprint": source["input_fingerprint"], "prompt_id": prompt_id,
            "confirmed_preview": preview,
            "qwen_config": copy.deepcopy(stored_config),
            "base_model_config": copy.deepcopy(preview["base_model_config"]),
            "request_id": payload.get("request_id"),
        }
        director_task._atomic_json(state_file(directory, "director_queue_snapshot.json"), snapshot)
        current["planning_method"] = "qwen"
        current["director_constraints"] = source["source"]["constraints"]
        current["director_job"] = {"prompt_id": prompt_id, "status": "queued", "phase": "queued",
                                   "queued_at": datetime.now(timezone.utc).isoformat(),
                                   "source_fingerprint": source["source_fingerprint"],
                                   "run_seed": config["seed"],
                                   "request_id": payload.get("request_id")}
        if source_id:
            current["director_qwen_source_id"] = source_id
        else:
            current.pop("director_qwen_source_id", None)
        current["director_qwen_config"] = copy.deepcopy(stored_config)
        current["director_base_qwen_config"] = copy.deepcopy(preview["base_model_config"])
        write_plan(root, current)
        extra = {"create_time": int(time.time()*1000),
                 "extra_pnginfo": {"workflow": {"nodes": [], "links": []}}}
        if snapshot["client_id"]:
            extra["client_id"] = snapshot["client_id"]
        number = server.number
        server.number += 1
        response = {"queued": True, "prompt_id": prompt_id, "revision": current["revision"],
                    "run_seed": config["seed"]}
        if receipt_path:
            receipt["response"] = response
            director_task._atomic_json(receipt_path, receipt)
        try:
            server.prompt_queue.put((number, prompt_id, queued_prompt, extra, valid[2], {}))
        except Exception as exc:
            current["director_job"].update(
                status="failed", phase="failed", error=str(exc), error_type=type(exc).__name__,
                finished_at=datetime.now(timezone.utc).isoformat())
            write_plan(root, current)
            if receipt_path:
                receipt["error"] = f"原请求入队失败，未自动重试：{exc}"
                director_task._atomic_json(receipt_path, receipt)
            raise
    return response
