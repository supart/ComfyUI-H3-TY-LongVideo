import copy
import importlib
import json
import tempfile
import unittest
from unittest.mock import patch

from test_plugin import core, controller, nodes, sample_plan


bridge = importlib.import_module("h3lv_test.execution_bridge")
task = importlib.import_module("h3lv_test.director_task")


def snapshot():
    return {"qwen_id": "20", "h3_id": "30", "prompt_output_id": "21",
            "loader_id": "1", "video_id": "40", "prompt": {
        "1": {"class_type": "TY_H3LVUnified", "inputs": {}},
        "10": {"class_type": "LoadImage", "inputs": {"image": "person.png"}},
        "11": {"class_type": "LoadImage", "inputs": {"image": "scene.png"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": "old-qwen.png"}},
        "20": {"class_type": "QwenH3PromptLocal", "inputs": {
            "prompt": ["1", 2], "duration": 10, "seed": 888, "max_tokens": 8192,
            "llm_model": "original.gguf", "vision_model": "mmproj.gguf",
            "think_mode": False, "reference_images.reference_image_0": ["12", 0],
            "reference_images.reference_image_1": ["11", 0]}},
        "21": {"class_type": "ShowText", "inputs": {"text": ["20", 0]}},
        "30": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
            "prompt": ["21", 0], "ref_images.ref_image_0": ["10", 0],
            "ref_images.ref_image_1": ["11", 0], "length": ["1", 3]}},
        "40": {"class_type": "VHS_VideoCombine", "inputs": {"images": ["30", 0]}}
    }}


def dual_snapshot():
    value = snapshot()
    value["prompt"]["30"]["inputs"].update({
        "ref_images.ref_image_0": ["1", 5],
        "ref_images.ref_image_1": ["1", 6],
    })
    return value


class ExecutionBridgeTests(unittest.TestCase):
    def setUp(self):
        self.plan = sample_plan()
        self.plan.update(planning_method="qwen", director_qwen_source_id="20")
        self.snapshot = snapshot()

    def test_external_prompt_motion_audit_ignores_negative_constraints(self):
        self.assertEqual(
            bridge.audit_expanded_prompt(
                "The presenter remains still; no hand gestures or camera movement.",
                "speaking",
            ),
            [],
        )
        self.assertEqual(
            bridge.audit_expanded_prompt(
                "She raises her right hand to wave and points toward the camera. "
                "The camera zooms in. A microphone appears.",
                "speaking",
            ),
            ["挥手", "指向", "抬手", "镜头运动", "新增道具"],
        )
        self.assertEqual(
            bridge.audit_expanded_prompt(
                "The cat has pointed ears and keeps its paws still.",
                "speaking",
            ),
            [],
        )
        self.assertEqual(
            bridge.audit_expanded_prompt(
                "The cat points toward the camera.",
                "speaking",
            ),
            ["指向"],
        )
        self.assertEqual(
            bridge.audit_expanded_prompt(
                "She briefly raises her right hand. The camera zooms in.",
                "speaking",
                "轻轻抬手一次。",
            ),
            ["镜头运动"],
        )
        self.assertEqual(bridge.audit_expanded_prompt("She waves.", "singing"), [])

    def test_rules_leave_graph_and_parameters_unchanged(self):
        self.plan.pop("planning_method")
        # An orphan canvas Qwen is legacy state, not a second pass.
        self.snapshot["prompt"]["30"]["inputs"]["prompt"] = ["25", 0]
        self.snapshot["prompt"]["25"] = {"class_type": "TextBox", "inputs": {}}
        original = copy.deepcopy(self.snapshot)
        result = bridge.apply_execution_contract(
            None, self.snapshot, self.snapshot["prompt"], self.plan, self.plan["segments"][0])
        self.assertEqual(result, {})
        self.assertEqual(self.snapshot, original)

    def test_connected_external_qwen_is_second_pass_in_rules_mode(self):
        plan = copy.deepcopy(self.plan)
        plan.pop("planning_method", None)
        row = plan["segments"][0]
        row["material_note"] = "图1是数字人，图2是背景。"
        original_settings = {
            key: self.snapshot["prompt"]["20"]["inputs"][key]
            for key in ("skill", "llm_model", "vision_model", "seed", "max_tokens")
            if key in self.snapshot["prompt"]["20"]["inputs"]
        }
        prompt = copy.deepcopy(self.snapshot["prompt"])
        metadata = bridge.apply_execution_contract(
            None, self.snapshot, prompt, plan, row)
        qwen_inputs = prompt["20"]["inputs"]
        self.assertEqual(metadata["expander"], "external_qwen")
        self.assertIn("素材说明：图1是数字人，图2是背景。", qwen_inputs["prompt"])
        self.assertEqual(qwen_inputs["reference_images.reference_image_0"], ["10", 0])
        self.assertEqual(qwen_inputs["reference_images.reference_image_1"], ["11", 0])
        self.assertEqual(metadata["reference_count"], 2)
        for key, value in original_settings.items():
            self.assertEqual(qwen_inputs[key], value)

    def test_bindings_are_derived_from_h3_prompt_chain_without_node_ids(self):
        for key in ("qwen_id", "h3_id", "prompt_output_id"):
            invalid = copy.deepcopy(self.snapshot)
            invalid[key] = "missing"
            bridge.bind_execution(invalid, self.plan)
            self.assertEqual(invalid["qwen_id"], "20")
            self.assertEqual(invalid["h3_id"], "30")
            self.assertEqual(invalid["prompt_output_id"], "21")
        self.plan["director_qwen_source_id"] = "other"
        with self.assertRaisesRegex(ValueError, "来源节点"):
            bridge.bind_execution(self.snapshot, self.plan)

    def test_multiple_h3_nodes_are_rejected_before_reference_rewrite(self):
        self.snapshot["prompt"]["31"] = copy.deepcopy(self.snapshot["prompt"]["30"])
        with self.assertRaisesRegex(ValueError, "只有一个"):
            bridge.bind_execution(self.snapshot, self.plan)

    def test_changed_bound_model_does_not_silently_expand_with_different_model(self):
        self.plan["director_qwen_config"] = {
            "llm_model": "original.gguf", "vision_model": "mmproj.gguf"}
        bridge.bind_execution(self.snapshot, self.plan)
        self.snapshot["prompt"]["20"]["inputs"]["llm_model"] = "substitute.gguf"
        with self.assertRaisesRegex(ValueError, "模型配置"):
            bridge.bind_execution(self.snapshot, self.plan)

    def test_ordered_shared_and_nonshared_references_two_one_zero_two(self):
        original = copy.deepcopy(self.snapshot)
        for count in (2, 1, 0, 2):
            with self.subTest(count=count):
                prompt = copy.deepcopy(self.snapshot["prompt"])
                h3_inputs = prompt["30"]["inputs"]
                for index in range(count, 2):
                    del h3_inputs[f"ref_images.ref_image_{index}"]
                with patch("h3lv_test.director_review.effective_segment_brief",
                           return_value="Reviewed locked studio direction."):
                    meta = bridge.apply_execution_contract(
                        None, self.snapshot, prompt, self.plan, self.plan["segments"][0])
                actual = {key: value for key, value in prompt["20"]["inputs"].items()
                          if key.startswith("reference_images.")}
                self.assertEqual(actual, {
                    f"reference_images.reference_image_{index}": [str(10+index), 0]
                    for index in range(count)})
                self.assertEqual(meta["reference_count"], count)
                self.assertNotIn("12", prompt)
                self.assertEqual(prompt["20"]["inputs"]["seed"], 888)
                self.assertEqual(prompt["20"]["inputs"]["llm_model"], "original.gguf")
                self.assertEqual(prompt["20"]["inputs"]["max_tokens"], 8192)
                self.assertEqual(prompt["30"]["inputs"]["length"], ["1", 3])
        self.assertEqual(self.snapshot, original)

    def test_qwen_duration_matches_generation_frames_not_content(self):
        row = self.plan["segments"][0]
        row["generation_frames"] = 243
        prompt = copy.deepcopy(self.snapshot["prompt"])
        with patch("h3lv_test.director_review.effective_segment_brief", return_value="Approved"):
            meta = bridge.apply_execution_contract(None, self.snapshot, prompt, self.plan, row)
        self.assertEqual(prompt["20"]["inputs"]["duration"], 10.125)
        self.assertEqual(meta["content_duration_seconds"], 10)
        self.assertEqual(row["generation_frames"], 243)
        self.assertEqual(prompt["20"]["inputs"]["prompt"], "Approved")

    def test_external_qwen_is_second_pass_even_when_longvideo_has_internal_controls(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["qwen_config"] = {
            "skill": "long_video_director", "duration": 10.0,
            "llm_model": "Qwen3.8-27B-UD-Q4_K_M.gguf",
            "vision_model": "mmproj-F16.gguf", "think_mode": False,
            "reasoning_effort": "medium", "seed": 0, "max_tokens": 8192,
            "video_sample_frames_per_sec": 2, "force_unload_model": True,
        }
        snapshot["prompt"]["13"] = {"class_type": "LoadVideo", "inputs": {}}
        snapshot["prompt"]["30"]["inputs"]["ref_videos.ref_video_0"] = ["13", 0]
        snapshot["prompt"]["20"]["inputs"]["reference_videos.reference_video_0"] = ["12", 0]
        row = self.plan["segments"][0]
        row["generation_frames"] = 243
        prompt = copy.deepcopy(snapshot["prompt"])
        with patch("h3lv_test.director_review.effective_segment_brief",
                   return_value="Approved single-segment relative brief."):
            metadata = bridge.apply_execution_contract(
                None, snapshot, prompt, self.plan, row)
        qwen_inputs = prompt["20"]["inputs"]
        self.assertEqual(qwen_inputs["prompt"], "Approved single-segment relative brief.")
        self.assertEqual(qwen_inputs["duration"], 10.125)
        self.assertEqual(qwen_inputs["reference_images.reference_image_0"], ["10", 0])
        self.assertEqual(qwen_inputs["reference_images.reference_image_1"], ["11", 0])
        self.assertEqual(qwen_inputs["reference_videos.reference_video_0"], ["13", 0])
        self.assertEqual(metadata["expander"], "external_qwen")
        self.assertEqual(metadata["prompt_binding"], "literal_brief")
        self.assertEqual(metadata["reference_count"], 2)
        self.assertEqual(metadata["reference_video_count"], 1)
        self.assertIn("20", prompt)
        self.assertIn("30", prompt)
        self.assertNotIn("256", prompt)
        self.assertNotIn("309", prompt)

    def test_dual_outputs_are_forwarded_to_external_qwen_in_order(self):
        snapshot_value = dual_snapshot()
        prompt = copy.deepcopy(snapshot_value["prompt"])
        self.plan["mode"] = "speaking"
        with patch("h3lv_test.director_review.effective_segment_brief",
                   return_value="Approved dual-image brief."):
            metadata = bridge.apply_execution_contract(
                None, snapshot_value, prompt, self.plan, self.plan["segments"][0])
        qwen_inputs = prompt["20"]["inputs"]
        self.assertEqual(qwen_inputs["reference_images.reference_image_0"], ["1", 5])
        self.assertEqual(qwen_inputs["reference_images.reference_image_1"], ["1", 6])
        self.assertEqual(metadata["reference_count"], 2)
        self.assertEqual(metadata["reference_output_mode"], "ty_h3lv_unified_dual")
        self.assertIn("参考图通用绑定", qwen_inputs["prompt"])
        self.assertIn("服装与图1可见穿戴配饰必须保持原样", qwen_inputs["prompt"])
        self.assertIn("禁止换装、改色、重染", qwen_inputs["prompt"])

    def test_digital_appearance_lock_is_not_added_to_singing(self):
        snapshot_value = dual_snapshot()
        prompt = copy.deepcopy(snapshot_value["prompt"])
        with patch("h3lv_test.director_review.effective_segment_brief",
                   return_value="Approved singing brief."):
            bridge.apply_execution_contract(
                None, snapshot_value, prompt, self.plan, self.plan["segments"][0])
        self.assertNotIn("数字人口播连续性锁定", prompt["20"]["inputs"]["prompt"])

    def test_dual_image_preview_nodes_survive_pruning_and_are_outputs(self):
        snapshot_value = dual_snapshot()
        snapshot_value["prompt"].update({
            "50": {"class_type": "PreviewImage", "inputs": {"images": ["1", 5]}},
            "51": {"class_type": "PreviewImage", "inputs": {"images": ["1", 6]}},
            "52": {"class_type": "PreviewImage", "inputs": {"images": ["10", 0]}},
        })
        prompt = copy.deepcopy(snapshot_value["prompt"])
        with patch("h3lv_test.director_review.effective_segment_brief",
                   return_value="Approved dual-image brief."):
            bridge.apply_execution_contract(
                None, snapshot_value, prompt, self.plan, self.plan["segments"][0])
        self.assertIn("50", prompt)
        self.assertIn("51", prompt)
        self.assertNotIn("52", prompt)
        self.assertEqual(bridge.execution_output_ids(snapshot_value, prompt),
                         ["40", "50", "51"])

    def test_shot_planner_guide_preview_is_an_execution_output(self):
        snapshot_value = {"loader_id": "1", "video_id": "40", "prompt": {
            "1": {"class_type": "TY_ShotPlanner", "inputs": {}},
            "40": {"class_type": "VHS_VideoCombine", "inputs": {}},
            "41": {"class_type": "BFSShotUnpack", "inputs": {"shot": ["1", 11]}},
            "42": {"class_type": "PreviewImage", "inputs": {"images": ["41", 0]}},
            "43": {"class_type": "PreviewImage", "inputs": {"images": ["41", 1]}},
            "44": {"class_type": "BFSShotUnpack", "inputs": {"shot": ["9", 11]}},
            "45": {"class_type": "PreviewImage", "inputs": {"images": ["44", 0]}},
        }}
        self.assertEqual(bridge.execution_output_ids(
            snapshot_value, snapshot_value["prompt"]), ["40", "42"])

    def test_six_ty_outputs_are_forwarded_to_external_qwen_in_order(self):
        snapshot_value = dual_snapshot()
        h3_inputs = snapshot_value["prompt"]["30"]["inputs"]
        for slot in range(2, 6):
            h3_inputs[f"ref_images.ref_image_{slot}"] = ["1", 5 + slot]
        prompt = copy.deepcopy(snapshot_value["prompt"])
        with patch("h3lv_test.director_review.effective_segment_brief",
                   return_value="Approved six-image brief."):
            metadata = bridge.apply_execution_contract(
                None, snapshot_value, prompt, self.plan, self.plan["segments"][0])
        self.assertEqual(metadata["reference_count"], 6)
        self.assertEqual(
            [prompt["20"]["inputs"][f"reference_images.reference_image_{slot}"]
             for slot in range(6)],
            [["1", 5 + slot] for slot in range(6)])

    def test_ty_outputs_allow_partial_slots_but_validate_order(self):
        incomplete = dual_snapshot()
        del incomplete["prompt"]["30"]["inputs"]["ref_images.ref_image_1"]
        bridge.bind_execution(incomplete, self.plan)

        swapped = dual_snapshot()
        swapped["prompt"]["30"]["inputs"]["ref_images.ref_image_0"] = ["1", 6]
        with self.assertRaisesRegex(ValueError, "顺序错误"):
            bridge.bind_execution(swapped, self.plan)

        misplaced = dual_snapshot()
        misplaced["prompt"]["30"]["inputs"]["ref_images.ref_image_2"] = ["1", 5]
        with self.assertRaisesRegex(ValueError, "顺序错误"):
            bridge.bind_execution(misplaced, self.plan)

    def test_direct_longvideo_qwen_writes_literal_h3_prompt_without_canvas_qwen_node(self):
        direct = copy.deepcopy(self.snapshot)
        direct["prompt"].pop("20")
        direct["prompt"].pop("21")
        direct["prompt"]["25"] = {"class_type": "TextBox", "inputs": {}}
        direct["prompt"]["30"]["inputs"]["prompt"] = ["25", 0]
        direct["prompt"]["1"]["inputs"].update({
            "skill": "long_video_director",
            "duration": 10.0,
            "llm_model": "Qwen3.8-27B-UD-Q4_K_M.gguf",
            "vision_model": "mmproj-F16.gguf",
            "think_mode": True,
            "reasoning_effort": "xhigh",
            "seed": 42,
            "max_tokens": 4096,
            "video_sample_frames_per_sec": 3,
            "force_unload_model": True,
        })
        direct["qwen_id"] = "missing"
        direct["h3_id"] = "missing"
        direct["prompt_output_id"] = "missing"
        plan = copy.deepcopy(self.plan)
        plan["planning_method"] = "rules"
        config = bridge.qwen_config_from_prompt(direct["prompt"], "1")
        self.assertEqual(config["llm_model"], "Qwen3.8-27B-UD-Q4_K_M.gguf")
        self.assertTrue(config["think_mode"])
        direct["qwen_config"] = config
        calls = []

        class FakeEngine:
            last_metadata = {"mode": "ref2va", "repaired": False}

            def __init__(self, selected):
                calls.append(("config", copy.deepcopy(selected)))

            def generate(self, request, *, images, videos, duration):
                calls.append((request, images, videos, duration))
                return "LongVideo generated prompt"

        with patch("h3lv_test.director_review.effective_segment_brief",
                   return_value="Approved long-video brief."), \
             patch("h3lv_test.qwen_h3_engine.QwenH3Engine", FakeEngine), \
             patch("h3lv_test.qwen_h3_engine.reference_media",
                   return_value=([], [])):
            prompt = copy.deepcopy(direct["prompt"])
            metadata = bridge.apply_execution_contract(
                None, direct, prompt, plan, plan["segments"][0])
        self.assertEqual(prompt["30"]["inputs"]["prompt"], "LongVideo generated prompt")
        self.assertEqual(calls[0][1]["reasoning_effort"], "xhigh")
        self.assertEqual(calls[1][3], plan["segments"][0]["generation_frames"] / 24)
        self.assertEqual(metadata["qwen_config"], config)
        self.assertNotIn("QwenH3PromptLocal", {
            node.get("class_type") for node in prompt.values()})

    def test_direct_longvideo_qwen_prunes_legacy_canvas_formatter_branch(self):
        direct = copy.deepcopy(self.snapshot)
        direct["prompt"]["25"] = {"class_type": "TextBox", "inputs": {}}
        direct["prompt"]["30"]["inputs"]["prompt"] = ["25", 0]
        direct["prompt"]["1"]["inputs"].update({
            "skill": "long_video_director",
            "duration": 10.0,
            "llm_model": "Qwen3.8-27B-UD-Q4_K_M.gguf",
            "vision_model": "mmproj-F16.gguf",
            "think_mode": False,
            "reasoning_effort": "medium",
            "seed": 0,
            "max_tokens": 8192,
            "video_sample_frames_per_sec": 2,
            "force_unload_model": True,
        })
        direct["qwen_config"] = bridge.qwen_config_from_prompt(direct["prompt"], "1")
        calls = []

        class FakeEngine:
            last_metadata = {"mode": "ref2va", "repaired": False}

            def __init__(self, _selected):
                pass

            def generate(self, request, *, images, videos, duration):
                calls.append((request, images, videos, duration))
                return "Literal prompt from LongVideo"

        with patch("h3lv_test.director_review.effective_segment_brief",
                   return_value="Approved long-video brief."), \
             patch("h3lv_test.qwen_h3_engine.QwenH3Engine", FakeEngine), \
             patch("h3lv_test.qwen_h3_engine.reference_media", return_value=([], [])):
            prompt = copy.deepcopy(direct["prompt"])
            bridge.apply_execution_contract(
                None, direct, prompt, self.plan, self.plan["segments"][0])

        self.assertEqual(prompt["30"]["inputs"]["prompt"], "Literal prompt from LongVideo")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("QwenH3PromptLocal", {
            node.get("class_type") for node in prompt.values()})
        self.assertNotIn("ShowText", {node.get("class_type") for node in prompt.values()})
        self.assertIn("1", prompt)
        self.assertIn("30", prompt)
        self.assertIn("40", prompt)
        for node in prompt.values():
            for value in (node.get("inputs") or {}).values():
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    self.assertIn(str(value[0]), prompt)

    def test_final_is_exact_literal_without_rewrite_or_material_prefix(self):
        row = self.plan["segments"][0]
        row.update(prompt_kind="final", prompt="Exact final text.\nNo extra prefix.",
                   material_note="must not prepend")
        prompt = copy.deepcopy(self.snapshot["prompt"])
        with patch("h3lv_test.director_review.effective_segment_brief") as expand:
            result = bridge.apply_execution_contract(None, self.snapshot, prompt, self.plan, row)
        expand.assert_not_called()
        self.assertEqual(prompt["30"]["inputs"]["prompt"], row["prompt"])
        self.assertEqual(result["final_prompt"], row["prompt"])
        # The selected video now has no prompt edge to the Qwen/show-text chain.
        self.assertNotIsInstance(prompt["30"]["inputs"]["prompt"], list)
        self.assertEqual(self.snapshot["prompt"]["30"]["inputs"]["prompt"], ["21", 0])

    def test_final_prompt_log_never_scans_unbound_outputs(self):
        history = {"outputs": {
            "other": {"text": ["subject_definitions: unrelated six-title text"]},
            "21": {"text": ["Actual selected", "formatter output."]}}}
        self.assertEqual(controller.final_prompt_from_history(history, "21"),
                         "Actual selected\nformatter output.")
        self.assertEqual(controller.final_prompt_from_history(history), "")
        self.assertEqual(controller.final_prompt_from_history(history, "missing"), "")

    def test_director_snapshot_does_not_replace_video_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            core.write_plan(root, self.plan)
            directory = core.project_path(root, self.plan["id"])
            video = core.state_file(directory, "queue_snapshot.json")
            original_video = json.dumps({"prompt": {"20": {"class_type": "QwenH3PromptLocal",
                "inputs": {"llm_model": "video.gguf", "vision_model": "v.gguf"}}}})
            video.write_text(original_video, encoding="utf-8")
            core.state_file(directory, "director_queue_snapshot.json").write_text(json.dumps({
                "prompt": {"20": {"class_type": "QwenH3PromptLocal",
                    "inputs": {"llm_model": "director.gguf", "vision_model": "d.gguf"}}}
            }), encoding="utf-8")
            config = task.resolve_model_config(directory, "", "", "20")
            self.assertEqual(config["llm_model"], "director.gguf")
            self.assertEqual(video.read_text(encoding="utf-8"), original_video)

    def test_direct_canvas_requires_approved_director_before_audio_read(self):
        self.plan["approved"] = True
        with patch.object(nodes, "read_plan", return_value=self.plan), \
             patch.object(nodes, "data_root", return_value="unused"), \
             patch("h3lv_test.director_review.require_director_approval",
                   side_effect=ValueError("director not approved")), \
             patch("soundfile.read") as read_audio:
            with self.assertRaisesRegex(ValueError, "director not approved"):
                nodes.LoadSegment().load(self.plan["id"], 0)
        read_audio.assert_not_called()


if __name__ == "__main__":
    unittest.main()
