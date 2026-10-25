"""Director review/queue tests never start a service or execute a model."""

import asyncio
import copy
import importlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_plugin import core, controller, sample_plan
from test_director_task import response_for, FakeAdapter

review = importlib.import_module("h3lv_test.director_review")


class FakeQueue:
    def __init__(self):
        self.running, self.waiting, self.history, self.puts = [], [], {}, []

    def get_current_queue(self):
        return self.running, self.waiting

    def get_history(self, prompt_id):
        return {prompt_id: self.history[prompt_id]} if prompt_id in self.history else {}

    def put(self, item):
        self.puts.append(item)
        self.waiting.append(item)


class DirectorReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = sample_plan()
        self.plan.update(approved=True, approved_fingerprint=core.fingerprint(self.plan))
        core.write_plan(self.root, self.plan)
        self.pid = self.plan["id"]
        self.directory = core.project_path(self.root, self.pid)
        for name, value in (
                ("transcript.json", {"segments": [{"text": "All confirmed words."}]}),
                ("analysis.json", {"sections": [{"kind": "speech"}]})):
            core.state_file(self.directory, name).write_text(json.dumps(value), encoding="utf-8")
        self.server = types.SimpleNamespace(number=1, prompt_queue=FakeQueue())

    def save(self):
        self.plan["approved_fingerprint"] = core.fingerprint(self.plan)
        core.write_plan(self.root, self.plan)

    def configure(self):
        self.plan = review.configure_director(self.root, self.pid, {
            "revision": self.plan["revision"], "planning_method": "qwen",
            "constraints": "Keep the presenter centered. No camera motion.",
        }, self.server)

    def test_configure_change_marks_every_segment_for_regeneration(self):
        self.plan["segments"][0]["job"] = {
            "status": "completed", "video": "old.mp4", "prompt_id": "old",
        }
        self.plan["segments"][1]["job"] = {"status": "failed", "error": "stopped"}
        self.plan["final_video"] = "old-final.mp4"
        self.save()
        before = [row.get("regeneration_nonce") for row in self.plan["segments"]]
        changed = review.configure_director(self.root, self.pid, {
            "revision": self.plan["revision"], "planning_method": "rules",
            "constraints": "New whole-film constraints.",
        }, self.server)
        self.assertTrue(changed["full_regeneration_pending"])
        self.assertTrue(changed["final_stale"])
        for index, row in enumerate(changed["segments"]):
            self.assertTrue(row.get("regeneration_nonce"))
            self.assertNotEqual(row["regeneration_nonce"], before[index])
        self.assertTrue(changed["segments"][0]["needs_regeneration"])
        self.assertEqual(changed["segments"][0]["job"]["video"], "old.mp4")
        self.assertNotIn("job", changed["segments"][1])

    def test_configure_same_values_does_not_request_another_full_regeneration(self):
        self.configure()
        nonces = [row.get("regeneration_nonce") for row in self.plan["segments"]]
        pending = self.plan.get("full_regeneration_pending")
        unchanged = review.configure_director(self.root, self.pid, {
            "revision": self.plan["revision"], "planning_method": "qwen",
            "constraints": "Keep the presenter centered. No camera motion.",
        }, self.server)
        self.assertEqual([row.get("regeneration_nonce") for row in unchanged["segments"]], nonces)
        self.assertEqual(unchanged.get("full_regeneration_pending"), pending)

    def test_idle_legacy_replacement_can_reuse_saved_qwen_input(self):
        self.plan.update(planning_method="qwen", director_constraints="Keep it stable.")
        for row in self.plan["segments"]:
            row.update(needs_regeneration=True, job={
                "status": "completed", "video": "old.mp4",
                "director_execution": {"qwen_input": "previous approved brief"},
            })
        self.save()
        with self.assertRaisesRegex(ValueError, "旧方案缺失"):
            review.require_director_approval(self.root, self.plan)
        review.require_director_approval(
            self.root, self.plan, allow_legacy_regeneration=True)
        self.assertEqual(review.effective_segment_brief(
            self.root, self.plan, self.plan["segments"][0],
            allow_legacy_regeneration=True), "previous approved brief")

    def test_source_snapshot_accepts_sparse_reference_slots(self):
        refs = core.reference_directory(self.directory)
        refs.mkdir(parents=True, exist_ok=True)
        (refs / "scene.png").write_bytes(b"scene")
        self.plan["segments"][0]["refs"] = [None, "scene.png"]
        self.save()
        snapshot = review.source_snapshot(self.root, self.plan)
        self.assertEqual(snapshot["source"]["segments"][0]["refs"], [None, "scene.png"])
        self.assertIn("scene.png", snapshot["source"]["reference_fingerprints"])

    def draft(self):
        snapshot = review.source_snapshot(self.root, self.plan)
        result = json.loads(response_for([{}, {"content": snapshot["model_message"]}]))
        draft = review.ai_director.draft_for_storage(snapshot, result)
        review.director_task._atomic_json(core.state_file(self.directory, "director_draft.json"), draft)
        return draft

    def payload(self, draft):
        return {"revision": self.plan["revision"], "draft_id": draft["id"],
                "global_contract": draft["result"]["global_contract"],
                "segments": copy.deepcopy(draft["result"]["segments"])}

    def approve(self, payload):
        self.plan = review.approve_director(self.root, self.pid, payload, self.server)
        return self.plan

    def queue_payload(self):
        return {"revision": self.plan["revision"], "qwen_source_id": "q2",
                "client_id": "client-a", "workflow": {"nodes": [{"id": 22}]},
                "prompt": {
                    "q1": {"class_type": "QwenH3PromptLocal", "inputs": {
                        "llm_model": "wrong.gguf", "vision_model": "wrong-mm.gguf"}},
                    "q2": {"class_type": "QwenH3PromptLocal", "inputs": {
                        "llm_model": "selected.gguf", "vision_model": "mmproj.gguf", "seed": 7}},
                    "h3": {"class_type": "SamplerCustomAdvanced", "inputs": {}},
                    "video": {"class_type": "VHS_VideoCombine", "inputs": {}},
                }}

    def queue(self, payload=None, validator=None):
        async def valid(prompt_id, prompt, outputs):
            self.assertEqual(outputs, ["director"])
            self.assertEqual(set(prompt), {"director"})
            return True, None, outputs, {}
        with patch.object(review.qwen_adapter, "discover_qwen_runtime", return_value=object()):
            payload = payload or self.queue_payload()
            if "preview_fingerprint" not in payload:
                payload = {**payload, "preview_fingerprint": review.preview_director(
                    self.root, self.pid, payload, self.server)["preview_fingerprint"]}
            return asyncio.run(review.queue_director(
                self.root, self.pid, payload, self.server,
                validate_prompt=validator or valid))

    def preview(self, payload=None):
        with patch.object(review.qwen_adapter, "discover_qwen_runtime", return_value=object()):
            return review.preview_director(
                self.root, self.pid, payload or self.queue_payload(), self.server)

    def test_preview_is_read_only_complete_and_can_start_from_rules_mode(self):
        before = {p.name: p.read_bytes() for p in (self.directory/"state").glob("*")}
        payload = self.queue_payload()
        payload["constraints"] = "New confirmed whole-film requirements."
        preview = self.preview(payload)
        self.assertEqual(preview["input_snapshot"]["source"]["constraints"], payload["constraints"])
        self.assertEqual(len(preview["input_snapshot"]["source"]["segments"]), len(self.plan["segments"]))
        self.assertEqual([r["text"] for r in preview["input_snapshot"]["source"]["segments"]],
                         [r["text"] for r in self.plan["segments"]])
        self.assertNotIn("transcript", preview["input_snapshot"]["source"])
        self.assertIn("长视频全片分镜导演", preview["system_prompt"])
        self.assertIn("全片模式：唱歌", preview["model_message"])
        self.assertIn("第 1 段 ·", preview["model_message"])
        for internal in ("project_id", "sample_rate", "start_sample", "generation_frames"):
            self.assertNotIn(internal, preview["model_message"])
        self.assertEqual(self.server.prompt_queue.puts, [])
        self.assertEqual(before, {p.name: p.read_bytes() for p in (self.directory/"state").glob("*")})
        result = self.queue({**payload, "preview_fingerprint": preview["preview_fingerprint"]})
        current = core.read_plan(self.root, self.pid)
        self.assertEqual(current["planning_method"], "qwen")
        self.assertEqual(current["director_constraints"], payload["constraints"])
        self.assertEqual(current["director_job"]["prompt_id"], result["prompt_id"])
        inputs = self.server.prompt_queue.puts[0][2]["director"]["inputs"]
        status = review.director_task.run_director_task(
            self.root, adapter_factory=FakeAdapter, **inputs)
        self.assertEqual(status["status"], "draft")
        self.assertEqual(status["input_fingerprint"], preview["input_snapshot"]["input_fingerprint"])

    def test_preview_ignores_source_skill_prompt_duration_and_thinking(self):
        payload = self.queue_payload()
        initial = self.preview(payload)
        for field, value in (("skill", "a-different-skill"), ("prompt", "irrelevant H3 text"),
                             ("duration", 60), ("think_mode", True)):
            payload["prompt"]["q2"]["inputs"][field] = value
            self.assertEqual(self.preview(payload), initial)

    def test_start_rejects_changed_revision_or_approval_without_any_writes(self):
        self.configure()
        self.approve(self.payload(self.draft()))
        before = {p.name: p.read_bytes() for p in (self.directory/"state").glob("*")}
        for payload in (
            {"revision": self.plan["revision"] - 1},
            {"revision": True},
            {"director_approval_id": "different"},
            {"director_approval_id": None},
        ):
            with self.subTest(payload=payload), patch.object(controller.asyncio, "create_task") as task:
                with self.assertRaisesRegex(ValueError, "已变化"):
                    controller.start(self.root, self.pid, payload, self.server)
                task.assert_not_called()
            self.assertEqual(before, {p.name: p.read_bytes() for p in (self.directory/"state").glob("*")})

    def test_approval_accepts_ui_timeline_without_embedded_sample_rate(self):
        self.configure()
        draft = self.draft()
        payload = self.payload(draft)
        source = review.source_snapshot(self.root, self.plan)["source"]["segments"]
        for submitted, source_row in zip(payload["segments"], source):
            submitted["timeline"] = review.ai_director.render_h3_timeline(
                submitted, dict(source_row), self.plan["mode"])
            submitted.pop("beats", None)
            submitted.pop("actions", None)
        approved = self.approve(payload)
        self.assertEqual(len(approved["approved_director"]["segments"]),
                         len(self.plan["segments"]))
        self.assertEqual(approved["approved_director"]["segments"][0]["beats"][-1]["end_seconds"], 10)

    def test_authored_timeline_is_saved_verbatim_without_framework_review(self):
        self.plan["mode"] = "speaking"
        self.save()
        self.configure()
        draft = self.draft()
        payload = self.payload(draft)
        source = review.source_snapshot(self.root, self.plan)["source"]["segments"]
        timeline = review.ai_director.render_h3_timeline(
            payload["segments"][0], dict(source[0]), self.plan["mode"])
        authored = "\n".join(
            line for line in timeline.splitlines() if not line.startswith("台词："))
        payload["segments"][0]["timeline"] = authored
        approved = self.approve(payload)
        segment = approved["approved_director"]["segments"][0]
        self.assertEqual(segment["timeline_override"], authored)
        self.assertEqual(review.ai_director.render_h3_timeline(
            segment, dict(source[0]), self.plan["mode"]), authored)
        self.assertIn(authored, review.effective_segment_brief(
            self.root, approved, approved["segments"][0]))

    def test_director_edits_save_while_video_is_running(self):
        self.configure()
        draft = self.draft()
        payload = self.payload(draft)
        self.plan["run_status"] = "running"
        self.plan["segments"][0]["job"] = {"status": "queued", "prompt_id": "active"}
        self.save()
        payload["revision"] = self.plan["revision"]
        payload["segments"][0]["brief"] = "运行中保存的人工镜头简报。"
        approved = self.approve(payload)
        row = approved["segments"][0]
        self.assertEqual(approved["run_status"], "running")
        self.assertEqual(row["job"]["prompt_id"], "active")
        self.assertTrue(row["needs_regeneration"])
        self.assertTrue(row["regeneration_after_current"])
        self.assertEqual(row["prompt"], "运行中保存的人工镜头简报。")

    def test_start_accepts_current_approval_and_legacy_optional_guards(self):
        self.configure()
        self.approve(self.payload(self.draft()))
        approved = copy.deepcopy(self.plan)
        for guards in ({}, {"revision": approved["revision"],
                           "director_approval_id": approved["director_approval"]["id"]}):
            core.write_plan(self.root, approved)
            payload = {**guards, "loader_id": "1", "video_id": "2", "prompt": {
                "1": {"class_type": "TY_H3LVUnified", "inputs": {}},
                "2": {"class_type": "VHS_VideoCombine", "inputs": {}},
            }}
            def capture(coroutine):
                coroutine.close()
                return object()
            try:
                with patch.object(controller, "bind_execution"), \
                     patch.object(controller.asyncio, "create_task", side_effect=capture) as task:
                    controller.start(self.root, self.pid, payload, self.server)
                    task.assert_called_once()
            finally:
                controller.TASKS.pop(self.pid, None)

    def test_manual_unlocked_prompt_edit_can_regenerate_after_cut_confirmation(self):
        self.configure()
        self.approve(self.payload(self.draft()))
        original = copy.deepcopy(self.plan)
        submitted = [
            {"end": row["end"], "prompt": row["prompt"]}
            for row in self.plan["segments"]
        ]
        submitted[0]["prompt"] = "手动修改后的镜头简报。"
        self.plan = core.edit_plan(self.plan, [
            *submitted,
        ], self.directory)
        self.plan["approved"] = True
        self.plan["approved_fingerprint"] = core.fingerprint(self.plan)
        self.assertFalse(review._approval_current(self.root, self.plan))
        self.assertTrue(review.adopt_manual_prompt_edits(self.root, self.plan))
        self.assertTrue(review._approval_current(self.root, self.plan))
        self.assertNotEqual(self.plan["segments"][0]["prompt"], original["segments"][0]["prompt"])

    def test_locked_prompt_edit_still_requires_director_review(self):
        self.configure()
        draft = self.draft()
        payload = self.payload(draft)
        payload["segments"][0]["prompt_locked"] = True
        self.approve(payload)
        submitted = [
            {"end": row["end"], "prompt": row["prompt"]}
            for row in self.plan["segments"]
        ]
        submitted[0]["prompt"] = "改动锁定提示词。"
        self.plan = core.edit_plan(self.plan, [
            *submitted,
        ], self.directory)
        self.plan["approved"] = True
        self.plan["approved_fingerprint"] = core.fingerprint(self.plan)
        self.assertFalse(review.adopt_manual_prompt_edits(self.root, self.plan))
        with self.assertRaisesRegex(ValueError, "旧方案缺失或已过期"):
            review.require_director_approval(self.root, self.plan)

    def test_five_complete_card_sentences_match_compact_model_input(self):
        template = self.plan["segments"][0]
        self.plan["segments"] = [
            {**copy.deepcopy(template), "index": i, "start_sample": i*600,
             "end_sample": (i+1)*600, "generation_frames": 147,
             "text": f"  第{i+1}段审核后的完整句子，保留错字与末尾内容。  "}
            for i in range(5)]
        self.save()
        small = self.preview()
        for name in ("transcript.json", "analysis.json"):
            core.state_file(self.directory, name).write_text(json.dumps({
                "words": [{"text": "原始单字", "start": i/10, "confidence": .9}
                          for i in range(20000)]}), encoding="utf-8")
        large = self.preview()
        self.assertEqual(small["model_input"], large["model_input"])
        self.assertNotEqual(small["preview_fingerprint"], large["preview_fingerprint"])
        with self.assertRaisesRegex(ValueError, "输入已经变化"):
            self.queue({**self.queue_payload(), "preview_fingerprint": small["preview_fingerprint"]})
        self.queue({**self.queue_payload(), "preview_fingerprint": large["preview_fingerprint"]})
        inputs = self.server.prompt_queue.puts[0][2]["director"]["inputs"]
        review.director_task.run_director_task(self.root, adapter_factory=FakeAdapter, **inputs)
        draft = review._draft(self.directory)
        actual = draft["attempts"][0]["messages"][1]["content"]
        self.assertEqual(actual, large["model_message"])
        self.assertIn("第 1 段 · 0.000–6.000 秒 · 6.000 秒", actual)
        self.assertIn("第 5 段 · 24.000–30.000 秒 · 6.000 秒", actual)
        self.assertIn("内容：第5段审核后的完整句子，保留错字与末尾内容。", actual)
        self.assertNotIn("start_sample", actual)
        self.assertNotIn("generation_frames", actual)

    def test_history_error_survives_history_eviction_without_content_changes(self):
        self.queue()
        before = core.read_plan(self.root, self.pid)
        pid = before["director_job"]["prompt_id"]
        self.server.prompt_queue.waiting.clear()
        self.server.prompt_queue.history[pid] = {"status": {
            "status_str": "error", "messages": [
                ("execution_start", {"timestamp": 100000}),
                ("execution_error", {"timestamp": 112000, "exception_type": "ValueError",
                                     "exception_message": "32635 input + 8192 output > 32768"})]}}
        state = review.director_state(self.root, self.pid, self.server)
        self.assertEqual(state["job"]["status"], "failed")
        self.assertEqual(state["job"]["elapsed_seconds"], 12)
        self.assertIn("32635", state["job"]["error"])
        self.server.prompt_queue.history.clear()
        self.assertEqual(review.director_state(self.root, self.pid, self.server)["job"], state["job"])
        after = core.read_plan(self.root, self.pid)
        self.assertEqual({k: v for k, v in before.items() if k != "director_job"},
                         {k: v for k, v in after.items() if k != "director_job"})

    def test_legacy_lost_job_is_interrupted_and_runtime_phase_is_not_queued(self):
        self.queue()
        item = self.server.prompt_queue.waiting.pop()
        self.server.prompt_queue.running.append(item)
        state = review.director_state(self.root, self.pid, self.server)
        self.assertEqual(state["job"]["phase"], "running")
        self.server.prompt_queue.running.clear()
        state = review.director_state(self.root, self.pid, self.server)
        self.assertEqual(state["job"]["status"], "interrupted")
        self.assertIn("不可恢复", state["job"]["error"])
        self.assertEqual(core.read_plan(self.root, self.pid)["director_job"]["status"], "interrupted")

    def test_confirmation_required_and_changed_model_constraints_or_words_are_rejected(self):
        original = core.state_file(self.directory, "segments.json").read_bytes()
        self.assertRaisesRegex(ValueError, "确认预览", self.queue,
                               {**self.queue_payload(), "preview_fingerprint": ""})
        for kind in ("model", "constraints", "seed", "budget", "words"):
            payload = self.queue_payload()
            payload["preview_fingerprint"] = self.preview(payload)["preview_fingerprint"]
            if kind == "model":
                payload["prompt"]["q2"]["inputs"]["llm_model"] = "changed.gguf"
            elif kind == "constraints":
                payload["constraints"] = "Different staging."
            elif kind == "seed":
                payload["prompt"]["q2"]["inputs"]["seed"] = 99
            elif kind == "budget":
                payload["max_tokens"] = 4096
            else:
                self.plan["segments"][0]["text"] = "Changed words after preview."
                self.save()
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "确认预览"):
                self.queue(payload)
        self.assertEqual(self.server.prompt_queue.puts, [])
        self.assertFalse(core.state_file(self.directory, "director_queue_snapshot.json").exists())
        # Only the deliberately simulated external word edit changed the source.
        self.assertNotEqual(core.state_file(self.directory, "segments.json").read_bytes(), original)

    def test_rules_default_is_usable_without_qwen_or_director_files(self):
        with patch.object(review.qwen_adapter, "discover_qwen_runtime", return_value=None):
            state = review.director_state(self.root, self.pid, self.server)
        self.assertEqual(state["planning_method"], "rules")
        self.assertFalse(state["qwen_available"])
        self.assertIsNone(state["draft"])
        review.require_director_approval(self.root, self.plan)
        self.assertEqual(review.effective_segment_brief(
            self.root, self.plan, self.plan["segments"][0]), self.plan["segments"][0]["prompt"])

    def test_state_does_not_hold_project_lock_during_qwen_discovery(self):
        class TrackingLock:
            def __init__(self):
                self.held = False

            def __enter__(self):
                self.assert_not_held = self.held
                self.held = True
                return self

            def __exit__(self, *_args):
                self.held = False

        lock = TrackingLock()
        observed = []

        def discover():
            self.assertFalse(lock.held)
            observed.append(True)
            return object()

        with patch.object(review, "LOCK", lock), \
             patch.object(review.qwen_adapter, "discover_qwen_runtime", side_effect=discover):
            state = review.director_state(self.root, self.pid, self.server)
        self.assertTrue(observed)
        self.assertTrue(state["qwen_available"])

    def test_cut_approval_and_director_approval_are_independent(self):
        self.configure()
        draft = self.draft()
        with self.assertRaisesRegex(ValueError, "导演方案"):
            review.require_director_approval(self.root, self.plan)
        self.plan["approved"] = False
        core.write_plan(self.root, self.plan)
        with self.assertRaisesRegex(ValueError, "确认当前切分"):
            self.approve(self.payload(draft))
        self.plan["approved"] = True
        self.save()
        self.approve(self.payload(draft))
        review.require_director_approval(self.root, self.plan)
        self.assertTrue(self.plan["approved"])

    def test_approval_updates_briefs_and_does_not_enqueue_or_drop_completed_videos(self):
        self.configure()
        self.plan["segments"][1]["job"] = {"status": "completed", "video": "existing.mp4", "prompt_id": "old"}
        self.plan["segments"][1]["takes"] = [{"video": "older.mp4"}]
        self.save()
        draft = self.draft()
        before_job = copy.deepcopy(self.plan["segments"][1]["job"])
        approved = self.approve(self.payload(draft))
        row = approved["segments"][1]
        self.assertEqual(row["job"], before_job)
        self.assertEqual(row["takes"], [{"video": "older.mp4"}])
        self.assertTrue(row["director_video_stale"])
        self.assertTrue(approved["final_stale"])
        self.assertTrue(row["needs_regeneration"])
        self.assertEqual(row["regeneration_reason"], "导演方案已修改")
        self.assertEqual(self.server.prompt_queue.puts, [])
        self.assertEqual(approved["approved_fingerprint"], core.fingerprint(approved))
        self.assertEqual(row["prompt"], draft["result"]["segments"][1]["brief"])

    def test_manual_lock_and_final_prompt_are_never_silently_overwritten(self):
        self.configure()
        self.plan["segments"][0].update(prompt_locked=True, prompt="Do not overwrite.")
        self.plan["segments"][1].update(prompt_kind="final", prompt="Exact final text.")
        self.save()
        draft = self.draft()
        payload = self.payload(draft)
        payload["segments"][2].update(brief="Reviewed hand movement.", prompt_locked=True)
        self.approve(payload)
        self.assertEqual(self.plan["segments"][0]["prompt"], "Do not overwrite.")
        self.assertEqual(self.plan["segments"][1]["prompt"], "Exact final text.")
        self.assertEqual(self.plan["segments"][2]["prompt"], "Reviewed hand movement.")
        self.assertTrue(self.plan["segments"][2]["prompt_locked"])
        review.require_director_approval(self.root, self.plan)
        self.assertFalse(review.director_state(self.root, self.pid, self.server)["stale"])
        self.assertEqual(review.effective_segment_brief(
            self.root, self.plan, self.plan["segments"][1]), "Exact final text.")

    def test_stale_source_and_forged_draft_cannot_apply(self):
        self.configure()
        draft = self.draft()
        original = core.state_file(self.directory, "segments.json").read_bytes()
        payload = self.payload(draft)
        payload["draft_id"] = "other"
        with self.assertRaisesRegex(ValueError, "过期"):
            self.approve(payload)
        self.assertEqual(core.state_file(self.directory, "segments.json").read_bytes(), original)
        self.plan["segments"][0]["text"] = "Edited confirmed words."
        self.save()
        with self.assertRaisesRegex(ValueError, "过期"):
            self.approve(self.payload(draft))

    def test_duplicate_missing_and_time_changed_rows_never_partially_apply(self):
        self.configure()
        draft = self.draft()
        for kind in ("duplicate", "missing", "time", "overtime"):
            payload = self.payload(draft)
            if kind == "duplicate":
                payload["segments"][1] = copy.deepcopy(payload["segments"][0])
            elif kind == "missing":
                payload["segments"].pop()
            elif kind == "time":
                payload["segments"][0]["end_sample"] += 1
            else:
                payload["segments"][0]["actions"][0]["end_seconds"] = 99
            original = core.state_file(self.directory, "segments.json").read_bytes()
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.approve(payload)
            self.assertEqual(core.state_file(self.directory, "segments.json").read_bytes(), original)

    def test_source_changes_or_tampered_approved_result_block_direct_generation(self):
        self.configure()
        self.approve(self.payload(self.draft()))
        original = copy.deepcopy(self.plan)
        for kind in ("text", "constraints", "result", "brief"):
            self.plan = copy.deepcopy(original)
            if kind == "text":
                self.plan["segments"][0]["text"] = "New words."
            elif kind == "constraints":
                self.plan["director_constraints"] = "Different constraints."
            elif kind == "result":
                self.plan["approved_director"]["global_contract"] = "Different contract."
            else:
                self.plan["segments"][0]["prompt"] = "Different brief."
            self.save()
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "过期"):
                review.require_director_approval(self.root, self.plan)

    def test_effective_brief_has_exact_generation_duration_and_padding_contract(self):
        self.configure()
        self.approve(self.payload(self.draft()))
        row = self.plan["segments"][0]
        value = review.effective_segment_brief(self.root, self.plan, row)
        self.assertIn("时间只从本段起点 00:00.000 计算", value)
        self.assertIn("本段有效内容时长：10.000 秒", value)
        self.assertIn("[Shot 1] 连续镜头", value)
        self.assertIn("At 00:00.000–00:10.000", value)
        self.assertIn("入段状态：", value)
        self.assertIn("结束状态：", value)
        self.assertIn("不得新增台词、对白、字幕、动作或镜头变化", value)
        self.assertIn("图1中的唯一表演主体、图2背景/PPT", value)
        self.assertNotIn("结合数字人图像", value)
        self.assertNotIn("0.000–10.000 秒", value)
        self.assertNotIn("start_sample", value)
        self.assertNotIn("generation_frames", value)
        self.assertNotIn(row["text"], value)
        self.assertNotIn("素材说明：\n", value)

    def test_effective_brief_uses_full_project_duration_for_motion_budget(self):
        self.plan.update(mode="speaking", sample_rate=100, samples=9000, duration=90.0)
        for index, row in enumerate(self.plan["segments"]):
            row.update(start_sample=index * 3000, end_sample=(index + 1) * 3000)
        self.save()
        self.configure()
        self.approve(self.payload(self.draft()))
        value = review.effective_segment_brief(
            self.root, self.plan, self.plan["segments"][0])
        self.assertIn("整片最多 4 个", value)

    def test_external_qwen_fields_filter_only_optional_segment_content(self):
        self.configure()
        self.approve(self.payload(self.draft()))
        row = self.plan["segments"][0]
        default = review.effective_segment_brief(self.root, self.plan, row)
        for heading in ("批准镜头简报", "入段状态：", "结束状态：", "本段 H3 时间线"):
            self.assertIn(heading, default)
        self.assertNotIn("本段意图：", default)
        self.assertNotIn("下一段衔接：", default)
        self.assertNotIn("台词：", default)
        self.assertNotIn("<d>", default)

        row["external_qwen_fields"] = ["timeline"]
        timeline_only = review.effective_segment_brief(self.root, self.plan, row)
        self.assertIn("本段 H3 时间线", timeline_only)
        for heading in ("本段意图：", "批准镜头简报", "入段状态：", "结束状态：",
                        "下一段衔接："):
            self.assertNotIn(heading, timeline_only)

        row["external_qwen_fields"] = []
        fixed_only = review.effective_segment_brief(self.root, self.plan, row)
        self.assertNotIn("本段 H3 时间线", fixed_only)
        self.assertIn("全片视觉契约：", fixed_only)
        self.assertIn("本段有效内容时长：", fixed_only)
        self.assertIn("不得新增台词、对白、字幕、动作或镜头变化", fixed_only)

    def test_external_qwen_fields_save_immediately_and_stale_completed_take(self):
        self.configure()
        draft = self.draft()
        self.approve(self.payload(draft))
        row = self.plan["segments"][0]
        row["job"] = {"status": "completed", "video": "old.mp4", "prompt_id": "old"}
        self.plan["final_video"] = "final.mp4"
        self.save()
        revision = self.plan["revision"]
        self.plan = review.save_external_qwen_fields(self.root, self.pid, {
            "revision": revision, "segment_index": 0, "fields": ["timeline"],
        }, self.server)
        row = self.plan["segments"][0]
        self.assertEqual(row["external_qwen_fields"], ["timeline"])
        self.assertEqual(self.plan["revision"], revision + 1)
        self.assertTrue(row["needs_regeneration"])
        self.assertEqual(row["regeneration_reason"], "外部 Qwen 输出项已修改")
        self.assertTrue(row["director_video_stale"])
        self.assertTrue(self.plan["final_stale"])
        review.require_director_approval(self.root, self.plan)

        self.plan = review.save_external_qwen_fields(self.root, self.pid, {
            "revision": self.plan["revision"], "segment_index": 0, "fields": [],
        }, self.server)
        self.assertEqual(self.plan["segments"][0]["external_qwen_fields"], [])
        for fields, message in ((["timeline", "timeline"], "不能重复"),
                                (["unknown"], "未知")):
            with self.subTest(fields=fields), self.assertRaisesRegex(ValueError, message):
                review.save_external_qwen_fields(self.root, self.pid, {
                    "revision": self.plan["revision"], "segment_index": 0, "fields": fields,
                }, self.server)

    def test_only_explicit_qwen_is_used_and_video_snapshot_is_not_changed(self):
        self.configure()
        video_snapshot = core.state_file(self.directory, "queue_snapshot.json")
        video_snapshot.write_text('{"do_not_replace":true}', encoding="utf-8")
        result = self.queue()
        item = self.server.prompt_queue.puts[0]
        inputs = item[2]["director"]["inputs"]
        self.assertEqual(inputs["model"], "selected.gguf")
        self.assertEqual(inputs["qwen_source_id"], "q2")
        self.assertEqual(inputs["seed"], 7)
        self.assertEqual(item[4], ["director"])
        self.assertEqual(item[3]["client_id"], "client-a")
        self.assertNotIn("h3", item[2])
        self.assertEqual(video_snapshot.read_text(), '{"do_not_replace":true}')
        saved = json.loads(core.state_file(self.directory, "director_queue_snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["prompt_id"], result["prompt_id"])
        self.assertEqual(saved["qwen_source_id"], "q2")
        self.assertEqual(core.read_plan(self.root, self.pid)["director_qwen_source_id"], "q2")
        self.assertEqual(core.read_plan(self.root, self.pid)["director_qwen_config"], {
            "llm_model": "selected.gguf", "vision_model": "mmproj.gguf", "seed": 7})

    def test_changed_qwen_config_invalidates_old_approval_even_if_replan_fails(self):
        self.configure()
        self.approve(self.payload(self.draft()))
        result = self.queue()
        self.plan = core.read_plan(self.root, self.pid)
        self.server.prompt_queue.waiting.clear()
        self.server.prompt_queue.history[result["prompt_id"]] = {"status": {"status_str": "error"}}
        with patch.object(review, "_server", return_value=self.server):
            with self.assertRaisesRegex(ValueError, "过期"):
                review.require_director_approval(self.root, self.plan)

    def test_draft_from_other_model_cannot_be_approved(self):
        self.configure()
        draft = self.draft()
        self.plan["director_qwen_config"] = {
            "llm_model": "new.gguf", "vision_model": "mmproj.gguf", "seed": 7}
        self.save()
        with self.assertRaisesRegex(ValueError, "模型配置"):
            self.approve(self.payload(draft))

    def test_new_successful_draft_requires_new_approval_but_is_reviewable(self):
        self.configure()
        first = self.draft()
        self.approve(self.payload(first))
        second = self.draft()
        self.assertNotEqual(second["id"], first["id"])
        with self.assertRaisesRegex(ValueError, "过期"):
            review.require_director_approval(self.root, self.plan)
        self.assertFalse(review.director_state(self.root, self.pid, self.server)["stale"])
        self.approve(self.payload(second))
        review.require_director_approval(self.root, self.plan)

    def test_locked_adopted_draft_can_be_reviewed_again_without_artificial_staleness(self):
        self.configure()
        draft = self.draft()
        payload = self.payload(draft)
        payload["segments"][0]["prompt_locked"] = True
        self.approve(payload)
        payload = self.payload(draft)
        payload["segments"][1]["brief"] = "A manual follow-up change."
        self.approve(payload)
        self.assertEqual(self.plan["segments"][1]["prompt"], "A manual follow-up change.")
        review.require_director_approval(self.root, self.plan)

    def test_duplicate_or_busy_planning_is_rejected(self):
        self.configure()
        self.queue()
        with self.assertRaisesRegex(ValueError, "排队"):
            self.queue()
        plan = core.read_plan(self.root, self.pid)
        with self.assertRaisesRegex(ValueError, "排队"):
            review.configure_director(self.root, self.pid, {
                "revision": plan["revision"], "planning_method": "rules", "constraints": "Keep."},
                self.server)

    def test_completed_and_failed_tasks_do_not_remain_pending(self):
        self.configure()
        old_draft = self.draft()
        result = self.queue()
        self.server.prompt_queue.waiting.clear()
        pid = result["prompt_id"]
        for status, expected in (("success", "completed"), ("error", "failed")):
            self.server.prompt_queue.history[pid] = {"status": {"status_str": status}}
            state = review.director_state(self.root, self.pid, self.server)
            self.assertEqual(state["job"]["status"], expected)
            self.assertEqual(state["draft"]["id"], old_draft["id"])
        self.plan = core.read_plan(self.root, self.pid)
        review._assert_idle(self.plan, self.server)

    def test_missing_qwen_invalid_selection_and_failed_validation_leave_plan_unchanged(self):
        self.configure()
        original = core.state_file(self.directory, "segments.json").read_bytes()
        missing_payload = self.queue_payload()
        missing_payload.pop("qwen_source_id")
        missing_payload.pop("prompt")
        with patch.object(review.qwen_adapter, "discover_qwen_runtime", return_value=None):
            with self.assertRaisesRegex(ValueError, "不可用"):
                asyncio.run(review.queue_director(
                    self.root, self.pid, missing_payload, self.server,
                    validate_prompt=lambda *args: None))
        payload = self.queue_payload()
        payload["qwen_source_id"] = "h3"
        with self.assertRaisesRegex(ValueError, "无效"):
            self.queue(payload)

        async def invalid(*args):
            return False, {"error": "bad graph"}, [], {}

        with self.assertRaisesRegex(ValueError, "校验失败"):
            self.queue(validator=invalid)
        self.assertEqual(core.state_file(self.directory, "segments.json").read_bytes(), original)
        self.assertEqual(self.server.prompt_queue.puts, [])

    def test_direct_qwen_defaults_work_without_canvas_source_id(self):
        self.configure()
        payload = self.queue_payload()
        payload.pop("qwen_source_id")
        payload.pop("prompt")
        with patch.object(review.qwen_adapter, "default_model_config", return_value={
            "llm_model": "default.gguf", "vision_model": "mmproj.gguf",
            "seed": 0, "context_size": 32768, "reasoning_effort": "medium",
            "think_mode": False,
        }):
            preview = review.preview_director(self.root, self.pid, payload, self.server)
            self.assertEqual(preview["qwen_source_id"], "")
            self.assertEqual(preview["model_config"]["llm_model"], "default.gguf")
            payload["preview_fingerprint"] = preview["preview_fingerprint"]
            result = self.queue(payload)
        inputs = self.server.prompt_queue.puts[0][2]["director"]["inputs"]
        self.assertEqual(result["prompt_id"], inputs["director_prompt_id"])
        self.assertNotIn("qwen_source_id", inputs)
        self.assertEqual(inputs["model"], "default.gguf")

    def test_saved_longvideo_base_config_is_used_before_default_model_discovery(self):
        self.configure()
        self.plan["director_base_qwen_config"] = {
            "llm_model": "saved.gguf", "vision_model": "saved-mm.gguf",
            "seed": 19, "max_tokens": 4096, "think_mode": False,
            "reasoning_effort": "medium",
        }
        self.save()
        payload = self.queue_payload()
        payload.pop("qwen_source_id")
        payload.pop("prompt")
        with patch.object(
            review.qwen_adapter, "default_model_config",
            side_effect=AssertionError("default discovery should not run"),
        ):
            preview = review.preview_director(self.root, self.pid, payload, self.server)
        self.assertEqual(preview["model_config"]["llm_model"], "saved.gguf")
        self.assertEqual(preview["model_config"]["vision_model"], "saved-mm.gguf")
        self.assertEqual(preview["model_config"]["seed"], 19)

    def test_request_id_makes_duplicate_submit_idempotent_and_rejects_changed_payload(self):
        self.configure()
        payload = self.queue_payload()
        payload["request_id"] = "director-request-123"
        preview = self.preview(payload)
        payload["preview_fingerprint"] = preview["preview_fingerprint"]

        first = self.queue(payload)
        second = self.queue(copy.deepcopy(payload))
        self.assertEqual(second, first)
        self.assertEqual(len(self.server.prompt_queue.puts), 1)

        changed = copy.deepcopy(payload)
        changed["constraints"] = "Changed after the request was accepted."
        with self.assertRaisesRegex(ValueError, "内容已变化"):
            self.queue(changed)
        self.assertEqual(len(self.server.prompt_queue.puts), 1)
        receipt = json.loads(core.state_file(
            self.directory, "director_request_director-request-123.json"
        ).read_text(encoding="utf-8"))
        self.assertEqual(receipt["response"], first)

    def test_completed_rerun_changes_only_internal_seed_and_requires_a_new_seed(self):
        self.configure()
        first = self.queue()
        current = core.read_plan(self.root, self.pid)
        current["director_job"].update(
            status="completed", phase="completed",
            finished_at="2026-09-22T15:00:00+00:00")
        core.write_plan(self.root, current)
        self.server.prompt_queue.waiting.clear()

        rerun = self.queue_payload()
        rerun.update(rerun_of=first["prompt_id"], run_seed=123)
        preview = self.preview(rerun)
        self.assertEqual(preview["base_model_config"]["seed"], 7)
        self.assertEqual(preview["model_config"]["seed"], 123)
        rerun["preview_fingerprint"] = preview["preview_fingerprint"]
        second = self.queue(rerun)

        self.assertNotEqual(second["prompt_id"], first["prompt_id"])
        self.assertEqual(self.server.prompt_queue.puts[1][2]["director"]["inputs"]["seed"], 123)
        current = core.read_plan(self.root, self.pid)
        self.assertEqual(current["director_base_qwen_config"]["seed"], 7)
        self.assertEqual(current["director_qwen_config"]["seed"], 123)

        current["director_job"].update(
            status="completed", phase="completed",
            finished_at="2026-09-22T15:01:00+00:00")
        core.write_plan(self.root, current)
        self.server.prompt_queue.waiting.clear()
        same_seed = self.queue_payload()
        same_seed.update(rerun_of=second["prompt_id"], run_seed=123)
        with self.assertRaisesRegex(ValueError, "新的内部分镜随机种子"):
            self.preview(same_seed)

    def test_async_source_conflict_is_not_enqueued(self):
        self.configure()

        async def edit_while_validating(*args):
            plan = core.read_plan(self.root, self.pid)
            plan["segments"][0]["text"] = "New confirmed words during validation."
            core.write_plan(self.root, plan)
            return True, None, ["director"], {}

        with self.assertRaisesRegex(ValueError, "提交期间"):
            self.queue(validator=edit_while_validating)
        self.assertEqual(self.server.prompt_queue.puts, [])


if __name__ == "__main__":
    unittest.main()
