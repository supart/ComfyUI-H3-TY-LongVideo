import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { createTimeline, pcmWavePeaks } from "./timeline.js";
import { directorNeedsApproval, directorUpdateAction, directorJobText } from "./director_review.js";

const modeLabels = {singing: "唱歌", speaking: "口播", speech_light: "居中演讲"};
const externalQwenFields = ["brief", "entry_state", "exit_state", "timeline"];
const ownerNodeTypes = new Set(["TY_H3LVUnified", "TY_ShotPlanner"]);
const isOwnerNode = node => ownerNodeTypes.has(node?.comfyClass ?? node?.class_type);

if (!document.querySelector("link[data-ty-h3lv-style]")) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = new URL("./ty-h3lv.css", import.meta.url).href;
  link.dataset.tyH3lvStyle = "1";
  document.head.append(link);
}

async function request(path, body) {
  const response = await api.fetchApi(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || JSON.stringify(result));
  return result;
}

function element(tag, text, parent, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  if (parent) parent.append(node);
  return node;
}

function actionButton(parent, label, action, className = "") {
  const node = element("button", label, parent, `ty-h3lv-button ${className}`.trim());
  node.type = "button";
  node.onclick = async event => {
    event?.preventDefault?.();
    event?.stopPropagation?.();
    node.disabled = true;
    try { await action(); } catch (error) {
      await messageDialog({title: "操作失败", message: error.message || String(error), tone: "error"});
    }
    finally { node.disabled = node.dataset.tyH3lvDisabled === "true"; }
  };
  return node;
}

function confirmDialog({title, message, confirmText = "确认", cancelText = "取消",
                        confirmClass = "primary", secondaryText = "", secondaryClass = "", tone = ""}) {
  return new Promise(resolve => {
    const shade = element("div", undefined, document.body,
      "ty-h3lv-shade ty-h3lv-settings-shade ty-h3lv-confirm-shade");
    const panel = element("div", undefined, shade,
      `ty-h3lv-settings-panel ty-h3lv-confirm-panel ${tone ? `ty-h3lv-dialog-${tone}` : ""}`.trim());
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", title);
    element("h2", title, panel);
    element("p", message, panel, "ty-h3lv-confirm-copy");
    const buttons = element("div", undefined, panel, "ty-h3lv-actions ty-h3lv-confirm-actions");
    let finished = false;
    const finish = value => {
      if (finished) return;
      finished = true;
      window.removeEventListener("keydown", onKeyDown);
      shade.remove();
      resolve(value);
    };
    if (cancelText) actionButton(buttons, cancelText, () => finish(false));
    if (secondaryText) actionButton(buttons, secondaryText, () => finish("secondary"), secondaryClass);
    const confirm = actionButton(buttons, confirmText, () => finish(true), confirmClass);
    const onKeyDown = event => {
      if (event.key === "Escape") finish(false);
      if (event.key === "Enter") finish(true);
    };
    window.addEventListener("keydown", onKeyDown);
    shade.onclick = event => { if (event.target === shade) finish(false); };
    queueMicrotask(() => confirm.focus());
  });
}

function messageDialog({title, message, buttonText = "知道了", tone = ""}) {
  return confirmDialog({title, message, confirmText: buttonText, cancelText: null, tone});
}

function toast(summary, detail = "", severity = "info") {
  app.extensionManager?.toast?.add({severity, summary, detail, life: 3000});
}

function editPromptDialog(index, value) {
  return new Promise(resolve => {
    const shade = element("div", undefined, document.body,
      "ty-h3lv-shade ty-h3lv-settings-shade ty-h3lv-prompt-editor-shade");
    const panel = element("div", undefined, shade, "ty-h3lv-settings-panel ty-h3lv-prompt-editor-panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    const title = element("div", undefined, panel, "ty-h3lv-title-row");
    element("h2", `编辑第 ${index + 1} 段镜头简报（提示词）`, title);
    element("p", "这是本段输出的文本，可直接连接下游文本输入。顺序生成时，LongVideo 会结合全片分镜、参考素材和实际生成时长生成本段提示词；旧工作流仍可交给提示词小助手扩写。", panel, "ty-h3lv-help");
    const editor = element("textarea", undefined, panel, "ty-h3lv-prompt-editor");
    editor.value = value;
    editor.spellcheck = false;
    const buttons = element("div", undefined, panel, "ty-h3lv-actions ty-h3lv-confirm-actions");
    let finished = false;
    const finish = result => {
      if (finished) return;
      finished = true;
      window.removeEventListener("keydown", onKeyDown);
      shade.remove();
      resolve(result);
    };
    actionButton(buttons, "取消", () => finish(null));
    actionButton(buttons, "应用到当前草稿", () => finish(editor.value), "primary");
    const onKeyDown = event => { if (event.key === "Escape") finish(null); };
    window.addEventListener("keydown", onKeyDown);
    shade.onclick = event => { if (event.target === shade) finish(null); };
    queueMicrotask(() => editor.focus());
  });
}

function downloadTextFile(name, text) {
  const url = URL.createObjectURL(new Blob([text], {type: "text/markdown;charset=utf-8"}));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function exportDialog({title, note, text, fileName}) {
  return new Promise(resolve => {
    const shade = element("div", undefined, document.body,
      "ty-h3lv-shade ty-h3lv-settings-shade ty-h3lv-confirm-shade");
    const panel = element("div", undefined, shade, "ty-h3lv-settings-panel ty-h3lv-export-panel");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-modal", "true");
    panel.setAttribute("aria-label", title);
    element("h2", title, panel);
    element("p", note, panel, "ty-h3lv-help");
    const editor = element("textarea", undefined, panel, "ty-h3lv-export-text");
    editor.value = text;
    editor.readOnly = true;
    editor.spellcheck = false;
    const buttons = element("div", undefined, panel, "ty-h3lv-actions ty-h3lv-confirm-actions");
    let finished = false;
    const finish = value => {
      if (finished) return;
      finished = true;
      window.removeEventListener("keydown", onKeyDown);
      shade.remove();
      resolve(value);
    };
    const onKeyDown = event => { if (event.key === "Escape") finish(null); };
    window.addEventListener("keydown", onKeyDown);
    actionButton(buttons, "关闭", () => finish(null));
    actionButton(buttons, "复制", async () => {
      await navigator.clipboard.writeText(text);
      toast("已复制到剪贴板");
    });
    actionButton(buttons, "下载 MD", () => {
      downloadTextFile(fileName, text);
      finish(true);
    }, "primary");
    shade.onclick = event => { if (event.target === shade) finish(null); };
    queueMicrotask(() => editor.focus());
  });
}

async function buildGenerationPayload(owner, plan) {
  const snapshot = await app.graphToPrompt();
  const loaders = Object.entries(snapshot.output).filter(([, node]) =>
    ownerNodeTypes.has(node.class_type));
  const videos = Object.entries(snapshot.output).filter(([, node]) => node.class_type === "VHS_VideoCombine");
  if (loaders.length !== 1 || videos.length !== 1) {
    throw new Error("当前工作流需要且只能有一个 TY H3 长视频节点和一个 VHS 输出节点。");
  }
  return {prompt: snapshot.output, workflow: snapshot.workflow,
    loader_id: loaders[0][0], video_id: videos[0][0], client_id: api.clientId || "",
    ...(owner?.comfyClass === "TY_ShotPlanner" ? {replace_snapshot: true} : {}),
    revision: plan.revision, director_approval_id: plan.director_approval?.id ?? null};
}

function ownerWidgetValue(owner, name) {
  return owner?.widgets?.find(widget => widget.name === name)?.value;
}

function qwenConfigFromOwner(owner) {
  const llmModel = ownerWidgetValue(owner, "llm_model");
  const visionModel = ownerWidgetValue(owner, "vision_model");
  if (typeof llmModel !== "string" || !llmModel.trim() ||
      typeof visionModel !== "string" || !visionModel.trim()) return null;
  return {
    skill: ownerWidgetValue(owner, "skill") || "long_video_director",
    duration: Number(ownerWidgetValue(owner, "duration") ?? 10),
    llm_model: llmModel,
    vision_model: visionModel,
    think_mode: Boolean(ownerWidgetValue(owner, "think_mode")),
    reasoning_effort: ownerWidgetValue(owner, "reasoning_effort") || "medium",
    seed: Number(ownerWidgetValue(owner, "seed") ?? 0),
    max_tokens: Number(ownerWidgetValue(owner, "max_tokens") ?? 8192),
    video_sample_frames_per_sec: Number(
      ownerWidgetValue(owner, "video_sample_frames_per_sec") ?? 2),
    force_unload_model: ownerWidgetValue(owner, "force_unload_model") !== false,
  };
}

function sameQwenConfig(left, right) {
  return JSON.stringify(left ?? null) === JSON.stringify(right ?? null);
}

const STORYBOARD_DIALOG_VERSION = "zh-approval-v6";

function directorRunSeed(previous) {
  let value;
  do {
    value = globalThis.crypto?.getRandomValues
      ? globalThis.crypto.getRandomValues(new Uint32Array(1))[0]
      : Math.floor(Math.random() * 0xFFFFFFFF);
  } while (value === 0xFFFFFFFF || value === Number(previous) % 0xFFFFFFFF);
  return value;
}

function directorRequestId() {
  return globalThis.crypto?.randomUUID?.() ||
    `director-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function storyboardTimeRange(row, input) {
  const raw = String(row?.time_range || "");
  const match = raw.match(/([0-9]+(?:\.[0-9]+)?)\s*[-–—]\s*([0-9]+(?:\.[0-9]+)?)/);
  if (match) return `${Number(match[1]).toFixed(3)}–${Number(match[2]).toFixed(3)} 秒`;
  const rate = Number(input?.sample_rate);
  if (rate > 0 && Number.isFinite(Number(row?.start_sample)) &&
      Number.isFinite(Number(row?.end_sample))) {
    return `${(Number(row.start_sample) / rate).toFixed(3)}–${
      (Number(row.end_sample) / rate).toFixed(3)} 秒`;
  }
  return "时间未指定";
}

function storyboardDuration(row) {
  const raw = row?.duration ?? row?.content_duration_seconds;
  const value = Number.parseFloat(String(raw ?? ""));
  return Number.isFinite(value) ? `${value.toFixed(3)} 秒` : "时长未指定";
}

function storyboardContent(row) {
  const value = row?.content ?? row?.text;
  return typeof value === "string" && value.trim() ? value.trim() : "无已确认文字";
}

function timelineClock(value) {
  const milliseconds = Math.max(0, Math.round(Number(value || 0) * 1000));
  const minutes = Math.floor(milliseconds / 60000);
  const seconds = (milliseconds % 60000) / 1000;
  return `${String(minutes).padStart(2, "0")}:${seconds.toFixed(3).padStart(6, "0")}`;
}

function timelineSourceCues(sourceRow, row) {
  const cues = Array.isArray(sourceRow?.speech_cues) ? sourceRow.speech_cues : [];
  if (cues.length) return cues.map((cue, index) => ({
    cue_index: Number.isInteger(cue.cue_index) ? cue.cue_index : index,
    start_seconds: Number(cue.start_seconds), end_seconds: Number(cue.end_seconds),
    text: String(cue.text || ""),
  }));
  const text = String(sourceRow?.text || row?.text || "").trim();
  const duration = Number(sourceRow?.content_duration_seconds ?? row?.duration ?? 0);
  return text ? [{cue_index: 0, start_seconds: 0, end_seconds: duration, text}] : [];
}

function directorTimelineText(draftRow, sourceRow, mode) {
  const cues = timelineSourceCues(sourceRow, draftRow);
  let legacy = false;
  let beats = Array.isArray(draftRow?.beats) ? draftRow.beats : [];
  if (!beats.length && Array.isArray(draftRow?.actions)) {
    legacy = true;
    beats = draftRow.actions.map(action => ({
      start_seconds: Number(action.start_seconds), end_seconds: Number(action.end_seconds),
      speech_cue_indices: cues.filter(cue => cue.end_seconds > Number(action.start_seconds) &&
        cue.start_seconds < Number(action.end_seconds)).map(cue => cue.cue_index),
      shot_description: String(action.description || "保持批准的连续性状态。"),
      pose: "保持批准的姿态与表情。", camera: "locked-off",
      motion: /挥手|招手|指向|摊开|拍手|抬手|转身|wave|point|clap|raise/i.test(
        String(action.description || "")) ? "minimal_gesture" : "hold",
    }));
  }
  const lines = ["[Shot 1] 连续镜头"];
  if (mode === "speaking" || mode === "speech_light") {
    lines.push("时间锚点表示表演节奏，不代表真实切镜。");
  }
  if (legacy) lines.push("[兼容方案] 旧版动作已转换为 H3 时间线。");
  beats.forEach(beat => {
    const indices = Array.isArray(beat.speech_cue_indices) ? beat.speech_cue_indices : [];
    lines.push("", `At ${timelineClock(beat.start_seconds)}–${timelineClock(beat.end_seconds)}`,
      `画面：${String(beat.shot_description || beat.description || "保持批准的连续性状态。")}`,
      `姿态：${String(beat.pose || "保持批准的姿态与表情。")}`,
      `镜头：${!beat.camera || beat.camera === "locked-off" ? "固定机位" : String(beat.camera)}`);
    if (mode === "speaking" || mode === "speech_light") {
      lines.push(`动作策略：${beat.motion === "minimal_gesture" ? "单次轻微手势" : "保持姿态"}`);
    }
  });
  return lines.join("\n");
}

function storyboardModelMessage(captured) {
  if (typeof captured?.model_message === "string" && captured.model_message.trim()) {
    return captured.model_message;
  }
  const input = captured?.model_input || captured?.input_snapshot?.model_input ||
    captured?.input_snapshot?.source || {};
  const mode = modeLabels[input.mode] || input.mode || "未指定";
  const constraints = String(input.constraints || "无额外要求").replace(/\s+/g, " ").trim();
  const lines = [`全片模式：${mode}`, `全片要求：${constraints || "无额外要求"}`, "", "分段："];
  for (const row of Array.isArray(input.segments) ? input.segments : []) {
    lines.push(`第 ${Number(row.segment_index) + 1} 段 · ${storyboardTimeRange(row, input)} · ${storyboardDuration(row)}`);
    lines.push(`内容：${storyboardContent(row)}`);
    const cues = Array.isArray(row.speech_cues) ? row.speech_cues : [];
    if (cues.length) {
      lines.push("语音时间线（段内，相对本段起点）：");
      cues.forEach(cue => lines.push(
        `C${Number(cue.cue_index)} · ${String(cue.time_range || "").replace("-", "–")}：${cue.text}`));
    } else {
      lines.push("语音时间线（段内，相对本段起点）：无已确认文字（由音频参考驱动口型，保持自然停顿）");
    }
    if (typeof row.material_note === "string" && row.material_note.trim()) {
      lines.push(`素材说明：${row.material_note.trim()}`);
    }
    lines.push("");
  }
  return lines.join("\n").trim();
}

const startingProjects = new Set();

async function startApprovedSequence(owner, plan, {approval = null, onlySegmentIndex = null,
  isCurrent = () => true, onApproved = () => {}} = {}) {
  if (startingProjects.has(plan.id)) {
    toast("顺序生成正在启动", "本次点击没有重复提交任务。");
    return false;
  }
  const segmentIndex = Number.isInteger(onlySegmentIndex) ? onlySegmentIndex : null;
  const segmentRow = segmentIndex === null ? null : plan.segments[segmentIndex];
  const forceRegeneration = segmentIndex === null &&
    plan.segments.some(needsReplacement) &&
    !plan.segments.some(row => row.job?.status === "failed");
  if (["running", "pausing", "stopping", "merging"].includes(plan.run_status)) {
    toast("顺序生成正在进行", "本次点击没有重复提交任务。");
    return false;
  }
  const projectId = plan.id;
  startingProjects.add(projectId);
  try {
    if (plan.planning_method === "qwen" && !approval && !forceRegeneration) {
      const director = await request(`/ty-h3lv/project/${encodeURIComponent(projectId)}/director`);
      if (directorNeedsApproval(director)) {
        await openReview(owner);
        return false;
      }
    }
    const payload = await buildGenerationPayload(owner, plan);
    if (segmentIndex !== null && !segmentRow) throw new Error("要生成的分段不存在。");
    payload.force_regeneration = forceRegeneration;
    const failedIndex = segmentIndex === null
      ? plan.segments.findIndex(row => row.job?.status === "failed") : -1;
    const failedRow = plan.segments[failedIndex];
    const failureReason = String(failedRow?.job?.error || "上一次任务被停止，或节点没有生成可用视频。");
    const segmentAction = segmentRow?.job ? "重新生成" : "仅生成";
    const fullRegeneration = segmentIndex === null && plan.full_regeneration_pending;
    const decision = await confirmDialog({
      title: segmentRow ? `${segmentAction}第 ${segmentIndex + 1} 段？` :
        fullRegeneration || forceRegeneration ? "确认新一轮重生成？" :
        failedIndex >= 0 ? `第 ${failedIndex + 1} 段上次没有生成完成` : "确认批量生成？",
      message: (approval ? `将先自动保存当前全部 ${plan.segments.length} 段分镜编辑。\n\n` : "") +
        (segmentRow ? "只提交这一段对应音频，不提交其他片段。已有成功版本会保留，生成失败时可以恢复。" :
          fullRegeneration ? `全片导演规划设置已修改，将重新生成全部 ${plan.segments.length} 段；每段使用新的随机种子，旧视频会保留。` :
          forceRegeneration ? `当前没有运行中的任务，将把 ${plan.segments.filter(needsReplacement).length} 段待重生成内容作为新一轮批量任务提交；每段使用新的随机种子，旧视频会保留。` :
          failedIndex >= 0 ? `第 ${failedIndex + 1} 段失败原因：${failureReason}\n将重试该段，再继续后续分段。` :
            "将启动顺序生成，完成后自动合并；已完成片段只会在标记待重生成时重跑。"),
      confirmText: segmentRow ? `${segmentAction}第 ${segmentIndex + 1} 段` :
          fullRegeneration || forceRegeneration ? "确认新一轮重生成" :
        failedIndex >= 0 ? `重跑第 ${failedIndex + 1} 段并继续` : "确认批量生成",
      secondaryText: !segmentRow && !fullRegeneration && failedIndex >= 0 && !approval ? "重新分析分段" : undefined,
    });
    if (decision === "secondary") {
      await reanalyzeProject(owner, {ask: false});
      return false;
    }
    if (!decision) return false;
    if (!isCurrent()) throw new Error("当前编辑已变化，请重新确认后再生成。");
    if (approval) {
      plan = await request(`/ty-h3lv/project/${projectId}/director/approve`, approval);
      onApproved(plan);
      payload.revision = plan.revision;
      payload.director_approval_id = plan.director_approval?.id ?? null;
    }
    if (!isCurrent()) throw new Error("当前编辑已变化；先前提交的审批已保留，尚未开始生成。");
    if (segmentRow) {
      await request(`/ty-h3lv/project/${projectId}/regenerate`, {...payload, index: segmentIndex});
      toast(`已提交第 ${segmentIndex + 1} 段生成`, "其他片段不会排队；可在 ComfyUI 任务队列中查看进度。");
      return true;
    }
    if (failedIndex >= 0) {
      await request(`/ty-h3lv/project/${projectId}/retry`, {index: failedIndex});
      payload.replace_snapshot = true;
    }
    await request(`/ty-h3lv/project/${projectId}/run`, payload);
    clearVideoNodePreview();
    toast("已开始顺序生成", "可在 ComfyUI 任务队列中查看进度；分段审核界面不会自动打开。");
    return true;
  } finally {
    startingProjects.delete(projectId);
  }
}

function analysisSnapshot(snapshot, targetId) {
  const output = snapshot?.output;
  if (!output || typeof output !== "object") throw new Error("当前工作流没有可执行节点。");
  const needed = new Set([String(targetId)]);
  const visit = id => {
    const node = output[id];
    if (!node || typeof node !== "object") return;
    for (const value of Object.values(node.inputs || {})) {
      if (!Array.isArray(value) || value.length < 2) continue;
      const upstream = String(value[0]);
      if (!Object.hasOwn(output, upstream) || needed.has(upstream)) continue;
      needed.add(upstream);
      visit(upstream);
    }
  };
  visit(String(targetId));
  const missing = [...needed].filter(id => !output[id]?.class_type);
  if (missing.length) {
    throw new Error(`重新分析所需的上游节点缺少类型：${missing.map(id => `#${id}`).join("、")}。请检查对应自定义节点是否加载。`);
  }
  return {...snapshot, output: Object.fromEntries(
    [...needed].map(id => [id, output[id]]))};
}

async function analyzeOnly(owner) {
  const snapshot = await app.graphToPrompt();
  const promptNode = snapshot.output?.[String(owner.id)];
  if (!promptNode) throw new Error("没有在执行图中找到 H3 一体化节点。");
  promptNode.inputs.project_id = "";
  promptNode.inputs.segment_index = 0;
  await api.queuePrompt(0, analysisSnapshot(snapshot, owner.id), {
    partialExecutionTargets: [String(owner.id)]});
}

async function reanalyzeProject(owner, {ask = true} = {}) {
  const projectId = String(owner.properties?.ty_h3lv_project || "").trim();
  if (projectId) {
    try {
      const current = await request(`/ty-h3lv/project/${projectId}`);
      if (["running", "pausing", "stopping", "merging"].includes(current.run_status)) {
        throw new Error("生成任务正在运行，不能重新分析。");
      }
    } catch (error) {
      if (String(error?.message || error).includes("生成任务正在运行")) throw error;
    }
  }
  if (ask && !await confirmDialog({
    title: "重新分析并分段？",
    message: "将按节点当前设置创建一个新的分段项目；现有项目和已生成文件不会删除。",
    confirmText: "重新分析并分段",
  })) return false;
  setProject(owner);
  const previous = document.getElementById("ty-h3lv-panel");
  previous?.querySelector(".ty-h3lv-source-preview video")?.pause?.();
  previous?.querySelector("canvas")?.disposeTimeline?.();
  previous?.remove();
  await analyzeOnly(owner);
  return true;
}

async function openStoryboardDialog(owner) {
  const already = document.getElementById("ty-h3lv-storyboard-dialog");
  if (already) {
    if (already.dataset.tyH3lvDialogVersion !== STORYBOARD_DIALOG_VERSION) {
      already.remove();
    } else {
      already.focus?.();
      return;
    }
  }
  const projectId = String(owner.properties?.ty_h3lv_project ||
    owner.widgets?.find(item => item.name === "project_id")?.value || "").trim();
  if (!projectId) throw new Error("请先分析音频并人工确认切分。");
  const endpoint = suffix => `/ty-h3lv/project/${encodeURIComponent(projectId)}${suffix}`;
  const reviewPanel = () => {
    const current = document.getElementById("ty-h3lv-panel");
    return current?.tyH3lvProjectId === projectId ? current : null;
  };
  const assertClean = () => {
    if (reviewPanel()?.tyH3lvHasEdits?.()) throw new Error("请先保存或确认当前分段、约束及导演编辑。");
  };
  assertClean();
  let [plan, director] = await Promise.all([
    request(endpoint("")), request(endpoint("/director")),
  ]);
  if (!plan.approved) throw new Error("请先保存并确认当前音频切分。");
  // A double click may have opened the dialog while the read requests awaited.
  if (document.getElementById("ty-h3lv-storyboard-dialog")) return;
  const shade = element("div", undefined, document.body, "ty-h3lv-shade ty-h3lv-settings-shade");
  shade.id = "ty-h3lv-storyboard-dialog";
  shade.dataset.tyH3lvDialogVersion = STORYBOARD_DIALOG_VERSION;
  shade.tabIndex = -1;
  const panel = element("div", undefined, shade, "ty-h3lv-settings-panel ty-h3lv-storyboard-panel");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", "Qwen 全片分镜设计");
  element("h2", "Qwen 全片分镜设计", panel);
  element("p", "仅设计分镜，不运行 H3，不使用 H3 skill。保留已确认的全部切点；提示词不写台词、对白或字幕，口型由音频参考驱动。", panel, "ty-h3lv-help");
  element("p", `全片共 ${plan.segments.length} 段 · 当前方案版本 ${plan.revision}`, panel);
  const constraintsLabel = element("label", "全片要求", panel, "ty-h3lv-director-field");
  const constraints = element("textarea", undefined, constraintsLabel, "ty-h3lv-rule-editor");
  constraints.value = director.constraints || "";
  constraints.rows = 4;
  const status = element("p", "先核对完整输入，再确认运行。", panel, "ty-h3lv-notice ty-h3lv-director-job ty-h3lv-storyboard-status");
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  const previewBody = element("section", undefined, panel, "ty-h3lv-storyboard-preview");
  const actions = element("div", undefined, panel, "ty-h3lv-actions");
  let preview = null, submitted = null, timer = null, polling = false, submitting = false;
  let rerunMode = director.job?.status === "completed" && director.draft && !director.stale;
  const disable = (button, value) => {
    button.disabled = Boolean(value);
    button.dataset.tyH3lvDisabled = String(Boolean(value));
  };
  function setConfirmRunning() {
    confirm.textContent = "运行中…";
    confirm.classList.add("is-running");
    confirm.setAttribute("aria-busy", "true");
    disable(confirm, true);
  }
  function clearConfirmRunning() {
    confirm.classList.remove("is-running");
    confirm.removeAttribute("aria-busy");
  }
  function close() {
    if (timer) clearInterval(timer);
    api.removeEventListener?.("ty-h3lv-director", onPhase);
    window.removeEventListener("keydown", onKey);
    shade.remove();
  }
  function onKey(event) { if (event.key === "Escape") close(); }
  function onPhase(event) {
    const data = event.detail || {};
    if (data.project_id !== projectId || data.prompt_id !== submitted?.prompt_id) return;
    if (data.job) {
      status.textContent = directorJobText(data.job);
      status.dataset.status = data.job.status;
    }
    void poll();
  }
  async function showResults() {
    const existing = document.getElementById("ty-h3lv-panel");
    if (existing) {
      if (existing !== reviewPanel() || existing.tyH3lvHasEdits?.()) {
        status.textContent = "新方案已就绪，已保留当前编辑；请保存或确认编辑后刷新导演草稿查看。";
        return false;
      }
      if (!await existing.tyH3lvRefreshDirector?.()) return false;
    } else {
      await openReview(owner);
    }
    close();
    toast("分镜草稿已自动回填", "请审核各段动作与衔接，点击生成时会自动保存当前方案。");
    return true;
  }
  async function poll() {
    if (!shade.isConnected || polling || !submitted) return;
    polling = true;
    try {
      const latest = await request(endpoint("/director"));
      if (!shade.isConnected) return;
      const job = latest.job;
      if (job?.prompt_id !== submitted.prompt_id) {
        clearInterval(timer);
        status.textContent = "项目任务已变化，请在分段审核面板查看；不会覆盖当前方案。";
        return;
      }
      status.textContent = directorJobText(job);
      status.dataset.status = job.status;
      if (["queued", "running", "validating"].includes(job.status)) {
        closeButton.textContent = "关闭（任务继续）";
      } else {
        clearInterval(timer);
        closeButton.textContent = "关闭";
        director = latest;
        submitted = preview = null;
        constraints.disabled = false;
        disable(previewButton, false);
        clearConfirmRunning();
        if (job.status === "completed" && latest.draft && !latest.stale) {
          status.textContent = `完整分镜已通过校验，共 ${latest.draft.result.segments.length} 段。`;
          rerunMode = true;
          confirm.textContent = "重新运行";
          disable(confirm, false);
          disable(results, false);
          const existing = reviewPanel();
          if (existing?.tyH3lvHasEdits?.()) {
            status.textContent += " 新方案已就绪，已保留当前编辑；请确认编辑后查看新草稿。";
          } else {
            await existing?.tyH3lvRefreshDirector?.();
          }
        } else {
          status.textContent += "\n本次任务未产生可用方案，原方案已保留；请重新预览后重试。";
          rerunMode = false;
          confirm.textContent = "确认运行";
          disable(confirm, true);
        }
      }
    } catch (error) {
      status.textContent = `暂时无法获取状态：${error.message}。不会重复提交任务。`;
    } finally { polling = false; }
  }
  async function readPreview({keepRerun = false, runSeed, rerunOf} = {}) {
    assertClean();
    preview = null;
    disable(confirm, true);
    plan = await request(endpoint(""));
    const payload = {revision: plan.revision, qwen_source_id: "",
      constraints: constraints.value, client_id: api.clientId || ""};
    if (runSeed !== undefined) payload.run_seed = runSeed;
    if (rerunOf) payload.rerun_of = rerunOf;
    const qwenConfig = qwenConfigFromOwner(owner);
    if (qwenConfig) payload.qwen_config = qwenConfig;
    const captured = await request(endpoint("/director/preview"), payload);
    if (!shade.isConnected) return;
    if (constraints.value !== payload.constraints) {
      status.textContent = "预览期间全片要求发生变化，请重新预览。";
      return;
    }
    preview = {value: captured, payload};
    if (!keepRerun) rerunMode = false;
    confirm.textContent = rerunMode ? "重新运行" : "确认运行";
    previewBody.replaceChildren();
    const input = captured.model_input || captured.input_snapshot?.model_input ||
      captured.input_snapshot?.source || {}, config = captured.model_config;
    const modelMessage = storyboardModelMessage(captured);
    element("p", `模型：${config.llm_model} · 投影：${config.vision_model} · seed ${config.seed} · 输出预算 ${config.max_tokens} · thinking ${config.think_mode ? "开启" : "关闭"} · 推理强度 ${config.reasoning_effort}`, previewBody);
    for (const row of input.segments) {
      const card = element("section", undefined, previewBody, "ty-h3lv-director-card");
      element("h4", `第 ${row.segment_index + 1} 段 · ${storyboardTimeRange(row, input)} · ${storyboardDuration(row)}`, card);
      element("p", storyboardContent(row), card, "ty-h3lv-storyboard-words");
      if (row.material_note) element("p", `素材说明：${row.material_note}`, card);
      if (row.fixed || row.locked || row.prompt_kind === "final") element("p", "人工锁定或 final 提示词：不覆盖。", card);
    }
    const full = element("section", undefined, previewBody, "ty-h3lv-director-original");
    element("h4", "发送给 Qwen 的简洁文本", full);
    element("pre", modelMessage, full);
    status.textContent = (director.job ? `${directorJobText(director.job)}\n` : "") +
      "已预览全部分段。确认后只运行这份输入；如有变化会拒绝提交。";
    status.dataset.status = director.job?.status || "";
    disable(confirm, false);
  }
  const previewButton = actionButton(actions, "预览将发送的内容", () => readPreview());
  const confirm = actionButton(actions, "确认运行", async () => {
    if (submitted || submitting) return;
    assertClean();
    submitting = true;
    setConfirmRunning();
    status.textContent = "正在同步最新全片要求并提交……";
    disable(confirm, true);
    disable(previewButton, true);
    constraints.disabled = true;
    try {
      if (rerunMode) {
        await readPreview({
          keepRerun: true, rerunOf: director.job.prompt_id,
          runSeed: directorRunSeed(director.job.run_seed ?? director.draft?.model_config?.seed),
        });
        setConfirmRunning();
      } else if (!preview || preview.payload.constraints !== constraints.value) {
        await readPreview();
        setConfirmRunning();
      }
      if (!preview) throw new Error("请先预览完整输入。");
      if (!sameQwenConfig(preview.payload.qwen_config, qwenConfigFromOwner(owner))) {
        throw new Error("Qwen 模型或推理设置已变化，请重新预览。");
      }
      submitted = await request(endpoint("/director/plan"), {
        ...preview.payload,
        preview_fingerprint: preview.value.preview_fingerprint,
        request_id: directorRequestId(),
      });
      status.textContent = `已提交文本任务 ${submitted.prompt_id}；没有提交视频。`;
      closeButton.textContent = "关闭（任务继续）";
      if (shade.isConnected) {
        timer = setInterval(poll, 2000);
        await poll();
      }
    } catch (error) {
      // An ambiguous network failure must not invite an immediate duplicate.
      preview = null;
      rerunMode = false;
      confirm.textContent = "确认运行";
      clearConfirmRunning();
      disable(confirm, true);
      constraints.disabled = false;
      disable(previewButton, false);
      status.textContent = `提交未确认：${error.message}；请检查顶部任务状态后重新预览，勿重复提交。`;
      throw error;
    } finally { submitting = false; }
  }, "primary");
  const results = actionButton(actions, "查看分段结果", showResults);
  const closeButton = actionButton(actions, "取消", close);
  disable(confirm, true);
  disable(results, true);
  constraints.oninput = () => {
    preview = null;
    rerunMode = false;
    confirm.textContent = "确认运行";
    clearConfirmRunning();
    disable(confirm, false);
    status.textContent = "全片要求已修改；点击确认运行时会自动同步最新内容。";
  };
  window.addEventListener("keydown", onKey);
  api.addEventListener?.("ty-h3lv-director", onPhase);
  if (director.job && ["queued", "running", "validating"].includes(director.job.status)) {
    submitted = {prompt_id: director.job.prompt_id};
    constraints.disabled = true;
    setConfirmRunning();
    disable(previewButton, true);
    closeButton.textContent = "关闭（任务继续）";
    timer = setInterval(poll, 2000);
    await poll();
  } else {
    status.textContent = directorJobText(director.job);
    status.dataset.status = director.job?.status || "";
    if (director.job?.status === "completed" && director.draft && !director.stale) {
      disable(results, false);
    }
    try { await readPreview({keepRerun: true}); } catch (error) {
      status.textContent = `${directorJobText(director.job)}\n无法预览：${error.message}`;
    }
  }
  if (shade.isConnected) constraints.focus?.();
}

async function openDirectorRules(owner) {
  const nodeMode = owner?.widgets?.find(item => item.name === "mode")?.value;
  const selectedMode = Object.hasOwn(modeLabels, nodeMode) ? nodeMode : "singing";
  const modeLabel = modeLabels[selectedMode];
  document.getElementById("ty-h3lv-rules")?.remove();
  const shade = element("div", undefined, document.body, "ty-h3lv-shade ty-h3lv-settings-shade");
  shade.id = "ty-h3lv-rules";
  const panel = element("div", undefined, shade, "ty-h3lv-settings-panel ty-h3lv-rules-panel");
  const title = element("div", undefined, panel, "ty-h3lv-title-row");
  element("h2", `${modeLabel}运镜规则`, title);
  actionButton(title, "关闭", () => shade.remove(), "ty-h3lv-close");
  const explanation = selectedMode === "speaking"
    ? "当前节点选择了 speaking。这里只编辑口播规则：连续固定机位和跨段一致构图。"
    : selectedMode === "speech_light"
    ? "居中演讲：人物始终正面居中，不走位、不左右漂移，镜头、人物大小和背景透视固定，禁止推拉及其他运镜。framing 只能选择 medium close-up（胸像）或 medium shot（半身），各段保持同一景别；angle=front、movement=steady 不可改为运动。唱歌的镜头活跃度/最远景别不影响此模式，口型、眨眼和克制手势保留。"
    : "唱歌景别由节点的“最远允许景别”直接控制；旧 allowed_framings 字段不再限制景别。dynamic 使用可见横移、环绕或推拉，不选择轻微构图调整。这里编辑能量对应的运镜候选及相邻运动关系。";
  element("p", `${explanation} 规则不判断图片内容，也不决定音频被切成几段；保存后只用于重新分析的新项目。`, panel, "ty-h3lv-help");
  const rules = await request("/ty-h3lv/rules");
  let fullConfig = JSON.parse(rules.config_text);
  if (!fullConfig[selectedMode]) {
    throw new Error("当前后端尚未加载此模式。请等任务结束后重启 ComfyUI，并刷新页面。");
  }
  const location = element("p", `当前模式：${modeLabel} · 保存位置：${rules.directory}`, panel, "ty-h3lv-notice");
  const form = element("div", undefined, panel, "ty-h3lv-settings-form");
  const configLabel = element("label", `${modeLabel}运镜配置（JSON）`, form);
  const config = element("textarea", undefined, configLabel, "ty-h3lv-rule-editor ty-h3lv-rule-config");
  config.value = JSON.stringify(fullConfig[selectedMode], null, 2);
  config.rows = selectedMode === "speaking" ? 10 : 28;
  const buttons = element("div", undefined, panel, "ty-h3lv-actions");
  actionButton(buttons, "保存并校验", async () => {
    try {
      fullConfig[selectedMode] = JSON.parse(config.value);
    } catch (_error) {
      throw new Error(`${modeLabel}规则不是有效的 JSON。`);
    }
    const saved = await request("/ty-h3lv/rules", {
      config_text: JSON.stringify(fullConfig),
    });
    fullConfig = JSON.parse(saved.config_text);
    config.value = JSON.stringify(fullConfig[selectedMode], null, 2);
    location.textContent = `当前模式：${modeLabel} · 保存位置：${saved.directory} · 版本 ${saved.revision}`;
    await messageDialog({title: `${modeLabel}规则已保存`, message: `其他模式的规则没有改变。重新分析音频后，新项目会使用这套${modeLabel}规则。`});
  }, "primary");
  actionButton(buttons, `恢复${modeLabel}默认规则`, async () => {
    if (!await confirmDialog({
      title: `恢复${modeLabel}默认规则？`,
      message: `只覆盖${modeLabel}规则，其他模式和已经生成的项目不会改变。`,
      confirmText: "恢复默认",
      tone: "warning",
    })) return;
    const reset = await request("/ty-h3lv/rules/reset", {mode: selectedMode});
    fullConfig = JSON.parse(reset.config_text);
    config.value = JSON.stringify(fullConfig[selectedMode], null, 2);
    location.textContent = `当前模式：${modeLabel} · 保存位置：${reset.directory} · 版本 ${reset.revision}`;
  });
  shade.onclick = event => { if (event.target === shade) shade.remove(); };
}

const originalWidgetComputeSize = new WeakMap();

function setWidgetHidden(widget, hidden) {
  if (!widget) return;
  if (!originalWidgetComputeSize.has(widget)) originalWidgetComputeSize.set(widget, widget.computeSize);
  widget.hidden = hidden;
  widget.computeSize = hidden ? (() => [0, -4]) : originalWidgetComputeSize.get(widget);
}

function resizeNodeToVisibleWidgets(node) {
  const computed = node.computeSize?.();
  if (computed) node.setSize?.([Math.max(node.size?.[0] || 0, computed[0]), computed[1]]);
  node.setDirtyCanvas?.(true, true);
}

function confidenceClass(value, kind) {
  if (kind === "endpoint") return "neutral";
  if (value === null || value === undefined || value < .5) return "risk";
  return value >= .8 ? "safe" : "review";
}

function needsReplacement(row) {
  return Boolean(row.needs_regeneration && row.job?.status === "completed" && row.job?.video);
}

function outputPreviewUrl(preview) {
  const query = new URLSearchParams({
    filename: preview.filename,
    subfolder: preview.subfolder || "",
    type: preview.type || "output",
  });
  return api.apiURL(`/view?${query.toString()}`);
}

function sourceVideoPreviewUrl(plan) {
  const parts = String(plan?.source_video || "").replaceAll("\\", "/").split("/").filter(Boolean);
  const filename = parts.pop();
  if (!plan?.id || !filename || parts.includes("..")) return "";
  return outputPreviewUrl({
    filename,
    subfolder: ["TYH3LongVideo", "projects", plan.id, ...parts].join("/"),
    type: "output",
  });
}

function refreshAnalysisButton(owner) {
  const button = owner.widgets?.find(item =>
    item.name === "分析并分段" || item.name === "重新分析并分段");
  if (!button) return;
  const projectId = owner.properties?.ty_h3lv_project ||
    owner.widgets?.find(item => item.name === "project_id")?.value;
  button.label = (owner.comfyClass ?? owner.class_type) === "TY_ShotPlanner" && !projectId
    ? "分析并分段" : "重新分析并分段";
  owner.setDirtyCanvas?.(true, false);
}

function setWidgetValue(owner, widget, value) {
  if (!widget || Object.is(widget.value, value)) return false;
  const oldValue = widget.value;
  if (typeof widget.setValue === "function") {
    widget.setValue(value, {e: null, node: owner, canvas: app.canvas});
  } else {
    widget.value = value;
    widget.callback?.(value);
    owner.onWidgetChanged?.(widget.name, value, oldValue, widget);
    owner.graph?.incrementVersion?.();
  }
  return true;
}

function setProject(owner, projectId = "") {
  owner.properties = {...owner.properties, ty_h3lv_project: projectId};
  const project = owner.widgets?.find(item => item.name === "project_id");
  setWidgetValue(owner, project, projectId);
  refreshAnalysisButton(owner);
}

function applySourceVideo(owner, value) {
  const widget = owner.widgets?.find(item => item.name === "source_video");
  if (!widget) throw new Error("没有找到 source_video 控件。");
  if (Array.isArray(widget.options?.values) && !widget.options.values.includes(value)) {
    widget.options.values.push(value);
  }
  setProject(owner);
  const changed = setWidgetValue(owner, widget, value);
  owner.widgets?.find(item => item.name === "source_video_preview")?.refreshSource(!changed);
  owner.setDirtyCanvas?.(true, true);
}

function addSourceUploadControls(owner) {
  const row = element("div", undefined, null, "ty-h3lv-source-upload");
  const controls = owner.addDOMWidget("source_video_upload", "buttons", row, {
    serialize: false, hideOnZoom: false, getMinHeight: () => 40, getMaxHeight: () => 40,
  });
  controls.serialize = false;
  controls.computeSize = width => [width, 40];
  for (const [label, clip] of [["上传视频", false], ["上传并截取视频", true]]) {
    actionButton(row, label, () => uploadSourceVideo(owner, {clip}));
  }
  for (const name of ["pointerdown", "pointermove", "pointerup", "click", "dblclick"]) {
    row.addEventListener(name, event => event.stopPropagation());
  }
  const oldRemove = controls.onRemove;
  controls.onRemove = function () {
    controls.uploadEpoch = (controls.uploadEpoch || 0)+1;
    controls.closeSourceClip?.();
    return oldRemove?.apply(this, arguments);
  };
}

function addSourceVideoPreview(owner) {
  const source = owner.widgets.find(item => item.name === "source_video");
  const container = element("div", undefined, null, "ty-h3lv-node-video");
  const video = element("video", undefined, container);
  video.controls = true;
  video.preload = "metadata";
  video.playsInline = true;
  video.setAttribute("aria-label", "源视频预览");
  const error = element("div", "视频无法预览，请检查文件或使用浏览器支持的编码。",
    container, "ty-h3lv-node-video-error");
  error.setAttribute("role", "status");
  error.hidden = true;
  container.hidden = true;
  for (const name of ["pointerdown", "pointermove", "pointerup", "click", "dblclick"]) {
    container.addEventListener(name, event => event.stopPropagation());
  }
  const height = (width = owner.size[0]) => container.hidden ? 0 :
    Math.min(300, Math.max(120, (width - 20) / (video.videoWidth / video.videoHeight || 16 / 9))) + 20;
  const preview = owner.addDOMWidget("source_video_preview", "preview", container, {
    serialize: false, hideOnZoom: false,
    getMinHeight: height, getMaxHeight: height,
  });
  preview.serialize = false;
  preview.hidden = true;
  preview.computeSize = width => [width, container.hidden ? -4 : height(width)];
  let currentUrl = "";
  preview.refreshSource = (force = false) => {
    const path = String(source.value || "").replaceAll("\\", "/");
    const parts = path.split("/").filter(Boolean);
    const filename = parts.pop();
    const url = filename && filename !== ".." && !path.startsWith("/") &&
      !path.includes(":") && !parts.includes("..") ?
      outputPreviewUrl({filename, subfolder: parts.join("/"), type: "input"}) : "";
    if (!force && url === currentUrl) return;
    currentUrl = url;
    video.pause();
    video.removeAttribute("src");
    video.hidden = false;
    error.hidden = true;
    container.hidden = preview.hidden = !url;
    if (url) video.src = url + (force ? `&timestamp=${Date.now()}` : "");
    video.load();
    resizeNodeToVisibleWidgets(owner);
  };
  video.onloadedmetadata = () => resizeNodeToVisibleWidgets(owner);
  video.onerror = () => {
    if (!currentUrl) return;
    video.hidden = true;
    error.hidden = false;
  };
  let previousSource = String(source.value || "");
  const oldCallback = source.callback;
  source.callback = function () {
    if (String(source.value || "") !== previousSource) setProject(owner);
    previousSource = String(source.value || "");
    const result = oldCallback?.apply(this, arguments);
    preview.refreshSource();
    return result;
  };
  const oldConfigure = owner.onConfigure;
  owner.onConfigure = function () {
    const result = oldConfigure?.apply(this, arguments);
    previousSource = String(source.value || "");
    preview.refreshSource();
    refreshAnalysisButton(owner);
    return result;
  };
  const oldAdded = owner.onAdded;
  owner.onAdded = function () {
    const result = oldAdded?.apply(this, arguments);
    previousSource = String(source.value || "");
    preview.refreshSource();
    refreshAnalysisButton(owner);
    return result;
  };
  const oldRemove = preview.onRemove;
  preview.onRemove = function () {
    currentUrl = "";
    video.pause();
    video.removeAttribute("src");
    video.load();
    return oldRemove?.apply(this, arguments);
  };
  preview.refreshSource();
}

function referenceSlotLimit() {
  if ((app.graph?._nodes || []).some(node => node.comfyClass === "TY_ShotPlanner")) return 6;
  let limit = 0;
  for (const node of app.graph?._nodes || []) {
    if (node.comfyClass !== "MiniMaxH3ReferenceToVideo") continue;
    for (const input of node.inputs || []) {
      const match = /^ref_images\.ref_image_(\d+)$/.exec(String(input.name || ""));
      if (match && input.link != null) limit = Math.max(limit, Number(match[1]) + 1);
    }
  }
  return limit;
}

function graphLinkOrigin(input) {
  const link = app.graph?.links?.[input?.link] || app.graph?._links?.[input?.link];
  if (!link) return null;
  const originId = link.origin_id ?? link.originId;
  const node = app.graph?.getNodeById?.(originId) ||
    (app.graph?._nodes || []).find(item => String(item.id) === String(originId));
  return node ? {node, slot: link.origin_slot ?? link.originSlot} : null;
}

function dualImageOutputMode() {
  if ((app.graph?._nodes || []).some(node => node.comfyClass === "TY_ShotPlanner")) return true;
  for (const h3 of app.graph?._nodes || []) {
    if (h3.comfyClass !== "MiniMaxH3ReferenceToVideo") continue;
    const links = (h3.inputs || [])
      .filter(input => /^ref_images\.ref_image_\d+$/.test(String(input.name || "")))
      .map(input => graphLinkOrigin(input));
    if (links.some(item => item?.node?.comfyClass === "TY_H3LVUnified")) return true;
  }
  return false;
}

function referenceRoleLabel(slot) {
  return `参考图 ${Number(slot) + 1}`;
}

function referencePreviewUrl(projectId, name) {
  return api.apiURL(`/ty-h3lv/project/${encodeURIComponent(projectId)}/refs/${encodeURIComponent(name)}`);
}

async function uploadReferenceAsset(projectId, file, index = null) {
  const form = new FormData();
  if (index !== null && index !== undefined) form.append("index", String(index));
  form.append("image", file, file.name);
  const response = await api.fetchApi(
    `/ty-h3lv/project/${encodeURIComponent(projectId)}/refs`, {method: "POST", body: form});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || "参考图上传失败。");
  return result;
}

async function uploadSourceVideo(owner, {clip = false} = {}) {
  const controls = owner.widgets?.find(item => item.name === "source_video_upload");
  if (controls?.sourceBusy) return;
  const graph = owner.graph;
  const uploadEpoch = controls?.uploadEpoch || 0;
  if (controls) {
    controls.sourceBusy = true;
    for (const button of controls.element.children) button.disabled = true;
  }
  try {
    const file = await new Promise(resolve => {
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "video/*,.mp4,.mov,.mkv,.webm,.avi,.m4v";
      input.onchange = () => resolve(input.files?.[0] || null);
      input.oncancel = () => resolve(null);
      input.click();
    });
    if (!file || (controls?.uploadEpoch || 0) !== uploadEpoch) return;
    const form = new FormData();
    form.append("image", file, file.name);
    form.append("type", "input");
    form.append("overwrite", clip ? "false" : "true");
    if (clip) form.append("subfolder", `TYH3LongVideo/uploads/${crypto.randomUUID()}`);
    const response = await api.fetchApi("/upload/image", {method: "POST", body: form});
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || "视频上传失败。");
    if (owner.graph !== graph || (controls?.uploadEpoch || 0) !== uploadEpoch) return;
    const value = [result.subfolder, result.name || file.name].filter(Boolean).join("/");
    if (clip) await openSourceClipDialog(owner, value);
    else applySourceVideo(owner, value);
  } finally {
    if (controls) {
      controls.sourceBusy = false;
      for (const button of controls.element.children) button.disabled = false;
    }
  }
}

function openSourceClipDialog(owner, source) {
  document.getElementById("ty-h3lv-source-clip")?.closeSourceClip?.();
  const controls = owner.widgets?.find(item => item.name === "source_video_upload");
  const graph = owner.graph;
  const shade = element("div", undefined, document.body, "ty-h3lv-shade ty-h3lv-settings-shade");
  shade.id = "ty-h3lv-source-clip";
  const panel = element("div", undefined, shade, "ty-h3lv-settings-panel ty-h3lv-source-clip");
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-modal", "true");
  panel.setAttribute("aria-label", "截取视频");
  const header = element("div", undefined, panel, "ty-h3lv-title-row");
  element("h2", "截取视频", header);
  const closeButton = actionButton(header, "关闭", () => close(), "ty-h3lv-close");
  const preview = element("div", undefined, panel, "ty-h3lv-source-preview");
  const video = element("video", undefined, preview);
  video.controls = true; video.preload = "metadata"; video.playsInline = true;
  video.setAttribute("aria-label", "截取源视频预览");
  const parts = source.replaceAll("\\", "/").split("/");
  video.src = outputPreviewUrl({filename: parts.pop(), subfolder: parts.join("/"), type: "input"});
  const status = element("div", "正在读取原音轨…", panel, "ty-h3lv-source-preview-status");
  status.setAttribute("role", "status");
  const canvas = element("canvas", undefined, panel, "ty-h3lv-timeline");
  canvas.hidden = true;
  let plan = null, selection = null, busy = false, closed = false, previewTime = 0, animation = 0;
  let resolveClosed;
  const finished = new Promise(resolve => { resolveClosed = resolve; });
  function close() {
    if (closed) return;
    closed = true;
    globalThis.cancelAnimationFrame?.(animation);
    canvas.disposeTimeline?.();
    video.pause(); video.removeAttribute("src"); video.load();
    video.onloadedmetadata = video.onplay = video.onpause = video.onseeked = video.ontimeupdate = video.onerror = null;
    window.removeEventListener("keydown", onKeyDown);
    if (controls?.closeSourceClip === close) controls.closeSourceClip = null;
    shade.remove();
    resolveClosed();
  }
  function seek(time) {
    video.pause();
    previewTime = Math.max(0, Math.min(plan?.duration || time, time));
    if (video.readyState >= 1) video.currentTime = previewTime;
    canvas.drawTimeline?.();
  }
  function syncPlayback() {
    if (closed || !selection) return;
    if (!video.paused && (video.currentTime >= selection.end || video.currentTime < selection.start)) {
      video.pause();
      video.currentTime = Math.max(selection.start, selection.end-1/24);
    }
    previewTime = video.currentTime;
    canvas.drawTimeline?.();
  }
  function tick() {
    animation = 0;
    syncPlayback();
    if (!closed && !video.paused) animation = globalThis.requestAnimationFrame?.(tick) || 0;
  }
  const onKeyDown = event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
    if (event.key === "Tab") {
      const focusable = [...panel.querySelectorAll("button,input,video")].filter(item => !item.disabled && !item.hidden);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  };
  window.addEventListener("keydown", onKeyDown);
  shade.onclick = event => { if (event.target === shade) close(); };
  shade.closeSourceClip = close;
  if (controls) controls.closeSourceClip = close;
  video.onloadedmetadata = () => { video.currentTime = previewTime; };
  video.onseeked = video.ontimeupdate = syncPlayback;
  video.onpause = () => { globalThis.cancelAnimationFrame?.(animation); animation = 0; syncPlayback(); };
  video.onplay = () => {
    if (!selection || busy) { video.pause(); return; }
    document.querySelectorAll("video,audio").forEach(item => { if (item !== video) item.pause(); });
    if (video.currentTime < selection.start || video.currentTime >= selection.end) video.currentTime = selection.start;
    if (!animation) animation = globalThis.requestAnimationFrame?.(tick) || 0;
  };
  video.onerror = () => { status.textContent = "浏览器无法预览该视频编码，仍可根据原音轨设置截取范围。"; };
  const actions = {
    only: true,
    change(range, edge = "start") {
      if (busy || closed) return;
      const total = Math.floor(plan.duration*24+1e-7);
      let first = Math.max(0, Math.min(total-1, Math.floor(range.start*24+.5)));
      let last = Math.max(1, Math.min(total, Math.floor(range.end*24+.5)));
      if (last <= first) {
        if (edge === "start") first = last-1;
        else last = first+1;
      }
      selection = {start: first/24, end: last/24};
      seek(selection[edge]);
    },
    cancel: close,
    async confirm() {
      if (busy || closed || !selection) return;
      busy = true; video.pause(); canvas.drawTimeline?.();
      status.textContent = "正在截取视频…";
      try {
        const result = await request("/ty-h3lv/source/clip", {
          source_video: source, start_seconds: selection.start, end_seconds: selection.end,
        });
        if (closed || owner.graph !== graph) return;
        applySourceVideo(owner, result.source_video);
        close();
      } catch (error) {
        if (!closed) status.textContent = `截取失败：${error.message}；可保留当前范围重试。`;
      } finally {
        busy = false;
        if (!closed) canvas.drawTimeline?.();
      }
    },
  };
  const retry = actionButton(panel, "重新读取原音轨", () => prepare());
  retry.hidden = true;
  async function prepare() {
    retry.hidden = true;
    status.textContent = "正在读取原音轨…";
    try {
      const info = await request("/ty-h3lv/source/preview", {source_video: source});
      if (closed) return;
      plan = {...info, id: source, max_seconds: info.duration, segments: [{}]};
      selection = {start: 0, end: Math.floor(info.duration*24+1e-7)/24};
      const rows = [{end: {value: info.duration}}];
      createTimeline(canvas, () => ({plan, rows, selected: 0, analysis: info,
        clipSelection: selection, clipBusy: busy, previewTime}),
      (_index, time) => seek(time), () => {}, () => {}, null, actions);
      canvas.hidden = false;
      status.textContent = video.error ? "浏览器不支持该编码，可根据原音轨设置截取范围。" : "";
      canvas.drawTimeline();
    } catch (error) {
      if (!closed) { status.textContent = `原音轨读取失败：${error.message}`; retry.hidden = false; }
    }
  }
  closeButton.focus?.();
  prepare();
  return finished;
}

async function removeReferenceImage(projectId, name) {
  return request(`/ty-h3lv/project/${encodeURIComponent(projectId)}/refs/remove`, {name});
}

function showFinalOnVideoNode(preview, projectId) {
  if (!preview?.filename) return false;
  const nodes = app.graph?._nodes || [];
  const owners = nodes.filter(isOwnerNode);
  const ownerProjects = owners.map(node => String(
    node.properties?.ty_h3lv_project || node.widgets?.find(item => item.name === "project_id")?.value || ""
  ).trim()).filter(Boolean);
  if (projectId && ownerProjects.length && !ownerProjects.includes(String(projectId))) return false;
  const videos = nodes.filter(node => node.comfyClass === "VHS_VideoCombine");
  if (videos.length !== 1 || typeof videos[0].updateParameters !== "function") return false;
  const key = `${preview.subfolder || ""}/${preview.filename}`;
  if (videos[0].__tyH3lvFinalPreview === key) return true;
  videos[0].updateParameters(preview, true);
  videos[0].__tyH3lvFinalPreview = key;
  return true;
}

function clearVideoNodePreview() {
  const videos = (app.graph?._nodes || []).filter(node => node.comfyClass === "VHS_VideoCombine");
  if (videos.length !== 1) return false;
  const node = videos[0];
  const preview = node.widgets?.find(widget => widget.name === "videopreview");
  if (!preview) return false;
  preview.videoEl?.pause();
  preview.videoEl?.removeAttribute("src");
  preview.videoEl?.load();
  if (preview.videoEl) preview.videoEl.hidden = true;
  preview.imgEl?.removeAttribute("src");
  if (preview.imgEl) preview.imgEl.hidden = true;
  if (preview.parentEl) preview.parentEl.hidden = true;
  if (preview.value && typeof preview.value === "object") preview.value.params = {};
  preview.aspectRatio = null;
  delete node.__tyH3lvFinalPreview;
  node.setDirtyCanvas?.(true, true);
  return true;
}

async function restoreFinalVideoPreview() {
  const owners = (app.graph?._nodes || []).filter(isOwnerNode);
  const projectIds = [...new Set(owners.map(node => String(
    node.properties?.ty_h3lv_project || node.widgets?.find(item => item.name === "project_id")?.value || ""
  ).trim()).filter(Boolean))];
  for (const projectId of projectIds) {
    try {
      const plan = await request(`/ty-h3lv/project/${encodeURIComponent(projectId)}`);
      if (plan.final_preview) showFinalOnVideoNode(plan.final_preview, projectId);
    } catch {}
  }
}

function statusText(plan, director, hasDirectorEdits = false) {
  const completed = plan.segments.filter(row => row.job?.status === "completed" && !needsReplacement(row)).length;
  const stale = plan.segments.filter(needsReplacement).length;
  const stage = ["running", "pausing", "stopping", "merging"].includes(plan.run_status) ? "生成中" :
    !plan.approved ? "切分待确认" :
    directorNeedsApproval(director) || hasDirectorEdits ? "分镜草稿已就绪" : "可开始生成";
  return `状态：${plan.run_status} · ${stage} · ${completed}/${plan.segments.length} 段可用`+
    `${stale ? ` · ${stale}段待重生成` : ""}${plan.final_stale ? " · 当前成片为旧版" : ""}${plan.error ? ` · ${plan.error}` : ""}`;
}

async function openReview(owner) {
  const shotPlanner = owner?.comfyClass === "TY_ShotPlanner";
  let plan = null;
  let analysis = null;
  let director = null;
  let directorDirty = false;
  let configDirty = false;
  let submittingDirector = false;
  let directorRows = [];
  let rows = [];
  let details = [];
  let dirty = false;
  let topologyDirty = false;
  let editEpoch = 0;
  let selected = 0;
  let previewTime = Number.NaN;
  let previewPlaybackSegment = -1;
  let previewAnimation = 0;
  let pendingPreviewTime = 0;
  let previewProject = "";
  let sourceVideo = null;
  const previous = document.getElementById("ty-h3lv-panel");
  previous?.querySelector(".ty-h3lv-source-preview video")?.pause?.();
  previous?.querySelector("canvas")?.disposeTimeline?.();
  previous?.remove();
  const shade = element("div", undefined, document.body, "ty-h3lv-shade");
  shade.id = "ty-h3lv-panel";
  shade.tyH3lvHasEdits = () => dirty || directorDirty || configDirty;
  shade.tyH3lvRefreshDirector = () => load({preserveEdits: true});
  shade.setAttribute("role", "dialog");
  shade.setAttribute("aria-modal", "true");
  shade.setAttribute("aria-label", shotPlanner ? "TY Shot Planner 视频切分审核" : "TY H3 长视频分段审核");
  const closeReview = () => {
    globalThis.cancelAnimationFrame?.(previewAnimation);
    previewAnimation = 0;
    sourceVideo?.pause?.();
    shade.querySelector("canvas")?.disposeTimeline?.();
    shade.remove();
  };
  const panel = element("div", undefined, shade, "ty-h3lv-panel");
  const header = element("header", undefined, panel, "ty-h3lv-header");
  const titleRow = element("div", undefined, header, "ty-h3lv-title-row");
  element("h2", shotPlanner ? "TY Shot Planner · 视频切分审核" : "TY H3 长视频 · 音频切分审核", titleRow);
  actionButton(titleRow, "关闭", async () => {
    if ((dirty || directorDirty || configDirty) && !await confirmDialog({
      title: "关闭并放弃未保存修改？",
      message: "未确认的切分或导演编辑不会自动保存。",
      confirmText: "关闭", tone: "warning",
    })) return;
    closeReview();
  }, "ty-h3lv-close");
  const projectRow = element("div", undefined, header, "ty-h3lv-project-row");
  const selectLabel = element("label", "分析项目", projectRow);
  const select = element("select", undefined, selectLabel);
  const projects = (await request("/ty-h3lv/projects")).filter(project => shotPlanner
    ? project.project_kind === "shot_planner" : project.project_kind !== "shot_planner");
  for (const project of projects) {
    const mode = modeLabels[project.mode] || "唱歌";
    const origin = project.clip_origin;
    const range = origin ? ` · 截取 ${origin.start_seconds.toFixed(3)}—${origin.end_seconds.toFixed(3)}s` : "";
    const option = element("option", `${new Date(project.created*1000).toLocaleString()} · ${mode} · ${project.duration.toFixed(2)}s · ${project.count}段${range} · ${project.id.slice(0,8)}`, select);
    option.value = project.id;
  }
  const widget = owner.widgets?.find(item => item.name === "project_id");
  const preferred = owner.properties?.ty_h3lv_project || widget?.value;
  if (projects.some(project => project.id === preferred)) select.value = preferred;
  const defaultRow = element("label", "默认参考图张数", projectRow, "ty-h3lv-default-references");
  const defaultSelect = element("select", undefined, defaultRow);
  defaultSelect.onchange = () => { if (plan) markDirty(); };
  const librarySection = element("section", undefined, header, "ty-h3lv-reference-library");
  const libraryHeader = element("div", undefined, librarySection, "ty-h3lv-reference-header");
  element("span", "图像素材库", libraryHeader, "ty-h3lv-audio-label");
  element("span", "上传顺序自动编号，数量不限", libraryHeader, "ty-h3lv-help");
  const libraryInput = document.createElement("input");
  libraryInput.type = "file";
  libraryInput.accept = "image/*";
  libraryInput.multiple = true;
  libraryInput.hidden = true;
  libraryHeader.append(libraryInput);
  const libraryList = element("div", undefined, librarySection, "ty-h3lv-reference-library-list");
  const libraryUpload = actionButton(libraryHeader, "上传图像素材（可多选）", () => {
    if (!plan) throw new Error("请先选择一个分析项目。");
    libraryInput.click();
  }, "reference-add");

  function libraryNames() {
    const names = [];
    const add = value => {
      const name = typeof value === "string" ? value : value?.name;
      if (name && !names.includes(name)) names.push(name);
    };
    (plan?.reference_library || []).forEach(add);
    (plan?.reference_assets || []).forEach(add);
    (plan?.reference_defaults || []).forEach(add);
    (plan?.segments || []).forEach(row => {
      (row.refs || []).forEach(add);
      (row.reference_slots || []).forEach(add);
    });
    return names;
  }

  function applyAssetResponse(result) {
    if (!plan) return;
    if (Array.isArray(result?.assets)) {
      plan.reference_library = result.assets.map(item => item.name).filter(Boolean);
      plan.reference_assets = result.assets;
    } else if (result?.name) {
      const names = libraryNames();
      if (!names.includes(result.name)) names.push(result.name);
      plan.reference_library = names;
      plan.reference_assets = names.map((name, index) => ({name, label: `图${index + 1}`}));
    }
    syncReferenceLibrary();
    rows.forEach(row => row.renderRefs?.());
  }

  function syncReferenceLibrary() {
    const names = libraryNames();
    libraryList.replaceChildren();
    libraryUpload.disabled = !plan;
    if (!names.length) {
      element("p", "还没有图像素材。可在这里批量上传，也可在分段内上传后自动加入素材库。",
        libraryList, "ty-h3lv-reference-empty");
      return;
    }
    names.forEach((name, index) => {
      const item = element("figure", undefined, libraryList, "ty-h3lv-reference-library-item");
      item.title = name;
      const image = document.createElement("img");
      image.src = plan ? referencePreviewUrl(plan.id, name) : "";
      image.alt = `图${index + 1}`;
      image.loading = "lazy";
      item.append(image);
      element("figcaption", `图${index + 1}`, item);
      const remove = actionButton(item, "移除", async () => {
        const inUse = rows.some(row =>
          (row.referenceSlots || []).some(slot => slot?.name === name) ||
          (row.refs || []).includes(name));
        if (inUse) throw new Error("该素材已被分段选用，请先取消分段中的选择再移除。");
        await removeReferenceImage(plan.id, name);
        plan.reference_library = names.filter(value => value !== name);
        plan.reference_assets = plan.reference_library.map((value, position) =>
          ({name: value, label: `图${position + 1}`}));
        syncReferenceLibrary();
        rows.forEach(row => row.renderRefs?.());
      }, "reference-tool");
      remove.title = "从素材库移除（正在使用的素材不能移除）";
    });
  }

  libraryInput.onchange = async () => {
    const files = [...(libraryInput.files || [])];
    libraryInput.value = "";
    if (!files.length || !plan) return;
    libraryUpload.disabled = true;
    try {
      for (const file of files) applyAssetResponse(await uploadReferenceAsset(plan.id, file));
      toast("素材已加入图像素材库", `已按上传顺序加入 ${files.length} 张图片。`);
    } catch (error) {
      await messageDialog({title: "素材上传失败", message: error.message || String(error), tone: "error"});
    } finally {
      libraryUpload.disabled = libraryUpload.dataset.tyH3lvDisabled === "true";
    }
  };

  function syncDefaultReferenceControl() {
    const available = referenceSlotLimit();
    const current = plan?.reference_default_count;
    const highest = Math.max(available, Number.isInteger(current) ? current : 0);
    defaultSelect.replaceChildren();
    const fallback = element("option", "全部（沿用画布）", defaultSelect);
    fallback.value = "all";
    for (let count = 0; count <= highest; count += 1) {
      const item = element("option", String(count), defaultSelect);
      item.value = String(count);
    }
    defaultSelect.value = current === null || current === undefined ? "all" : String(current);
    defaultRow.hidden = dualImageOutputMode() || available === 0;
  }

  function defaultReferenceText() {
    const value = plan?.reference_default_count;
    if (value === null || value === undefined) return "画布上全部已接出的图片";
    if (!value) return "不使用参考图";
    return `画布上的前 ${value} 张`;
  }

  const status = element("div", "", header, "ty-h3lv-status");
  const directorJobStatus = element("div", "", header, "ty-h3lv-director-job");
  directorJobStatus.setAttribute("role", "status");
  directorJobStatus.setAttribute("aria-live", "polite");
  const controls = element("div", undefined, header, "ty-h3lv-actions");
  const content = element("main", undefined, panel, "ty-h3lv-content");
  const overview = element("section", undefined, content, "ty-h3lv-overview");
  element("p", shotPlanner
    ? "点击波形定位后可切割；选中切点可右键或按 Del 删除并合并。手动分段最长 15 秒，提示词统一使用工作流外部 fixed_prompt。绿色按钮保存并开始串行批量。"
    : "先看整曲切点和风险，再逐段试听。调整完成后保存并确认；在生成工作流中点击绿色按钮启动完整循环。",
    overview, "ty-h3lv-help");
  const notice = element("div", "", overview, "ty-h3lv-notice");
  const sourcePreview = shotPlanner
    ? element("section", undefined, overview, "ty-h3lv-source-preview") : null;
  const sourcePreviewStatus = sourcePreview
    ? element("div", "源视频预览加载中…", sourcePreview, "ty-h3lv-source-preview-status") : null;
  if (sourcePreview) {
    sourceVideo = element("video", undefined, sourcePreview);
    sourceVideo.controls = true;
    sourceVideo.preload = "metadata";
    sourceVideo.playsInline = true;
  }
  const canvas = element("canvas", undefined, overview, "ty-h3lv-timeline");
  canvas.tabIndex = 0;
  canvas.setAttribute("role", "img");
  canvas.setAttribute("aria-label", "整首歌原曲和人声波形；内部切点可用鼠标或触控拖动，键盘用户可使用下方结束时间输入框");
  let selectedTrack = "original";
  const selectedBar = element("div", undefined, overview, "ty-h3lv-selected-bar");
  const selectedInfo = element("div", undefined, selectedBar, "ty-h3lv-selected-info");
  const trackSwitch = element("div", undefined, selectedInfo, "ty-h3lv-track-switch");
  const trackButtons = {};
  for (const [value, label] of [["original", "原曲"], ["vocals", "人声"]]) {
    const button = element("button", label, trackSwitch, "ty-h3lv-track-button");
    button.type = "button";
    button.onclick = () => selectTrack(value);
    trackButtons[value] = button;
  }
  const selectedText = element("strong", "", selectedInfo);
  const selectedAudio = element("audio", undefined, selectedBar);
  selectedAudio.controls = true;
  selectedAudio.preload = "metadata";

  function previewBounds(index = selected) {
    const start = index > 0 ? Number(rows[index-1]?.end.value || 0) : 0;
    const end = Number(rows[index]?.end.value || plan?.duration || 0);
    return {start, end};
  }
  function segmentAtTime(time) {
    const index = rows.findIndex(row => time <= Number(row.end.value)+1e-6);
    return index < 0 ? Math.max(0, rows.length-1) : index;
  }
  function updateSourcePreviewStatus(message = "") {
    if (!sourcePreviewStatus) return;
    if (message) {
      sourcePreviewStatus.textContent = message;
      sourcePreviewStatus.classList.add("is-error");
      return;
    }
    sourcePreviewStatus.classList.remove("is-error");
    const {start, end} = previewBounds();
    const current = Number.isFinite(previewTime) ? previewTime : start;
    sourcePreviewStatus.textContent = `当前视频预览：第 ${selected+1} 段 · ${start.toFixed(3)}—${end.toFixed(3)}s · `+
      `当前 ${current.toFixed(3)}s`;
  }
  function pauseOtherMedia(active) {
    if (active !== sourceVideo) sourceVideo?.pause?.();
    if (active !== selectedAudio) selectedAudio.pause?.();
    rows.forEach(row => {
      if (row.vocals !== active) row.vocals?.pause?.();
    });
  }
  function syncSourcePlayback() {
    if (!sourceVideo || !rows.length) return;
    previewTime = Math.max(0, Math.min(Number(plan?.duration || sourceVideo.duration || 0),
      Number(sourceVideo.currentTime) || 0));
    if (!sourceVideo.paused && previewPlaybackSegment >= 0) {
      const {start, end} = previewBounds(previewPlaybackSegment);
      if (previewTime >= end-1e-3) {
        sourceVideo.pause?.();
        previewTime = Math.max(start, end-.5/24);
        sourceVideo.currentTime = previewTime;
        previewPlaybackSegment = -1;
      }
    }
    const index = segmentAtTime(previewTime);
    if (index !== selected) updateSelected(index, true, false);
    updateSourcePreviewStatus();
    canvas.drawTimeline?.();
  }
  function previewLoop() {
    previewAnimation = 0;
    syncSourcePlayback();
    if (sourceVideo && !sourceVideo.paused && shade.isConnected) {
      previewAnimation = globalThis.requestAnimationFrame?.(previewLoop) || 0;
    }
  }
  function seekSourceVideo(time, pause = true) {
    if (!sourceVideo || !Number.isFinite(Number(time))) return;
    if (pause) sourceVideo.pause?.();
    previewPlaybackSegment = -1;
    pendingPreviewTime = Math.max(0, Math.min(Number(plan?.duration || time), Number(time)));
    previewTime = pendingPreviewTime;
    if (sourceVideo.readyState >= 1) sourceVideo.currentTime = pendingPreviewTime;
    updateSourcePreviewStatus();
    canvas.drawTimeline?.();
  }
  function syncSourceVideo(positionAtSegment = false) {
    if (!sourceVideo || !plan) return false;
    const url = sourceVideoPreviewUrl(plan);
    if (!url) {
      sourceVideo.removeAttribute?.("src");
      sourceVideo.load?.();
      sourcePreview.hidden = true;
      return false;
    }
    sourcePreview.hidden = false;
    const changed = previewProject !== plan.id || sourceVideo.dataset.previewUrl !== url;
    if (changed) {
      sourceVideo.pause?.();
      previewProject = plan.id;
      sourceVideo.dataset.previewUrl = url;
      pendingPreviewTime = previewBounds().start;
      previewTime = pendingPreviewTime;
      sourcePreviewStatus.textContent = "源视频预览加载中…";
      sourceVideo.src = url;
      sourceVideo.load?.();
    } else if (positionAtSegment) {
      seekSourceVideo(previewBounds().start);
    }
    return changed;
  }
  if (sourceVideo) {
    sourceVideo.onloadedmetadata = () => {
      sourceVideo.currentTime = pendingPreviewTime;
      updateSourcePreviewStatus();
    };
    sourceVideo.onplay = () => {
      pauseOtherMedia(sourceVideo);
      previewPlaybackSegment = segmentAtTime(sourceVideo.currentTime);
      updateSelected(previewPlaybackSegment, true, false);
      globalThis.cancelAnimationFrame?.(previewAnimation);
      previewAnimation = globalThis.requestAnimationFrame?.(previewLoop) || 0;
    };
    sourceVideo.onpause = () => {
      globalThis.cancelAnimationFrame?.(previewAnimation);
      previewAnimation = 0;
      syncSourcePlayback();
    };
    sourceVideo.onseeked = () => {
      previewPlaybackSegment = sourceVideo.paused ? -1 : segmentAtTime(sourceVideo.currentTime);
      syncSourcePlayback();
    };
    sourceVideo.ontimeupdate = syncSourcePlayback;
    sourceVideo.onerror = () => updateSourcePreviewStatus(
      "源视频无法在浏览器中预览；请检查文件是否存在或编码是否受浏览器支持。音轨审核仍可继续。");
  }
  selectedAudio.onplay = () => pauseOtherMedia(selectedAudio);
  const directorSection = element("section", undefined, content, "ty-h3lv-director");
  element("h3", "全片导演 · 文字镜头预演", directorSection);
  element("p", "① 确认音频切分 → ② 生成并检查导演草稿 → ③ 点击生成，自动保存当前分镜并开始视频生成。规划方式与口播／唱歌表演模式相互独立。", directorSection, "ty-h3lv-help");
  const directorForm = element("div", undefined, directorSection, "ty-h3lv-director-form");
  const methodLabel = element("label", "规划方式", directorForm);
  const methodSelect = element("select", undefined, methodLabel);
  for (const [value, text] of [["rules", "原规则规划"], ["qwen", "Qwen 全片导演"]]) {
    element("option", text, methodSelect).value = value;
  }
  const constraintsLabel = element("label", "全片统一约束（不修改生成参数）", directorSection, "ty-h3lv-director-field");
  const constraintsEditor = element("textarea", undefined, constraintsLabel, "ty-h3lv-material-note");
  constraintsEditor.rows = 3;
  const directorStatus = element("p", "", directorSection, "ty-h3lv-notice");
  directorStatus.setAttribute("role", "status");
  const directorActions = element("div", undefined, directorSection, "ty-h3lv-actions");
  const contractLabel = element("label", "AI 全片合同（审核后可修改）", directorSection, "ty-h3lv-director-field");
  const contractEditor = element("textarea", undefined, contractLabel, "ty-h3lv-material-note");
  contractEditor.rows = 3;
  directorSection.hidden = shotPlanner;
  directorJobStatus.hidden = shotPlanner;
  const segmentsBody = element("section", undefined, content, "ty-h3lv-segments");
  const endpoint = (suffix = "") => `/ty-h3lv/project/${encodeURIComponent(select.value)}${suffix}`;
  const state = () => ({plan, analysis, rows, selected, previewTime});

  function markDirty() {
    editEpoch++;
    dirty = true;
    const completed = plan.segments.filter(row => row.job?.status === "completed" && !needsReplacement(row)).length;
    status.textContent = `状态：draft · 修改未保存，尚不可生成 · ${completed}/${plan.segments.length} 段完成`;
    status.classList.add("is-dirty");
    updateDirectorControls();
  }

  function markDirectorDirty() {
    editEpoch++;
    directorDirty = true;
    directorStatus.textContent = ["running", "pausing", "stopping", "merging"].includes(plan?.run_status)
      ? "导演内容有未保存修改；点击“保存并确认”可立即保存，当前任务结束后按新内容重生成。"
      : "导演内容有未保存修改；点击生成时会自动保存。";
    updateDirectorControls();
  }

  function updateDirectorControls() {
    const usesQwen = !shotPlanner && methodSelect.value === "qwen";
    constraintsLabel.hidden = !usesQwen;
    contractLabel.hidden = !usesQwen || !director?.draft?.result;
    const busy = ["running", "pausing", "stopping", "merging"].includes(plan?.run_status);
    const pending = !shotPlanner && ["queued", "running", "validating"].includes(director?.job?.status);
    const submitting = submittingDirector || startingProjects.has(plan?.id);
    const disable = (button, value) => {
      button.disabled = Boolean(value);
      button.dataset.tyH3lvDisabled = String(Boolean(value));
    };
    const reason = !plan?.approved ? "请先保存并确认音频切分。" :
      dirty || configDirty ? "请先保存切分或规划设置。" :
      directorDirty ? (busy
        ? "当前分镜有未保存编辑；可点击“保存并确认”，当前任务结束后按新内容重生成。"
        : "当前分镜有未保存编辑；点击生成可自动保存，刷新草稿可放弃修改。") :
      busy || submitting ? "当前项目正在生成、合成或提交，请先等待结束。" :
      pending ? "导演任务已提交，请等待完成，不能重复排队。" :
      director?.qwen_available === false ? "LongVideo 独立 Qwen 分镜运行时或共享模型不可用。" : "";
    disable(queueDirectorButton, Boolean(reason));
    queueDirectorButton.title = reason || "预览全部分段文案，再确认文本规划任务。";
    directorJobStatus.textContent = directorJobText(director?.job) +
      (reason ? `\n${reason}` : "");
    if (director?.job?.status === "completed" && !directorNeedsApproval(director) && !directorDirty) {
      directorJobStatus.textContent = directorJobStatus.textContent.replace("可检查后生成", "已保存，可生成");
    }
    directorJobStatus.dataset.status = director?.job?.status || "";
    const needsApproval = usesQwen && (directorDirty || directorNeedsApproval(director));
    const replacementCount = plan?.segments?.filter(needsReplacement).length || 0;
    const forceRegeneration = replacementCount > 0 && !busy && !pending;
    const runReason = !plan?.approved ? "请先保存并确认音频切分。" :
      busy || submitting ? "当前项目正在生成、合成或提交，请等待结束。" :
      pending ? "请等待分镜任务完成。" :
      usesQwen && director?.stale && !forceRegeneration ? "分镜已过期，请重新规划并审核。" :
      needsApproval && !director?.draft?.result && !forceRegeneration ? "请先生成完整的分镜草稿。" : "";
    disable(runButton, Boolean(runReason));
    runButton.title = runReason || (configDirty
      ? `生成前会自动保存规划设置，并重跑全部 ${plan?.segments?.length || 0} 段。`
      : dirty
      ? "生成前会自动保存当前分镜修改，并重跑需要替换的片段。"
      : needsApproval ? "生成前会自动保存当前全部分镜编辑。" : "");
    const failedIndex = plan?.segments.findIndex(row => row.job?.status === "failed") ?? -1;
    runButton.textContent = failedIndex >= 0 ? `▶ 重试第 ${failedIndex+1} 段并继续` :
      replacementCount > 0 ? `▶ 重生成 ${replacementCount} 段并继续` :
      (["paused", "stopped"].includes(plan?.run_status) ? "▶ 继续顺序生成" :
        shotPlanner ? "保存并开始批量" : "▶ 批量生成");
    select.disabled = submitting;
    if (plan && !dirty) status.textContent = statusText(plan, director, directorDirty);
  }

  methodSelect.onchange = constraintsEditor.oninput = () => {
    editEpoch++;
    configDirty = true;
    directorStatus.textContent = "规划设置尚未保存。";
    updateDirectorControls();
  };
  contractEditor.oninput = markDirectorDirty;

  function renderDirectorState() {
    methodSelect.value = director?.planning_method || "rules";
    constraintsEditor.value = director?.constraints || "";
    contractEditor.value = reviewResult()?.global_contract || "";
    const job = director?.job;
    directorStatus.textContent = director?.error || job?.error || (methodSelect.value === "rules"
      ? "原规则模式：沿用现有分段简报，不需要额外模型。"
      : director.stale ? "导演方案已过期或生成期间项目发生变化；保留结果但不可确认，请重新规划。"
      : job && ["queued", "running", "validating"].includes(job.status)
        ? `导演任务 ${job.status}${job.prompt_id ? ` · ${job.prompt_id}` : ""}。完成后自动回填，编辑中的内容不会覆盖；不会自动生成视频。`
      : director.approval?.draft_id === director.draft?.id && director.approval
        ? "分镜方案已保存。可直接批量生成，或先使用下方“仅生成本段”测试；已完成片段只会在标记待重生成时重跑。"
      : director.draft ? "分镜草稿已就绪，检查后点击生成即可自动保存。"
      : "先保存并确认切分，然后生成导演草稿；此按钮只运行 Qwen 文本规划，不运行 H3。");
    updateDirectorControls();
  }

  function reviewResult() {
    return director?.approval?.draft_id === director?.draft?.id && !director?.stale
      ? plan?.approved_director || director?.draft?.result : director?.draft?.result;
  }

  actionButton(directorActions, "保存规划方式与全片约束", async () => {
    if (dirty) throw new Error("请先保存并确认切分修改。");
    if (directorDirty) throw new Error("当前分镜卡片有修改；请点击生成自动保存，或刷新草稿放弃修改。");
    await request(endpoint("/director/configure"), {
      revision: plan.revision, planning_method: methodSelect.value, constraints: constraintsEditor.value,
    });
    await load();
  });
  async function queueDirector() {
    if (dirty || configDirty || directorDirty) throw new Error("请先保存设置并确认切分；未保存的导演卡片修改不能被覆盖。");
    if (!plan?.approved) throw new Error("请先保存并确认音频切分。");
    await openStoryboardDialog(owner);
  }
  actionButton(directorActions, "刷新导演草稿", async () => {
    if (dirty) throw new Error("请先保存分段修改，避免刷新覆盖当前切点。");
    if ((directorDirty || configDirty) && !await confirmDialog({
      title: "放弃未保存的导演修改？",
      message: "刷新将重新读取磁盘上的导演方案，不会运行模型。",
      confirmText: "放弃修改并刷新", tone: "warning",
    })) return;
    await load();
  });
  function directorApprovalPayload() {
    if (dirty || configDirty) throw new Error("请先保存并确认切分及规划设置。");
    if (!director?.draft || director.stale) throw new Error("需要一份与当前输入一致的完整导演草稿。");
    const segments = directorRows.map(item => {
      return {...item.value, intent: item.intent.value, brief: item.brief.value,
        entry_state: item.entry.value, exit_state: item.exit.value,
        next_transition: item.transition.value, timeline: item.timeline.value,
        prompt_locked: item.lock.checked};
    });
    return {revision: plan.revision, draft_id: director.draft.id,
      global_contract: contractEditor.value, segments};
  }
  async function saveCurrentReview() {
    let revision = plan.revision;
    const cutEdits = dirty;
    if (dirty) {
      const saved = await request(endpoint("/edit"), {revision,
        reference_default_count: dualImageOutputMode() ? null :
          (defaultSelect.value === "all" ? null : Number(defaultSelect.value)),
        reference_defaults: dualImageOutputMode() ? (plan.reference_defaults || []) : undefined,
        segments: rows.map(row => ({end: Number(row.end.value), prompt: row.prompt.value,
          material_note: row.note.value,
          refs: row.dualMode ? row.legacyRefs.filter(Boolean) : row.refs,
          ...(row.dualMode ? {reference_slots: row.referenceSlots} : {})}))});
      plan = saved;
      revision = saved.revision;
      dirty = false;
      topologyDirty = false;
    }
    plan = await request(endpoint("/approve"), {revision});
    revision = plan.revision;
    if (configDirty) {
      plan = await request(endpoint("/director/configure"), {
        revision, planning_method: methodSelect.value, constraints: constraintsEditor.value,
      });
      revision = plan.revision;
      configDirty = false;
    }
    if (directorDirty) {
      if (cutEdits) {
        throw new Error("切分或参考图已修改，当前导演草稿已失效；请先刷新导演草稿，再保存导演卡片。");
      }
      const approval = directorApprovalPayload();
      approval.revision = revision;
      plan = await request(endpoint("/director/approve"), approval);
      directorDirty = false;
    }
    return plan;
  }
  async function runGeneration(onlySegmentIndex = null) {
    if (submittingDirector || startingProjects.has(plan.id)) return;
    if (dirty && directorDirty) {
      throw new Error("切分或参考图与导演卡片同时有修改；请先分别保存并确认，再生成。");
    }
    if (configDirty && directorDirty) {
      throw new Error("规划设置与导演卡片同时有修改；请先保存规划设置并刷新导演草稿，再生成。");
    }
    const epoch = editEpoch, projectId = plan.id;
    let started = false;
    submittingDirector = true;
    updateDirectorControls();
    try {
      // A prompt/reference edit is already the user's generation intent. Save
      // it here so the green batch button really regenerates changed takes.
      if (dirty || configDirty) plan = await saveCurrentReview();
      if (!plan?.approved) throw new Error("请先确认分段。");
      if (!directorDirty) {
        directorJobStatus.textContent = "正在同步最新分镜状态……";
        const refreshed = await load({preserveEdits: true, allowSubmitting: true});
        if (refreshed === false) throw new Error("当前页面有未保存的导演编辑，请先确认或放弃编辑。");
      }
      const needsDirectorApproval = !shotPlanner && plan.planning_method === "qwen" &&
        (directorDirty || directorNeedsApproval(director));
      const approval = needsDirectorApproval && !director?.stale && director?.draft?.result
        ? directorApprovalPayload() : null;
      started = await startApprovedSequence(owner, plan, {
        approval, onlySegmentIndex,
        isCurrent: () => epoch === editEpoch && projectId === plan.id,
        onApproved: approved => {
          if (epoch === editEpoch && projectId === plan.id) {
            plan = approved;
            directorDirty = false;
          }
        },
      });
    } finally {
      submittingDirector = false;
      if (started) {
        closeReview();
      } else {
        await load({preserveEdits: true, allowSubmitting: true});
      }
      updateDirectorControls();
    }
  }
  function previewUrl(index, vocals = false) {
    const start = index === 0 ? 0 : Number(rows[index-1].end.value);
    const end = Number(rows[index].end.value);
    return api.apiURL(endpoint(`/audio?index=${shotPlanner ? 0 : index}&start=${start.toFixed(6)}&end=${end.toFixed(6)}`+
      `${vocals ? "&vocals=1" : ""}&revision=${plan.revision}`));
  }
  function selectedPreviewUrl(index) {
    return previewUrl(index, selectedTrack === "vocals");
  }
  function syncTrackSwitch() {
    for (const [value, button] of Object.entries(trackButtons)) {
      button.classList.toggle("is-active", value === selectedTrack);
      button.setAttribute("aria-pressed", String(value === selectedTrack));
    }
  }
  function selectTrack(value) {
    if (selectedTrack === value) return;
    selectedTrack = value;
    syncTrackSwitch();
    if (rows.length) selectedAudio.src = selectedPreviewUrl(selected);
    updateSelected(selected, false, false);
  }
  syncTrackSwitch();
  function refreshPreviewAudio(index) {
    if (!rows[index]) return;
    if (selected === index || selected === index+1) selectedAudio.src = selectedPreviewUrl(selected);
    if (rows[index].vocals) rows[index].vocals.src = previewUrl(index, true);
  }
  function updateSelected(index, refreshAudio = true, positionVideo = true) {
    if (!plan?.segments.length) return;
    selected = Math.max(0, Math.min(plan.segments.length-1, index));
    const start = selected === 0 ? 0 : Number(rows[selected-1].end.value);
    const end = Number(rows[selected].end.value);
    selectedText.textContent = `当前试听（${selectedTrack === "vocals" ? "人声" : "原曲"}）：`+
      `第 ${selected+1} 段 · ${start.toFixed(3)}—${end.toFixed(3)}s`;
    if (refreshAudio) selectedAudio.src = selectedPreviewUrl(selected);
    if (positionVideo && sourceVideo) seekSourceVideo(start);
    else updateSourcePreviewStatus();
    canvas.drawTimeline?.();
  }
  function refreshDraftDisplays() {
    rows.forEach((item, index) => {
      const start = index === 0 ? 0 : Number(rows[index-1].end.value);
      const end = Number(item.end.value);
      const duration = end-start;
      const editFrames = Math.round(end*24)-Math.round(start*24);
      const generationFrames = 5+17*Math.ceil((Math.max(124, Math.ceil(duration*24), editFrames)-5)/17);
      item.time.textContent = `${start.toFixed(3)}—${end.toFixed(3)}s`;
      item.duration.textContent = `${duration.toFixed(3)}s`;
      item.editFrames.textContent = `${editFrames} 剪辑帧`;
      item.generationFrames.textContent = `${generationFrames} 生成帧`;
    });
  }
  function segmentTimingMarkdown() {
    const lines = ["# TY H3 长视频 · 分段时长", "",
      "| 分段 | 开始(s) | 结束(s) | 时长(s) |",
      "| --- | --- | --- | --- |"];
    let total = 0;
    rows.forEach((item, index) => {
      const start = index === 0 ? 0 : Number(rows[index-1].end.value);
      const end = Number(item.end.value);
      total += end-start;
      lines.push(`| 第${index+1}段 | ${start.toFixed(3)} | ${end.toFixed(3)} | ${(end-start).toFixed(3)} |`);
    });
    lines.push("", `共 ${rows.length} 段 · 合计 ${total.toFixed(3)}s`);
    return {text: lines.join("\n"), total};
  }
  function moveBoundary(index, proposed) {
    if (shotPlanner && cutActions.disabled()) return;
    const start = index === 0 ? 0 : Number(rows[index-1].end.value);
    const nextEnd = Number(rows[index+1].end.value);
    const minimum = shotPlanner ? 1/24 : 3, limit = shotPlanner ? 15 : plan.max_seconds;
    const lower = Math.max(start+minimum, nextEnd-limit);
    const upper = Math.min(start+limit, nextEnd-minimum);
    const value = shotPlanner
      ? Math.max(Math.ceil(lower*24-1e-7), Math.min(Math.floor(upper*24+1e-7), Math.round(proposed*24)))/24
      : Math.max(lower, Math.min(upper, proposed));
    rows[index].end.value = value.toFixed(shotPlanner ? 6 : 3);
    refreshDraftDisplays();
    markDirty();
    updateSelected(index, false, false);
    seekSourceVideo(Number(rows[index].end.value));
  }
  function draftSegments() {
    return plan.segments.map((segment, index) => ({
      ...segment, start: index ? Number(rows[index-1].end.value) : 0,
      end: Number(rows[index].end.value), prompt: rows[index].prompt.value,
      material_note: rows[index].note.value,
      refs: [...(rows[index].dualMode ? rows[index].legacyRefs.filter(Boolean) : rows[index].refs)],
      ...(rows[index].dualMode ? {reference_slots: rows[index].referenceSlots.map(item => ({...item}))} : {}),
    }));
  }
  function renderDraftSegments(segments, index, time) {
    pauseOtherMedia(null);
    plan.segments = segments.map((segment, i) => {
      const start = i ? segments[i-1].end : 0, end = segment.end;
      const row = {...segment, index: i, start, end, duration: end-start};
      const startSample = Math.round(start*plan.sample_rate), endSample = Math.round(end*plan.sample_rate);
      if (startSample !== segment.start_sample || endSample !== segment.end_sample) {
        for (const key of ["job", "takes", "video_preview", "needs_regeneration",
          "replacement_previous_job", "failed_attempts", "regeneration_after_current"]) delete row[key];
        row.reason = "手动切割或合并";
        row.boundary_kind = "manual";
        row.boundary_confidence = null;
      }
      row.start_sample = startSample; row.end_sample = endSample;
      row.edit_frames = Math.round(end*24)-Math.round(start*24);
      row.generation_frames = 5+17*Math.ceil((Math.max(124, Math.ceil((end-start)*24), row.edit_frames)-5)/17);
      return row;
    });
    topologyDirty = true;
    selected = index;
    renderCards();
    markDirty();
    updateSelected(index, true, false);
    seekSourceVideo(time);
    canvas.drawTimeline?.();
  }
  const cutActions = shotPlanner ? {
    disabled: () => !plan || submittingDirector ||
      ["running", "pausing", "stopping", "merging"].includes(plan.run_status) ||
      plan.segments.some(row => ["queued", "running"].includes(row.job?.status)),
    split: time => {
      const frame = Math.round(time*24), segments = draftSegments();
      const index = segments.findIndex(row => frame > Math.round(row.start*24) && frame < Math.round(row.end*24));
      if (index < 0) throw new Error("请选择片段内部位置切割，不能重复切点或切割首尾边界。");
      const row = segments[index], at = frame/24;
      segments.splice(index, 1, {...row, end: at}, {...row, start: at});
      renderDraftSegments(segments, index+1, at);
    },
    remove: async index => {
      if (index < 0 || index >= rows.length-1) throw new Error("请先选中一条内部切点线。");
      const segments = draftSegments(), left = segments[index], right = segments[index+1];
      if (right.end-left.start > 15+1/plan.sample_rate) throw new Error("合并后超过 15 秒，请先调整切点。");
      const different = JSON.stringify([left.refs, left.reference_slots]) !== JSON.stringify([right.refs, right.reference_slots]);
      segments.splice(index, 2, {...left, end: right.end});
      renderDraftSegments(segments, index, left.start);
      if (different) await messageDialog({
        title: "已合并片段", message: "合并后沿用左段参考图，右段参考图设置未采用；可在分段卡片中修改。",
      });
    },
    error: error => messageDialog({title: "切点操作未完成", message: error.message, tone: "error"}),
  } : undefined;
  createTimeline(canvas, state, (index, time) => {
      updateSelected(index, true, false);
      seekSourceVideo(time);
    }, moveBoundary,
    index => {
      refreshPreviewAudio(index);
      updateSelected(selected, false, false);
      seekSourceVideo(Number(rows[index].end.value));
    },
    async (start, end, signal) => {
      const path = endpoint(`/audio?index=0&start=${start.toFixed(6)}&end=${end.toFixed(6)}`);
      const waves = await Promise.all([false, true].map(async vocals => {
        const response = await api.fetchApi(path+(vocals ? "&vocals=1" : ""), {signal});
        if (!response.ok) throw new Error("局部波形读取失败");
        return pcmWavePeaks(await response.arrayBuffer());
      }));
      return {original: waves[0], vocals: waves[1]};
    }, null, cutActions);

  function renderCards() {
    segmentsBody.replaceChildren(); rows = []; details = []; directorRows = [];
    plan.segments.forEach(row => {
      const card = element("details", undefined, segmentsBody, "ty-h3lv-card");
      const summary = element("summary", undefined, card);
      const main = element("span", undefined, summary, "ty-h3lv-summary-main");
      element("strong", `第 ${row.index+1} 段`, main);
      const time = element("span", `${row.start.toFixed(3)}—${row.end.toFixed(3)}s`, main, "ty-h3lv-time");
      const duration = element("span", `${row.duration.toFixed(3)}s`, main, "ty-h3lv-duration");
      element("span", row.reason || "未标注切点原因", summary, "ty-h3lv-reason");
      if (needsReplacement(row)) element("span", "待重生成", summary, "ty-h3lv-chip risk");
      else if (row.job?.status) element("span", row.job.status, summary, "ty-h3lv-chip neutral");
      card.ontoggle = () => { if (card.open) updateSelected(row.index); };
      const inner = element("div", undefined, card, "ty-h3lv-card-body");
      if (row.video_preview?.filename) {
        inner.classList.add("has-preview");
        const preview = element("figure", undefined, inner, "ty-h3lv-segment-preview");
        element("figcaption", "当前分段结果", preview);
        const video = element("video", undefined, preview);
        video.controls = true;
        video.preload = "metadata";
        video.playsInline = true;
        video.src = outputPreviewUrl(row.video_preview);
      }
      element("p", row.text || "未识别出文字，人声状态仍需试听确认。", inner, "ty-h3lv-lyrics");
      const warnings = (row.warnings || []).filter(warning => !shotPlanner || !warning.includes("提示词"));
      if (warnings.length) {
        const warningList = element("ul", undefined, inner, "ty-h3lv-warnings");
        warnings.forEach(warning => element("li", warning, warningList));
      }
      const metrics = element("div", undefined, inner, "ty-h3lv-metrics");
      const generationFrames = element("span", `${row.generation_frames} 生成帧`, metrics);
      const editFrames = element("span", `${row.edit_frames} 剪辑帧`, metrics);
      const end = document.createElement("input");
      end.type = "number"; end.step = ".01"; end.value = row.end;
      end.disabled = row.index === plan.segments.length-1;
      end.min = row.start+(shotPlanner ? 1/24 : 3); end.max = row.start+(shotPlanner ? 15 : plan.max_seconds);
      end.oninput = () => {
        if (!Number.isFinite(Number(end.value))) return;
        refreshDraftDisplays(); markDirty(); updateSelected(selected, false); canvas.drawTimeline?.();
      };
      end.onchange = () => {
        if (shotPlanner && row.index < rows.length-1 && Number.isFinite(Number(end.value))) {
          moveBoundary(row.index, Number(end.value));
        }
        refreshPreviewAudio(row.index); updateSelected(selected);
      };
      element("p", "本段分离人声试听（该段完整人声，可拖动进度条）", inner, "ty-h3lv-audio-label");
      const vocalsAudio = element("audio", undefined, inner, "ty-h3lv-segment-audio");
      vocalsAudio.controls = true;
      vocalsAudio.preload = "metadata";
      vocalsAudio.src = shotPlanner
        ? api.apiURL(endpoint(`/audio?index=0&start=${row.start.toFixed(6)}&end=${row.end.toFixed(6)}&vocals=1&revision=${plan.revision}`))
        : api.apiURL(endpoint(`/audio?index=${row.index}&vocals=1&revision=${plan.revision}`));
      vocalsAudio.onplay = () => pauseOtherMedia(vocalsAudio);
      const note = element("textarea", undefined, inner, "ty-h3lv-material-note");
      note.value = row.material_note || "";
      note.placeholder = "本段素材说明，例如：图1是人物，图2是场景。编号要和下面的参考图顺序一致。";
      note.oninput = markDirty;
      const referenceBlock = element("div", undefined, inner, "ty-h3lv-references");
      const referenceHeader = element("div", undefined, referenceBlock, "ty-h3lv-reference-header");
      element("span", "本段参考图", referenceHeader, "ty-h3lv-audio-label");
      const dualMode = dualImageOutputMode();
      const referenceLimit = referenceSlotLimit();
      const referenceList = element("div", undefined, referenceBlock, "ty-h3lv-reference-list");
      const legacyRefs = Array.isArray(row.refs) ? [...row.refs] : [];
      let rowRefs = [...legacyRefs];
      let referenceSlots = null;
      let renderReferences;
      if (dualMode) {
        const savedSlots = Array.isArray(row.reference_slots) ? row.reference_slots : [];
        const highestSaved = savedSlots.reduce((max, item) =>
          Math.max(max, Number(item?.slot) + 1), 0);
        const initialCount = Math.min(6, Math.max(2, highestSaved, rowRefs.length));
        referenceSlots = Array.from({length: initialCount}, (_, slot) => {
          const saved = (row.reference_slots || []).find(item => Number(item.slot) === slot);
          if (saved) return {...saved, slot};
          const legacyName = rowRefs[slot];
          return legacyName
            ? {slot, role: "reference", source: "custom", name: legacyName}
            : {slot, role: "reference", source: "default"};
        });
        rowRefs = [];
        renderReferences = () => {
          referenceList.replaceChildren();
          referenceSlots.forEach((slotData, slot) => {
            const item = element("figure", undefined, referenceList, "ty-h3lv-reference-item ty-h3lv-reference-dual-item");
            const defaultItem = (plan.reference_defaults || []).find(entry => Number(entry.slot) === slot);
            const name = slotData.source === "custom" ? slotData.name :
              slotData.source === "ignore" ? "" : defaultItem?.name;
            if (name) {
              const image = document.createElement("img");
              image.src = referencePreviewUrl(plan.id, name);
              const names = libraryNames();
              const number = names.indexOf(name);
              image.alt = number >= 0 ? `图${number + 1}` : `参考图 ${slot + 1}`;
              image.loading = "lazy";
              item.append(image);
            } else {
              element("div", slotData.source === "ignore" ? "已忽略" : "未设置图片",
                item, "ty-h3lv-reference-placeholder");
            }
            const names = libraryNames();
            const picker = document.createElement("select");
            picker.className = "ty-h3lv-reference-select";
            picker.title = `参考图 ${slot + 1}：从图像素材库选择`;
            const ignore = element("option", "忽略此槽位", picker);
            ignore.value = "__ignore__";
            const fallback = element("option", defaultItem?.name ? "使用项目默认图" : "使用节点默认输入图", picker);
            fallback.value = "";
            names.forEach((assetName, index) => {
              const option = element("option", `图${index + 1}`, picker);
              option.value = assetName;
              option.title = assetName;
            });
            if (name && !names.includes(name)) {
              const option = element("option", `当前图片 · ${name}`, picker);
              option.value = name;
            }
            picker.value = slotData.source === "ignore" ? "__ignore__" : name || "";
            picker.onchange = () => {
              if (picker.value === "__ignore__") {
                slotData.source = "ignore";
                delete slotData.name;
              } else if (picker.value) {
                slotData.source = "custom";
                slotData.name = picker.value;
              } else {
                slotData.source = "default";
                delete slotData.name;
              }
              markDirty();
              renderReferences();
            };
            item.append(picker);
            const source = slotData.source === "ignore" ? "已忽略" :
              slotData.source === "custom" ? "本段选用" :
              defaultItem?.name ? "项目默认" : "节点默认输入";
            const number = names.indexOf(name);
            const label = name && number >= 0 ? `图${number + 1}` : source;
            element("figcaption", `参考图 ${slot + 1} · ${label}`, item);
            const tools = element("div", undefined, item, "ty-h3lv-reference-tools");
            const input = document.createElement("input");
            input.type = "file";
            input.accept = "image/*";
            input.hidden = true;
            const upload = actionButton(tools, "上传/替换", () => input.click(), "reference-tool");
            input.onchange = async () => {
              const file = input.files?.[0];
              input.value = "";
              if (!file) return;
              upload.disabled = true;
              try {
                if (topologyDirty) await saveCurrentReview();
                const result = await uploadReferenceAsset(plan.id, file, row.index);
                slotData.source = "custom";
                slotData.name = result.name;
                applyAssetResponse(result);
                markDirty();
                renderReferences();
              } catch (error) {
                await messageDialog({title: "参考图上传失败", message: error.message || String(error), tone: "error"});
              } finally {
                upload.disabled = upload.dataset.tyH3lvDisabled === "true";
              }
            };
            tools.append(input);
            const drop = actionButton(tools, "清空", () => {
              slotData.source = "ignore";
              delete slotData.name;
              markDirty();
              renderReferences();
            }, "reference-tool");
            drop.disabled = slotData.source === "ignore";
            element("span", "上传后自动加入素材库", tools, "ty-h3lv-reference-upload-hint");
          });
        };
        const addButton = actionButton(referenceHeader, "＋ 添加参考图", () => {
          if (referenceSlots.length >= 6) return;
          if (referenceLimit < referenceSlots.length + 1) {
            throw new Error("当前工作流只接出了前面的参考图槽位，请先在画布上接出更多 H3 ref_image 输出。");
          }
          referenceSlots.push({slot: referenceSlots.length, role: "reference", source: "ignore"});
          addButton.disabled = referenceSlots.length >= 6 || referenceLimit <= referenceSlots.length;
          markDirty();
          renderReferences();
        }, "reference-add");
        addButton.disabled = referenceSlots.length >= 6 || referenceLimit <= referenceSlots.length;
        actionButton(referenceHeader, "套用参考图到所有分段", async () => {
          if (!await confirmDialog({
            title: "把本段参考图套用到所有分段？",
            message: "所有分段的六个参考图槽位都会改为本段当前选择；其他分段的自定义图片会被替换。",
            confirmText: "套用参考图",
          })) return;
          rows.forEach(item => {
            if (!item.referenceSlots) return;
            item.referenceSlots.splice(0, item.referenceSlots.length,
              ...referenceSlots.map(value => ({...value})));
            item.note.value = note.value;
            item.renderRefs();
          });
          markDirty();
        }, "reference-copy");
      } else {
        if (!referenceLimit) {
          element("p", "当前工作流还没有接出 ref_image 槽位：请先在画布上用“加载图像”节点依次接到 "
            + "MiniMax H3 视频参考节点的 ref_image_0、ref_image_1……每接一个槽位，可用的参考图就多一张。",
            referenceBlock, "ty-h3lv-reference-empty");
        }
        renderReferences = () => {
          referenceList.replaceChildren();
          if (!referenceLimit) {
            element("p", `本段未配置参考图，将使用默认参考图（${defaultReferenceText()}）。`,
              referenceList, "ty-h3lv-reference-empty");
            return;
          }
          const names = libraryNames();
          for (let position = 0; position < referenceLimit; position += 1) {
            const name = rowRefs[position] || "";
            const item = element("figure", undefined, referenceList, "ty-h3lv-reference-item");
            if (name) {
              const image = document.createElement("img");
              image.src = referencePreviewUrl(plan.id, name);
              image.alt = `图${names.indexOf(name) + 1}`;
              image.loading = "lazy";
              item.append(image);
            } else {
              element("div", "未选择图片", item, "ty-h3lv-reference-placeholder");
            }
            const picker = document.createElement("select");
            picker.className = "ty-h3lv-reference-select";
            element("option", "未选择（使用默认图）", picker).value = "";
            names.forEach((assetName, index) => {
              const option = element("option", `图${index + 1}`, picker);
              option.value = assetName;
              option.title = assetName;
            });
            if (name && !names.includes(name)) {
              const option = element("option", `当前图片 · ${name}`, picker);
              option.value = name;
            }
            picker.value = name;
            picker.onchange = () => {
              rowRefs[position] = picker.value || null;
              markDirty();
              renderReferences();
            };
            item.append(picker);
            const number = names.indexOf(name);
            element("figcaption", name && number >= 0 ? `槽位 ${position + 1} · 图${number + 1}` :
              `槽位 ${position + 1} · 未选择`, item);
            const tools = element("div", undefined, item, "ty-h3lv-reference-tools");
            const move = step => {
              if (position + step < 0 || position + step >= rowRefs.length) return;
              [rowRefs[position], rowRefs[position + step]] = [rowRefs[position + step], rowRefs[position]];
              markDirty();
              renderReferences();
            };
            const earlier = element("button", "←", tools, "ty-h3lv-reference-tool");
            earlier.type = "button";
            earlier.title = "前移";
            earlier.disabled = !name || position === 0;
            earlier.onclick = () => move(-1);
            const later = element("button", "→", tools, "ty-h3lv-reference-tool");
            later.type = "button";
            later.title = "后移";
            later.disabled = !name || position === rowRefs.length - 1;
            later.onclick = () => move(1);
            const drop = element("button", "移除", tools, "ty-h3lv-reference-tool");
            drop.type = "button";
            drop.disabled = !name;
            drop.onclick = async () => {
              rowRefs[position] = null;
              markDirty();
              renderReferences();
            };
          }
        };
        const fileInput = document.createElement("input");
        fileInput.type = "file";
        fileInput.accept = "image/*";
        fileInput.hidden = true;
        fileInput.onchange = async () => {
          const file = fileInput.files?.[0];
          fileInput.value = "";
          if (!file) return;
          addButton.disabled = true;
          try {
            if (topologyDirty) await saveCurrentReview();
            if (rowRefs.length >= referenceLimit) throw new Error(`本段最多 ${referenceLimit} 个参考图槽位。`);
            const result = await uploadReferenceAsset(plan.id, file, row.index);
            rowRefs.push(result.name);
            applyAssetResponse(result);
            markDirty();
            renderReferences();
          } catch (error) {
            await messageDialog({title: "参考图上传失败", message: error.message || String(error), tone: "error"});
          } finally {
            addButton.disabled = false;
          }
        };
        referenceHeader.append(fileInput);
        const addButton = actionButton(referenceHeader,
          referenceLimit ? `＋ 上传参考图（上限 ${referenceLimit} 张）` : "＋ 上传参考图", async () => {
            if (!referenceLimit) {
              throw new Error("当前工作流还没有接出 ref_image 槽位。请先在画布上用“加载图像”节点接到 "
                + "MiniMax H3 视频参考节点的 ref_image_0、ref_image_1……");
            }
            if (rowRefs.length >= referenceLimit) throw new Error(`本段最多 ${referenceLimit} 张参考图。`);
            fileInput.click();
          }, "reference-add");
        actionButton(referenceHeader, "套用到所有分段", async () => {
          if (!await confirmDialog({
            title: "把本段的参考图套用到所有分段？",
            message: "所有分段的参考图列表与素材说明都会被本段的内容覆盖，其他分段已有的参考图会被替换。",
            confirmText: "套用到所有分段",
          })) return;
          rows.forEach(item => {
            item.refs.splice(0, item.refs.length, ...rowRefs);
            item.note.value = note.value;
            item.renderRefs();
          });
          markDirty();
        }, "reference-copy");
      }
      renderReferences();
      const prompt = document.createElement("textarea");
      prompt.value = row.prompt;
      prompt.oninput = markDirty;
      if (!shotPlanner) {
        const promptActions = element("div", undefined, inner, "ty-h3lv-actions ty-h3lv-prompt-actions");
        actionButton(promptActions, "编辑本段镜头简报（提示词）", async () => {
        const updated = await editPromptDialog(row.index, prompt.value);
        if (updated === null || updated === prompt.value) return;
        prompt.value = updated;
        prompt.dispatchEvent(new Event("input"));
      }, "prompt-edit");
      }
      rows.push({end, prompt, note, refs: rowRefs, referenceSlots, dualMode,
        legacyRefs,
        renderRefs: renderReferences, duration, time, generationFrames, editFrames, vocals: vocalsAudio});
      if (!shotPlanner && director?.planning_method === "qwen") {
        const review = element("section", undefined, inner, "ty-h3lv-director-card");
        element("h4", "本段文字镜头预演", review);
        const draftRow = reviewResult()?.segments?.find(item => item.segment_index === row.index);
        const sourceRow = director.draft?.input_snapshot?.source?.segments?.find(item => item.segment_index === row.index);
        if (draftRow) {
          element("p", `第 ${draftRow.segment_index + 1} 段 · 时间线从 00:00.000 起算，不能超出 ${row.duration.toFixed(3)} 秒有效内容。全片分段位置只用于导演规划，不写入本段 H3 时间线。`,
            review, "ty-h3lv-notice");
          const selectedOutputs = new Set(Array.isArray(row.external_qwen_fields)
            ? row.external_qwen_fields : externalQwenFields);
          const outputBoxes = [];
          const saveOutputs = async changed => {
            const checked = changed.checked;
            for (const box of outputBoxes) box.disabled = true;
            try {
              plan = await request(endpoint("/director/output-fields"), {
                revision: plan.revision, segment_index: row.index,
                fields: outputBoxes.filter(box => box.checked).map(box => box.dataset.field),
              });
              status.textContent = statusText(plan, director, directorDirty);
              toast("已保存", `第 ${row.index + 1} 段外部 Qwen 输出项已更新。`);
            } catch (error) {
              changed.checked = !checked;
              await messageDialog({title: "保存失败", message: error.message || String(error), tone: "error"});
            } finally {
              for (const box of outputBoxes) box.disabled = row.prompt_kind === "final";
            }
          };
          const field = (key, label, value, rows = 2) => {
            const wrapper = element("div", undefined, review, "ty-h3lv-director-field");
            const title = element("label", undefined, wrapper, "ty-h3lv-director-field-title");
            const output = element("input", undefined, title);
            output.type = "checkbox";
            output.checked = selectedOutputs.has(key);
            output.dataset.field = key;
            output.title = "勾选后输出到外部 Qwen";
            output.setAttribute("aria-label", `${label}：输出到外部 Qwen`);
            output.onchange = () => saveOutputs(output);
            outputBoxes.push(output);
            element("span", label, title);
            const editor = element("textarea", undefined, wrapper, "ty-h3lv-material-note");
            editor.value = value;
            editor.rows = rows;
            editor.oninput = markDirectorDirty;
            return editor;
          };
          const intent = field("intent", "本段演讲意图", draftRow.intent, 1);
          const brief = field("brief", "AI 镜头简报（待审核，可编辑）", draftRow.brief, 3);
          const entry = field("entry_state", "入段姿态与表情", draftRow.entry_state);
          const exit = field("exit_state", "结束姿态与表情", draftRow.exit_state);
          const transition = field("next_transition", "下一段衔接目标", draftRow.next_transition);
          const timeline = field("timeline", "H3 时间线（段内相对时间，可编辑）",
            directorTimelineText(draftRow, sourceRow, plan.mode), 14);
          timeline.spellcheck = false;
          const lockLabel = element("label", undefined, review, "ty-h3lv-director-lock");
          const lock = element("input", undefined, lockLabel);
          lock.type = "checkbox";
          lock.checked = Boolean(row.prompt_locked || row.prompt_kind === "final");
          lock.onchange = markDirectorDirty;
          element("span", "锁定本段简报：后续重新规划不得覆盖", lockLabel);
          if (row.prompt_kind === "final") {
            for (const output of outputBoxes) output.disabled = true;
            lock.disabled = true;
            brief.readOnly = true;
            brief.value = row.prompt;
            element("p", "本段为明确的 final 完整提示词。AI 方案仅供参考，现有完整提示词不会被覆盖。", review, "ty-h3lv-notice");
          } else if (row.prompt_locked) {
            brief.value = row.prompt;
            for (const editor of [intent, brief, entry, exit, transition, timeline]) editor.readOnly = true;
            element("p", "已保留人工锁定的方案。要改变它，请先取消锁定并确认，再重新规划；新草稿不会静默覆盖锁定内容。", review, "ty-h3lv-notice");
          }
          directorRows.push({value: draftRow, intent, brief, entry, exit, transition, timeline, lock});
        } else {
          element("p", "还没有本段 AI 导演草稿。确认切分后，在上方提交独立的全片导演文本任务。", review, "ty-h3lv-notice");
        }
      }
      details.push(card);
      const motionAudit = row.job?.motion_audit?.findings?.length
        ? ` · 外部 Qwen 动作审计：${row.job.motion_audit.findings.join("、")}` : "";
      element("p", `生成状态：${row.job?.status || "待生成"}${needsReplacement(row) ? ` · ${row.regeneration_reason || "需要重新生成"}` : ""}`+
        `${row.director_video_stale ? " · 当前视频尚未按新导演方案生成" : ""}`+
        `${row.job?.error ? ` · ${row.job.error}` : ""}${motionAudit}`, inner, "ty-h3lv-job");
      const generateLabel = row.job ? "重新生成本段" : "仅生成本段";
      actionButton(inner, generateLabel, () => runGeneration(row.index), "segment-run");
      if (row.takes?.length) {
        actionButton(inner, `恢复上一版（${row.takes.length}）`, async () => {
          if (topologyDirty) await saveCurrentReview();
          if (dirty || directorDirty || configDirty) throw new Error("请先保存或明确放弃当前编辑，再恢复上一版。");
          if (!await confirmDialog({
            title: `恢复第 ${row.index+1} 段上一版？`,
            message: "恢复后不会重跑其他片段，但需要重新合成最终视频。",
            confirmText: "恢复上一版",
          })) return;
          await request(endpoint("/restore"), {index: row.index}); await load();
        });
      }
      if (row.failed_attempts?.length) {
        element("p", `保留了 ${row.failed_attempts.length} 次失败的新版本记录；当前仍采用上一个成功版本。`, inner, "ty-h3lv-job");
      }
    });
  }

  async function load({preserveEdits = false, allowSubmitting = false} = {}) {
    if (preserveEdits && submittingDirector && !allowSubmitting) return false;
    if (!select.value) {
      status.textContent = "还没有分析项目，请先运行 H3 音频分析节点。";
      notice.textContent = "没有可审核的项目。";
      updateDirectorControls();
      return;
    }
    const loadedProject = select.value, epoch = editEpoch;
    const [latestPlan, latestAnalysis, latestDirector] = await Promise.all([
      request(endpoint()), request(endpoint("/analysis")), request(endpoint("/director")),
    ]);
    if (loadedProject !== select.value || (preserveEdits &&
        (epoch !== editEpoch || dirty || directorDirty || configDirty))) {
      directorStatus.textContent = "新方案已就绪，当前编辑已保留；请确认编辑后刷新导演草稿。";
      return false;
    }
    plan = latestPlan;
    shade.tyH3lvProjectId = plan.id;
    syncReferenceLibrary();
    syncDefaultReferenceControl();
    analysis = latestAnalysis;
    setProject(owner, plan.id);
    if (plan.final_preview) showFinalOnVideoNode(plan.final_preview, plan.id);
    dirty = false; topologyDirty = false; selected = Math.min(selected, plan.segments.length-1);
    director = latestDirector;
    configDirty = directorDirty = false;
    renderDirectorState();
    status.textContent = statusText(plan, director);
    status.classList.remove("is-dirty");
    notice.textContent = analysis.available === false ? analysis.reason :
      `诊断：${analysis.phrases?.length || 0} 个识别句段 · ${analysis.sections?.length || 0} 个疑似无人声区 · `+
      `${analysis.rhythm?.tempo_bpm ? `约 ${analysis.rhythm.tempo_bpm} BPM（仅次级参考）` : "未取得稳定节拍参考"}`+
      ` · 运镜：${plan.mode === "speech_light" ? "居中演讲稳镜规则" : plan.mode === "speaking" ? "口播固定机位规则" : "唱歌动态规则"}`+
      `${analysis.legacy_notice ? ` · ${analysis.legacy_notice}` : ""}`;
    renderCards();
    syncSourceVideo();
    updateSelected(selected, true, false);
    if (details[selected]) details[selected].open = true;
    if (plan.final_video && plan.final_preview?.filename) {
      const result = element("section", undefined, segmentsBody, "ty-h3lv-result");
      const resultHeader = element("div", undefined, result, "ty-h3lv-result-header");
      element("h3", plan.final_stale ? "当前旧版成片（有片段待更新）" : "合并结果", resultHeader);
      const revealButton = actionButton(resultHeader, "打开文件位置", async () => {
        await request(endpoint("/reveal-final"), {});
        revealButton.textContent = "已打开并选中文件";
        await new Promise(resolve => setTimeout(resolve, 1200));
        revealButton.textContent = "打开文件位置";
      }, "reveal-final");
      const video = element("video", undefined, result);
      video.controls = true;
      video.preload = "metadata";
      video.playsInline = true;
      video.src = outputPreviewUrl(plan.final_preview);
    }
    return true;
  }

  actionButton(controls, "保存并确认", async () => {
    if (!await confirmDialog({
      title: "保存并确认分段？",
      message: methodSelect.value === "qwen"
        ? "请确认已经试听并检查所有切点。当前切分、规划设置和导演卡片修改都会保存；保存后可直接生成视频。"
        : "请确认已经试听并检查所有切点。保存后，这个项目将允许批量生成。",
      confirmText: "保存并确认",
    })) return;
    await saveCurrentReview();
    await load();
  }, "primary");
  actionButton(controls, "重新分析分段", async () => {
    await reanalyzeProject(owner);
  });
  const queueDirectorButton = actionButton(controls, "Qwen 全片分镜设计", queueDirector, "primary");
  queueDirectorButton.hidden = shotPlanner;
  const runButton = actionButton(controls, "▶ 批量生成", () => runGeneration(), "run");
  actionButton(controls, "当前段完成后暂停", async () => {
    await request(endpoint("/pause"), {});
    if (!dirty && !directorDirty && !configDirty) await load();
  }, "pause");
  actionButton(controls, "■ 停止后续生成", async () => {
    if (!await confirmDialog({
      title: "停止提交后续片段？",
      message: "当前正在生成的片段会正常完成，完成后不再提交新的片段。",
      confirmText: "停止后续生成",
      confirmClass: "stop",
      tone: "warning",
    })) return;
    await request(endpoint("/stop"), {});
    if (!dirty && !directorDirty && !configDirty) await load();
  }, "stop");
  actionButton(controls, "仅重新合成", async () => {
    if (dirty || directorDirty || configDirty) throw new Error("请先保存或明确放弃当前编辑，再重新合成。");
    await request(endpoint("/assemble"), {}); await load();
  });
  actionButton(controls, "复制成片目录", async () => {
    const result = await request("/ty-h3lv/final-folder");
    try {
      await navigator.clipboard.writeText(result.path);
      await messageDialog({title: "成片目录已复制", message: result.path});
    } catch {
      await messageDialog({title: "成片目录", message: result.path});
    }
  });
  actionButton(controls, "刷新状态", async () => {
    if ((dirty || directorDirty || configDirty) && !await confirmDialog({
      title: "放弃未保存的修改并刷新？",
      message: "将重新读取已保存的切分和导演方案；未保存的编辑会丢失。",
      confirmText: "放弃修改并刷新", tone: "warning",
    })) return;
    await load();
  });
  actionButton(controls, "导出分段时长", async () => {
    if (!rows.length) throw new Error("还没有可导出的分段。");
    const {text, total} = segmentTimingMarkdown();
    let copied = false;
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch {}
    await exportDialog({
      title: copied ? "分段时长已复制" : "分段时长",
      note: `共 ${rows.length} 段 · 合计 ${total.toFixed(3)}s · ${copied
        ? "已复制到剪贴板，也可以下载为 Markdown 文件。"
        : "浏览器拦截了自动复制，可手动选取下方文本，或直接下载 Markdown 文件。"}`,
      text,
      fileName: `TYH3LongVideo_分段时长_${String(select.value).slice(0, 8)}.md`,
    });
  });
  select.onchange = async () => {
    if ((dirty || directorDirty || configDirty) && !await confirmDialog({
      title: "放弃未保存的修改并切换项目？",
      message: "未确认的切分或导演编辑不会自动保存。",
      confirmText: "切换项目", tone: "warning",
    })) {
      select.value = plan?.id || "";
      return;
    }
    selected = 0;
    load().catch(error => messageDialog({title: "项目加载失败", message: error.message, tone: "error"}));
  };
  await load();
  let checkingStatus = false;
  let checkAgain = false;
  function onDirectorEvent(event) {
    const data = event.detail || {};
    if (data.project_id !== select.value) return;
    if (director?.job?.prompt_id && data.prompt_id !== director.job.prompt_id &&
        ["queued", "running", "validating"].includes(director.job.status)) return;
    // Read the authoritative status; a delayed old event cannot replace a new job.
    if (checkingStatus) checkAgain = true;
    else void pollStatus();
  }
  async function pollStatus() {
    if (!shade.isConnected) {
      clearInterval(timer);
      api.removeEventListener?.("ty-h3lv-director", onDirectorEvent);
      canvas.disposeTimeline?.();
      return;
    }
    if (!plan || checkingStatus) return;
    checkingStatus = true;
    try {
      if (plan.approved) {
        const checkedProject = select.value;
        const latestDirector = await request(endpoint("/director"));
        if (!shade.isConnected || checkedProject !== select.value) return;
        const action = directorUpdateAction(director, latestDirector, dirty || directorDirty || configDirty);
        if (action === "preserve") {
          directorStatus.textContent = latestDirector.job?.error ||
            "导演状态已更新，新方案已保留；当前编辑不覆盖，请确认后刷新导演草稿。";
          director = {...director, job: latestDirector.job, stale: true};
          updateDirectorControls();
        } else if (action === "refresh") {
          await load({preserveEdits: true});
        } else {
          director = {...director, job: latestDirector.job};
        }
        if (!shade.isConnected || checkedProject !== select.value) return;
        updateDirectorControls();
      }
      if (dirty || directorDirty || configDirty ||
          !["running", "pausing", "stopping", "merging"].includes(plan.run_status)) return;
      const latest = await request(endpoint());
      status.textContent = statusText(latest);
      const oldDone = plan.segments.filter(row => row.job?.status === "completed").length;
      const newDone = latest.segments.filter(row => row.job?.status === "completed").length;
      if (latest.run_status !== plan.run_status || oldDone !== newDone) await load({preserveEdits: true});
    } catch (error) {
      directorJobStatus.textContent = `暂时无法获取状态：${error.message}；未重新提交或取消任务，将继续查询。`;
    } finally {
      checkingStatus = false;
      if (checkAgain) { checkAgain = false; void pollStatus(); }
    }
  }
  const timer = setInterval(pollStatus, 2000);
  api.addEventListener?.("ty-h3lv-director", onDirectorEvent);
}

app.registerExtension({
  name: "TY.H3LongVideo",
  afterConfigureGraph() {
    setTimeout(() => restoreFinalVideoPreview(), 0);
  },
  async setup() {
    if (!app.__tyH3lvFinalListenerInstalled) {
      app.__tyH3lvFinalListenerInstalled = true;
      api.addEventListener("ty-h3lv-final", event => {
        const data = event.detail || {};
        showFinalOnVideoNode(data.preview, data.project_id);
      });
      api.addEventListener("ty-h3lv-segment", event => {
        const data = event.detail || {};
        showFinalOnVideoNode(data.preview, data.project_id);
      });
      api.addEventListener("ty-h3lv-model-download", event => {
        const data = event.detail || {};
        const totalMiB = Math.round((Number(data.total) || 0) / 1024 / 1024);
        if (data.state === "started") {
          toast("正在下载人声分离模型", `首次使用约需下载 ${totalMiB} MiB，完成后会自动继续分析。`);
        } else if (data.state === "resuming") {
          const doneMiB = Math.round((Number(data.downloaded) || 0) / 1024 / 1024);
          toast("正在续传人声分离模型", `已下载 ${doneMiB}/${totalMiB} MiB，完成后会自动继续分析。`);
        } else if (data.state === "completed") {
          toast("人声分离模型已就绪", "正在继续音频分析。", "success");
        }
      });
    }
    if (app.__tyH3lvQueueGuardInstalled) return;
    app.__tyH3lvQueueGuardInstalled = true;
    const originalQueuePrompt = app.queuePrompt.bind(app);
    app.queuePrompt = async function (number, batchCount = 1, options = {}) {
      const nodes = app.graph?._nodes || [];
      const unified = nodes.filter(isOwnerNode);
      if (unified.length === 1) {
        const node = unified[0];
        const requestedTargets = Array.isArray(options) ? options :
          (options?.queueNodeIds ?? options?.partialExecutionTargets);
        const partialTargets = Array.isArray(requestedTargets)
          ? requestedTargets.map(item => String(item?.nodeId ?? item)) : [];
        if (partialTargets.length) {
          const selectedItems = app.canvas?.selectedItems;
          const nodeSelected = Boolean(selectedItems?.has?.(node) ||
            app.canvas?.selected_nodes?.[node.id] === node || node.selected);
          if (partialTargets.includes(String(node.id)) || nodeSelected) {
            await analyzeOnly(node);
            return;
          }
          return originalQueuePrompt.apply(this, arguments);
        }
        const previousProject = String(node.properties?.ty_h3lv_project || "").trim();
        if (previousProject) {
          try {
            const plan = await request(`/ty-h3lv/project/${previousProject}`);
            if (plan.approved) {
              await startApprovedSequence(node, plan);
            } else {
              await openReview(node);
            }
            return;
          } catch (error) {
            if (!String(error?.message || error).includes("项目文件不完整")) throw error;
            setProject(node);
          }
        }
        await analyzeOnly(node);
        return;
      }
      return originalQueuePrompt.apply(this, arguments);
    };
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (!ownerNodeTypes.has(nodeData.name)) return;
    const shotPlanner = nodeData.name === "TY_ShotPlanner";
    const oldCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = oldCreated?.apply(this, arguments);
      const directorLabels = {
        mode: "模式（speech_light＝居中演讲）",
        camera_activity: "镜头活跃度（仅唱歌）",
        widest_framing: "最远允许景别（仅唱歌）",
        skill: "LongVideo 规划模式",
        duration: "Qwen 默认分段时长",
        llm_model: "Qwen 语言模型",
        vision_model: "Qwen 多模态投影",
        think_mode: "Qwen 思考模式",
        reasoning_effort: "Qwen 推理强度",
        seed: "Qwen seed",
        max_tokens: "Qwen 输出预算",
        video_sample_frames_per_sec: "参考视频采样帧/秒",
        force_unload_model: "Qwen 完成后释放模型",
        asr_python: "语音识别 Python 覆盖（可选）",
        asr_model: "语音识别模型覆盖（可选）",
        asr_device: "语音识别设备",
      };
      for (const item of this.widgets || []) {
        if (directorLabels[item.name]) item.label = directorLabels[item.name];
      }
      for (const name of ["director_mode", "project_id", "segment_index"]) {
        const internal = this.widgets?.find(item => item.name === name);
        setWidgetHidden(internal, true);
      }
      const inputLabels = {vocals: "分离人声（可选）"};
      for (const input of this.inputs || []) {
        if (inputLabels[input.name]) input.label = inputLabels[input.name];
      }
      if (!shotPlanner) {
        const rulesWidget = this.addWidget("button", "运镜规则：查看与修改", null,
          () => openDirectorRules(this).catch(error => messageDialog({
            title: "无法打开运镜规则", message: error.message, tone: "error"})));
        rulesWidget.serialize = false;
      } else {
        addSourceUploadControls(this);
        addSourceVideoPreview(this);
      }

      const technicalWidgets = ["asr_python", "asr_model", "asr_device"]
        .map(name => this.widgets?.find(item => item.name === name)).filter(Boolean);
      for (const item of technicalWidgets) setWidgetHidden(item, true);
      const reanalyzeWidget = this.addWidget("button", shotPlanner ? "分析并分段" : "重新分析并分段", null,
        () => reanalyzeProject(this, {
          ask: !shotPlanner || Boolean(this.properties?.ty_h3lv_project ||
            this.widgets?.find(item => item.name === "project_id")?.value),
        }).catch(error => messageDialog({
          title: "无法重新分析分段", message: error.message, tone: "error"})));
      reanalyzeWidget.serialize = false;
      refreshAnalysisButton(this);
      const widget = this.addWidget("button", "打开分段与生成控制", null,
        () => openReview(this).catch(error => messageDialog({
          title: "无法打开分段审核", message: error.message, tone: "error"})));
      widget.serialize = false;
      setTimeout(() => resizeNodeToVisibleWidgets(this), 0);
      return result;
    };
    const oldExecuted = nodeType.prototype.onExecuted;
    nodeType.prototype.onExecuted = function (message) {
      oldExecuted?.apply(this, arguments);
      if (message?.ty_h3lv_project?.[0]) {
        const projectId = message.ty_h3lv_project[0];
        setProject(this, projectId);
        setTimeout(() => openReview(this).catch(error => messageDialog({
          title: "无法打开分段审核", message: error.message, tone: "error"})), 0);
      }
    };
  }
});
