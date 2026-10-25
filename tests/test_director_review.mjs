import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";
import vm from "node:vm";

const source = await readFile(new URL("../web/director_review.js", import.meta.url), "utf8");
const {qwenCandidates, selectedQwenId, directorNeedsApproval, directorUpdateAction, directorJobText, videoPromptBindings} =
  await import(`data:text/javascript,${encodeURIComponent(source)}`);
const graph = {
  "7": {class_type: "QwenH3PromptLocal", _meta: {title: "Selected Qwen"}},
  "8": {class_type: "TextBox", inputs: {}},
  "9": {class_type: "MiniMaxH3ReferenceToVideo", inputs: {prompt: ["7", 0]}},
};

test("bind the actual Qwen node, never silently select one of multiple candidates", () => {
  assert.deepEqual(qwenCandidates(graph), [{id: "7", title: "Selected Qwen"}]);
  assert.equal(selectedQwenId(graph), "7");
  assert.equal(selectedQwenId(graph, "old-missing-node"), "");
  const multiple = {...graph, "11": {class_type: "QwenH3PromptLocal"}};
  assert.equal(selectedQwenId(multiple), "");
  assert.equal(selectedQwenId(multiple, "11"), "11");
  assert.throws(() => videoPromptBindings(multiple), /明确选择/);
});

test("trace final prompt only from bound H3 input, not a formatted output elsewhere", () => {
  assert.deepEqual(videoPromptBindings(graph), {
    qwen_id: "7", h3_id: "9", prompt_output_id: "7",
  });
  const otherSource = {...graph, "9": {...graph["9"], inputs: {prompt: ["8", 0]}}};
  assert.equal(videoPromptBindings(otherSource).prompt_output_id, "8");
  assert.throws(() => videoPromptBindings({...graph, "9": {...graph["9"], inputs: {prompt: "literal"}}}), /必须连接/);
  assert.throws(() => videoPromptBindings({...graph, "12": graph["9"]}), /一个 MiniMax/);
});

test("legacy rules stay usable; Qwen still requires a current approval record", () => {
  assert.equal(directorNeedsApproval(null), false);
  assert.equal(directorNeedsApproval({planning_method: "rules", stale: true}), false);
  assert.equal(directorNeedsApproval({planning_method: "qwen"}), true);
  assert.equal(directorNeedsApproval({planning_method: "qwen", approval: {id: "approved"}}), false);
  assert.equal(directorNeedsApproval({planning_method: "qwen", approval: {}, stale: true}), true);
  assert.equal(directorNeedsApproval({planning_method: "qwen", approval: {draft_id: "old"}, draft: {id: "new"}}), true);
  assert.equal(directorNeedsApproval({planning_method: "qwen", approval: {}, job: {status: "running"}}), true);
});

test("new results refresh clean cards and never replace edits", () => {
  const old = {draft: {id: "old"}, job: {status: "running"}};
  const next = {draft: {id: "new"}, job: {status: "completed"}};
  assert.equal(directorUpdateAction(old, next, false), "refresh");
  assert.equal(directorUpdateAction(old, next, true), "preserve");
  assert.equal(directorUpdateAction(next, next, true), "none");
  assert.equal(directorUpdateAction(old, {...old, job: {status: "failed"}}, true), "none");
});

test("shared job text shows true phases, elapsed time and exact budget without percentages", () => {
  const now = Date.parse("2026-09-20T12:00:12Z");
  const job = {status: "running", phase: "checking_context", started_at: "2026-09-20T12:00:00Z"};
  assert.match(directorJobText(job, now), /检查上下文长度 · 耗时 12 秒/);
  assert.doesNotMatch(directorJobText(job, now), /%/);
  const failed = {...job, status: "failed", finished_at: "2026-09-20T12:00:05Z",
    error: "Too long", context_budget: {input_tokens: 32635, output_budget: 8192,
      reserve_tokens: 8, context_size: 32768}};
  assert.match(directorJobText(failed, now), /失败 · 耗时 5 秒/);
  assert.match(directorJobText(failed, now), /32635.*8192.*32768/);
  assert.match(directorJobText({status: "interrupted", error: "状态不可恢复"}), /不可恢复/);
});

test("review opens with a fresh single-segment button and never queues a job", async () => {
  const created = [], timers = [], listeners = new Map();
  class Element {
    children = []; dataset = {}; value = ""; isConnected = true;
    classList = {add() {}, remove() {}, toggle() {}};
    constructor(tag) { this.tagName = tag; created.push(this); }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    querySelector() { return null; }
    remove() { this.isConnected = false; }
  }
  const id = "a".repeat(32);
  const plan = {id, created: 0, mode: "speaking", revision: 1, approved: true,
    planning_method: "qwen", run_status: "idle", reference_default_count: null,
    max_seconds: 15, segments: [{
      index: 0, start: 0, end: 5, duration: 5, generation_frames: 124, edit_frames: 120,
      start_sample: 0, end_sample: 240000, text: "Synthetic speech", prompt: "Original brief",
    }]};
  const responses = new Map([
    ["/ty-h3lv/projects", [{...plan, duration: 5, count: 1}]],
    [`/ty-h3lv/project/${id}`, plan],
    [`/ty-h3lv/project/${id}/analysis`, {}],
    [`/ty-h3lv/project/${id}/director`, {planning_method: "qwen", constraints: "Fixed frame",
      draft: null, approval: null, stale: false, qwen_available: true}],
  ]);
  const requested = [];
  const sandbox = {
    console, URL, URLSearchParams, Blob, Event, setTimeout, clearTimeout, queueMicrotask,
    setInterval: callback => { timers.push(callback); return 1; }, clearInterval() {},
    document: {body: new Element("body"), getElementById: () => null,
      querySelector: () => ({}), createElement: tag => new Element(tag)},
    window: {}, navigator: {},
    app: {graph: {_nodes: []}, graphToPrompt: async () => ({output: graph}),
      registerExtension() {}},
    api: {apiURL: value => value,
      addEventListener: (name, callback) => listeners.set(name, callback), removeEventListener() {},
      fetchApi: async (path, options) => {
      requested.push(path);
      if (path.endsWith("/director/output-fields")) {
        assert.equal(options?.method, "POST");
        const body = JSON.parse(options.body);
        plan.segments[body.segment_index].external_qwen_fields = body.fields;
        plan.revision++;
        return {ok: true, json: async () => structuredClone(plan)};
      }
      assert.equal(options?.method, undefined, "opening a review must not submit a task");
      assert.ok(responses.has(path), `unexpected request ${path}`);
      return {ok: true, json: async () => structuredClone(responses.get(path))};
    }},
    createTimeline: () => {}, pcmWavePeaks: () => [],
    qwenCandidates, selectedQwenId, directorNeedsApproval, directorUpdateAction, directorJobText, videoPromptBindings,
  };
  const code = (await readFile(new URL("../web/ty-h3lv.js", import.meta.url), "utf8"))
    .replace(/^import .*;$/gm, "")
    .replaceAll("import.meta.url", JSON.stringify("http://local/ty-h3lv.js"));
  vm.runInNewContext(`${code}\nglobalThis.openReviewForTest = openReview;`, sandbox);
  await sandbox.openReviewForTest({id: "1", properties: {ty_h3lv_project: id}, widgets: []});
  const single = created.find(item => item.textContent === "仅生成本段");
  assert.ok(single, "a fresh segment must support one-segment manual validation");
  const run = created.find(item => item.className?.includes("ty-h3lv-button run"));
  assert.equal(run.disabled, true, "cut approval alone does not permit video generation");
  const generateDraft = created.find(item => item.textContent === "Qwen 全片分镜设计");
  assert.equal(generateDraft.disabled, false, "confirmed cuts and one Qwen allow a text draft");
  assert.equal(requested.length, 4);
  const toolbar = created.find(item => item.children.includes(generateDraft));
  assert.deepEqual(toolbar.children.slice(0, 4).map(item => item.textContent),
    ["保存并确认", "重新分析分段", "Qwen 全片分镜设计", "▶ 批量生成"]);
  assert.equal(created.filter(item => item.textContent === "确认分镜方案（第二次确认）").length, 0);
  assert.equal(created.filter(item => item.textContent?.includes("生成全片导演草稿")).length, 0);
  assert.equal(created.filter(item => item.title === "勾选后输出到外部 Qwen").length, 0);
  assert.doesNotMatch(code, /addWidget\("button", "Qwen 全片分镜设计"/);
  const state = responses.get(`/ty-h3lv/project/${id}/director`);
  state.job = {prompt_id: "current", status: "running", phase: "loading_model"};
  listeners.get("ty-h3lv-director")({detail: {project_id: id, prompt_id: "current"}});
  await new Promise(resolve => setTimeout(resolve, 0));
  let top = created.find(item => item.className === "ty-h3lv-director-job");
  assert.match(top.textContent, /加载 Qwen 模型/);
  assert.equal(generateDraft.disabled, true);
  const brief = created.find(item => item.tagName === "textarea" && item.value === "Original brief");
  brief.value = "Unsaved manual edit";
  brief.oninput();
  state.job = {...state.job, status: "failed", phase: "failed", error: "32635 + 8192 > 32768"};
  await timers[0]();
  assert.match(top.textContent, /32635 \+ 8192 > 32768/);
  assert.equal(brief.value, "Unsaved manual edit");
  assert.match(generateDraft.title, /保存/);
  await sandbox.openReviewForTest({id: "1", properties: {ty_h3lv_project: id}, widgets: []});
  top = created.findLast(item => item.className === "ty-h3lv-director-job");
  assert.match(top.textContent, /32635 \+ 8192 > 32768/);

  state.job = {prompt_id: "complete", status: "completed"};
  state.draft = {id: "draft", status: "draft", input_snapshot: {source: {segments: [{
    segment_index: 0, content_duration_seconds: 5, speech_cues: [],
  }]}}, result: {global_contract: "Keep continuity.", segments: [{
    segment_index: 0, intent: "Explain", brief: "Stay centered",
    entry_state: "Neutral", exit_state: "Neutral", next_transition: "Hold",
    beats: [{start_seconds: 0, end_seconds: 5, speech_cue_indices: [],
      shot_description: "Hold", pose: "Neutral", camera: "locked-off", motion: "hold"}],
  }]}};
  await sandbox.openReviewForTest({id: "1", properties: {ty_h3lv_project: id}, widgets: []});
  const outputs = created.filter(item => item.title === "勾选后输出到外部 Qwen");
  assert.equal(outputs.length, 6);
  assert.deepEqual(outputs.map(item => [item.dataset.field, item.checked]), [
    ["intent", false], ["brief", true], ["entry_state", true],
    ["exit_state", true], ["next_transition", false], ["timeline", true],
  ]);
  assert.match(code, /referenceSlots\.push\(\{slot: referenceSlots\.length, role: "reference", source: "ignore"\}\);\s*addButton\.disabled = referenceSlots\.length >= 6/);
  const timelineOutput = outputs.find(item => item.dataset.field === "timeline");
  timelineOutput.checked = false;
  await timelineOutput.onchange();
  assert.deepEqual(plan.segments[0].external_qwen_fields,
    ["brief", "entry_state", "exit_state"]);
  assert.ok(requested.some(path => path.endsWith("/director/output-fields")));

  plan.segments[0].prompt_kind = "final";
  await sandbox.openReviewForTest({id: "1", properties: {ty_h3lv_project: id}, widgets: []});
  const finalOutputs = created.filter(item => item.title === "勾选后输出到外部 Qwen").slice(-6);
  assert.equal(finalOutputs.length, 6);
  assert.ok(finalOutputs.every(item => item.disabled));
});

async function storyboardHarness({withReview = false, initialJob = null, qwenWidgets = []} = {}) {
  const created = [], requests = [], errors = [], timers = new Map();
  class Element {
    children = []; dataset = {}; value = ""; isConnected = true;
    classList = {add() {}, remove() {}, toggle() {}};
    constructor(tag) { this.tagName = tag; created.push(this); }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    querySelector() { return null; }
    focus() {}
    remove() { this.isConnected = false; }
  }
  const id = "b".repeat(32);
  const row = {segment_index: 0, start_sample: 0, end_sample: 100,
    content_duration_seconds: 5, text: "All synthetic confirmed words.", material_note: ""};
  const modelInput = {mode: "speaking", segments: [{
    segment_index: 0, time_range: "0.000-5.000s", duration: "5.000s",
    content: "All synthetic confirmed words.", material_note: "",
  }], constraints: "Fixed background."};
  const plan = {id, revision: 1, approved: true, segments: [row]};
  const state = {planning_method: "qwen", constraints: "Fixed background.", stale: false,
    draft: null, job: initialJob};
  const modelMessage = "全片模式：口播\n全片要求：Fixed background.\n\n分段：\n第 1 段 · 0.000–5.000 秒 · 5.000 秒\n内容：All synthetic confirmed words.";
  const preview = {preview_fingerprint: "confirmed-input", model_input: modelInput,
    model_message: modelMessage, input_snapshot: {source: modelInput},
    model_config: {llm_model: "same.gguf", vision_model: "same-mm.gguf", seed: 7, max_tokens: 8192},
    system_prompt: "Storyboard only; no H3 skill"};
  let refreshed = 0, edits = false, disconnected = false;
  if (withReview) {
    const panel = new Element("div");
    panel.id = "ty-h3lv-panel";
    panel.tyH3lvProjectId = id;
    panel.tyH3lvHasEdits = () => edits;
    panel.tyH3lvRefreshDirector = async () => { refreshed++; return true; };
  }
  const sandbox = {
    console, URL, URLSearchParams, Blob, Event, setTimeout, clearTimeout, queueMicrotask,
    setInterval(fn) { const id = timers.size + 1; timers.set(id, fn); return id; },
    clearInterval(id) { timers.delete(id); },
    document: {body: new Element("body"),
      getElementById: id => created.find(item => item.id === id && item.isConnected),
      querySelector: () => ({}), createElement: tag => new Element(tag)},
    window: {addEventListener() {}, removeEventListener() {}}, navigator: {},
    app: {graphToPrompt: async () => ({output: graph, workflow: {nodes: []}}), registerExtension() {}},
    api: {clientId: "test", apiURL: value => value, addEventListener() {}, removeEventListener() {},
      fetchApi: async (path, options) => {
        if (disconnected) throw new Error("Offline");
        const body = options.body && JSON.parse(options.body);
        requests.push({path, body});
        let value;
        if (path.endsWith("/director/preview")) value = preview;
        else if (path.endsWith("/director/plan")) {
          assert.equal(body.preview_fingerprint, preview.preview_fingerprint);
          state.job = {prompt_id: "text-only-id", status: "queued"};
          value = {prompt_id: "text-only-id"};
        } else if (path.endsWith("/director")) value = state;
        else if (path === `/ty-h3lv/project/${id}`) value = plan;
        else throw new Error(`unexpected request ${path}`);
        return {ok: true, json: async () => structuredClone(value)};
      }},
    createTimeline() {}, pcmWavePeaks: () => [],
    qwenCandidates, selectedQwenId, directorNeedsApproval, directorUpdateAction, directorJobText, videoPromptBindings,
    testError: value => errors.push(value),
  };
  const code = (await readFile(new URL("../web/ty-h3lv.js", import.meta.url), "utf8"))
    .replace(/^import .*;$/gm, "")
    .replaceAll("import.meta.url", JSON.stringify("http://local/ty-h3lv.js"));
  vm.runInNewContext(`${code}
    messageDialog = async value => testError(value);
    globalThis.openStory = openStoryboardDialog;`, sandbox);
  const owner = {id: "1", properties: {ty_h3lv_project: id}, widgets: qwenWidgets};
  await sandbox.openStory(owner);
  return {created, requests, errors, state, preview, owner,
    button: text => created.find(item => item.textContent === text && item.tagName === "button"),
    setEdits(value) { edits = value; },
    disconnect(value) { disconnected = value; },
    refreshed: () => refreshed,
    tick: async () => { for (const callback of [...timers.values()]) await callback(); },
  };
}

test("storyboard dialog previews all input but cancelling never queues", async () => {
  const h = await storyboardHarness();
  assert.ok(h.created.some(item => item.textContent === "All synthetic confirmed words."));
  assert.equal(h.button("确认运行").disabled, false);
  assert.equal(h.requests.filter(item => item.path.endsWith("/plan")).length, 0);
  await h.button("取消").onclick();
  assert.equal(h.requests.filter(item => item.path.endsWith("/plan")).length, 0);
});

test("storyboard preview renders only screenshot-style plain text", async () => {
  const h = await storyboardHarness();
  const previewText = h.created.find(item => item.tagName === "pre");
  assert.ok(previewText);
  assert.equal(previewText.textContent, h.preview.model_message);
  assert.match(previewText.textContent, /第 1 段 · 0\.000–5\.000 秒 · 5\.000 秒/);
  assert.doesNotMatch(previewText.textContent, /project_id|sample_rate|start_sample|generation_frames/);
  assert.doesNotMatch(previewText.textContent, /Storyboard only; no H3 skill/);
  assert.doesNotMatch((await readFile(new URL("../web/ty-h3lv.js", import.meta.url), "utf8")),
    /查看实际发送的完整输入|不截断/);
  assert.equal(h.created.filter(item => item.tagName === "details").length, 0);
  assert.ok(h.created.some(item => item.textContent === "发送给 Qwen 的简洁文本"));
});

test("storyboard preview carries the owner node's LongVideo Qwen controls", async () => {
  const qwenWidgets = [
    {name: "skill", value: "long_video_director"},
    {name: "duration", value: 10},
    {name: "llm_model", value: "Qwen3.8-27B-UD-Q4_K_M.gguf"},
    {name: "vision_model", value: "mmproj-F16.gguf"},
    {name: "think_mode", value: true},
    {name: "reasoning_effort", value: "xhigh"},
    {name: "seed", value: 42},
    {name: "max_tokens", value: 4096},
    {name: "video_sample_frames_per_sec", value: 3},
    {name: "force_unload_model", value: false},
  ];
  const h = await storyboardHarness({qwenWidgets});
  const preview = h.requests.find(item => item.path.endsWith("/director/preview"));
  assert.deepEqual(preview.body.qwen_config, {
    skill: "long_video_director", duration: 10,
    llm_model: "Qwen3.8-27B-UD-Q4_K_M.gguf", vision_model: "mmproj-F16.gguf",
    think_mode: true, reasoning_effort: "xhigh", seed: 42, max_tokens: 4096,
    video_sample_frames_per_sec: 3, force_unload_model: false,
  });
});

test("editing full-film requirements refreshes on confirmation and shows running state", async () => {
  const h = await storyboardHarness();
  const requirements = h.created.find(item => item.tagName === "textarea");
  requirements.value = "新的全片要求";
  requirements.oninput();
  const confirm = h.button("确认运行");
  assert.equal(confirm.disabled, false);
  const requestCount = h.requests.length;
  await confirm.onclick();
  const newRequests = h.requests.slice(requestCount);
  assert.ok(newRequests.some(item => item.path.endsWith("/director/preview")));
  assert.ok(newRequests.some(item => item.path.endsWith("/director/plan")));
  assert.ok((await readFile(new URL("../web/ty-h3lv.js", import.meta.url), "utf8"))
    .includes('confirm.textContent = "运行中…";'));
});

test("one confirmation queues once and completion automatically refreshes clean cards", async () => {
  const h = await storyboardHarness({withReview: true});
  const confirm = h.button("确认运行");
  await Promise.all([confirm.onclick(), confirm.onclick()]);
  assert.equal(h.requests.filter(item => item.path.endsWith("/plan")).length, 1);
  h.state.job.status = "completed";
  h.state.draft = {id: "new", result: {segments: [{}]}};
  await h.tick();
  assert.equal(h.refreshed(), 1);
  assert.deepEqual(h.errors, []);
});

test("dialog restores terminal errors and never disguises a connection error as completion", async () => {
  const error = "32635 input + 8192 output > 32768";
  const h = await storyboardHarness({initialJob: {prompt_id: "old", status: "failed", error}});
  const status = h.created.find(item => item.className?.includes("ty-h3lv-storyboard-status"));
  assert.match(status.textContent, /32635 input/);
  await h.button("确认运行").onclick();
  h.disconnect(true);
  await h.tick();
  assert.match(status.textContent, /暂时无法获取状态/);
  assert.doesNotMatch(status.textContent, /草稿已完成/);
  h.disconnect(false);
  h.state.job = {...h.state.job, status: "failed", error};
  await h.tick();
  assert.match(status.textContent, /32635 input/);
  assert.equal(h.button("确认运行").disabled, true);
  assert.equal(h.button("预览将发送的内容").disabled, false);
});

test("completed storyboard never overwrites edits made during inference", async () => {
  const h = await storyboardHarness({withReview: true});
  await h.button("确认运行").onclick();
  h.setEdits(true);
  h.state.job.status = "completed";
  h.state.draft = {id: "new", result: {segments: [{}]}};
  await h.tick();
  assert.equal(h.refreshed(), 0);
  assert.ok(h.created.some(item => item.textContent?.includes("新方案已就绪，已保留当前编辑")));
  assert.deepEqual(h.errors, []);
});

test("completed storyboard reruns once with a new internal seed and unchanged owner settings", async () => {
  const qwenWidgets = [
    {name: "skill", value: "long_video_director"},
    {name: "duration", value: 10},
    {name: "llm_model", value: "same.gguf"},
    {name: "vision_model", value: "same-mm.gguf"},
    {name: "think_mode", value: false},
    {name: "reasoning_effort", value: "medium"},
    {name: "seed", value: 7},
    {name: "max_tokens", value: 8192},
    {name: "video_sample_frames_per_sec", value: 2},
    {name: "force_unload_model", value: true},
  ];
  const h = await storyboardHarness({qwenWidgets});
  await h.button("确认运行").onclick();
  const firstPlan = h.requests.find(item => item.path.endsWith("/director/plan"));
  h.state.job = {prompt_id: "text-only-id", status: "completed", run_seed: 7};
  h.state.draft = {id: "first-draft", model_config: {seed: 7}, result: {segments: [{}]}};
  await h.tick();
  assert.equal(h.button("重新运行").disabled, false);

  const before = h.requests.length;
  const rerun = h.button("重新运行");
  await Promise.all([rerun.onclick(), rerun.onclick()]);
  const rerunRequests = h.requests.slice(before);
  const rerunPreview = rerunRequests.find(item => item.path.endsWith("/director/preview"));
  const rerunPlan = rerunRequests.find(item => item.path.endsWith("/director/plan"));
  assert.ok(rerunPreview);
  assert.ok(rerunPlan);
  assert.equal(rerunPreview.body.rerun_of, "text-only-id");
  assert.notEqual(rerunPreview.body.run_seed, 7);
  assert.equal(rerunPreview.body.qwen_config.seed, 7);
  assert.equal(rerunPlan.body.rerun_of, "text-only-id");
  assert.notEqual(rerunPlan.body.request_id, firstPlan.body.request_id);
  assert.equal(qwenWidgets.find(item => item.name === "seed").value, 7);
  assert.equal(rerunRequests.filter(item => item.path.endsWith("/director/plan")).length, 1);
});
