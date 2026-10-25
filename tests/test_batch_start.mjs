import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";
import vm from "node:vm";

const reviewSource = await readFile(new URL("../web/director_review.js", import.meta.url), "utf8");
const review = await import(`data:text/javascript,${encodeURIComponent(reviewSource)}`);
const source = (await readFile(new URL("../web/ty-h3lv.js", import.meta.url), "utf8"))
  .replace(/^import .*;$/gm, "")
  .replaceAll("import.meta.url", JSON.stringify("http://local/ty-h3lv.js"));

function harness({decision = true, approvalError = false, runError = false, rules = false, shot = false, clipError = false} = {}) {
  const created = [], posts = [], confirmations = [], timers = [];
  let child;
  const messages = [];
  class Element {
    children = []; dataset = {}; value = ""; isConnected = true;
    classList = {add() {}, remove() {}, toggle() {}};
    constructor(tag) { this.tagName = tag; created.push(this); }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute(name, value) { this[name] = value; }
    querySelector() { return null; }
    remove() { this.isConnected = false; }
    focus() {}
  }
  const id = "c".repeat(32);
  const row = {index: 0, segment_index: 0, start: 0, end: 5, duration: 5,
    start_sample: 0, end_sample: 500, generation_frames: 124, edit_frames: 120,
    text: "原始台词", prompt: "原始简报", refs: []};
  const designed = {...row, intent: "介绍", brief: "克制讲述", entry_state: "自然站姿",
    exit_state: "放松双手", next_transition: "保持姿态", beats: [{
      start_seconds: 0, end_seconds: 5, speech_cue_indices: [0],
      shot_description: "自然讲述", pose: "保持居中", camera: "locked-off",
    }]};
  const plan = {id, created: 0, revision: 1, approved: true, sample_rate: 100,
    planning_method: rules ? "rules" : "qwen", mode: "speaking", run_status: "draft",
    project_kind: shot ? "shot_planner" : "long_video",
    reference_default_count: null, max_seconds: 15, segments: [row]};
  const director = {planning_method: plan.planning_method, constraints: "固定机位",
    qwen_available: true, stale: false, approval: null,
    job: {status: "completed", elapsed_seconds: 83},
    draft: {id: "draft", result: {global_contract: "连续讲述", segments: [designed]},
      input_snapshot: {source: {segments: [{...row, content_duration_seconds: 5}]}}}};
  const owner = {id: "1", comfyClass: shot ? "TY_ShotPlanner" : "TY_H3LVUnified",
    properties: {ty_h3lv_project: id}, widgets: []};
  const approval = {revision: 1, draft_id: "draft", global_contract: "连续讲述",
    segments: [designed]};
  const sandbox = {
    ...review, console, URL, URLSearchParams, Blob, Event, setTimeout, clearTimeout, queueMicrotask,
    setInterval: fn => { timers.push(fn); return timers.length; }, clearInterval() {},
    document: {body: new Element("body"), getElementById: () => null,
      querySelector: () => ({}), createElement: tag => new Element(tag)},
    window: {}, navigator: {}, createTimeline(canvas, state, select, move, commit, detail, actions, cuts) {
      sandbox.timeline = {state, actions, cuts, select, move};
    }, pcmWavePeaks: () => [],
    app: {graph: {_nodes: []}, registerExtension() {}, graphToPrompt: async () => ({
      output: {"1": {class_type: shot ? "TY_ShotPlanner" : "TY_H3LVUnified"},
        "2": {class_type: "VHS_VideoCombine"}},
      workflow: {nodes: []},
    })},
    testConfirm: async options => {
      confirmations.push(options);
      return typeof decision === "function" ? decision(options) : decision;
    },
    api: {clientId: "test", apiURL: value => value, addEventListener() {}, removeEventListener() {},
      fetchApi: async (path, options = {}) => {
        const body = options.body ? JSON.parse(options.body) : null;
        let result;
        if (body) {
          posts.push({path, body});
          if (path.endsWith("/clip")) {
            if (clipError) throw new Error("截取失败");
            child = {...structuredClone(plan), id: "d".repeat(32), approved: false,
              duration: 2, revision: 1, clip_origin: {start_seconds: 1, end_seconds: 3}};
            result = child;
          } else if (path.endsWith("/edit")) {
            const running = ["running", "pausing", "stopping", "merging"].includes(plan.run_status);
            plan.revision++;
            plan.approved = running;
            plan.run_status = running ? plan.run_status : "draft";
            if (shot) {
              let start = 0;
              plan.segments = body.segments.map((row, index) => {
                const result = {...row, index, start, end: row.end, duration: row.end-start};
                start = row.end;
                return result;
              });
            }
            result = plan;
          } else if (path.endsWith("/director/approve")) {
            if (approvalError) throw new Error("审批失败");
            plan.revision++;
            plan.director_approval = {id: "approved", draft_id: "draft"};
            plan.approved_director = {global_contract: body.global_contract, segments: body.segments};
            director.approval = plan.director_approval;
            result = plan;
          } else if (path.endsWith("/director/configure")) {
            plan.revision++;
            plan.planning_method = body.planning_method;
            plan.director_constraints = body.constraints;
            plan.full_regeneration_pending = true;
            result = plan;
           } else if (path.endsWith("/approve")) {
             plan.approved = true;
             result = plan;
          } else if (path.endsWith("/run")) {
            if (runError) throw new Error("启动失败");
            result = {started: true};
            plan.run_status = "running";
          } else if (path.endsWith("/regenerate")) {
            if (runError) throw new Error("启动失败");
            result = {started: true};
          } else if (path.endsWith("/retry")) {
            result = plan;
          } else throw new Error(`Unexpected POST ${path}`);
        } else if (path === "/ty-h3lv/projects") result = [{...plan, duration: 5, count: 1}];
        else if (path.endsWith("/director")) result = director;
        else if (path.endsWith("/analysis")) result = {};
        else if (path.endsWith(id)) result = plan;
        else if (child && path.endsWith(child.id)) result = child;
        else throw new Error(`Unexpected GET ${path}`);
        return {ok: true, json: async () => structuredClone(result)};
      }},
  };
  vm.runInNewContext(`${source}
    confirmDialog = options => testConfirm(options);
    messageDialog = async options => testMessage(options);
    toast = () => {};
    globalThis.start = startApprovedSequence;
    globalThis.openReviewTest = openReview;
    globalThis.statusTextTest = statusText;
  `, sandbox);
  sandbox.testMessage = options => messages.push(options);
  return {sandbox, created, posts, confirmations, timers, owner, plan, director, approval, messages};
}

test("shot cuts retain draft settings, use bounds for audio and hide internal prompt controls", async () => {
  const h = harness({rules: true, shot: true});
  h.plan.duration = 5;
  await h.sandbox.openReviewTest(h.owner);
  const {state, cuts} = h.sandbox.timeline;
  state().rows[0].note.value = "未保存备注";
  state().rows[0].refs.push("new-ref.png");
  cuts.split(2);
  assert.equal(state().rows.length, 2);
  assert.equal(state().rows[1].note.value, "未保存备注");
  assert.equal(state().rows[1].refs[0], "new-ref.png");
  assert.match(state().rows[1].vocals.src, /index=0&start=2\.000000&end=5\.000000/);
  assert.equal(h.created.some(item => item.textContent === "编辑本段镜头简报（提示词）"), false);
  state().rows[1].refs.push("other.png");
  await cuts.remove(0);
  assert.equal(state().rows.length, 1);
  assert.equal(state().rows[0].refs.length, 1);
  assert.match(h.messages.at(-1).message, /左段参考图/);
  const save = h.created.find(item => item.textContent === "保存并确认");
  await save.onclick();
  assert.equal(h.posts[0].body.segments.length, 1);
  assert.equal(h.posts[0].body.segments[0].material_note, "未保存备注");
  assert.equal(h.posts.some(item => /\/run$/.test(item.path)), false);
  const ordinary = harness({rules: true});
  await ordinary.sandbox.openReviewTest(ordinary.owner);
  assert.equal(ordinary.sandbox.timeline.cuts, undefined);
});

test("shot cut limits reject endpoints, duplicate cuts and merges longer than fifteen seconds", async () => {
  const h = harness({rules: true, shot: true});
  h.plan.duration = 18;
  h.plan.segments = [6, 12, 18].map((end, index) => ({...h.plan.segments[0], index,
    start: index*6, end, duration: 6, start_sample: index*600, end_sample: end*100}));
  await h.sandbox.openReviewTest(h.owner);
  const {state, cuts, actions} = h.sandbox.timeline;
  assert.throws(() => cuts.split(0), /内部/);
  assert.throws(() => cuts.split(6), /重复/);
  await cuts.remove(0);
  assert.equal(state().rows.length, 2);
  assert.equal(Number(state().rows[0].end.value), 12);
  await assert.rejects(cuts.remove(0), /15 秒/);
  assert.equal(state().rows.length, 2);
  state().plan.run_status = "running";
  assert.equal(cuts.disabled(), true);
  state().plan.run_status = "draft";
  assert.equal(cuts.disabled(), false);
  assert.equal(actions, null);
});

test("every new shot batch replaces the frozen graph to resolve the latest external fixed_prompt", async () => {
  const h = harness({rules: true, shot: true});
  await h.sandbox.openReviewTest(h.owner);
  assert.equal(h.created.find(item => item.textContent === "Qwen 全片分镜设计").hidden, true);
  assert.equal(await h.sandbox.start(h.owner, h.plan), true);
  assert.equal(h.posts.at(-1).body.replace_snapshot, true);
});

test("review retains cut editing without exposing the old project clip flow", async () => {
  const h = harness({rules: true, shot: true});
  h.plan.duration = 5;
  await h.sandbox.openReviewTest(h.owner);
  assert.equal(h.sandbox.timeline.actions, null);
  assert.equal(typeof h.sandbox.timeline.cuts.split, "function");
  assert.equal(h.posts.length, 0);
  assert.equal(h.owner.properties.ty_h3lv_project, h.plan.id);
});

test("shot planner hides director controls and exposes combined save/start action", async () => {
  const h = harness({rules: true, shot: true});
  await h.sandbox.openReviewTest(h.owner);
  const run = h.created.find(item => item.className?.includes("ty-h3lv-button run"));
  const director = h.created.find(item => item.className === "ty-h3lv-director");
  assert.equal(run.textContent, "保存并开始批量");
  assert.equal(director.hidden, true);
});

test("fresh unapproved draft enables combined approval/start and submits current edited cards", async () => {
  const h = harness();
  await h.sandbox.openReviewTest(h.owner);
  const run = h.created.find(item => item.className?.includes("ty-h3lv-button run"));
  assert.equal(run.disabled, false);
  assert.equal(run.textContent, "▶ 批量生成");
  assert.match(h.sandbox.statusTextTest(h.plan, h.director), /分镜草稿已就绪/);
  assert.equal(h.created.filter(item => item.textContent === "确认分镜方案（第二次确认）").length, 0);
  const brief = h.created.find(item => item.tagName === "textarea" && item.value === "克制讲述");
  brief.value = "审核后的小幅手势";
  brief.oninput();
  assert.equal(run.disabled, false, "director edits can be explicitly approved from the top action");
  let prevented = false, stopped = false;
  await run.onclick({preventDefault: () => {prevented = true}, stopPropagation: () => {stopped = true}});
  assert.equal(prevented, true);
  assert.equal(stopped, true);
  assert.equal(h.posts.length, 2);
  assert.match(h.posts[0].path, /director\/approve$/);
  assert.equal(h.posts[0].body.segments[0].brief, "审核后的小幅手势");
  assert.match(h.posts[1].path, /\/run$/);
  assert.equal(h.posts[1].body.revision, 2);
  assert.equal(h.posts[1].body.director_approval_id, "approved");
  assert.equal(h.confirmations.length, 1);
  assert.equal(h.created.find(item => item.className?.includes("ty-h3lv-shade"))?.isConnected, false,
    "successful batch submission closes the review window");
});

test("cancelled batch submission keeps the review window open", async () => {
  const h = harness({decision: false});
  await h.sandbox.openReviewTest(h.owner);
  const run = h.created.find(item => item.className?.includes("ty-h3lv-button run"));
  await run.onclick();
  assert.equal(h.posts.length, 0);
  assert.equal(h.created.find(item => item.className?.includes("ty-h3lv-shade"))?.isConnected, true);
});

test("save and confirm persists edited director cards before generation", async () => {
  const h = harness();
  await h.sandbox.openReviewTest(h.owner);
  const brief = h.created.find(item => item.tagName === "textarea" && item.value === "克制讲述");
  brief.value = "保存后的镜头简报";
  brief.oninput();
  const save = h.created.find(item => item.textContent === "保存并确认");
  await save.onclick();
  assert.deepEqual(h.posts.map(item => item.path), [
    `/ty-h3lv/project/${h.plan.id}/approve`,
    `/ty-h3lv/project/${h.plan.id}/director/approve`,
  ]);
  assert.equal(h.posts[1].body.segments[0].brief, "保存后的镜头简报");
  assert.equal(h.created.find(item => item.className?.includes("ty-h3lv-shade"))?.isConnected, true,
    "saving does not close the review window");
});

test("save and confirm persists edits while video generation is running", async () => {
  const h = harness();
  h.plan.run_status = "running";
  await h.sandbox.openReviewTest(h.owner);
  const timeline = h.created.find(item => item.tagName === "textarea" &&
    item.value?.includes("[Shot 1]"));
  timeline.value = timeline.value.split("\n").filter(line => !line.startsWith("台词：")).join("\n");
  timeline.oninput();
  const save = h.created.find(item => item.textContent === "保存并确认");
  await save.onclick();
  assert.deepEqual(h.posts.map(item => item.path), [
    `/ty-h3lv/project/${h.plan.id}/approve`,
    `/ty-h3lv/project/${h.plan.id}/director/approve`,
  ]);
  assert.equal(h.posts[1].body.segments[0].timeline.includes("台词："), false);
  assert.equal(h.plan.run_status, "running");
});

test("fresh unapproved draft auto-approves before one-segment generation", async () => {
  const h = harness();
  await h.sandbox.openReviewTest(h.owner);
  const single = h.created.find(item => item.className?.includes("segment-run"));
  await single.onclick();
  assert.equal(h.posts.length, 2);
  assert.match(h.posts[0].path, /director\/approve$/);
  assert.match(h.posts[1].path, /\/regenerate$/);
  assert.equal(h.posts[1].body.index, 0);
  assert.equal(h.posts[1].body.revision, 2);
  assert.equal(h.posts[1].body.director_approval_id, "approved");
  assert.equal(h.confirmations.length, 1);
  assert.match(h.confirmations[0].title, /仅生成第 1 段/);
});

test("already-approved generation skips duplicate approval but still confirms once", async () => {
  const h = harness();
  h.director.approval = {id: "approved", draft_id: "draft"};
  h.plan.director_approval = h.director.approval;
  assert.equal(await h.sandbox.start(h.owner, h.plan), true);
  assert.equal(h.posts.length, 1);
  assert.match(h.posts[0].path, /\/run$/);
  assert.equal(h.posts[0].body.director_approval_id, "approved");
  assert.equal(h.confirmations.length, 1);
});

test("idle legacy replacements force a fresh batch without reopening missing Qwen draft", async () => {
  const h = harness();
  h.plan.segments[0].job = {status: "completed", video: "old.mp4"};
  h.plan.segments[0].needs_regeneration = true;
  h.plan.segments[0].regeneration_nonce = "new-round";
  h.director.draft = null;
  h.director.approval = null;
  h.director.job = null;
  assert.equal(await h.sandbox.start(h.owner, h.plan), true);
  assert.deepEqual(h.posts.map(item => item.path), [`/ty-h3lv/project/${h.plan.id}/run`]);
  assert.equal(h.posts[0].body.force_regeneration, true);
  assert.equal(h.confirmations.length, 1);
  assert.match(h.confirmations[0].title, /新一轮重生成/);
});

test("batch generation auto-saves an edited segment prompt before rerun", async () => {
  const h = harness();
  await h.sandbox.openReviewTest(h.owner);
  const prompt = h.created.find(item => item.tagName === "textarea" && item.value === "原始简报");
  prompt.value = "手动修改后的镜头简报";
  prompt.oninput();
  const run = h.created.find(item => item.className?.includes("ty-h3lv-button run"));
  await run.onclick();
  assert.deepEqual(h.posts.map(item => item.path), [
    `/ty-h3lv/project/${h.plan.id}/edit`,
    `/ty-h3lv/project/${h.plan.id}/approve`,
    `/ty-h3lv/project/${h.plan.id}/director/approve`,
    `/ty-h3lv/project/${h.plan.id}/run`,
  ]);
  assert.equal(h.posts[0].body.segments[0].prompt, "手动修改后的镜头简报");
  assert.equal(h.posts[3].body.revision, 3);
});

test("cancel does not approve, retry or start", async () => {
  const h = harness({decision: false});
  h.plan.segments[0].job = {status: "failed", error: "stopped"};
  assert.equal(await h.sandbox.start(h.owner, h.plan, {approval: h.approval}), false);
  assert.equal(h.posts.length, 0);
});

test("approval failure never starts; start failure retains approved state", async () => {
  for (const kind of ["approvalError", "runError"]) {
    const h = harness({[kind]: true});
    await assert.rejects(h.sandbox.start(h.owner, h.plan, {approval: h.approval}), /失败/);
    assert.equal(h.posts.length, kind === "approvalError" ? 1 : 2);
    assert.equal(Boolean(h.plan.director_approval), kind === "runError");
  }
});

test("double click while confirmation is pending submits exactly once", async () => {
  let resolve, opened;
  const ready = new Promise(done => { opened = done; });
  const h = harness({decision: () => { opened(); return new Promise(done => { resolve = done; }); }});
  const first = h.sandbox.start(h.owner, h.plan, {approval: h.approval});
  await ready;
  await h.sandbox.start(h.owner, h.plan, {approval: h.approval});
  assert.equal(h.posts.length, 0);
  resolve(true);
  await first;
  assert.equal(h.posts.length, 2);
  assert.equal(h.confirmations.length, 1);
});

test("edits during confirmation prevent all mutations", async () => {
  const h = harness();
  await assert.rejects(h.sandbox.start(h.owner, h.plan, {
    approval: h.approval, isCurrent: () => false,
  }), /编辑已变化/);
  assert.equal(h.posts.length, 0);
});

test("stale and pending director work remain blocked, but settings save on batch start", async () => {
  const h = harness();
  await h.sandbox.openReviewTest(h.owner);
  const run = h.created.find(item => item.className?.includes("ty-h3lv-button run"));
  h.director.stale = true;
  await h.timers[0]();
  assert.equal(run.disabled, true);
  assert.match(run.title, /过期/);
  h.director.stale = false;
  h.director.job.status = "running";
  await h.timers[0]();
  assert.equal(run.disabled, true);
  assert.match(run.title, /等待分镜/);
  h.director.job.status = "completed";
  await h.timers[0]();
  const constraints = h.created.find(item => item.tagName === "textarea" && item.value === "固定机位");
  constraints.value = "新要求";
  constraints.oninput();
  assert.equal(run.disabled, false);
  assert.match(run.title, /自动保存规划设置/);
  assert.equal(h.posts.length, 0);
});

test("planning settings auto-save and force full rerun", async () => {
  const h = harness({rules: true});
  await h.sandbox.openReviewTest(h.owner);
  const constraints = h.created.find(item => item.tagName === "textarea" && item.value === "固定机位");
  constraints.value = "新全片约束";
  constraints.oninput();
  const run = h.created.find(item => item.className?.includes("ty-h3lv-button run"));
  await run.onclick();
  assert.deepEqual(h.posts.map(item => item.path), [
    `/ty-h3lv/project/${h.plan.id}/approve`,
    `/ty-h3lv/project/${h.plan.id}/director/configure`,
    `/ty-h3lv/project/${h.plan.id}/run`,
  ]);
  assert.match(h.confirmations[0].message, /全部 1 段/);
  assert.match(h.confirmations[0].message, /新的随机种子/);
  assert.doesNotMatch(h.confirmations[0].message, /已完成片段不会自动重跑/);
});

test("rules mode starts without director approval; failed segment retains explicit retry", async () => {
  const h = harness({rules: true});
  h.plan.segments[0].job = {status: "failed", error: "stopped"};
  await h.sandbox.start(h.owner, h.plan);
  assert.equal(h.posts.length, 2);
  assert.match(h.posts[0].path, /\/retry$/);
  assert.match(h.posts[1].path, /\/run$/);
  assert.equal(h.posts[1].body.replace_snapshot, true);
  assert.match(h.confirmations[0].confirmText, /重跑第 1 段并继续/);
});
