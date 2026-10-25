"""Text-only task integration using temporary projects and no model runtime."""

import copy
import importlib
import json
from pathlib import Path
import re
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from test_plugin import core, nodes, sample_plan


task = importlib.import_module("h3lv_test.director_task")


def response_for(messages):
    rows = []
    pattern = re.compile(
        r"^第\s+(\d+)\s+段\s+·\s+([0-9.]+)–([0-9.]+)\s+秒\s+·\s+([0-9.]+)\s+秒$",
        re.MULTILINE)
    for match in pattern.finditer(messages[1]["content"]):
        rows.append({
            "segment_index": int(match.group(1)) - 1,
            "time_range": f"{float(match.group(2)):.3f}-{float(match.group(3)):.3f}s",
            "duration": float(match.group(4)),
        })
    return json.dumps({
        "schema_version": 1, "global_contract": "Centered presenter in the same studio.",
        "segments": [{
            "segment_index": row["segment_index"], "time_range": row["time_range"],
            "intent": "Explain this point",
            "brief": "One restrained gesture matching the words.",
            "entry_state": "Hands resting", "exit_state": "Hands resting",
            "next_transition": "Maintain the pose",
            "actions": [{"start_seconds": 0,
                         "end_seconds": row["duration"],
                         "description": "Speak naturally without moving the camera."}],
        } for row in rows],
    })


class FakeAdapter:
    def __init__(self, config, hook=None, response=None):
        self.config, self.hook, self.response = config, hook, response
        self.calls = []
        self.model, self.mmproj = config["llm_model"], config["vision_model"]
        self.context_size = 32768
        self.seed, self.reasoning_effort = config["seed"], "low"
        self.last_usage, self.last_metadata = {}, {}

    def complete(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs))
        self.last_usage = {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}
        self.last_metadata = {"input_tokens": 100, "context_size": 32768,
                              "output_budget": kwargs["max_tokens"], "elapsed_seconds": .25}
        if self.hook:
            self.hook()
        if self.response is not None:
            return self.response
        if kwargs.get("response_schema"):
            from test_ai_director import fill_response
            return json.dumps(fill_response(kwargs["response_schema"]))
        return response_for(kwargs["messages"])


class DirectorTaskTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.plan = sample_plan()
        self.plan.update(approved=True, approved_fingerprint=core.fingerprint(self.plan))
        core.write_plan(self.root, self.plan)
        self.directory = core.project_path(self.root, self.plan["id"])
        self.write_json("transcript.json", {"segments": [
            {"start": 0, "end": 30, "text": "Whole transcript remains available."}]})
        self.write_json("analysis.json", {"rhythm": {"tempo_bpm": 100},
                                          "phrases": [{"start": 0, "end": 30}]})
        self.draft_path = core.state_file(self.directory, "director_draft.json")
        self.factory_calls, self.adapters = [], []

    def write_json(self, name, value):
        core.state_file(self.directory, name).write_text(
            json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def factory(self, config, **kwargs):
        self.factory_calls.append(copy.deepcopy(config))
        result = FakeAdapter(config, **kwargs)
        self.adapters.append(result)
        return result

    def run_task(self, **kwargs):
        return task.run_director_task(
            self.root, self.plan["id"], "selected.gguf", "selected-mmproj.gguf",
            adapter_factory=kwargs.pop("adapter_factory", self.factory), **kwargs)

    def old_draft(self):
        old = b'{"id":"previous","status":"draft","preserved":true}'
        self.draft_path.write_bytes(old)
        return old

    def test_queue_wait_source_change_stops_before_model_acquire(self):
        snapshot = task._snapshot(self.root, self.plan["id"])
        old = self.old_draft()
        self.write_json("transcript.json", {"segments": [{"text": "Changed while queued."}]})
        with self.assertRaisesRegex(ValueError, "排队期间"):
            self.run_task(expected_input_fingerprint=snapshot["input_fingerprint"])
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(self.draft_path.read_bytes(), old)

    def test_queued_card_cut_and_material_edits_stop_before_model_acquire(self):
        for kind in ("text", "cut", "material"):
            with self.subTest(kind=kind):
                core.write_plan(self.root, self.plan)
                snapshot = task._snapshot(self.root, self.plan["id"])
                changed = copy.deepcopy(self.plan)
                if kind == "cut":
                    changed["segments"][0]["end_sample"] -= 1
                    changed["segments"][1]["start_sample"] -= 1
                else:
                    changed["segments"][0]["text" if kind == "text" else "material_note"] = "Edited"
                changed["approved_fingerprint"] = core.fingerprint(changed)
                core.write_plan(self.root, changed)
                with self.assertRaisesRegex(ValueError, "排队期间"):
                    self.run_task(expected_input_fingerprint=snapshot["input_fingerprint"])
                self.assertEqual(self.factory_calls, [])

    def test_context_budget_details_are_saved_for_ui_without_replacing_draft(self):
        old = self.old_draft()
        self.plan["director_job"] = {"prompt_id": "budget-id", "status": "queued"}
        core.write_plan(self.root, self.plan)
        error = task.qwen_adapter.QwenAdapterConfigurationError("context budget exceeded")
        error.context_budget = {"input_tokens": 32635, "output_budget": 8192,
                                "reserve_tokens": 8, "context_size": 32768}
        def fail(config):
            raise error
        with self.assertRaises(type(error)):
            self.run_task(director_prompt_id="budget-id", adapter_factory=fail)
        job = core.read_plan(self.root, self.plan["id"])["director_job"]
        self.assertEqual(job["context_budget"], error.context_budget)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.draft_path.read_bytes(), old)

    def test_confirmed_task_uses_director_messages_and_emits_real_phases(self):
        self.plan["director_job"] = {"prompt_id": "text-id", "status": "queued"}
        core.write_plan(self.root, self.plan)
        snapshot = task._snapshot(self.root, self.plan["id"])
        events = []
        server = types.SimpleNamespace(send_sync=lambda name, data: events.append((name, data)))
        with patch.dict(sys.modules, {"server": types.SimpleNamespace(
                PromptServer=types.SimpleNamespace(instance=server))}):
            self.run_task(expected_input_fingerprint=snapshot["input_fingerprint"],
                          director_prompt_id="text-id")
        self.assertEqual([event[1]["phase"] for event in events],
                         ["preparing", "validating", "completed"])
        current = core.read_plan(self.root, self.plan["id"])
        self.assertEqual(current["director_job"]["status"], "completed")
        self.assertIn("finished_at", current["director_job"])
        self.assertEqual(current["revision"], self.plan["revision"])
        self.assertEqual(current["segments"], self.plan["segments"])
        messages = self.adapters[0].calls[0]["messages"]
        self.assertIn("你是长视频全片分镜导演", messages[0]["content"])
        self.assertIn("简体中文", messages[0]["content"])
        self.assertNotIn("whole-film storyboard", messages[0]["content"])
        self.assertEqual(messages[1]["content"], snapshot["model_message"])
        self.assertIn("第 1 段 · 0.000–10.000 秒 · 10.000 秒", messages[1]["content"])
        self.assertNotIn("Existing brief", messages[1]["content"])
        for internal in ("project_id", "sample_rate", "start_sample", "generation_frames"):
            self.assertNotIn(internal, messages[1]["content"])

    def test_saves_draft_atomically_and_never_changes_project_or_approval(self):
        source_bytes = {p.name: p.read_bytes() for p in (self.directory/"state").glob("*.json")}
        status = self.run_task(seed=321, max_tokens=4096)
        draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "draft")
        self.assertFalse(status["approved"])
        self.assertEqual(status["draft_id"], draft["id"])
        self.assertEqual(status["segment_count"], len(self.plan["segments"]))
        self.assertEqual(self.factory_calls[0], {
            "llm_model": "selected.gguf", "vision_model": "selected-mmproj.gguf", "seed": 321})
        self.assertEqual(self.adapters[0].calls[0]["max_tokens"], 4096)
        self.assertFalse(self.adapters[0].calls[0]["enable_thinking"])
        self.assertEqual(len(draft["input_snapshot"]["source"]["segments"]), 3)
        self.assertNotIn("transcript", draft["input_snapshot"]["source"])
        self.assertNotIn("analysis", draft["input_snapshot"]["source"])
        self.assertEqual(set(draft["input_snapshot"]["local_source_hashes"]),
                         {"transcript", "analysis"})
        runtime = draft["runtime_metadata"]
        self.assertEqual(runtime["model"], "selected.gguf")
        self.assertEqual(runtime["context_size"], 32768)
        self.assertEqual(runtime["calls"][0]["usage"]["total_tokens"], 300)
        self.assertEqual(runtime["calls"][0]["metadata"]["input_tokens"], 100)
        self.assertGreaterEqual(runtime["calls"][0]["elapsed_seconds"], 0)
        self.assertGreaterEqual(runtime["elapsed_seconds"], 0)
        self.assertEqual(draft["reference_hash_scope"], "project_refs_only")
        for name, data in source_bytes.items():
            self.assertEqual(core.state_file(self.directory, name).read_bytes(), data)
        self.assertEqual(len(list((self.directory/"state").glob("director_draft_*.json"))), 1)
        self.assertEqual(list((self.directory/"state").glob("*.tmp")), [])

    def test_raw_asr_remains_local_and_confirmed_sentences_are_not_truncated(self):
        phrases = [{"start": index, "end": index+1, "text": f"phrase-{index}"}
                   for index in range(100)]
        words = [{"start": index, "end": index+.25, "text": f"word-{index}"}
                 for index in range(200)]
        transcript = {"segments": phrases, "words": words}
        self.write_json("transcript.json", transcript)
        self.write_json("analysis.json", {
            "schema": 2, "duration": 30, "waveform": {"vocals": [0.1]*1000},
            "candidates": [{"time": index} for index in range(600)],
            "phrases": phrases, "protected_words": words,
            "sections": [{"start": 0, "end": 30, "kind": "speech"}],
            "rhythm": {"tempo_bpm": 120, "confidence": .8,
                       "beats": list(range(60)), "bars": list(range(15))},
            "limitations": ["Acoustic estimates require review."],
        })
        self.run_task()
        draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
        source = draft["input_snapshot"]["source"]
        self.assertNotIn("transcript", source)
        self.assertNotIn("analysis", source)
        self.assertEqual(task._read_state(self.directory, "transcript.json"), transcript)
        self.assertEqual(task._read_state(self.directory, "analysis.json")["protected_words"], words)
        expected = [" ".join(f"word-{index}" for index in range(start, end))
                    for start, end in ((0, 10), (10, 20), (20, 30))]
        self.assertEqual([row["text"] for row in source["segments"]],
                         expected)
        self.assertTrue(all(row["speech_cues"] for row in source["segments"]))

    def test_failures_and_interruptions_persist_without_changing_content(self):
        old = self.old_draft()
        for error, status in ((TimeoutError("specific timeout"), "failed"),
                              (KeyboardInterrupt("user cancelled"), "interrupted")):
            self.plan["director_job"] = {"prompt_id": "task-id", "status": "queued"}
            core.write_plan(self.root, self.plan)
            def fail():
                raise error
            with self.assertRaises(type(error)):
                self.run_task(director_prompt_id="task-id",
                              adapter_factory=lambda config: self.factory(config, hook=fail))
            current = core.read_plan(self.root, self.plan["id"])
            self.assertEqual(current["director_job"]["status"], status)
            self.assertEqual(current["director_job"]["error"], str(error))
            self.assertIn("finished_at", current["director_job"])
            self.assertEqual(current["revision"], self.plan["revision"])
            self.assertEqual(current["segments"], self.plan["segments"])
            self.assertEqual(self.draft_path.read_bytes(), old)

    def test_preflight_failure_is_persisted_and_old_job_cannot_replace_new_job(self):
        self.plan["director_job"] = {"prompt_id": "old-id", "status": "queued"}
        core.write_plan(self.root, self.plan)
        with self.assertRaisesRegex(ValueError, "排队期间"):
            self.run_task(director_prompt_id="old-id", expected_input_fingerprint="stale")
        self.assertEqual(self.factory_calls, [])
        self.assertEqual(core.read_plan(self.root, self.plan["id"])["director_job"]["status"], "failed")
        self.plan["director_job"] = {"prompt_id": "new-id", "status": "queued"}
        core.write_plan(self.root, self.plan)
        task._update_job(self.root, self.plan["id"], "old-id", "failed", "failed", error="late")
        self.assertEqual(core.read_plan(self.root, self.plan["id"])["director_job"],
                         self.plan["director_job"])

    def test_replaced_task_cannot_save_a_late_draft(self):
        self.plan["director_job"] = {"prompt_id": "old-id", "status": "queued"}
        core.write_plan(self.root, self.plan)
        old = self.old_draft()
        def replace_job():
            current = core.read_plan(self.root, self.plan["id"])
            current["director_job"] = {"prompt_id": "new-id", "status": "queued"}
            core.write_plan(self.root, current)
        with self.assertRaisesRegex(ValueError, "未覆盖原草稿"):
            self.run_task(director_prompt_id="old-id",
                          adapter_factory=lambda config: self.factory(config, hook=replace_job))
        self.assertEqual(self.draft_path.read_bytes(), old)
        self.assertEqual(core.read_plan(self.root, self.plan["id"])["director_job"],
                         {"prompt_id": "new-id", "status": "queued"})

    def test_unapproved_or_outdated_confirmation_fails_before_qwen(self):
        for approved, signature in ((False, core.fingerprint(self.plan)), (True, "outdated")):
            self.plan.update(approved=approved, approved_fingerprint=signature)
            core.write_plan(self.root, self.plan)
            with self.subTest(approved=approved), self.assertRaisesRegex(ValueError, "确认"):
                self.run_task()
        self.assertEqual(self.factory_calls, [])
        self.assertFalse(self.draft_path.exists())

    def test_locked_briefs_and_explicit_final_prompts_are_retained_as_source_only(self):
        self.plan["segments"][0].update(prompt_locked=True, prompt="Authored locked brief.")
        self.plan["segments"][1].update(prompt_kind="final",
                                        prompt="Exact authored final prompt, no rewrite.")
        self.plan["approved_fingerprint"] = core.fingerprint(self.plan)
        core.write_plan(self.root, self.plan)
        original = core.state_file(self.directory, "segments.json").read_bytes()
        self.run_task()
        draft = json.loads(self.draft_path.read_text(encoding="utf-8"))
        rows = draft["input_snapshot"]["source"]["segments"]
        self.assertTrue(rows[0]["locked"])
        self.assertEqual(rows[0]["existing_brief"], "Authored locked brief.")
        self.assertEqual(rows[1]["prompt_kind"], "final")
        self.assertEqual(rows[1]["existing_brief"], "Exact authored final prompt, no rewrite.")
        self.assertEqual(core.state_file(self.directory, "segments.json").read_bytes(), original)

    def test_busy_project_and_missing_source_data_fail_before_qwen(self):
        self.plan["run_status"] = "running"
        core.write_plan(self.root, self.plan)
        with self.assertRaisesRegex(ValueError, "仍在生成"):
            self.run_task()
        self.plan["run_status"] = "draft"
        core.write_plan(self.root, self.plan)
        core.state_file(self.directory, "analysis.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.run_task()
        self.assertEqual(self.factory_calls, [])

    def test_unavailable_qwen_does_not_overwrite_old_draft(self):
        old = self.old_draft()
        with patch.object(task.qwen_adapter.QwenAdapter, "from_registered",
                          side_effect=task.qwen_adapter.QwenAdapterUnavailable("not registered")):
            with self.assertRaisesRegex(task.qwen_adapter.QwenAdapterUnavailable, "not registered"):
                task.run_director_task(self.root, self.plan["id"], "a.gguf", "mmproj.gguf")
        self.assertEqual(self.draft_path.read_bytes(), old)

    def test_invalid_json_never_retries_or_overwrites_old_draft(self):
        old = self.old_draft()
        with self.assertRaisesRegex(ValueError, "no whole-film retry"):
            self.run_task(adapter_factory=lambda config: self.factory(config, response="not JSON"))
        self.assertEqual(len(self.adapters[0].calls), 1)
        self.assertEqual(self.draft_path.read_bytes(), old)
        failures = list((self.directory/"state").glob("director_failure_*.json"))
        self.assertEqual(len(failures), 1)
        failure = json.loads(failures[0].read_text(encoding="utf-8"))
        self.assertEqual(failure["error"]["type"], "DirectorValidationError")
        self.assertEqual(len(failure["attempts"]), 1)
        self.assertTrue(all(item["response"] == "not JSON" for item in failure["attempts"]))
        self.assertTrue(all("validation_error" in item for item in failure["attempts"]))
        self.assertEqual(len(failure["runtime_metadata"]["calls"]), 1)

    def test_inference_error_preserves_old_draft(self):
        old = self.old_draft()
        def fail():
            raise TimeoutError("Qwen timeout")
        with self.assertRaisesRegex(TimeoutError, "timeout"):
            self.run_task(adapter_factory=lambda config: self.factory(config, hook=fail))
        self.assertEqual(self.draft_path.read_bytes(), old)
        failure = json.loads(next((self.directory/"state").glob(
            "director_failure_*.json")).read_text(encoding="utf-8"))
        self.assertEqual(failure["error"], {"type": "TimeoutError", "message": "Qwen timeout"})
        self.assertEqual(len(failure["attempts"]), 1)
        self.assertIn("messages", failure["attempts"][0])
        self.assertEqual(len(failure["input_snapshot"]["source"]["segments"]), 3)

    def test_cancellation_is_recorded_once_and_reraised_without_replacing_draft(self):
        old = self.old_draft()
        def cancel():
            raise KeyboardInterrupt("user cancelled")
        with self.assertRaisesRegex(KeyboardInterrupt, "user cancelled"):
            self.run_task(adapter_factory=lambda config: self.factory(config, hook=cancel))
        self.assertEqual(self.draft_path.read_bytes(), old)
        failures = list((self.directory/"state").glob("director_failure_*.json"))
        self.assertEqual(len(failures), 1)
        failure = json.loads(failures[0].read_text(encoding="utf-8"))
        self.assertEqual(failure["error"]["type"], "KeyboardInterrupt")
        self.assertEqual(len(failure["attempts"]), 1)

    def test_failed_diagnostic_write_does_not_hide_cancellation(self):
        old = self.old_draft()
        def cancel():
            raise KeyboardInterrupt("original cancellation")
        with patch.object(task, "_atomic_json", side_effect=OSError("disk full")), \
             patch.object(task.LOGGER, "exception") as report:
            with self.assertRaisesRegex(KeyboardInterrupt, "original cancellation") as raised:
                self.run_task(adapter_factory=lambda config: self.factory(config, hook=cancel))
        report.assert_called_once()
        self.assertIn("disk full", raised.exception.__notes__[0])
        self.assertEqual(self.draft_path.read_bytes(), old)

    def test_async_source_change_retains_stale_result_without_replacing_draft(self):
        old = self.old_draft()
        def edit():
            plan = core.read_plan(self.root, self.plan["id"])
            plan["segments"][0]["text"] = "Changed confirmed words"
            core.write_plan(self.root, plan)
        with self.assertRaisesRegex(ValueError, "未覆盖原草稿"):
            self.run_task(adapter_factory=lambda config: self.factory(config, hook=edit))
        self.assertEqual(self.draft_path.read_bytes(), old)
        stale_files = list((self.directory/"state").glob("director_stale_*.json"))
        self.assertEqual(len(stale_files), 1)
        stale = json.loads(stale_files[0].read_text(encoding="utf-8"))
        self.assertEqual(stale["status"], "stale")
        self.assertFalse(stale["approved"])

    def test_transcript_and_reference_content_changes_are_conflicts(self):
        name = "reference.png"
        (self.directory/"refs").mkdir()
        image = self.directory/"refs"/name
        image.write_bytes(b"original image")
        self.plan["segments"][0]["refs"] = [name]
        self.plan["approved_fingerprint"] = core.fingerprint(self.plan)
        core.write_plan(self.root, self.plan)
        for mutation in (
            lambda: self.write_json("transcript.json", {"segments": [{"text": "new transcript"}]}),
            lambda: image.write_bytes(b"replaced image"),
        ):
            old = self.old_draft()
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "未覆盖原草稿"):
                self.run_task(adapter_factory=lambda config: self.factory(config, hook=mutation))
            self.assertEqual(self.draft_path.read_bytes(), old)

    def test_newer_draft_is_not_clobbered_by_a_slower_task(self):
        self.old_draft()
        newer = b'{"id":"newer-review"}'
        def edit():
            self.draft_path.write_bytes(newer)
        with self.assertRaisesRegex(ValueError, "其他任务更新"):
            self.run_task(adapter_factory=lambda config: self.factory(config, hook=edit))
        self.assertEqual(self.draft_path.read_bytes(), newer)

    def test_atomic_replace_error_preserves_old_draft_and_cleans_temporary(self):
        old = self.old_draft()
        replace = task.os.replace
        def fail_current(source, destination):
            if Path(destination) == self.draft_path:
                raise OSError("simulated replace failure")
            replace(source, destination)
        with patch.object(task.os, "replace", side_effect=fail_current):
            with self.assertRaisesRegex(OSError, "replace failure"):
                self.run_task()
        self.assertEqual(self.draft_path.read_bytes(), old)
        self.assertEqual(list((self.directory/"state").glob("*.tmp")), [])

    def test_explicit_source_binding_is_resolved_without_scanning_other_qwen_nodes(self):
        self.write_json("queue_snapshot.json", {"prompt": {
            "101": {"class_type": "QwenH3PromptLocal",
                    "inputs": {"llm_model": "chosen.gguf", "vision_model": "chosen-mmproj.gguf"}},
            "102": {"class_type": "QwenH3PromptLocal",
                    "inputs": {"llm_model": "other.gguf", "vision_model": "other-mmproj.gguf"}},
        }})
        result = task.run_director_task(
            self.root, self.plan["id"], qwen_source_id="101", adapter_factory=self.factory)
        self.assertEqual(result["status"], "draft")
        self.assertEqual(self.factory_calls[0]["llm_model"], "chosen.gguf")
        self.assertEqual(self.factory_calls[0]["qwen_source_id"], "101")
        with self.assertRaisesRegex(ValueError, "不一致"):
            task.run_director_task(self.root, self.plan["id"], "wrong.gguf", "",
                                   qwen_source_id="101", adapter_factory=self.factory)
        with self.assertRaisesRegex(ValueError, "不在"):
            task.run_director_task(self.root, self.plan["id"], qwen_source_id="missing",
                                   adapter_factory=self.factory)
        with self.assertRaisesRegex(ValueError, "明确指定"):
            task.run_director_task(self.root, self.plan["id"], adapter_factory=self.factory)
        self.assertEqual(len(self.factory_calls), 1)

    def test_changed_qwen_source_or_linked_model_is_rejected(self):
        snapshot = {"prompt": {"101": {"class_type": "QwenH3PromptLocal", "inputs": {
            "llm_model": "chosen.gguf", "vision_model": "chosen-mmproj.gguf"}}}}
        self.write_json("queue_snapshot.json", snapshot)
        old = self.old_draft()
        def edit():
            snapshot["prompt"]["101"]["inputs"]["llm_model"] = "changed.gguf"
            self.write_json("queue_snapshot.json", snapshot)
        with self.assertRaisesRegex(ValueError, "Qwen 来源配置"):
            task.run_director_task(
                self.root, self.plan["id"], qwen_source_id="101",
                adapter_factory=lambda config: self.factory(config, hook=edit))
        self.assertEqual(self.draft_path.read_bytes(), old)
        snapshot["prompt"]["101"]["inputs"]["llm_model"] = ["connected-node", 0]
        self.write_json("queue_snapshot.json", snapshot)
        with self.assertRaisesRegex(ValueError, "未解析连线"):
            task.run_director_task(self.root, self.plan["id"], qwen_source_id="101",
                                   adapter_factory=self.factory)
        self.assertEqual(len(self.factory_calls), 1)

    def test_node_is_text_only_and_reports_traceable_status(self):
        cls = nodes.NODE_CLASS_MAPPINGS["TY_H3LVDirectorTask"]
        self.assertIs(cls, task.DirectorTask)
        self.assertEqual(cls.RETURN_TYPES, ("STRING",))
        self.assertTrue(cls.OUTPUT_NODE)
        self.assertEqual(set(cls.INPUT_TYPES()["required"]),
                         {"project_id", "model", "mmproj", "seed", "max_tokens"})
        status = {"project_id": self.plan["id"], "status": "draft", "draft_id": "trace-id"}
        with patch.object(nodes, "data_root", return_value=self.root), \
             patch.object(task, "run_director_task", return_value=status) as run:
            result = cls().plan(self.plan["id"], "chosen.gguf", "projector.gguf", 42, 4096, "101")
        self.assertEqual(json.loads(result["result"][0]), status)
        self.assertEqual(result["ui"]["text"], [result["result"][0]])
        run.assert_called_once_with(self.root, self.plan["id"], "chosen.gguf",
                                        "projector.gguf", 42, 4096, "101",
                                        expected_input_fingerprint="", director_prompt_id="")


if __name__ == "__main__":
    unittest.main()
