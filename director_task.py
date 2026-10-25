"""Text-only ComfyUI director task; never submits or applies a video workflow."""

import copy
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

from . import ai_director, qwen_adapter
from .core import (LOCK, fingerprint, inside, normalize_reference_names,
                   project_path, read_plan, rebuild_speech_timeline,
                   reference_directory, replace_with_retry, state_file,
                   write_plan)


BUSY_STATUSES = {"running", "pausing", "stopping", "merging"}
LOGGER = logging.getLogger(__name__)


def _file_digest(path):
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_state(directory, name):
    value = json.loads(state_file(directory, name).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{name} 必须是项目 JSON 对象。")
    return value


def _snapshot(root, project_id):
    """The caller holds core.LOCK across all source reads."""
    plan = read_plan(root, project_id)
    if plan.get("id") != project_id:
        raise ValueError("项目编号与状态文件不一致。")
    if (not plan.get("approved")
            or plan.get("approved_fingerprint") != fingerprint(plan)):
        raise ValueError("请先保存并确认当前切分，再运行 AI 总导演。")
    if plan.get("run_status") in BUSY_STATUSES:
        raise ValueError("当前项目仍在生成或合成，请停止后再规划。")
    directory = project_path(root, project_id)
    transcript = _read_state(directory, "transcript.json")
    analysis = _read_state(directory, "analysis.json")
    candidate = copy.deepcopy(plan)
    rebuild_speech_timeline(candidate["segments"], transcript, candidate["sample_rate"])
    refs = {}
    for row in candidate["segments"]:
        for name in normalize_reference_names(row.get("refs"), directory, allow_blank=True):
            if not name:
                continue
            if name not in refs:
                path = inside(reference_directory(directory),
                              reference_directory(directory)/name)
                refs[name] = _file_digest(path)
    return ai_director.build_input_snapshot(
        candidate, transcript=transcript, analysis=analysis,
        constraints=candidate.get("director_constraints"),
        reference_fingerprints=refs)


def resolve_model_config(directory, model, mmproj, qwen_source_id="", seed=0):
    """Bind explicit models, or the selected node in a saved execution snapshot.

    No "first Qwen node" or installed-model fallback is permitted. Source IDs
    refer to the dedicated director snapshot when present. The legacy video
    snapshot is read only as a compatibility fallback, never overwritten.
    """
    for value, label in ((model, "model"), (mmproj, "mmproj"),
                         (qwen_source_id, "qwen_source_id")):
        if not isinstance(value, str):
            raise ValueError(f"{label} 必须是文本。")
    model, mmproj, source_id = model.strip(), mmproj.strip(), qwen_source_id.strip()
    if type(seed) is not int or seed < 0:
        raise ValueError("seed 必须是非负整数。")
    config = {"seed": seed}
    if source_id:
        snapshot_name = ("director_queue_snapshot.json"
                         if state_file(directory, "director_queue_snapshot.json").is_file()
                         else "queue_snapshot.json")
        snapshot = _read_state(directory, snapshot_name)
        prompt = snapshot.get("prompt")
        source = prompt.get(source_id) if isinstance(prompt, dict) else None
        if not isinstance(source, dict) or source.get("class_type") != qwen_adapter.NODE_ID:
            raise ValueError("指定的 Qwen 来源节点不在已保存的工作流快照中。")
        inputs = source.get("inputs")
        if not isinstance(inputs, dict):
            raise ValueError("指定 Qwen 节点没有有效的模型配置。")
        for supplied, field in ((model, "llm_model"), (mmproj, "vision_model")):
            selected = inputs.get(field)
            if not isinstance(selected, str) or not selected.strip():
                raise ValueError("Qwen 来源模型必须是明确的文件名，不能使用未解析连线。")
            if supplied and supplied != selected.strip():
                raise ValueError("任务模型与指定 Qwen 来源配置不一致，请重新捕获工作流。")
            config[field] = selected.strip()
        config["qwen_source_id"] = source_id
    else:
        if not model or not mmproj:
            raise ValueError("必须明确指定 model 和 mmproj，不能自动选择其他模型。")
        config.update(llm_model=model, vision_model=mmproj)
    return config


def _atomic_json(path, value):
    """Same-directory replace leaves the previous draft intact on write failure."""
    path = Path(path)
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run_director_task(root, project_id, model="", mmproj="", seed=0, max_tokens=8192,
                      qwen_source_id="", adapter_factory=None, expected_input_fingerprint="",
                      director_prompt_id="", think_mode=False, reasoning_effort="medium"):
    """Scope every lifecycle update to the actual queued task, not a newer job."""
    def phase(value):
        _update_job(root, project_id, director_prompt_id, "running", value)

    try:
        phase("preparing")
        result = _run_director_task(
            root, project_id, model, mmproj, seed, max_tokens, qwen_source_id,
            adapter_factory, expected_input_fingerprint, phase, director_prompt_id,
            think_mode, reasoning_effort)
    except BaseException as exc:
        interrupted = isinstance(exc, (KeyboardInterrupt, InterruptedError)) or (
            type(exc).__name__ == "InterruptProcessingException")
        state = "interrupted" if interrupted else "failed"
        _update_job(root, project_id, director_prompt_id, state, state,
                    error=str(exc) or type(exc).__name__, error_type=type(exc).__name__,
                    **({"context_budget": exc.context_budget}
                       if hasattr(exc, "context_budget") else {}))
        raise
    _update_job(root, project_id, director_prompt_id, "completed", "completed")
    return result


def _update_job(root, project_id, prompt_id, status, phase, **details):
    """Persist status only; never bump content revision or overwrite another job."""
    if not prompt_id:
        return
    now = datetime.now(timezone.utc).isoformat()
    with LOCK:
        plan = read_plan(root, project_id)
        job = plan.get("director_job", {})
        if job.get("prompt_id") != prompt_id:
            return
        if job.get("status") in {"completed", "failed", "interrupted"}:
            return
        if job.get("status") == status and job.get("phase") == phase and job.get("started_at"):
            return
        job.update(status=status, phase=phase, updated_at=now, **details)
        job.setdefault("started_at", now)
        if status in {"completed", "failed", "interrupted"}:
            job["finished_at"] = now
        write_plan(root, plan)
        event = {"project_id": project_id, "prompt_id": prompt_id,
                 "phase": phase, "job": copy.deepcopy(job)}
    server = getattr(getattr(sys.modules.get("server"), "PromptServer", None), "instance", None)
    if callable(getattr(server, "send_sync", None)):
        try:
            server.send_sync("ty-h3lv-director", event)
        except Exception:
            LOGGER.debug("Director progress notification unavailable.", exc_info=True)


def _run_director_task(root, project_id, model="", mmproj="", seed=0, max_tokens=8192,
                       qwen_source_id="", adapter_factory=None, expected_input_fingerprint="",
                       on_phase=None, director_prompt_id="", think_mode=False,
                       reasoning_effort="medium"):
    """Generate one immutable draft, checking asynchronous edits before commit.

    A conflicting result is retained separately with ``status=stale`` and the
    previous director_draft.json is not touched. A successful task only saves a
    draft: approvals, prompts and video jobs remain unchanged.
    """
    if type(max_tokens) is not int or not 0 < max_tokens <= 32768:
        raise ValueError("max_tokens 必须是 1 到 32768 之间的整数。")
    if type(think_mode) is not bool:
        raise ValueError("think_mode 必须是布尔值。")
    if reasoning_effort not in {"low", "medium", "xhigh"}:
        raise ValueError("reasoning_effort 不受支持。")
    directory = project_path(root, project_id)
    draft_path = state_file(directory, "director_draft.json")
    with LOCK:
        if (director_prompt_id and read_plan(root, project_id).get(
                "director_job", {}).get("prompt_id") != director_prompt_id):
            raise ValueError("导演任务已被其他任务替代；未运行模型。")
        snapshot = _snapshot(root, project_id)
        if expected_input_fingerprint and snapshot["input_fingerprint"] != expected_input_fingerprint:
            raise ValueError("排队期间项目输入已变化；未运行模型，请重新预览并确认。")
        config = resolve_model_config(directory, model, mmproj, qwen_source_id, seed)
        base_model_config = copy.deepcopy(
            read_plan(root, project_id).get("director_base_qwen_config"))
        old_draft_digest = _file_digest(draft_path)
    adapter_factory = adapter_factory or qwen_adapter.QwenAdapter.from_registered
    model_config = {
        **config, "max_tokens": max_tokens,
        "think_mode": think_mode, "reasoning_effort": reasoning_effort,
    }
    adapter_config = dict(config)
    if think_mode:
        adapter_config["think_mode"] = True
    if reasoning_effort != "medium":
        adapter_config["reasoning_effort"] = reasoning_effort
    started = time.monotonic()
    runtime = {"started_at": datetime.now(timezone.utc).isoformat(), "calls": []}
    captured_attempts = []
    adapter = None

    def complete(**kwargs):
        call_started = time.monotonic()
        attempt = {"messages": copy.deepcopy(kwargs["messages"]),
                   "response_schema": copy.deepcopy(kwargs.get("response_schema"))}
        captured_attempts.append(attempt)
        try:
            attempt["response"] = adapter.complete(**kwargs)
            return attempt["response"]
        finally:
            runtime["calls"].append({
                "elapsed_seconds": time.monotonic()-call_started,
                "usage": copy.deepcopy(getattr(adapter, "last_usage", {})),
                "metadata": copy.deepcopy(getattr(adapter, "last_metadata", {})),
            })

    try:
        adapter = adapter_factory(adapter_config)
        adapter.on_phase = on_phase
        for name in ("model", "mmproj", "context_size", "seed", "reasoning_effort"):
            value = getattr(adapter, name, None)
            if value is not None:
                runtime[name] = str(value) if isinstance(value, Path) else value
        draft = ai_director.generate_director_draft(
            snapshot, complete, model_config=model_config,
            max_output_tokens=max_tokens, on_phase=on_phase,
            enable_thinking=think_mode)
    except BaseException as exc:
        # A diagnostic must not replace the old draft, nor consume cancellation.
        runtime["elapsed_seconds"] = time.monotonic()-started
        diagnostic = {
            "id": uuid.uuid4().hex, "status": "failed", "project_id": project_id,
            "input_snapshot": snapshot, "model_config": model_config,
            "attempts": copy.deepcopy(getattr(exc, "attempts", None) or captured_attempts),
            "runtime_metadata": runtime,
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
        path = state_file(directory, f"director_failure_{diagnostic['id']}.json")
        try:
            with LOCK:
                _atomic_json(path, diagnostic)
        except Exception as diagnostic_error:
            exc.add_note(f"Director diagnostic could not be saved: {diagnostic_error}")
            LOGGER.exception("Failed to retain director task diagnostic.")
        raise
    runtime["elapsed_seconds"] = time.monotonic()-started
    draft["runtime_metadata"] = runtime
    if base_model_config:
        draft["base_model_config"] = base_model_config
    # Only project-uploaded refs are hashed here. Before a future video run, its
    # execution snapshot must also bind/hash any canvas-default reference inputs.
    draft["reference_hash_scope"] = "project_refs_only"
    with LOCK:
        conflict = None
        try:
            if (director_prompt_id and read_plan(root, project_id).get(
                    "director_job", {}).get("prompt_id") != director_prompt_id):
                raise ValueError("导演任务已被更新任务替代。")
            current = _snapshot(root, project_id)
            current_config = resolve_model_config(
                directory, model, mmproj, qwen_source_id, seed)
            if (current["source_fingerprint"] != snapshot["source_fingerprint"]
                    or current["input_fingerprint"] != snapshot["input_fingerprint"]
                    or current_config != config):
                conflict = "规划期间项目输入或 Qwen 来源配置发生变化。"
            elif _file_digest(draft_path) != old_draft_digest:
                conflict = "规划期间已有导演草稿被其他任务更新。"
        except (ValueError, OSError) as exc:
            conflict = f"规划期间项目不再满足已确认条件：{exc}"
        if conflict:
            draft.update(status="stale", conflict_reason=conflict)
            retained = state_file(directory, f"director_stale_{draft['id']}.json")
            _atomic_json(retained, draft)
            raise ValueError(
                f"{conflict} 未覆盖原草稿；过期结果已保留为 {retained.name}。")
        # Keep every successful version independently before changing the pointer.
        # The prior director_draft.json remains readable if replacement fails.
        version = state_file(directory, f"director_draft_{draft['id']}.json")
        _atomic_json(version, draft)
        _atomic_json(draft_path, draft)
    return {"project_id": project_id, "draft_id": draft["id"],
            "status": draft["status"], "approved": False,
            "source_fingerprint": draft["source_fingerprint"],
            "input_fingerprint": draft["input_fingerprint"],
            "segment_count": len(draft["result"]["segments"]),
            "draft_file": draft_path.name}


class DirectorTask:
    """An output node for an independent text-planning queue submission."""

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "project_id": ("STRING", {"default": ""}),
            "model": ("STRING", {"default": ""}),
            "mmproj": ("STRING", {"default": ""}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            "max_tokens": ("INT", {"default": 8192, "min": 1, "max": 32768}),
        }, "optional": {
            "qwen_source_id": ("STRING", {"default": ""}),
            "expected_input_fingerprint": ("STRING", {"default": ""}),
            "director_prompt_id": ("STRING", {"default": ""}),
            "think_mode": ("BOOLEAN", {"default": False}),
            "reasoning_effort": (["low", "medium", "xhigh"], {"default": "medium"}),
        }}

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("director_status",)
    FUNCTION = "plan"
    CATEGORY = "TY/H3 长视频"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def plan(self, project_id, model, mmproj, seed=0, max_tokens=8192,
             qwen_source_id="", expected_input_fingerprint="", director_prompt_id="",
             think_mode=False, reasoning_effort="medium"):
        from .nodes import data_root
        kwargs = {
            "expected_input_fingerprint": expected_input_fingerprint,
            "director_prompt_id": director_prompt_id,
        }
        if think_mode or reasoning_effort != "medium":
            kwargs.update(think_mode=think_mode, reasoning_effort=reasoning_effort)
        status = run_director_task(
            data_root(), project_id, model, mmproj, seed, max_tokens,
            qwen_source_id, **kwargs)
        text = json.dumps(status, ensure_ascii=False, sort_keys=True)
        return {"ui": {"text": [text]}, "result": (text,)}
