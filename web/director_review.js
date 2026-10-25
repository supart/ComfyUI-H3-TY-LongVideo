// Pure review helpers; no model calls, DOM writes, or queue submission.
export function qwenCandidates(prompt) {
  return Object.entries(prompt || {})
    .filter(([, node]) => node.class_type === "QwenH3PromptLocal")
    .map(([id, node]) => ({id, title: node._meta?.title || "Qwen H3 Prompt (Local)"}));
}

export function selectedQwenId(prompt, selected = "") {
  const candidates = qwenCandidates(prompt);
  if (candidates.some(item => item.id === String(selected))) return String(selected);
  if (selected) return "";
  return candidates.length === 1 ? candidates[0].id : "";
}

export function directorNeedsApproval(director) {
  return director?.planning_method === "qwen" &&
    (Boolean(director.stale) || !director.approval ||
      ["queued", "running", "validating"].includes(director.job?.status) ||
      Boolean(director.draft && director.approval.draft_id !== director.draft.id));
}

export function directorUpdateAction(previous, current, hasEdits) {
  const changed = previous?.draft?.id !== current?.draft?.id ||
    Boolean(previous?.stale) !== Boolean(current?.stale);
  return !changed ? "none" : hasEdits ? "preserve" : "refresh";
}

export function videoPromptBindings(prompt, selected = "") {
  const qwenId = selectedQwenId(prompt, selected);
  if (!qwenId) throw new Error("请在导演审核面板明确选择当前工作流的 Qwen 节点。");
  const h3 = Object.entries(prompt || {})
    .filter(([, node]) => node.class_type === "MiniMaxH3ReferenceToVideo");
  if (h3.length !== 1) {
    throw new Error("AI 导演测试工作流必须明确包含一个 MiniMax H3 视频参考节点；多个候选不能自动选择。");
  }
  const source = h3[0][1].inputs?.prompt;
  if (!Array.isArray(source) || source.length !== 2 || !prompt[String(source[0])]) {
    throw new Error("H3 的 prompt 必须连接到明确的提示词输出节点。");
  }
  return {qwen_id: qwenId, h3_id: h3[0][0], prompt_output_id: String(source[0])};
}

export function directorJobText(job, now = Date.now()) {
  if (!job) return "分镜任务：尚未提交；只规划文本，不运行视频。";
  const labels = {queued: "排队中", preparing: "准备分镜输入", waiting_model: "等待模型推理锁",
    loading_model: "加载 Qwen 模型", checking_context: "检查上下文长度",
    generating: "正在生成全片分镜", repairing: "修复一次分镜格式",
    validating: "校验分镜结果", completed: "分镜草稿已完成，可检查后生成",
    failed: "分镜任务失败", interrupted: "分镜任务已中断", running: "运行中"};
  const terminal = ["completed", "failed", "interrupted"].includes(job.status);
  const label = labels[terminal ? job.status : job.phase] || labels[job.status] || "状态未知";
  const start = Date.parse(job.started_at || job.queued_at || "");
  const end = job.finished_at ? Date.parse(job.finished_at) : now;
  const elapsed = Number.isFinite(start) && Number.isFinite(end) && (!terminal || job.finished_at)
    ? Math.max(0, Math.floor((end - start) / 1000)) : job.elapsed_seconds;
  const duration = Number.isFinite(elapsed) ? ` · 耗时 ${Math.floor(elapsed)} 秒` : "";
  const budget = job.context_budget;
  const context = budget ? `\n上下文超限：输入 ${budget.input_tokens} token + 预留输出 ${budget.output_budget}` +
    ` + 安全余量 ${budget.reserve_tokens} > 上限 ${budget.context_size}；未截断输入。` : "";
  return `${label}${duration}${job.prompt_id ? ` · 任务 ${job.prompt_id.slice(0, 8)}` : ""}` +
    (job.error ? `\n原因：${job.error}` : "") + context;
}
