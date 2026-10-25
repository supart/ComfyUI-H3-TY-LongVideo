"""Apply approved text contracts to a copy of a selected video execution graph."""
import copy
import re

from . import qwen_adapter


H3_REFERENCE = re.compile(r"^ref_images\.ref_image_(\d+)$")
H3_VIDEO_REFERENCE = re.compile(r"^ref_videos\.ref_video_(\d+)$")
QWEN_REFERENCE = re.compile(r"^reference_images\.reference_image_(\d+)$")
QWEN_VIDEO_REFERENCE = re.compile(r"^reference_videos\.reference_video_(\d+)$")
QWEN_CONFIG_FIELDS = qwen_adapter.QWEN_CONFIG_FIELDS

DIGITAL_APPEARANCE_LOCK = (
    "数字人口播连续性锁定（最高优先级）：在口播或居中演讲模式中，图1表演主体的角色设定必须逐帧保持一致。"
    "服装与图1可见穿戴配饰必须保持原样，包括服装款式、主色、色相、纹样、材质、层次、领口袖口、发型和肤色；"
    "禁止换装、改色、重染、渐变色偏、补造参考图不可见的服装，禁止新增、删除或替换任何可见配饰。"
)

_POSITIVE_MOTION_PATTERNS = (
    ("挥手", re.compile(r"挥手|招手|\bwave(?:s|d|ing)?\b", re.IGNORECASE)),
    ("指向", re.compile(
        r"指向|指着|\bpoints?\b|\bpointing\b|"
        r"\bpointed\s+(?:at|to|toward|towards)\b", re.IGNORECASE)),
    ("拍手", re.compile(r"拍手|鼓掌|\bclap(?:s|ped|ping)?\b", re.IGNORECASE)),
    ("抬手", re.compile(
        r"抬手|举手|\braise(?:s|d|ing)?\s+(?:(?:the|her|his)\s+)?"
        r"(?:(?:right|left)\s+)?hand\b",
        re.IGNORECASE)),
    ("摊手", re.compile(
        r"摊开.*手|\bopen(?:s|ed|ing)? both hands\b", re.IGNORECASE)),
    ("触胸", re.compile(
        r"触胸|按住胸|\btouch(?:es|ed|ing)? (?:the|her|his) chest\b", re.IGNORECASE)),
    ("头肩动作", re.compile(
        r"点头|摇头|耸肩|\bnod(?:s|ded|ding)?\b|"
        r"\bshake(?:s|d|n|ing)? (?:the|her|his) head\b|"
        r"\bshrug(?:s|ged|ging)?\b", re.IGNORECASE)),
    ("身体移动", re.compile(
        r"前倾|后仰|转身|迈步|走向|\blean(?:s|ed|ing)? forward\b|"
        r"\bturn(?:s|ed|ing)? around\b|\bwalk(?:s|ed|ing)?\b|"
        r"\bstep(?:s|ped|ping)?\b", re.IGNORECASE)),
    ("镜头运动", re.compile(
        r"推镜|拉镜|变焦|摇镜|平移|横移|环绕|跟拍|"
        r"\b(?:dolly|zoom|pan|truck|orbit)(?:s|ed|ing)?\b|"
        r"\barc camera\b|\bcamera (?:moves?|push(?:es)? in|pulls? back)\b",
        re.IGNORECASE)),
    ("新增道具", re.compile(
        r"拿起|手持|麦克风|新增道具|\b(?:pick(?:s|ed|ing)? up|grab(?:s|bed|bing)?|"
        r"hold(?:s|ing) (?:a|the))\s+(?:microphone|tablet|phone|prop)\b|"
        r"\b(?:microphone|new prop) appears?\b", re.IGNORECASE)),
    ("背景变化", re.compile(
        r"背景.*(?:变化|切换|新增|出现)|\bbackground (?:changes?|switches?|transforms?)\b|"
        r"\bnew .* appears? in the background\b", re.IGNORECASE)),
)
_MOTION_NEGATION = re.compile(
    r"禁止|不得|不要|不新增|仅允许|保持姿态|no\b|not\b|without\b|only\b|"
    r"remain(?:s)? still|stay(?:s)? still",
    re.IGNORECASE,
)


def _audit_findings(prompt_text):
    findings = []
    clauses = re.split(r"[\r\n。！？；;]+", str(prompt_text or ""))
    for clause in clauses:
        if _MOTION_NEGATION.search(clause):
            continue
        for label, pattern in _POSITIVE_MOTION_PATTERNS:
            if pattern.search(clause) and label not in findings:
                findings.append(label)
    return findings


def audit_expanded_prompt(prompt_text, mode, approved_motion_text=""):
    """Report positive speech gestures added by an external Qwen expansion."""
    if mode not in {"speaking", "speech_light"}:
        return []
    approved = set(_audit_findings(approved_motion_text))
    return [item for item in _audit_findings(prompt_text) if item not in approved]


def _node(prompt, node_id, class_type, label):
    node = prompt.get(str(node_id))
    if not isinstance(node, dict) or node.get("class_type") != class_type:
        raise ValueError(f"请明确绑定当前工作流的 {label} 节点，不能复用失效节点编号。")
    return node


def image_preview_output_ids(prompt, loader_id):
    """Return PreviewImage outputs for TY reference images and Shot Planner guides."""
    loader_id = str(loader_id or "")
    shot_unpack_ids = {
        str(node_id) for node_id, node in prompt.items()
        if isinstance(node, dict) and node.get("class_type") == "BFSShotUnpack"
        and (node.get("inputs") or {}).get("shot") == [loader_id, 11]
    }
    result = []
    for node_id, node in prompt.items():
        if not isinstance(node, dict) or node.get("class_type") != "PreviewImage":
            continue
        link = (node.get("inputs") or {}).get("images")
        if not isinstance(link, (list, tuple)) or len(link) != 2:
            continue
        direct_reference = str(link[0]) == loader_id and link[1] in (5, 6)
        shot_guide = str(link[0]) in shot_unpack_ids and link[1] == 0
        if direct_reference or shot_guide:
            result.append(str(node_id))
    return result


def execution_output_ids(snapshot, prompt):
    video_id = str(snapshot.get("video_id") or "")
    return ([video_id] if video_id else []) + image_preview_output_ids(
        prompt, snapshot.get("loader_id"))


def _prune_unreachable_canvas_nodes(prompt, roots):
    """Drop disconnected legacy canvas branches from a submitted video graph.

    Direct LongVideo Qwen writes a literal prompt into the H3 node. Older
    workflows can still contain QwenH3PromptLocal and formatter preview nodes
    that are no longer connected, but ComfyUI validates every serialized node
    before execution. Keep the selected video, loader and H3 roots plus their
    actual input ancestors; remove only disconnected branches.
    """
    reachable = set()
    pending = [str(item) for item in roots if item is not None and str(item)]
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        node = prompt.get(node_id)
        if not isinstance(node, dict):
            continue
        reachable.add(node_id)
        for value in (node.get("inputs") or {}).values():
            source_id, source = _linked_node(prompt, value)
            if source is not None and source_id not in reachable:
                pending.append(source_id)
    for node_id in list(prompt):
        if str(node_id) not in reachable:
            prompt.pop(node_id, None)


def _linked_node(prompt, value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None, None
    node_id = str(value[0])
    node = prompt.get(node_id)
    return node_id, node if isinstance(node, dict) else None


def _find_qwen_ancestor(prompt, node_id, seen=None):
    """Find the Qwen node feeding a formatter without a saved canvas ID."""
    seen = set() if seen is None else seen
    node_id = str(node_id)
    if node_id in seen:
        return None
    seen.add(node_id)
    node = prompt.get(node_id)
    if not isinstance(node, dict):
        return None
    if node.get("class_type") == "QwenH3PromptLocal":
        return node_id
    for value in (node.get("inputs") or {}).values():
        source_id, source = _linked_node(prompt, value)
        if source is None:
            continue
        found = _find_qwen_ancestor(prompt, source_id, seen)
        if found:
            return found
    return None


def _derive_bindings(prompt):
    """Derive execution bindings from the actual H3 prompt chain."""
    h3_candidates = [
        (str(node_id), node) for node_id, node in prompt.items()
        if isinstance(node, dict) and node.get("class_type") == "MiniMaxH3ReferenceToVideo"
    ]
    if len(h3_candidates) != 1:
        raise ValueError("导演测试版要求执行图只有一个 H3 参考生成节点。")
    h3_id, h3 = h3_candidates[0]
    prompt_link = h3.get("inputs", {}).get("prompt")
    prompt_output_id, prompt_output = _linked_node(prompt, prompt_link)
    if prompt_output is None:
        prompt_output_id = None
    qwen_id = _find_qwen_ancestor(prompt, prompt_output_id) if prompt_output_id else None
    return {"qwen_id": qwen_id, "h3_id": h3_id,
            "prompt_output_id": prompt_output_id}


def _try_external_bindings(prompt):
    """Return bindings only when a Qwen node is actually connected to H3.

    Rule-mode projects may still contain an unrelated, legacy Qwen node. That
    node must remain inert; only a Qwen ancestor of the selected H3 prompt is
    treated as the image-aware second pass.
    """
    h3_candidates = [
        (str(node_id), node) for node_id, node in prompt.items()
        if isinstance(node, dict)
        and node.get("class_type") == "MiniMaxH3ReferenceToVideo"
    ]
    if len(h3_candidates) != 1:
        return None
    h3_id, h3 = h3_candidates[0]
    output_id, output = _linked_node(prompt, h3.get("inputs", {}).get("prompt"))
    if output is None:
        return None
    qwen_id = _find_qwen_ancestor(prompt, output_id)
    if not qwen_id:
        return None
    return {"qwen_id": qwen_id, "h3_id": h3_id,
            "prompt_output_id": output_id}


def _copy_reference_links(qwen_inputs, h3_inputs):
    """Mirror the selected H3 image/video references into external Qwen."""
    for key in list(qwen_inputs):
        if QWEN_REFERENCE.fullmatch(str(key)) or QWEN_VIDEO_REFERENCE.fullmatch(str(key)):
            del qwen_inputs[key]
    images = sorted(
        (int(match.group(1)), value)
        for key, value in h3_inputs.items()
        if (match := H3_REFERENCE.fullmatch(str(key)))
    )
    videos = sorted(
        (int(match.group(1)), value)
        for key, value in h3_inputs.items()
        if (match := H3_VIDEO_REFERENCE.fullmatch(str(key)))
    )
    for position, (_slot, link) in enumerate(images):
        if not isinstance(link, (list, tuple)) or len(link) != 2:
            raise ValueError("H3 参考图必须是有效图像连线。")
        qwen_inputs[f"reference_images.reference_image_{position}"] = copy.deepcopy(link)
    for position, (_slot, link) in enumerate(videos):
        if not isinstance(link, (list, tuple)) or len(link) != 2:
            raise ValueError("H3 参考视频必须是有效视频连线。")
        qwen_inputs[f"reference_videos.reference_video_{position}"] = copy.deepcopy(link)
    return len(images), len(videos)


def _dual_image_output_mode(snapshot, prompt, h3_inputs):
    """Validate the fixed image_1-image_6 contract when it is connected."""
    loader_id = str(snapshot.get("loader_id") or "")
    unified_slots = []
    for key, value in h3_inputs.items():
        match = H3_REFERENCE.fullmatch(str(key))
        if not match or not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        source = prompt.get(str(value[0])) or {}
        if source.get("class_type") == "TY_H3LVUnified":
            unified_slots.append(int(match.group(1)))
    if not unified_slots:
        return False
    if any(slot not in range(6) for slot in unified_slots):
        raise ValueError("TY_H3LVUnified 输出只能连接到 H3 的 ref_image_0 到 ref_image_5。")
    links = {}
    saw_unified = False
    for slot in sorted(set(unified_slots)):
        value = h3_inputs.get(f"ref_images.ref_image_{slot}")
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            continue
        source_id, output_slot = str(value[0]), int(value[1])
        source = prompt.get(source_id) or {}
        if source_id == loader_id or source.get("class_type") == "TY_H3LVUnified":
            saw_unified = True
            if source.get("class_type") != "TY_H3LVUnified" or output_slot != 5 + slot:
                raise ValueError(
                    f"TY_H3LVUnified 输出连线顺序错误：ref_image_{slot} 必须连接 image_{slot + 1}。")
            links[slot] = [source_id, output_slot]
    if not saw_unified:
        return False
    if len(links) != len(set(unified_slots)) or any(link[0] != loader_id for link in links.values()):
        raise ValueError("TY_H3LVUnified 输出必须完整连接到当前顺序生成节点。")
    image_links = {
        int(match.group(1)): value for key, value in h3_inputs.items()
        if (match := H3_REFERENCE.fullmatch(str(key)))
    }
    if any(slot not in links for slot in image_links):
        raise ValueError("TY_H3LVUnified 六图模式不能与其他参考图来源混接。")
    return True


def qwen_config_from_prompt(prompt, loader_id):
    """Read the appended TY_H3LVUnified controls without importing Qwen nodes."""
    loader = prompt.get(str(loader_id))
    inputs = loader.get("inputs") if isinstance(loader, dict) else None
    if not isinstance(inputs, dict):
        return {}
    if not any(field in inputs for field in QWEN_CONFIG_FIELDS):
        return {}
    config = {field: copy.deepcopy(inputs[field])
              for field in QWEN_CONFIG_FIELDS if field in inputs}
    if not config.get("llm_model") or not config.get("vision_model"):
        raise ValueError("TY_H3LVUnified 的 Qwen 模型必须选择完整文件名。")
    try:
        return qwen_adapter.normalize_qwen_config(config)
    except qwen_adapter.QwenAdapterConfigurationError as exc:
        raise ValueError(str(exc)) from exc


def _direct_qwen(snapshot):
    config = snapshot.get("qwen_config")
    return config if isinstance(config, dict) and config.get("llm_model") and config.get("vision_model") else None


def bind_execution(snapshot, plan):
    """Fail closed for AI/final execution; legacy rule snapshots stay unchanged."""
    enabled = plan.get("planning_method", "rules") == "qwen"
    has_final = any(row.get("prompt_kind") == "final" for row in plan["segments"])
    direct_config = _direct_qwen(snapshot)
    prompt = snapshot["prompt"]
    derived = (_derive_bindings(prompt) if direct_config or enabled or has_final
               else _try_external_bindings(prompt))
    if derived is None:
        return snapshot
    snapshot.update(derived)
    external_qwen_id = str(derived.get("qwen_id") or "")
    external_qwen = None
    if external_qwen_id:
        external_qwen = _node(
            prompt, external_qwen_id, "QwenH3PromptLocal", "Qwen 扩写")
    # A connected canvas Qwen is the deliberate second-pass formatter. The
    # H3LV controls still provide the independent whole-film director config,
    # but they must not silently bypass the user's image-aware Qwen node.
    if direct_config and enabled and external_qwen is None:
        configured = (plan.get("director_base_qwen_config")
                      or plan.get("director_qwen_config") or {})
        for key in QWEN_CONFIG_FIELDS:
            if key in configured and direct_config.get(key) != configured[key]:
                raise ValueError(
                    "当前 TY_H3LVUnified 的 Qwen 配置与已审全片分镜不同，"
                    "请重新预览并审核分镜。")
    h3 = _node(prompt, snapshot.get("h3_id"), "MiniMaxH3ReferenceToVideo", "H3 参考生成")
    dual_image_mode = _dual_image_output_mode(snapshot, prompt, h3.get("inputs") or {})
    # The upstream reference-rewriting helper accepts one H3 node. Do not allow
    # a second ambiguous node to receive a different image set silently.
    if sum(node.get("class_type") == "MiniMaxH3ReferenceToVideo"
           for node in prompt.values()) != 1:
        raise ValueError("导演测试版要求执行图只有一个 H3 参考生成节点。")
    if enabled and external_qwen is not None:
        selected = str(plan.get("director_qwen_source_id") or "")
        if selected and selected != external_qwen_id:
            raise ValueError("生成工作流与已确认导演方案的 Qwen 来源节点不一致。")
        # New drafts use the LongVideo node's own runtime for the whole-film
        # director, while the connected canvas Qwen may intentionally use a
        # different model/projector for image-aware second-pass expansion.
        # Only legacy drafts that explicitly recorded a canvas source retain
        # the old model consistency check.
        if selected:
            config = plan.get("director_qwen_config", {})
            for key in ("llm_model", "vision_model"):
                if key in config and external_qwen.get("inputs", {}).get(key) != config[key]:
                    raise ValueError("Qwen 模型配置与已审导演来源不同，请重新规划审核。")
    if enabled and direct_config is None and external_qwen is None:
        raise ValueError(
            "当前工作流没有连接到 H3 的外部 Qwen 扩写节点，也没有可用的 LongVideo 内置 Qwen。")
    link = h3.get("inputs", {}).get("prompt")
    if external_qwen is not None and not isinstance(link, (list, tuple)):
        raise ValueError("外部 Qwen 扩写必须通过连线接入 H3 prompt。")
    if external_qwen is not None and isinstance(link, (list, tuple)) and len(link) == 2:
        bound = str(snapshot.get("prompt_output_id") or "")
        if bound != str(link[0]) or bound not in prompt:
            raise ValueError("请明确绑定 H3 prompt 上游输出节点以记录最终提示词。")
    return snapshot


def apply_execution_contract(root, snapshot, prompt, plan, row,
                             allow_legacy_regeneration=False):
    """Mutate only this submission, after its H3 reference rewrite."""
    enabled = plan.get("planning_method", "rules") == "qwen"
    final = row.get("prompt_kind", "brief") == "final"
    direct_config = _direct_qwen(snapshot)
    if (not direct_config and not enabled and not final
            and _try_external_bindings(snapshot.get("prompt", {})) is None):
        return {}
    bind_execution(snapshot, plan)
    h3 = _node(prompt, snapshot["h3_id"], "MiniMaxH3ReferenceToVideo", "H3 参考生成")
    dual_image_mode = _dual_image_output_mode(snapshot, prompt, h3.get("inputs") or {})
    preview_ids = image_preview_output_ids(prompt, snapshot.get("loader_id"))
    external_qwen_id = str(snapshot.get("qwen_id") or "")
    external_qwen = (prompt.get(external_qwen_id)
                     if external_qwen_id and isinstance(prompt.get(external_qwen_id), dict)
                     and prompt[external_qwen_id].get("class_type") == "QwenH3PromptLocal"
                     else None)
    if final:
        # Cutting this edge makes Qwen/templates unnecessary to the selected
        # video output. No title-pattern inference and no material-note prefix.
        h3.setdefault("inputs", {})["prompt"] = row["prompt"]
        if direct_config or external_qwen:
            _prune_unreachable_canvas_nodes(
                prompt, (snapshot.get("video_id"), snapshot.get("loader_id"),
                         snapshot.get("h3_id"), *preview_ids))
        return {"prompt_kind": "final", "final_prompt": row["prompt"]}
    from .director_review import effective_segment_brief
    fps = float(plan.get("fps", 24))
    if fps <= 0:
        raise ValueError("项目 FPS 无效。")
    duration = row["generation_frames"] / fps
    if not 1 <= duration <= 60:
        raise ValueError("本段真实生成时长超出 Qwen 支持范围，未修改帧数。")
    request = effective_segment_brief(
        root, plan, row, allow_legacy_regeneration=allow_legacy_regeneration)
    if plan.get("mode") in {"speaking", "speech_light"}:
        request = DIGITAL_APPEARANCE_LOCK + "\n" + request
    if dual_image_mode:
        request = (
            "参考图通用绑定（最高优先级）：按 <Picture 1> 到 <Picture 6> 的连接顺序使用实际存在的参考图；"
            "忽略槽位没有上传内容，不得自行补图、换图或改变参考图顺序。参考图中的可见主体、场景、服装、"
            "发型、配饰和材质必须保持连续；数字人模式仍必须保持已批准人物身份和服装不变。\n"
            + request)
    if external_qwen:
        qwen_inputs = external_qwen.setdefault("inputs", {})
        qwen_inputs["duration"] = duration
        # The external node is the image-aware second pass. Replace only its
        # text source with the approved single-segment brief; retain its own
        # skill, model, sampling and output rules.
        qwen_inputs["prompt"] = request
        image_count, video_count = _copy_reference_links(
            qwen_inputs, h3.get("inputs") or {})
        if image_count > 6:
            raise ValueError("TY_H3LVUnified 最多只能向外部 Qwen 传递六张参考图。")
        # Remove the old text-mixer branch from this submitted copy. The Qwen
        # node and its H3/reference ancestors remain reachable from the output.
        _prune_unreachable_canvas_nodes(
            prompt, (snapshot.get("video_id"), snapshot.get("loader_id"),
                     snapshot.get("h3_id"), *preview_ids))
        return {
            "prompt_kind": "brief", "expander": "external_qwen",
            "qwen_id": external_qwen_id,
            "generation_duration_seconds": duration,
            "content_duration_seconds": (
                row["end_sample"] - row["start_sample"]) / plan["sample_rate"],
            "qwen_input": request, "prompt_binding": "literal_brief",
            "reference_count": image_count, "reference_video_count": video_count,
            "reference_output_mode": "ty_h3lv_unified_dual" if dual_image_mode else "legacy",
        }
    if direct_config:
        from .qwen_h3_engine import QwenH3Engine, reference_media
        images, videos = reference_media(prompt, snapshot["h3_id"])
        if len(images) > 6:
            raise ValueError("TY_H3LVUnified 最多只能传递六张参考图。")
        engine = QwenH3Engine(direct_config)
        result = engine.generate(
            request, images=images, videos=videos, duration=duration)
        h3.setdefault("inputs", {})["prompt"] = result
        _prune_unreachable_canvas_nodes(
            prompt, (snapshot.get("video_id"), snapshot.get("loader_id"),
                     snapshot.get("h3_id"), *preview_ids))
        metadata = {
            "prompt_kind": "brief",
            "generation_duration_seconds": duration,
            "content_duration_seconds": (
                row["end_sample"] - row["start_sample"]) / plan["sample_rate"],
            "qwen_config": copy.deepcopy(direct_config),
            "qwen_input": request,
            "generated_prompt": result,
            "reference_count": len(images),
            "reference_video_count": len(videos),
            "reference_output_mode": "ty_h3lv_unified_dual" if dual_image_mode else "legacy",
        }
        metadata["qwen_metadata"] = copy.deepcopy(engine.last_metadata)
        return metadata
    qwen = _node(prompt, snapshot["qwen_id"], "QwenH3PromptLocal", "Qwen 扩写")
    qwen_inputs = qwen.setdefault("inputs", {})
    fps = float(plan.get("fps", 24))
    if fps <= 0:
        raise ValueError("项目 FPS 无效。")
    duration = row["generation_frames"] / fps
    if not 1 <= duration <= 60:
        raise ValueError("本段真实生成时长超出当前 Qwen 节点支持范围，未修改帧数。")
    qwen_inputs["duration"] = duration
    qwen_inputs["prompt"] = request
    # Rebind to the actual H3 references instead of editing shared LoadImage
    # nodes again. Handles separate loaders, ordered subsets and zero images.
    image_count, video_count = _copy_reference_links(qwen_inputs, h3.get("inputs") or {})
    if image_count > 6:
        raise ValueError("TY_H3LVUnified 最多只能向外部 Qwen 传递六张参考图。")
    return {"prompt_kind": "brief", "qwen_id": str(snapshot["qwen_id"]),
            "generation_duration_seconds": duration,
            "content_duration_seconds": (row["end_sample"]-row["start_sample"])/plan["sample_rate"],
            "qwen_input": qwen_inputs["prompt"], "reference_count": image_count,
            "reference_video_count": video_count,
            "reference_output_mode": "ty_h3lv_unified_dual" if dual_image_mode else "legacy"}


def bound_prompt_from_history(history, node_id):
    """Only inspect the explicitly selected formatter output, never title-scan."""
    if not node_id:
        return ""
    output = history.get("outputs", {}).get(str(node_id), {})
    texts = output.get("text") if isinstance(output, dict) else None
    return "\n".join(str(item) for item in texts).strip() if isinstance(texts, list) else ""
