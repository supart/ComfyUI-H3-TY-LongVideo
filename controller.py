import asyncio
import copy
import gc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

from .core import (LOCK, REFERENCE_MIRROR_ROOT, archive_take, audio_file, fingerprint, inside,
                   normalize_reference_names, output_preview, project_path, read_plan, reference_directory,
                   request_regeneration, resolve_reference_images, segment_fingerprint, write_plan)
from .execution_bridge import (apply_execution_contract, bind_execution,
                               audit_expanded_prompt, bound_prompt_from_history,
                               execution_output_ids, qwen_config_from_prompt)

TASKS = {}
SEGMENT_NODE_TYPES = {"TY_H3LVUnified", "TY_ShotPlanner"}


def media_tool(name):
    """Find FFmpeg tools even when ComfyUI was launched without the user's PATH."""
    found = shutil.which(name)
    if found:
        return found
    env_name = "FFMPEG_PATH" if name == "ffmpeg" else "FFPROBE_PATH"
    configured = os.environ.get(env_name, "").strip()
    executable = name + ".exe" if os.name == "nt" else name
    candidates = []
    if configured:
        configured_path = Path(configured)
        candidates.append(configured_path / executable if configured_path.is_dir() else configured_path)
    plugin_root = Path(__file__).resolve().parents[2]
    candidates.extend([
        plugin_root / "ffmpeg" / "bin" / executable,
        plugin_root / "tools" / "ffmpeg" / executable,
        plugin_root.parent / "MiniMax_H3_ComfyUI" / "ffmpeg" / "bin" / executable,
        plugin_root.parent / "MiniMax_H3_ComfyUI" / "ComfyUI" / "tools" / "ffmpeg" / executable,
    ])
    return next((str(path) for path in candidates if path.is_file()), None)


def refresh_segment_sampling_seed(prompt):
    """Return a prompt copy with a fresh sampling seed for one regeneration.

    The frozen workflow snapshot is intentionally left unchanged so ordinary
    continuation remains deterministic. Only the known H3 sampling seed inputs
    are changed; model, conditioning, audio, and other seed inputs stay intact.
    """
    refreshed = copy.deepcopy(prompt)
    seed = int.from_bytes(os.urandom(8), "big") & ((1 << 63) - 1)
    seed_inputs = {
        "RandomNoise": "noise_seed",
        "SelfLiftH3Sampler": "seed",
        "SelfLiftAvatarH3Sampler": "seed",
    }
    changed = 0
    for node in refreshed.values():
        if not isinstance(node, dict):
            continue
        seed_input = seed_inputs.get(node.get("class_type"))
        if not seed_input:
            continue
        inputs = node.setdefault("inputs", {})
        if seed_input in inputs:
            inputs[seed_input] = seed
            changed += 1
    # Some legacy snapshots omit a recognized sampler. Returning None keeps
    # them runnable and tells the caller that no refresh was applied.
    return refreshed, (seed if changed else None)


def restore_legacy_prompt_rules(snapshot, current_prompt):
    """Undo the old forced rule once, preserving the rest of the saved graph."""
    if snapshot.get("prompt_rule_source") == "workflow":
        return
    saved = {key: node for key, node in snapshot.get("prompt", {}).items()
             if node.get("class_type") == "PromptExpand"}
    current = {key: node for key, node in current_prompt.items()
               if node.get("class_type") == "PromptExpand"}
    if saved.keys() != current.keys():
        raise ValueError("旧项目的提示词小助手节点与当前画布不一致。请使用“重新生成本段”更新工作流快照，或重新分析创建项目。")
    fields = ("rule", "custom_rule", "custom_rule_content")
    for key in saved:
        source = current[key].get("inputs", {})
        if any(isinstance(source.get(field), list) for field in fields):
            raise ValueError("旧项目的扩写规则已改为连线输入。请使用“重新生成本段”更新完整工作流快照，或重新分析创建项目。")
        inputs = saved[key].setdefault("inputs", {})
        for field in fields:
            if field in source:
                inputs[field] = copy.deepcopy(source[field])
            else:
                inputs.pop(field, None)
    snapshot["prompt_rule_source"] = "workflow"


def normalize_output_contract(snapshot):
    """Upgrade frozen queue graphs from the former 7-output loader contract."""
    loader = str(snapshot.get("loader_id", ""))
    video = str(snapshot.get("video_id", ""))
    prompt = snapshot.get("prompt", {})
    inputs = prompt.get(video, {}).get("inputs", {})
    output_node = prompt.get(video, {})
    loader_node = prompt.get(loader, {})
    if (output_node.get("class_type") == "VHS_VideoCombine"
            and loader_node.get("class_type") in SEGMENT_NODE_TYPES):
        # The plugin owns the project take path; user-edited VHS prefixes may
        # point outside the project and cannot be reconciled safely.
        inputs["filename_prefix"] = [loader, 4]
        inputs["frame_rate"] = 24
        inputs["save_output"] = True
    snapshot["output_contract_version"] = 2
    return snapshot


def normalize_legacy_workflow_nodes(prompt, workflow):
    """Bypass the optional LayerStyle VRAM pass-through when it is unavailable.

    LayerStyle can fail to import because of an unrelated environment dependency.
    Its PurgeVRAM V2 node only clears memory and returns its input, while the TY
    controller already releases resources between segments, so removing that node
    keeps the video graph executable without changing generation inputs.
    """
    if not isinstance(prompt, dict):
        raise ValueError("视频工作流提示词不是有效对象。")
    workflow_types = {}
    if isinstance(workflow, dict):
        workflow_types = {
            str(node.get("id")): str(node.get("type") or "")
            for node in workflow.get("nodes", [])
            if isinstance(node, dict) and node.get("id") is not None
        }
    repaired = []
    for node_id, node in list(prompt.items()):
        if not isinstance(node, dict):
            raise ValueError(f"工作流节点 #{node_id} 不是有效对象。")
        class_type = str(node.get("class_type") or "")
        workflow_type = workflow_types.get(str(node_id), "")
        legacy_purge = class_type == "LayerUtility: PurgeVRAM V2" or (
            not class_type and workflow_type == "LayerUtility: PurgeVRAM V2")
        if not legacy_purge and not class_type:
            raise ValueError(
                f"工作流节点 #{node_id} 未注册（{workflow_type or '未知类型'}）。"
                "请加载对应自定义节点后再生成。")
        if not legacy_purge:
            continue
        source = (node.get("inputs") or {}).get("anything")
        if not isinstance(source, list) or len(source) < 2:
            raise ValueError(f"工作流节点 #{node_id} 缺少可旁路的输入连接。")
        source = [str(source[0]), source[1]]
        for downstream in prompt.values():
            inputs = downstream.get("inputs") if isinstance(downstream, dict) else None
            if not isinstance(inputs, dict):
                continue
            for key, value in list(inputs.items()):
                if isinstance(value, list) and len(value) >= 2 and str(value[0]) == str(node_id):
                    inputs[key] = list(source)
        prompt.pop(node_id, None)
        repaired.append(str(node_id))
    return prompt, repaired


def ensure_video_workflow_metadata(snapshot):
    """Make the actual VHS output self-describing for later archive recovery."""
    video_id = str(snapshot.get("video_id", ""))
    video = snapshot.get("prompt", {}).get(video_id)
    if not isinstance(video, dict) or video.get("class_type") != "VHS_VideoCombine":
        return False
    video.setdefault("inputs", {})["save_metadata"] = True
    workflow = snapshot.get("workflow")
    if isinstance(workflow, dict):
        for node in workflow.get("nodes", []):
            if not isinstance(node, dict) or str(node.get("id")) != video_id:
                continue
            for key in ("widgets_values", "widgets_values_named"):
                values = node.get(key)
                if isinstance(values, dict) and "save_metadata" in values:
                    values["save_metadata"] = True
    return True


def final_prompt_from_history(history, node_id=None):
    return bound_prompt_from_history(history, node_id)


def halt_status(plan):
    if plan.get("stop_requested"):
        return "stopped"
    if plan.get("pause_requested"):
        return "paused"
    return None


def video_from_history(history, video_node, directory, output_root):
    if history.get("status", {}).get("status_str") != "success":
        raise RuntimeError("该段 ComfyUI 生成失败，请检查原始节点报错，再点击重试。")
    entries = history.get("outputs", {}).get(str(video_node), {}).get("gifs", [])
    for entry in reversed(entries):
        if entry.get("type") == "output" and str(entry.get("filename", "")).lower().endswith(".mp4"):
            path = inside(directory, Path(output_root)/entry.get("subfolder", "")/entry["filename"])
            if path.is_file():
                return str(path)
    raise RuntimeError("未找到当前项目内的 MP4。请把一体化节点的 filename_prefix 接到 VHS，并开启 save_output。")


def archive_segment_video(video, output_root, project_id, segment_index, prompt_id):
    """Keep a second, easy-to-find copy outside the per-project cache tree."""
    source = Path(video).resolve()
    target_root = Path(output_root).resolve()/"TYH3LongVideo"/"segments"
    target_root.mkdir(parents=True, exist_ok=True)
    stem = f"{project_id[:8]}_seg_{int(segment_index):04d}_{str(prompt_id)[:8]}"
    target = target_root/f"{stem}{source.suffix.lower()}"
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)
    metadata_name = (source.name[:-len("-audio.mp4")] + ".png"
                     if source.name.endswith("-audio.mp4") else source.with_suffix(".png").name)
    metadata_image = source.with_name(metadata_name)
    if metadata_image.is_file():
        image_target = target_root/f"{stem}.png"
        image_temporary = image_target.with_name(f".{image_target.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(metadata_image, image_temporary)
        os.replace(image_temporary, image_target)
    return str(target)


def queued_ids(server):
    running, waiting = server.prompt_queue.get_current_queue()
    return {item[1] for item in running+waiting}


def release_comfy_resources():
    """Release ComfyUI model state between LongVideo segments.

    H3 stages can leave the text encoder, diffusion model, or VAE cached after
    a prompt finishes.  The next segment is intentionally queued only after
    this boundary, so unloading here prevents the next stage from inheriting
    the previous segment's peak allocation.  Cleanup is best-effort: a hook
    failure is recorded for diagnostics but never hides the original task
    result.
    """
    started = time.perf_counter()
    errors = []
    management = None
    try:
        import comfy.model_management as management
    except Exception as exc:
        errors.append(f"model_management unavailable: {exc}")

    if management is not None:
        unload = getattr(management, "unload_all_models", None)
        if callable(unload):
            try:
                unload()
            except Exception as exc:
                errors.append(f"unload_all_models: {exc}")
        empty = getattr(management, "soft_empty_cache", None)
        if callable(empty):
            try:
                try:
                    empty(True)
                except TypeError:
                    empty()
            except Exception as exc:
                errors.append(f"soft_empty_cache: {exc}")

    try:
        gc.collect()
    except Exception as exc:
        errors.append(f"gc.collect: {exc}")

    # Keep the fallback for test/minimal ComfyUI builds which do not expose
    # model_management hooks.  Normal ComfyUI builds use the hooks above.
    if management is None:
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception as exc:
            errors.append(f"torch cache cleanup: {exc}")

    result = {"seconds": round(time.perf_counter() - started, 3)}
    if errors:
        result["error"] = "; ".join(errors)
    return result


def reconcile_orphaned_jobs(root, project_id, server):
    """Mark saved jobs lost during a ComfyUI restart as retryable failures."""
    if project_id in TASKS:
        return read_plan(root, project_id)
    plan = read_plan(root, project_id)
    try:
        active = queued_ids(server)
    except Exception:
        return plan
    changed = []
    for index, row in enumerate(plan.get("segments", [])):
        job = row.get("job") or {}
        status = job.get("status")
        prompt_id = str(job.get("prompt_id") or "")
        if status not in {"queued", "running"} or not prompt_id:
            continue
        try:
            history = server.prompt_queue.get_history(prompt_id=prompt_id).get(prompt_id)
        except Exception:
            # A queue implementation which cannot answer history is not enough
            # evidence to declare a live job lost.
            continue
        if prompt_id in active or history:
            continue
        message = f"第 {index + 1} 段任务在 ComfyUI 重启后丢失，未找到队列或历史记录；请点击重试。"
        job.update(status="failed", error=message, orphaned=True)
        changed.append(message)
    if changed:
        if plan.get("run_status") in {"running", "pausing", "stopping", "merging"}:
            plan["run_status"] = "failed"
        plan["error"] = changed[0]
        write_plan(root, plan)
    return plan


REFERENCE_SLOT = re.compile(r"^ref_images\.ref_image_(\d+)$")


def reference_node(prompt):
    """Locate the MiniMax H3 reference node inside a frozen prompt graph."""
    for node_id, node in prompt.items():
        if node.get("class_type") == "MiniMaxH3ReferenceToVideo":
            return str(node_id), node
    return None, None


def reference_slots(prompt):
    """Return connected ref_image slots as (index, input key, source node id)."""
    _node_id, node = reference_node(prompt)
    if node is None:
        return []
    slots = []
    for key, link in (node.get("inputs") or {}).items():
        match = REFERENCE_SLOT.match(str(key))
        if match and isinstance(link, (list, tuple)) and link:
            slots.append((int(match.group(1)), str(key), str(link[0])))
    return sorted(slots)


def dual_image_output_slots(prompt, loader_id=None):
    """Detect TY_H3LVUnified image outputs without changing a graph."""
    _node_id, node = reference_node(prompt)
    if node is None:
        return None
    unified_slots = []
    for key, value in (node.get("inputs") or {}).items():
        match = REFERENCE_SLOT.fullmatch(str(key))
        if not match or not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        source = prompt.get(str(value[0])) or {}
        if source.get("class_type") == "TY_H3LVUnified":
            unified_slots.append(int(match.group(1)))
    if not unified_slots:
        return None
    if any(slot not in range(6) for slot in unified_slots):
        raise ValueError("TY_H3LVUnified 输出只能连接到 H3 的 ref_image_0 到 ref_image_5。")
    expected = {}
    saw_unified = False
    for slot in sorted(set(unified_slots)):
        value = (node.get("inputs") or {}).get(f"ref_images.ref_image_{slot}")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        source_id, output_slot = str(value[0]), int(value[1])
        source = prompt.get(source_id) or {}
        if source_id == str(loader_id or "") or source.get("class_type") == "TY_H3LVUnified":
            saw_unified = True
            if source.get("class_type") != "TY_H3LVUnified" or output_slot != 5 + slot:
                raise ValueError(
                    f"TY_H3LVUnified 输出连线顺序错误：ref_image_{slot} 必须连接 image_{slot + 1}。")
            expected[slot] = [source_id, output_slot]
    if not saw_unified:
        return None
    if len(expected) != len(set(unified_slots)):
        raise ValueError("TY_H3LVUnified 的参考图输出连线不完整。")
    if loader_id is not None and any(link[0] != str(loader_id) for link in expected.values()):
        raise ValueError("TY_H3LVUnified 输出必须来自当前顺序生成节点。")
    image_links = {
        int(match.group(1)): value for key, value in (node.get("inputs") or {}).items()
        if (match := REFERENCE_SLOT.fullmatch(str(key)))
    }
    mixed = [slot for slot, value in image_links.items()
             if slot not in expected and isinstance(value, (list, tuple)) and len(value) == 2]
    if mixed:
        raise ValueError("TY_H3LVUnified 六图模式不能与其他参考图来源混接。")
    return expected


def validate_dual_image_sources(prompt, loader_id, plan, row, directory):
    """Validate TY image output mapping before queueing."""
    links = dual_image_output_slots(prompt, loader_id)
    if links is None:
        return False
    resolved = resolve_reference_images(plan, row, directory)
    loader = prompt.get(str(loader_id)) or {}
    inputs = loader.get("inputs") or {}
    fallback_names = (
        "default_performer_image", "default_background_image",
        "default_image_3", "default_image_4", "default_image_5", "default_image_6",
    )
    for slot in links:
        item = resolved[slot]
        if item.get("name") or item.get("source") == "ignore":
            continue
        value = inputs.get(fallback_names[slot])
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
    return True


def mirror_reference_images(directory, project_id, names):
    """Copy the project's canonical pictures into ComfyUI's input directory."""
    import folder_paths
    source_root = reference_directory(directory)
    target_root = Path(folder_paths.get_input_directory())/REFERENCE_MIRROR_ROOT/project_id
    target_root.mkdir(parents=True, exist_ok=True)
    relative = []
    for name in names:
        source = inside(source_root, source_root/name)
        if not source.is_file():
            raise ValueError(f"参考图文件已丢失：{name}。请在分段审核界面重新上传。")
        target = target_root/source.name
        if not target.is_file() or target.stat().st_size != source.stat().st_size:
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        relative.append(f"{REFERENCE_MIRROR_ROOT}/{project_id}/{source.name}")
    return relative


def drop_orphan_loaders(prompt, candidates):
    """Remove detached LoadImage nodes that no remaining input consumes."""
    for source_id in candidates:
        node = prompt.get(source_id)
        if not node or node.get("class_type") != "LoadImage":
            continue
        still_used = any(
            isinstance(value, (list, tuple)) and value and str(value[0]) == source_id
            for other in prompt.values() for value in (other.get("inputs") or {}).values())
        if not still_used:
            prompt.pop(source_id, None)


def detach_reference_slots(prompt, node, slots):
    """Drop reference slots from this submission and clean the freed loaders."""
    for key, _source_id in slots:
        (node.get("inputs") or {}).pop(key, None)
    drop_orphan_loaders(prompt, [source_id for _key, source_id in slots])


def apply_segment_references(prompt, plan, row, directory):
    """Rewrite one segment's reference pictures into the frozen workflow graph.

    Only the slots the user connected on the canvas exist in the graph, so a
    segment may never use more pictures than that. The slots a segment does not
    use are removed from this submission instead of receiving a placeholder image.

    A segment without its own list follows the project default: the first
    ``reference_default_count`` canvas references, where 0 feeds no picture at
    all. Without that setting the canvas wiring is submitted untouched.
    """
    unified = dual_image_output_slots(prompt)
    if unified:
        _node_id, h3 = reference_node(prompt)
        loader_id = next((node_id for node_id, node in prompt.items()
                          if node.get("class_type") == "TY_H3LVUnified"), None)
        loader_inputs = (prompt.get(str(loader_id)) or {}).get("inputs") or {}
        resolved = resolve_reference_images(plan, row, directory)
        fallback_names = (
            "default_performer_image", "default_background_image",
            "default_image_3", "default_image_4", "default_image_5", "default_image_6",
        )
        detached = []
        for slot, link in unified.items():
            item = resolved[slot]
            fallback = loader_inputs.get(fallback_names[slot])
            has_fallback = isinstance(fallback, (list, tuple)) and len(fallback) == 2
            if item.get("name") or (item.get("source") != "ignore" and has_fallback):
                continue
            key = f"ref_images.ref_image_{slot}"
            value = (h3.get("inputs") or {}).get(key)
            if isinstance(value, (list, tuple)) and len(value) == 2:
                detached.append((key, str(value[0])))
        detach_reference_slots(prompt, h3, detached)
        return
    names = normalize_reference_names(row.get("refs"), directory, allow_blank=True)
    limit = plan.get("reference_default_count")
    if not names and limit is None:
        return
    _node_id, node = reference_node(prompt)
    if node is None:
        if not names:
            return
        raise ValueError("当前工作流没有 MiniMax H3 视频参考节点，无法使用分段参考图。")
    slots = reference_slots(prompt)
    position_label = int(row.get("index", 0))+1
    if not names:
        if len(slots) <= limit:
            return
        if not limit:
            detach_reference_slots(prompt, node, [(key, source_id)
                                                 for _index, key, source_id in slots])
            return
        detach_reference_slots(prompt, node, [(key, source_id)
                                              for _index, key, source_id in slots[limit:]])
        return
    if not slots:
        raise ValueError(
            "MiniMax H3 视频参考节点还没有接出 ref_image 槽位。请先在画布上用“加载图像”节点"
            "依次接到 ref_image_0、ref_image_1……再开始生成。")
    if len(names) > len(slots):
        raise ValueError(
            f"第 {position_label} 段配置了 {len(names)} 张参考图，但工作流只接出了 {len(slots)} 个 "
            "ref_image 槽位。请在画布上继续接“加载图像”节点补足后重试。")
    if any(name is None for name in names):
        selected = [name for name in names if name]
        mirrored = iter(mirror_reference_images(directory, plan["id"], selected)) if selected else iter(())
        detached = []
        for position, (_index, key, source_id) in enumerate(slots):
            if position >= len(names):
                detached.append((key, source_id))
                continue
            name = names[position]
            if not name:
                continue
            source = prompt.get(source_id)
            if not source or source.get("class_type") != "LoadImage":
                raise ValueError(
                    f"第 {position_label} 段有一个 ref_image 槽位没有连接到“加载图像”节点，"
                    "请把该槽位改接到加载图像节点。")
            source.setdefault("inputs", {})["image"] = next(mirrored)
        detach_reference_slots(prompt, node, detached)
        return
    paths = mirror_reference_images(directory, plan["id"], names)
    detached = []
    for position, (_index, key, source_id) in enumerate(slots):
        if position < len(paths):
            source = prompt.get(source_id)
            if not source or source.get("class_type") != "LoadImage":
                raise ValueError(
                    f"第 {position_label} 段有一个 ref_image 槽位没有连接到“加载图像”节点，"
                    "请把该槽位改接到加载图像节点。")
            source.setdefault("inputs", {})["image"] = paths[position]
        else:
            detached.append((key, source_id))
    detach_reference_slots(prompt, node, detached)


async def execute_project(root, project_id, server):
    import execution
    import folder_paths
    directory = project_path(root, project_id)
    snapshot_file = directory/"state"/"queue_snapshot.json"
    snapshot = normalize_output_contract(json.loads(snapshot_file.read_text(encoding="utf-8")))
    snapshot["prompt"], repaired = normalize_legacy_workflow_nodes(
        snapshot.get("prompt", {}), snapshot.get("workflow", {}))
    if repaired:
        snapshot.setdefault("compatibility_repairs", []).extend(repaired)
    ensure_video_workflow_metadata(snapshot)
    snapshot_file.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    try:
        initial_plan = read_plan(root, project_id)
        only_segment = initial_plan.get("run_only_segment")
        allow_legacy_regeneration = bool(initial_plan.get("force_regeneration"))
        indices = ([int(only_segment)] if only_segment is not None
                   else range(len(initial_plan["segments"])))
        for index in indices:
            plan = read_plan(root, project_id)
            halted = halt_status(plan)
            if halted:
                plan["run_status"] = halted
                write_plan(root, plan)
                return
            row = plan["segments"][index]
            # Older Qwen approvals only persisted this marker. Promote it to
            # the normal replacement flag so a later batch run cannot skip the
            # completed old take.
            if (row.get("director_video_stale")
                    and row.get("job", {}).get("status") == "completed"
                    and not row.get("needs_regeneration")):
                request_regeneration(row, "导演方案已修改")
                write_plan(root, plan)
            if row.get("job", {}).get("status") == "completed" and not row.get("needs_regeneration"):
                continue
            if not plan.get("approved") or plan.get("approved_fingerprint") != fingerprint(plan):
                raise ValueError("方案已变化，请重新审核确认。")
            from .director_review import require_director_approval
            require_director_approval(
                root, plan, allow_legacy_regeneration=allow_legacy_regeneration)
            if row.get("needs_regeneration") and row.get("job", {}).get("status") == "completed":
                previous_job = copy.deepcopy(row["job"])
                archive_take(row, previous_job)
                row["replacement_previous_job"] = previous_job
                row.pop("job", None)
                write_plan(root, plan)
            job = row.get("job")
            if job and job.get("status") == "failed":
                raise ValueError(f"第 {index+1} 段已失败，请先点击该段重试。")
            if not job:
                prompt_id = str(uuid.uuid4())
                prompt = copy.deepcopy(snapshot["prompt"])
                prompt[snapshot["loader_id"]]["inputs"].update(project_id=project_id, segment_index=index)
                loader_type = prompt[snapshot["loader_id"]].get("class_type")
                if loader_type == "TY_ShotPlanner":
                    contract = {}
                else:
                    validate_dual_image_sources(
                        prompt, snapshot["loader_id"], plan, row, directory)
                    apply_segment_references(prompt, plan, row, directory)
                    if snapshot.get("qwen_config"):
                        contract = await asyncio.to_thread(
                            apply_execution_contract, root, snapshot, prompt, plan, row,
                            allow_legacy_regeneration)
                    else:
                        contract = apply_execution_contract(
                            root, snapshot, prompt, plan, row, allow_legacy_regeneration)
                if row.get("regeneration_nonce"):
                    prompt, regeneration_seed = refresh_segment_sampling_seed(prompt)
                else:
                    regeneration_seed = None
                valid = await execution.validate_prompt(
                    prompt_id, prompt, execution_output_ids(snapshot, prompt))
                if not valid[0]:
                    raise ValueError("视频工作流校验失败："+str(valid[1]))
                # Save intent before enqueue; uncertain interrupted jobs are never blindly resubmitted.
                row["job"] = job = {"prompt_id": prompt_id, "status": "queued"}
                if contract:
                    row["job"]["director_execution"] = contract
                if regeneration_seed is not None:
                    job["regeneration_seed"] = regeneration_seed
                write_plan(root, plan)
                extra = {
                    "extra_pnginfo": {"workflow": snapshot.get("workflow", {})},
                    # Match ComfyUI's /prompt metadata contract. The modern task
                    # queue UI uses this timestamp to register and order jobs.
                    "create_time": int(time.time() * 1000),
                }
                client_id = str(snapshot.get("client_id") or "").strip()
                if client_id:
                    extra["client_id"] = client_id
                number = server.number
                server.number += 1
                server.prompt_queue.put((number, prompt_id, prompt, extra, valid[2], {}))
            prompt_id = job["prompt_id"]
            while True:
                history = server.prompt_queue.get_history(prompt_id=prompt_id).get(prompt_id)
                if history:
                    break
                if prompt_id not in queued_ids(server):
                    history = server.prompt_queue.get_history(prompt_id=prompt_id).get(prompt_id)
                    if history:
                        break
                    message = (f"第 {index+1} 段任务记录不在队列/历史中，可能曾中断。"
                               "已标记为失败，不会自动重复提交；请点击该段重试。")
                    current = read_plan(root, project_id)
                    current_row = current["segments"][index]
                    current_job = current_row.get("job")
                    if current_job and current_job.get("prompt_id") == prompt_id:
                        current_job.update(status="failed", error=message, orphaned=True)
                        current["error"] = message
                        write_plan(root, current)
                    raise RuntimeError(message)
                await asyncio.sleep(.5)
            plan = read_plan(root, project_id)
            row = plan["segments"][index]
            try:
                video = video_from_history(history, snapshot["video_id"], directory, folder_paths.get_output_directory())
                archived_video = archive_segment_video(
                    video, folder_paths.get_output_directory(), project_id, index, prompt_id)
                contract = row["job"].get("director_execution", {})
                final_prompt = contract.get("final_prompt") or final_prompt_from_history(
                    history, snapshot.get("prompt_output_id"))
                if final_prompt:
                    row["job"]["final_prompt"] = final_prompt
                    if contract.get("expander") == "external_qwen":
                        approved_segments = plan.get("approved_director", {}).get("segments", [])
                        approved_segment = (approved_segments[index]
                                            if index < len(approved_segments) else {})
                        approved_motion = "\n".join(
                            f"{beat.get('shot_description', '')} {beat.get('pose', '')}"
                            for beat in approved_segment.get("beats", [])
                            if beat.get("motion") == "minimal_gesture")
                        motion_findings = audit_expanded_prompt(
                            final_prompt, plan.get("mode", "speaking"), approved_motion)
                        if motion_findings:
                            row["job"]["motion_audit"] = {
                                "status": "blocked",
                                "findings": motion_findings,
                                "message": "外部 Qwen 扩写增加了未批准的动作或画面变化；本段结果已拒收，后续批量已停止。",
                            }
                            raise ValueError(
                                row["job"]["motion_audit"]["message"]
                                + " 检出：" + "、".join(motion_findings))
                pending_regeneration = bool(row.get("regeneration_after_current"))
                pending_reason = row.get("regeneration_reason")
                row["job"].update(status="completed", video=video,
                                  input_fingerprint=segment_fingerprint(row))
                row["job"]["output_video"] = archived_video
                if pending_regeneration:
                    row["needs_regeneration"] = True
                    row["regeneration_reason"] = pending_reason or "当前任务完成后待重生成"
                    row.pop("regeneration_after_current", None)
                else:
                    row.pop("needs_regeneration", None)
                    row.pop("regeneration_reason", None)
                    row.pop("regeneration_after_current", None)
                row.pop("director_video_stale", None)
                row.pop("replacement_previous_job", None)
            except Exception as exc:
                cleanup = release_comfy_resources()
                row["job"].update(status="failed", error=str(exc))
                row["job"]["resource_cleanup_seconds"] = cleanup["seconds"]
                if cleanup.get("error"):
                    row["job"]["resource_cleanup_error"] = cleanup["error"]
                previous_job = row.pop("replacement_previous_job", None)
                if previous_job:
                    row.setdefault("failed_attempts", []).append(copy.deepcopy(row["job"]))
                    row["job"] = previous_job
                    row["needs_regeneration"] = True
                write_plan(root, plan)
                raise
            cleanup = release_comfy_resources()
            row["job"]["resource_cleanup_seconds"] = cleanup["seconds"]
            if cleanup.get("error"):
                row["job"]["resource_cleanup_error"] = cleanup["error"]
            else:
                row["job"].pop("resource_cleanup_error", None)
            write_plan(root, plan)
            preview = output_preview(folder_paths.get_output_directory(), video)
            if preview and callable(getattr(server, "send_sync", None)):
                server.send_sync("ty-h3lv-segment", {
                    "project_id": project_id, "segment_index": index, "preview": preview})
        plan = read_plan(root, project_id)
        if only_segment is not None:
            plan.pop("run_only_segment", None)
            plan["run_status"] = halt_status(plan) or "paused"
            write_plan(root, plan)
            return
        pending_edits = [row["index"] for row in plan["segments"]
                         if row.get("needs_regeneration")]
        if pending_edits:
            plan["run_status"] = "paused"
            plan["error"] = (
                "运行中保存的新修改已生效；待重生成第 "
                + "、".join(str(index + 1) for index in pending_edits)
                + " 段，未合并旧版本。")
            write_plan(root, plan)
            return
        halted = halt_status(plan)
        if halted:
            plan["run_status"] = halted
        else:
            plan["run_status"] = "merging"
            write_plan(root, plan)
            final = await asyncio.to_thread(assemble, root, project_id)
            plan = read_plan(root, project_id)
            previous_final = plan.get("final_video")
            if previous_final and previous_final != final:
                versions = plan.setdefault("final_versions", [])
                if previous_final not in versions:
                    versions.append(previous_final)
            plan.update(run_status="completed", final_video=final, final_stale=False)
            plan.pop("full_regeneration_pending", None)
            plan.pop("full_regeneration_reason", None)
        write_plan(root, plan)
        preview = output_preview(folder_paths.get_output_directory(), plan.get("final_video"))
        if preview and callable(getattr(server, "send_sync", None)):
            server.send_sync("ty-h3lv-final", {"project_id": project_id, "preview": preview})
    except Exception as exc:
        plan = read_plan(root, project_id)
        plan.pop("run_only_segment", None)
        plan.update(run_status="failed", error=str(exc))
        write_plan(root, plan)
    finally:
        try:
            with LOCK:
                final_plan = read_plan(root, project_id)
                if final_plan.pop("force_regeneration", None) is not None:
                    write_plan(root, final_plan)
        except Exception:
            pass
        # Also cover cancellation, validation failures, and final merge errors.
        release_comfy_resources()
        TASKS.pop(project_id, None)


def start(root, project_id, payload, server):
    with LOCK:
        if project_id in TASKS:
            raise ValueError("该项目已经在生成中。")
        plan = read_plan(root, project_id)
        if "revision" in payload and (type(payload["revision"]) is not int
                                     or payload["revision"] != plan["revision"]):
            raise ValueError("项目已变化，请重新审核后再启动生成。")
        if ("director_approval_id" in payload
                and payload["director_approval_id"] != plan.get("director_approval", {}).get("id")):
            raise ValueError("分镜审批已变化，请重新审核后再启动生成。")
        if not plan.get("approved") or plan.get("approved_fingerprint") != fingerprint(plan):
            raise ValueError("请先保存并确认分段方案。")
        force_regeneration = bool(payload.get("force_regeneration"))
        if force_regeneration:
            if payload.get("only_segment_index") is not None:
                raise ValueError("强制新一轮批量生成不能只提交单段。")
            if not any(row.get("needs_regeneration")
                       and row.get("job", {}).get("status") == "completed"
                       and row.get("job", {}).get("video")
                       for row in plan["segments"]):
                raise ValueError("当前没有待重生成的已完成片段。")
            plan["force_regeneration"] = True
        from .director_review import require_director_approval
        require_director_approval(
            root, plan, allow_legacy_regeneration=force_regeneration)
        if (plan.get("full_regeneration_pending")
                and payload.get("only_segment_index") is None):
            reason = plan.get("full_regeneration_reason", "全片导演规划设置已修改")
            for row in plan["segments"]:
                request_regeneration(row, reason)
            if plan.get("final_video"):
                plan["final_stale"] = True
        directory = project_path(root, project_id)
        snapshot_file = directory/"state"/"queue_snapshot.json"
        replace_snapshot = bool(payload.get("replace_snapshot"))
        if not any(row.get("job") for row in plan["segments"]) or replace_snapshot:
            prompt = copy.deepcopy(payload.get("prompt", {}))
            loader, video = str(payload.get("loader_id", "")), str(payload.get("video_id", ""))
            prompt, repaired = normalize_legacy_workflow_nodes(
                prompt, payload.get("workflow", {}))
            if prompt.get(loader, {}).get("class_type") not in SEGMENT_NODE_TYPES:
                raise ValueError("请打开含 H3 分段读取节点或一体化节点的视频工作流。")
            if prompt.get(video, {}).get("class_type") != "VHS_VideoCombine":
                raise ValueError("请选择此工作流的 VHS Video Combine 输出节点。")
            snapshot = normalize_output_contract({"prompt": prompt, "loader_id": loader,
                                                  "video_id": video, "workflow": payload.get("workflow", {}),
                                                  "client_id": str(payload.get("client_id") or "").strip(),
                                                  "prompt_rule_source": "workflow"})
            if repaired:
                snapshot["compatibility_repairs"] = repaired
            qwen_config = qwen_config_from_prompt(prompt, loader)
            if qwen_config:
                snapshot["qwen_config"] = qwen_config
            for key in ("qwen_id", "h3_id", "prompt_output_id"):
                if payload.get(key) is not None:
                    snapshot[key] = str(payload[key])
            if prompt[loader].get("class_type") != "TY_ShotPlanner":
                bind_execution(snapshot, plan)
            snapshot_file.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        elif not snapshot_file.is_file():
            raise ValueError("缺少原工作流快照，无法安全继续。")
        else:
            snapshot = json.loads(snapshot_file.read_text(encoding="utf-8"))
            restore_legacy_prompt_rules(snapshot, payload.get("prompt", {}))
            loader_type = snapshot.get("prompt", {}).get(
                str(snapshot.get("loader_id", "")), {}).get("class_type")
            if loader_type != "TY_ShotPlanner":
                bind_execution(snapshot, plan)
            client_id = str(payload.get("client_id") or "").strip()
            if client_id and snapshot.get("client_id") != client_id:
                snapshot["client_id"] = client_id
            snapshot_file.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        ensure_video_workflow_metadata(snapshot)
        snapshot_file.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        only_segment = payload.get("only_segment_index")
        if only_segment is None:
            plan.pop("run_only_segment", None)
        else:
            only_segment = int(only_segment)
            if not 0 <= only_segment < len(plan["segments"]):
                raise ValueError("要重新生成的片段编号无效。")
            plan["run_only_segment"] = only_segment
        plan.update(run_status="running", pause_requested=False, stop_requested=False, error="")
        write_plan(root, plan)
        TASKS[project_id] = asyncio.create_task(execute_project(root, project_id, server))


def command(args, timeout=1800):
    result = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if __import__("os").name=="nt" else 0)
    if result.returncode:
        raise RuntimeError("视频合并工具失败："+result.stderr[-2000:])
    return result.stdout


def reveal_command(path, os_name=None, platform=None):
    if (os_name or os.name) == "nt":
        return ["explorer.exe", "/separate,", "/select,", str(path)]
    if (platform or sys.platform) == "darwin":
        return ["open", "-R", str(path)]
    return ["xdg-open", str(Path(path).parent)]


def explorer_windows():
    if os.name != "nt":
        return set()
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    handles = set()
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd, _):
        name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(hwnd, name, len(name))
        if name.value in {"CabinetWClass", "ExploreWClass"}:
            handles.add(int(hwnd))
        return True

    user32.EnumWindows(collect, 0)
    return handles


def activate_explorer_window(hwnd):
    import ctypes
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    user32.BringWindowToTop(hwnd)
    if not user32.SetForegroundWindow(hwnd):
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        user32.SetForegroundWindow(hwnd)


def reveal_file(path):
    """Open the platform file manager and reveal one completed output file."""
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    before = explorer_windows()
    subprocess.Popen(reveal_command(path), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            created = explorer_windows() - before
            if created:
                activate_explorer_window(next(iter(created)))
                break
            time.sleep(.05)
    return str(path)


def probe_video(path):
    ffprobe = media_tool("ffprobe")
    if not ffprobe:
        raise ValueError("未找到 ffprobe，请配置现有 FFmpeg 到 PATH。插件不会自动安装。")
    data = json.loads(command([ffprobe, "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,nb_read_frames,r_frame_rate,avg_frame_rate,duration", "-of", "json", str(path)]))
    if not data.get("streams"):
        raise ValueError("文件没有视频轨道。")
    return data["streams"][0]


def assemble(root, project_id):
    ffmpeg = media_tool("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 FFmpeg，请配置已有 FFmpeg 到 PATH。")
    plan = read_plan(root, project_id)
    directory = project_path(root, project_id)
    work_root = directory / "work"
    work_root.mkdir(exist_ok=True)
    work = work_root / ("assembly_"+uuid.uuid4().hex[:8])
    work.mkdir()
    cache = directory / "cache"
    cache.mkdir(exist_ok=True)
    size, clips = None, []
    for row in plan["segments"]:
        job = row.get("job", {})
        if job.get("status") != "completed":
            raise ValueError("仍有片段未完成，暂不能合并。")
        path = inside(directory, job["video"])
        info = probe_video(path)
        dimensions = (info["width"], info["height"])
        if size and dimensions != size:
            raise ValueError("片段分辨率不一致，请检查工作流。")
        size = dimensions
        rate = info["r_frame_rate"].split("/")
        fps = float(rate[0])/float(rate[1])
        if abs(fps-24) > .001 or int(info["nb_read_frames"]) < row["edit_frames"]:
            raise ValueError("片段必须为 24fps 且有足够帧数，不能靠补帧掩盖缺失内容。")
        stat = path.stat()
        cache_key = hashlib.sha256(json.dumps({
            "path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "edit_frames": row["edit_frames"], "width": dimensions[0], "height": dimensions[1],
            "fps": 24, "codec": "libx264-crf18-yuv420p-workflow-metadata-v2",
        }, sort_keys=True).encode()).hexdigest()[:16]
        normalized = cache/f"{row['index']:04d}_{cache_key}.mp4"
        if not normalized.is_file():
            temporary = cache/f".{normalized.stem}-{uuid.uuid4().hex}.tmp.mp4"
            # Normalize timestamps, packet duration and frame count once per take.
            # Later assemblies reuse this clip without re-encoding it.
            command([ffmpeg, "-v", "error", "-n", "-i", str(path), "-map_metadata", "0", "-an", "-vf",
                     f"trim=end_frame={row['edit_frames']},setpts=N/(24*TB)", "-r", "24", "-fps_mode", "cfr",
                     "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                     "-video_track_timescale", "24000", "-movie_timescale", "24000",
                     "-movflags", "use_metadata_tags", str(temporary)])
            temporary.replace(normalized)
        linked = work/f"{row['index']:04d}.mp4"
        try:
            os.link(normalized, linked)
        except OSError:
            shutil.copyfile(normalized, linked)
        clips.append(linked)
    listing = work/"concat.txt"
    # Use safe relative generated names, never user-supplied concat directives.
    listing.write_text("\n".join(f"file '{p.name}'" for p in clips), encoding="utf-8")
    temporary_final = work/"final.mp4"
    command([ffmpeg, "-v", "error", "-n", "-f", "concat", "-safe", "1", "-i", str(listing),
             "-i", str(audio_file(directory, "source.wav")), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
             "-map_metadata", "0",
             "-c:a", "aac", "-b:a", "320k", "-t", f"{plan['duration']:.9f}",
             "-video_track_timescale", "24000", "-movie_timescale", "24000", "-movflags", "+faststart", str(temporary_final)])
    created = time.localtime(float(plan.get("created") or time.time()))
    stamp = time.strftime("%Y%m%d_%H%M%S", created)
    mode = plan.get("mode") if plan.get("mode") in {"speaking", "speech_light"} else "singing"
    final_directory = Path(root).resolve().parent / "final_videos"
    final_directory.mkdir(parents=True, exist_ok=True)
    stem = f"{stamp}_{mode}_{project_id[:8]}"
    final = final_directory/f"{stem}.mp4"
    version = 2
    while final.exists():
        final = final_directory/f"{stem}_v{version}.mp4"
        version += 1
    os.replace(temporary_final, final)
    shutil.rmtree(work, ignore_errors=True)
    return str(final)
