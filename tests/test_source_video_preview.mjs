import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {test} from "node:test";
import vm from "node:vm";

const source = (await readFile(new URL("../web/ty-h3lv.js", import.meta.url), "utf8"))
  .replace(/^import .*;$/gm, "")
  .replaceAll("import.meta.url", JSON.stringify("http://local/ty-h3lv.js"));

async function harness(type = "TY_ShotPlanner", value = "", projectValue = "") {
  let extension;
  const elements = [], callbacks = [], widgetChanges = [];
  let serializedSource = value, serializedProject = projectValue, graphVersions = 0;
  class Element {
    children = []; listeners = {}; dataset = {}; loads = 0; pauses = 0;
    videoWidth = 0; videoHeight = 0; currentTime = 0; readyState = 1; paused = true; isConnected = true;
    constructor(tag) { this.tagName = tag; elements.push(this); }
    append(...items) { this.children.push(...items); }
    setAttribute(name, value) { this[name] = value; }
    removeAttribute(name) { delete this[name]; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
    pause() { this.pauses++; const wasPlaying = !this.paused; this.paused = true; if (wasPlaying) this.onpause?.(); }
    load() { this.loads++; this.currentTime = 0; }
    focus() {}
    remove() { this.isConnected = false; }
    querySelectorAll() { return []; }
    click() {
      this.files = [{name: "新视频.mp4"}];
      this.onchange();
    }
  }
  class Node {
    id = 1;
    comfyClass = type;
    size = [400, 200]; inputs = []; properties = projectValue
      ? {ty_h3lv_project: projectValue} : {};
    widgets = [{
      name: "source_video", value, options: {values: [value]},
      callback: () => callbacks.push("source"),
      setValue(nextValue, {node} = {}) {
        const oldValue = this.value;
        if (Object.is(oldValue, nextValue)) return;
        this.value = nextValue;
        this.callback?.(nextValue);
        node?.onWidgetChanged?.(this.name, nextValue, oldValue, this);
        node?.graph?.incrementVersion?.();
      },
    }, {
      name: "project_id", value: projectValue,
      callback: () => callbacks.push("project"),
      setValue(nextValue, {node} = {}) {
        const oldValue = this.value;
        if (Object.is(oldValue, nextValue)) return;
        this.value = nextValue;
        this.callback?.(nextValue);
        node?.onWidgetChanged?.(this.name, nextValue, oldValue, this);
        node?.graph?.incrementVersion?.();
      },
    }];
    graph = {incrementVersion: () => { graphVersions++; }};
    onNodeCreated() { callbacks.push("created"); }
    onAdded() { callbacks.push("added"); }
    onConfigure() { callbacks.push("configure"); }
    onWidgetChanged(name, nextValue, oldValue, widget) {
      widgetChanges.push({name, nextValue, oldValue, widget});
      if (name === "source_video") serializedSource = nextValue;
      if (name === "project_id") serializedProject = nextValue;
    }
    addWidget(type, name) {
      const widget = {type, name, label: name};
      this.widgets.push(widget);
      return widget;
    }
    addDOMWidget(name, type, element, options) {
      const widget = {name, type, element, options,
        onRemove: () => callbacks.push("remove")};
      this.widgets.push(widget);
      return widget;
    }
    computeSize() { return this.size; }
    setSize(size) { this.size = size; }
    setDirtyCanvas() {}
  }
  const network = {requests: [], forms: [], clipError: false, clipDelay: null};
  const context = {
    console, URL, URLSearchParams, crypto: {randomUUID: () => "unique-upload"},
    FormData: class {values = {}; append(key, value) { this.values[key] = value; }},
    setTimeout: fn => fn(),
    window: {addEventListener() {}, removeEventListener() {}},
    document: {body: new Element("body"), querySelector: () => ({}), createElement: tag => new Element(tag),
      getElementById: id => elements.find(item => item.id === id && item.isConnected),
      querySelectorAll: () => elements.filter(item => ["video", "audio"].includes(item.tagName))},
    createTimeline(canvas, state, select, move, commit, detail, actions) {
      canvas.drawTimeline = () => {};
      canvas.disposeTimeline = () => { network.disposed = true; };
      network.timeline = {state, actions, select};
    },
    app: {canvas: {}, registerExtension: value => { extension = value; },
      graphToPrompt: async () => ({output: {"1": {class_type: type,
        inputs: {source_video: serializedSource, project_id: serializedProject}}}, workflow: {}})},
    api: {apiURL: value => value, fetchApi: async (path, options) => {
      network.requests.push(path);
      if (path === "/upload/image") {
        network.forms.push(options.body.values);
        return {ok: true, json: async () => ({name: "新视频.mp4",
          subfolder: options.body.values.subfolder || "上传"})};
      }
      if (path.endsWith("/preview")) return {ok: true, json: async () => ({duration: 40, waveform: {original: [0, .8]}})};
      if (network.clipDelay) await network.clipDelay;
      return {ok: !network.clipError, json: async () => network.clipError
        ? {error: "encode failed"} : {source_video: "TYH3LongVideo/clips/new/clip.mp4"}};
    }},
  };
  vm.runInNewContext(source, context);
  await extension.beforeRegisterNodeDef(Node, {name: type});
  const node = new Node();
  node.onNodeCreated();
  const preview = node.widgets.find(item => item.name === "source_video_preview");
  return {node, preview, context, callbacks, elements, network, widgetChanges,
    get graphVersions() { return graphVersions; },
    get serializedSource() { return serializedSource; }, source: node.widgets[0],
    get serializedProject() { return serializedProject; }, project: node.widgets[1],
    video: elements.find(item => item.tagName === "video")};
}

test("inline video follows selection and restored widgets without resetting unchanged playback", async () => {
  const h = await harness();
  const {preview, video} = h;
  assert.equal(preview.element.hidden, true);
  assert.equal(preview.computeSize(400)[1], -4);
  assert.equal(preview.serialize, false);
  assert.equal(preview.options.serialize, false);
  const uploads = h.node.widgets.find(item => item.name === "source_video_upload");
  assert.deepEqual(uploads.element.children.map(item => item.textContent), ["上传视频", "上传并截取视频"]);
  assert.equal(uploads.serialize, false);
  assert.ok(h.node.widgets.indexOf(uploads) < h.node.widgets.indexOf(preview));
  assert.ok(h.node.widgets.indexOf(preview) < h.node.widgets.findIndex(item => item.name === "分析并分段"));
  assert.equal(video.controls, true);
  assert.equal(video.preload, "metadata");
  assert.equal(video.playsInline, true);
  assert.notEqual(video.autoplay, true);
  h.source.value = "素材\\中文 视频.mp4";
  h.source.callback(h.source.value);
  let url = new URL(video.src, "http://local");
  assert.equal(url.searchParams.get("filename"), "中文 视频.mp4");
  assert.equal(url.searchParams.get("subfolder"), "素材");
  assert.equal(url.searchParams.get("type"), "input");
  assert.equal(preview.element.hidden, false);
  assert.equal(preview.hidden, false);
  video.currentTime = 3.25;
  const loads = video.loads;
  h.source.callback(h.source.value);
  h.node.onConfigure();
  assert.equal(video.loads, loads);
  assert.equal(video.currentTime, 3.25);
  assert.ok(h.callbacks.includes("configure"));
  assert.ok(h.callbacks.includes("source"));
  h.source.value = "恢复.mp4";
  h.node.onConfigure();
  assert.equal(new URL(video.src, "http://local").searchParams.get("filename"), "恢复.mp4");
  assert.equal(video.currentTime, 0);
  video.videoWidth = 720;
  video.videoHeight = 1280;
  video.onloadedmetadata();
  assert.equal(preview.computeSize(400)[1], 320);
  assert.equal(preview.options.getMinHeight(), 320);
  video.videoWidth = 1280;
  video.videoHeight = 720;
  assert.ok(preview.computeSize(400)[1] < 320);
  let stopped = false;
  preview.element.listeners.pointerdown({stopPropagation() { stopped = true; }});
  assert.equal(stopped, true);
  video.onerror();
  assert.equal(video.hidden, true);
  assert.equal(preview.element.children[1].hidden, false);
  h.source.value = "";
  h.source.callback("");
  assert.equal(preview.hidden, true);
  assert.equal(video.src, undefined);
});

test("same-name uploads refresh once and removing the preview releases media", async () => {
  const h = await harness("TY_ShotPlanner", "上传/新视频.mp4", "old-project");
  const button = h.node.widgets.find(item => item.name === "分析并分段");
  assert.equal(button.label, "重新分析并分段");
  const before = h.video.loads;
  await h.context.uploadSourceVideo(h.node);
  assert.equal(h.video.loads, before + 1);
  assert.equal(h.source.value, "上传/新视频.mp4");
  const url = new URL(h.video.src, "http://local");
  assert.ok(url.searchParams.get("timestamp"));
  assert.equal(h.callbacks.filter(item => item === "source").length, 0);
  assert.equal(h.callbacks.filter(item => item === "project").length, 1);
  assert.deepEqual(h.widgetChanges.map(item => item.name), ["project_id"]);
  assert.equal(h.graphVersions, 1);
  assert.equal(h.project.value, "");
  assert.equal(h.serializedProject, "");
  assert.equal(h.node.properties.ty_h3lv_project, "");
  assert.equal(button.name, "分析并分段");
  assert.equal(button.label, "分析并分段");
  h.video.currentTime = 4;
  h.preview.onRemove();
  assert.equal(h.video.src, undefined);
  assert.equal(h.video.currentTime, 0);
  assert.equal(typeof h.video.onloadedmetadata, "function");
  assert.equal(typeof h.video.onerror, "function");
  assert.ok(h.callbacks.includes("remove"));
});

test("workflow reactivation restores released video and keeps media callbacks across repeated switches", async () => {
  const h = await harness("TY_ShotPlanner", "素材/中文 视频.mp4");
  const {preview, video} = h;
  const metadata = video.onloadedmetadata, error = video.onerror;
  for (let i = 0; i < 3; i++) {
    preview.onRemove();
    assert.equal(video.src, undefined);
    const before = video.loads;
    h.node.onAdded();
    assert.equal(video.loads, before + 1);
    assert.equal(new URL(video.src, "http://local").searchParams.get("filename"), "中文 视频.mp4");
    assert.equal(video.onloadedmetadata, metadata);
    assert.equal(video.onerror, error);
    video.videoWidth = 1080;
    video.videoHeight = 1920;
    video.onloadedmetadata();
    assert.equal(preview.options.getMinHeight(), 320);
    video.currentTime = 2.5;
    h.node.onConfigure();
    h.node.onAdded();
    assert.equal(video.loads, before + 1);
    assert.equal(video.currentTime, 2.5);
    video.onerror();
    assert.equal(video.hidden, true);
    assert.equal(preview.element.children[1].hidden, false);
  }
  assert.equal(h.callbacks.filter(item => item === "added").length, 6);
  preview.onRemove();
  h.node.onConfigure();
  assert.equal(video.hidden, false);
  assert.equal(preview.element.children[1].hidden, true);
  assert.ok(video.src);
});

test("preview rejects paths outside input and does not change the ordinary TY node", async () => {
  const h = await harness("TY_ShotPlanner", "../private.mp4");
  assert.equal(h.preview.element.hidden, true);
  assert.equal(h.video.src, undefined);
  const ordinary = await harness("TY_H3LVUnified");
  assert.equal(ordinary.preview, undefined);
  assert.equal(ordinary.video, undefined);
  assert.ok(ordinary.callbacks.includes("created"));
  assert.equal(ordinary.node.widgets.find(item => item.name === "source_video_upload"), undefined);
});

test("source changes invalidate the binding while restoring a workflow preserves it", async () => {
  const h = await harness("TY_ShotPlanner", "old.mp4", "old-project");
  h.node.onConfigure();
  assert.equal(h.project.value, "old-project");
  h.source.value = "new.mp4"; h.source.callback();
  assert.equal(h.project.value, "");
  assert.equal(h.serializedProject, "");
  assert.equal(h.node.properties.ty_h3lv_project, "");
});

test("uploads synchronize the reactive widget store exactly once", async () => {
  const h = await harness("TY_ShotPlanner", "old.mp4", "old-project");
  await h.context.uploadSourceVideo(h.node);
  assert.equal(h.source.value, "上传/新视频.mp4");
  assert.equal(h.serializedSource, "上传/新视频.mp4");
  assert.equal((await h.context.app.graphToPrompt()).output["1"].inputs.source_video,
    "上传/新视频.mp4");
  assert.equal((await h.context.app.graphToPrompt()).output["1"].inputs.project_id, "");
  assert.equal(h.callbacks.filter(item => item === "source").length, 1);
  assert.equal(h.callbacks.filter(item => item === "project").length, 1);
  assert.deepEqual(h.widgetChanges.map(item => item.name), ["project_id", "source_video"]);
  const sourceChange = h.widgetChanges.find(item => item.name === "source_video");
  assert.equal(sourceChange.oldValue, "old.mp4");
  assert.equal(sourceChange.nextValue, "上传/新视频.mp4");
  assert.equal(sourceChange.widget, h.source);
  assert.equal(h.graphVersions, 2);
  assert.equal(h.project.value, "");
  assert.equal(h.serializedProject, "");
  assert.equal(h.node.properties.ty_h3lv_project, "");
});

test("legacy widgets use the same change notification fallback", async () => {
  const h = await harness("TY_ShotPlanner", "old.mp4");
  delete h.source.setValue;
  await h.context.uploadSourceVideo(h.node);
  assert.equal(h.source.value, "上传/新视频.mp4");
  assert.equal(h.serializedSource, "上传/新视频.mp4");
  assert.equal(h.callbacks.filter(item => item === "source").length, 1);
  assert.equal(h.widgetChanges.length, 1);
  assert.equal(h.widgetChanges[0].oldValue, "old.mp4");
  assert.equal(h.widgetChanges[0].nextValue, "上传/新视频.mp4");
  assert.equal(h.graphVersions, 1);
});

test("external analysis button follows execution, project restore, reanalysis and source replacement", async () => {
  const h = await harness("TY_ShotPlanner", "old.mp4");
  const button = h.node.widgets.find(item => item.name === "分析并分段");
  assert.equal(button.serialize, false);
  assert.equal(button.label, "分析并分段");
  h.context.openReview = async () => h.callbacks.push("review");
  h.node.onExecuted({ty_h3lv_project: ["analyzed-project"]});
  assert.equal(button.name, "分析并分段");
  assert.equal(button.label, "重新分析并分段");
  assert.equal(h.project.value, "analyzed-project");
  assert.ok(h.callbacks.includes("review"));
  h.node.onExecuted({});
  assert.equal(button.label, "重新分析并分段");
  h.context.analyzeOnly = async () => h.callbacks.push("analyze");
  await h.context.reanalyzeProject(h.node, {ask: false});
  assert.equal(button.name, "分析并分段");
  assert.equal(button.label, "分析并分段");
  assert.equal(h.project.value, "");
  assert.ok(h.callbacks.includes("analyze"));
  h.node.properties = {ty_h3lv_project: "restored-project"};
  h.node.onConfigure();
  assert.equal(button.label, "重新分析并分段");
  h.source.value = "new.mp4"; h.source.callback();
  assert.equal(button.label, "分析并分段");
  h.project.setValue("widget-only-project", {node: h.node});
  h.node.onAdded();
  assert.equal(button.label, "重新分析并分段");
  await h.context.uploadSourceVideo(h.node);
  assert.equal(button.label, "分析并分段");
  h.node.onExecuted({ty_h3lv_project: ["another-project"]});
  await h.context.uploadSourceVideo(h.node);
  assert.equal(button.label, "分析并分段");
  assert.equal(h.node.widgets.filter(item =>
    ["分析并分段", "重新分析并分段"].includes(item.name)).length, 1);
  const ordinary = await harness("TY_H3LVUnified");
  const ordinaryButton = ordinary.node.widgets.find(item => item.name === "重新分析并分段");
  ordinary.context.setProject(ordinary.node, "ordinary-project");
  ordinary.context.setProject(ordinary.node);
  assert.equal(ordinaryButton.name, "重新分析并分段");
  assert.equal(ordinaryButton.label, "重新分析并分段");
});

test("upload-and-clip cancellation and workflow removal never replace the source", async () => {
  const h = await harness("TY_ShotPlanner", "old.mp4", "old-project");
  const pending = h.context.uploadSourceVideo(h.node, {clip: true});
  await new Promise(setImmediate);
  assert.equal(h.source.value, "old.mp4");
  assert.equal(h.node.properties.ty_h3lv_project, "old-project");
  assert.equal(h.network.forms[0].overwrite, "false");
  assert.match(h.network.forms[0].subfolder, /^TYH3LongVideo\/uploads\//);
  assert.equal(h.network.timeline.state().clipSelection.end, 40);
  h.network.timeline.actions.cancel();
  await pending;
  assert.equal(h.source.value, "old.mp4");
  assert.equal(h.network.disposed, true);
  const controls = h.node.widgets.find(item => item.name === "source_video_upload");
  const again = h.context.uploadSourceVideo(h.node, {clip: true});
  await new Promise(setImmediate);
  const modalVideo = h.elements.filter(item => item.tagName === "video").at(-1);
  controls.onRemove();
  await again;
  assert.equal(modalVideo.src, undefined);
  assert.equal(modalVideo.onplay, null);
  assert.equal(h.network.requests.filter(path => path.endsWith("/clip")).length, 0);
  assert.equal(h.serializedSource, "old.mp4");
  assert.equal(h.widgetChanges.length, 0);
  assert.equal(h.graphVersions, 0);
});

test("clip retries retain range, quantize frames, stop playback and submit once", async () => {
  const h = await harness("TY_ShotPlanner", "old.mp4", "old-project");
  const button = h.node.widgets.find(item => item.name === "分析并分段");
  assert.equal(button.label, "重新分析并分段");
  const pending = h.context.uploadSourceVideo(h.node, {clip: true});
  await new Promise(setImmediate);
  const {actions, state} = h.network.timeline;
  actions.change({start: 1.01, end: 20.01}, "end");
  assert.equal(state().clipSelection.start, 1);
  assert.equal(state().clipSelection.end, 20);
  const video = h.elements.filter(item => item.tagName === "video").at(-1);
  video.currentTime = 0; video.paused = false; video.onplay();
  assert.equal(video.currentTime, 1);
  video.currentTime = 20.1; video.ontimeupdate();
  assert.equal(video.paused, true);
  assert.ok(video.currentTime < 20 && video.currentTime > 19.9);
  h.network.clipError = true;
  await actions.confirm();
  assert.equal(h.source.value, "old.mp4");
  assert.equal(state().clipSelection.end, 20);
  assert.equal(state().clipBusy, false);
  assert.ok(h.elements.some(item => item.textContent?.includes("encode failed")));
  h.network.clipError = false;
  let release;
  h.network.clipDelay = new Promise(resolve => { release = resolve; });
  const submit = actions.confirm();
  await actions.confirm();
  assert.equal(h.network.requests.filter(path => path.endsWith("/clip")).length, 2);
  release(); await submit; await pending;
  assert.equal(h.source.value, "TYH3LongVideo/clips/new/clip.mp4");
  assert.equal(h.serializedSource, "TYH3LongVideo/clips/new/clip.mp4");
  assert.equal((await h.context.app.graphToPrompt()).output["1"].inputs.source_video,
    "TYH3LongVideo/clips/new/clip.mp4");
  assert.equal(h.callbacks.filter(item => item === "source").length, 1);
  assert.equal(h.callbacks.filter(item => item === "project").length, 1);
  assert.deepEqual(h.widgetChanges.map(item => item.name), ["project_id", "source_video"]);
  const sourceChange = h.widgetChanges.find(item => item.name === "source_video");
  assert.equal(sourceChange.oldValue, "old.mp4");
  assert.equal(sourceChange.nextValue, "TYH3LongVideo/clips/new/clip.mp4");
  assert.equal(h.graphVersions, 2);
  assert.equal(h.node.properties.ty_h3lv_project, "");
  assert.equal(h.serializedProject, "");
  assert.equal(button.name, "分析并分段");
  assert.equal(button.label, "分析并分段");
  assert.equal(h.network.disposed, true);
  assert.equal(h.network.requests.some(path => path.includes("/prompt")), false);
});
