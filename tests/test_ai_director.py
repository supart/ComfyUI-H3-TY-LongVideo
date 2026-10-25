import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("h3lv_ai_director_test", ROOT/"ai_director.py")
director = importlib.util.module_from_spec(spec)
spec.loader.exec_module(director)


def sample_plan():
    return {"id": "a"*32, "sample_rate": 100, "samples": 2000, "mode": "speaking",
            "approved": True, "segments": [
                {"index": index, "start_sample": index*1000, "end_sample": (index+1)*1000,
                 "generation_frames": 260, "text": f"Confirmed words {index}",
                 "prompt": f"Existing brief {index}", "refs": ["person.png", "studio.png"],
                 "material_note": "same studio"} for index in range(2)]}


def valid_response(snapshot):
    return {"schema_version": 1, "global_contract": "Centered presenter in the unchanged studio.",
            "segments": [
                {"segment_index": row["segment_index"], "start_sample": row["start_sample"],
                 "end_sample": row["end_sample"], "intent": "Explain the point",
                 "brief": f"Restrained emphasis for segment {row['segment_index']}",
                 "entry_state": "Hands resting", "exit_state": "Hands resting",
                 "next_transition": "Continue the same pose",
                 "actions": [{"start_seconds": 0, "end_seconds": 10,
                              "description": "Speak naturally with a small open-hand gesture."}]}
                for row in snapshot["source"]["segments"]]}


def fill_response(schema):
    if schema.get("enum"):
        return schema["enum"][0]
    if schema["type"] == "string":
        return "保持已批准的姿态，配合克制的小幅手势。"
    return {key: fill_response(value) for key, value in schema["properties"].items()}


def valid_fill(snapshot):
    return fill_response(director.director_fill_schema(snapshot))


class SnapshotTests(unittest.TestCase):
    def test_language_contract_changes_preview_not_existing_source(self):
        current = director.build_input_snapshot(sample_plan(), constraints="Keep authored words.")
        with patch.object(director, "FILL_CONTRACT", "director-fill-v1"):
            legacy = director.build_input_snapshot(sample_plan(), constraints="Keep authored words.")
        self.assertNotEqual(current["input_fingerprint"], legacy["input_fingerprint"])
        self.assertEqual(current["source_fingerprint"], legacy["source_fingerprint"])
        self.assertEqual(current["model_message"], legacy["model_message"])
        self.assertIn("简体中文", director.SYSTEM_PROMPT)

    def test_full_input_and_exact_durations_are_preserved_without_mutation(self):
        plan = sample_plan()
        before = copy.deepcopy(plan)
        transcript = {"segments": [{"text": "all of the transcription"}]}
        analysis = {"phrases": [{"start": 0, "end": 20}], "waveform": [0.2, 0.4]}
        snapshot = director.build_input_snapshot(plan, transcript=transcript, analysis=analysis)
        self.assertNotIn("transcript", snapshot["source"])
        self.assertNotIn("analysis", snapshot["source"])
        self.assertEqual(snapshot["local_source_hashes"]["transcript"],
                         director.snapshot_fingerprint(transcript))
        self.assertEqual(snapshot["local_source_hashes"]["analysis"],
                         director.snapshot_fingerprint(analysis))
        self.assertEqual(len(snapshot["source"]["segments"]), 2)
        self.assertEqual(snapshot["source"]["segments"][1]["content_duration_seconds"], 10)
        self.assertEqual(snapshot["source"]["segments"][1]["generation_duration_seconds"], 260/24)
        snapshot["source"]["segments"][0]["refs"].clear()
        self.assertEqual(plan, before)

    def test_model_input_is_a_compact_human_timeline(self):
        snapshot = director.build_input_snapshot(sample_plan())
        model_input = snapshot["model_input"]
        self.assertEqual(set(model_input), {"mode", "constraints", "segments"})
        self.assertEqual(len(model_input["segments"]),
                         len(snapshot["source"]["segments"]))
        for row in model_input["segments"]:
            self.assertIn("segment_index", row)
            self.assertIn("time_range", row)
            self.assertIn("duration", row)
            self.assertIn("content", row)
            encoded = json.dumps(row, ensure_ascii=False)
            for internal in ("project_id", "sample_rate", "start_sample",
                             "end_sample", "generation_frames",
                             "reference_fingerprints"):
                self.assertNotIn(internal, encoded)

    def test_model_message_matches_screenshot_style_and_omits_internal_fields(self):
        snapshot = director.build_input_snapshot(sample_plan())
        message = snapshot["model_message"]
        self.assertIn("全片模式：口播", message)
        self.assertIn("第 1 段 · 0.000–10.000 秒 · 10.000 秒", message)
        self.assertIn("第 2 段 · 10.000–20.000 秒 · 10.000 秒", message)
        self.assertIn("内容：Confirmed words 0", message)
        self.assertIn("素材说明：same studio", message)
        self.assertNotIn("Existing brief 0", message)
        self.assertNotIn("project_id", message)
        self.assertNotIn("sample_rate", message)
        self.assertNotIn("start_sample", message)
        self.assertNotIn("generation_frames", message)

        empty = sample_plan()
        empty["segments"][0]["text"] = ""
        empty["segments"][0]["material_note"] = ""
        empty_message = director.build_input_snapshot(empty)["model_message"]
        self.assertIn("内容：无已确认文字", empty_message)
        first_block, second_block = empty_message.split("第 2 段", 1)
        self.assertNotIn("素材说明：", first_block)
        self.assertIn("素材说明：same studio", second_block)

    def test_source_and_full_input_fingerprints_are_separate(self):
        plan = sample_plan()
        first = director.build_input_snapshot(plan, source_fingerprint="review-signature",
                                              analysis={"rhythm": 1})
        second = director.build_input_snapshot(plan, source_fingerprint="review-signature",
                                               analysis={"rhythm": 2})
        self.assertEqual(first["source_fingerprint"], second["source_fingerprint"])
        self.assertNotEqual(first["input_fingerprint"], second["input_fingerprint"])
        self.assertEqual(first["model_input_fingerprint"], second["model_input_fingerprint"])
        self.assertEqual(first["model_message"], second["model_message"])
        self.assertEqual(first["model_message_fingerprint"], second["model_message_fingerprint"])

    def test_raw_word_growth_never_changes_or_enlarges_model_input(self):
        plan = sample_plan()
        small = director.build_input_snapshot(plan)
        huge = director.build_input_snapshot(
            plan, transcript={"words": [{"word": "raw-only", "start": n / 100}
                                       for n in range(10000)]},
            analysis={"protected_words": ["never-send"] * 10000})
        self.assertEqual(small["source"], huge["source"])
        self.assertNotEqual(small["source_fingerprint"], huge["source_fingerprint"])
        self.assertEqual([row["text"] for row in huge["source"]["segments"]],
                         [row.get("text", "") for row in plan["segments"]])

    def test_replaceable_brief_does_not_stale_source_but_authored_constraints_do(self):
        plan = sample_plan()
        original = director.build_input_snapshot(plan)
        changed = copy.deepcopy(plan)
        changed["segments"][0]["prompt"] = "New AI-generated brief"
        replaced = director.build_input_snapshot(changed)
        self.assertEqual(original["source_fingerprint"], replaced["source_fingerprint"])
        self.assertNotEqual(original["input_fingerprint"], replaced["input_fingerprint"])
        for field, value in (("prompt_locked", True), ("prompt_kind", "final")):
            before = copy.deepcopy(plan)
            before["segments"][0][field] = value
            after = copy.deepcopy(before)
            after["segments"][0]["prompt"] = "Changed authored constraint"
            with self.subTest(field=field):
                self.assertNotEqual(
                    director.build_input_snapshot(before)["source_fingerprint"],
                    director.build_input_snapshot(after)["source_fingerprint"])

    def test_snapshot_fingerprint_is_stable_but_tracks_text_assets_and_constraints(self):
        plan = sample_plan()
        original = director.build_input_snapshot(plan)
        same = director.build_input_snapshot(copy.deepcopy(plan))
        self.assertEqual(original["input_fingerprint"], same["input_fingerprint"])
        for field, value in (("text", "changed words"), ("refs", ["different.png"])):
            changed = copy.deepcopy(plan)
            changed["segments"][0][field] = value
            self.assertNotEqual(original["input_fingerprint"],
                                director.build_input_snapshot(changed)["input_fingerprint"])
        self.assertNotEqual(original["input_fingerprint"],
                            director.build_input_snapshot(plan, constraints="Different framing")["input_fingerprint"])

    def test_invalid_source_timeline_and_nonfinite_data_are_rejected(self):
        for field, value in (("start_sample", 1), ("end_sample", 0), ("generation_frames", True)):
            plan = sample_plan()
            plan["segments"][0][field] = value
            with self.subTest(field=field), self.assertRaises(director.DirectorValidationError):
                director.build_input_snapshot(plan)
        with self.assertRaises(ValueError):
            director.build_input_snapshot(sample_plan(), analysis={"energy": float("nan")})


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = director.build_input_snapshot(sample_plan())
        self.result = valid_response(self.snapshot)

    def test_valid_json_and_reordered_segments_return_timeline_order(self):
        self.result["segments"].reverse()
        validated = director.parse_director_response(json.dumps(self.result), self.snapshot)
        self.assertEqual([row["segment_index"] for row in validated["segments"]], [0, 1])
        self.assertEqual(self.result["segments"][0]["segment_index"], 1)

    def test_compact_time_ranges_map_back_to_exact_local_boundaries(self):
        compact = copy.deepcopy(self.result)
        for row, source in zip(compact["segments"], self.snapshot["source"]["segments"]):
            row.pop("start_sample")
            row.pop("end_sample")
            row["time_range"] = (
                f"{source['start_sample'] / 100:.3f}-"
                f"{source['end_sample'] / 100:.3f}s")
        validated = director.parse_director_response(json.dumps(compact), self.snapshot)
        self.assertEqual(
            [(row["start_sample"], row["end_sample"]) for row in validated["segments"]],
            [(row["start_sample"], row["end_sample"])
             for row in self.snapshot["source"]["segments"]])

    def test_missing_duplicate_unknown_and_boolean_segment_ids_are_rejected(self):
        cases = []
        missing = copy.deepcopy(self.result)
        missing["segments"].pop()
        cases.append(missing)
        for value in (0, 4, True):
            changed = copy.deepcopy(self.result)
            changed["segments"][1]["segment_index"] = value
            cases.append(changed)
        for changed in cases:
            with self.subTest(value=changed), self.assertRaises(director.DirectorValidationError):
                director.validate_director_result(changed, self.snapshot)

    def test_sample_boundaries_cannot_be_changed_or_coerced(self):
        for field, value in (("start_sample", 1), ("end_sample", 1001),
                             ("start_sample", 0.0), ("end_sample", "1000")):
            changed = copy.deepcopy(self.result)
            changed["segments"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(director.DirectorValidationError):
                director.validate_director_result(changed, self.snapshot)

    def test_actions_cannot_use_padding_overlap_or_nonfinite_times(self):
        for action in (
            {"start_seconds": 0, "end_seconds": 10.5, "description": "uses padding"},
            {"start_seconds": -1, "end_seconds": 2, "description": "negative"},
            {"start_seconds": 1, "end_seconds": 1, "description": "empty interval"},
            {"start_seconds": 0, "end_seconds": float("inf"), "description": "infinite"},
            {"start_seconds": False, "end_seconds": 2, "description": "boolean"},
        ):
            changed = copy.deepcopy(self.result)
            changed["segments"][0]["actions"] = [action]
            with self.subTest(action=action), self.assertRaises(director.DirectorValidationError):
                director.validate_director_result(changed, self.snapshot)
        changed = copy.deepcopy(self.result)
        changed["segments"][0]["actions"] = [
            {"start_seconds": 0, "end_seconds": 5, "description": "first"},
            {"start_seconds": 4, "end_seconds": 8, "description": "overlaps"}]
        with self.assertRaises(director.DirectorValidationError):
            director.validate_director_result(changed, self.snapshot)

    def test_rounded_display_endpoint_is_normalized_to_exact_sample_boundary(self):
        plan = {"id": "b"*32, "sample_rate": 44100, "samples": 359676,
                "mode": "speaking", "approved": True, "segments": [{
                    "index": 0, "start_sample": 0, "end_sample": 359676,
                    "generation_frames": 209, "text": "final words",
                    "prompt": "Existing brief", "refs": [], "material_note": "",
                }]}
        snapshot = director.build_input_snapshot(plan)
        result = valid_response(snapshot)
        duration = 359676 / 44100
        result["segments"][0]["actions"] = [{
            "start_seconds": 0.0, "end_seconds": round(duration, 3),
            "description": "Hold the final expression.",
        }]
        validated = director.validate_director_result(result, snapshot)
        self.assertEqual(validated["segments"][0]["actions"][0]["end_seconds"], duration)

    def test_bad_json_fences_duplicate_keys_unknown_keys_and_nonfinite_are_rejected(self):
        for value in ("not json", "```json\n{}\n```", "{}", '[]',
                      '{"schema_version":1,"schema_version":1}',
                      '{"schema_version":NaN}', json.dumps(self.result)+" trailing"):
            with self.subTest(value=value), self.assertRaises(director.DirectorValidationError):
                director.parse_director_response(value, self.snapshot)
        self.result["segments"][0]["duration"] = 11
        with self.assertRaises(director.DirectorValidationError):
            director.validate_director_result(self.result, self.snapshot)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = director.build_input_snapshot(sample_plan(), source_fingerprint="source-v1")
        self.result = valid_response(self.snapshot)

    def test_valid_response_is_a_draft_not_approval_or_applied_prompts(self):
        calls = []
        def complete(**kwargs):
            calls.append(kwargs)
            return json.dumps(valid_fill(self.snapshot))
        draft = director.generate_director_draft(self.snapshot, complete,
                                                model_config={"model": "existing.gguf"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["max_tokens"], 8192)
        self.assertFalse(calls[0]["enable_thinking"])
        self.assertEqual(draft["status"], "draft")
        self.assertFalse(draft["approved"])
        self.assertEqual(draft["input_snapshot"], self.snapshot)
        self.assertEqual(draft["model_config"], {"model": "existing.gguf"})
        self.assertNotIn("prompt", draft)
        self.assertEqual(len(draft["attempts"]), 1)
        self.assertEqual(calls[0]["messages"][1]["content"], self.snapshot["model_message"])
        self.assertIn("全片模式：口播", calls[0]["messages"][1]["content"])
        self.assertNotIn("Existing brief", calls[0]["messages"][1]["content"])
        self.assertNotIn("The user message is a compact reviewed timeline", calls[0]["messages"][0]["content"])

    def test_legacy_snapshot_without_model_message_rebuilds_the_same_text(self):
        legacy = copy.deepcopy(self.snapshot)
        legacy.pop("model_message", None)
        legacy.pop("model_message_fingerprint", None)
        calls = []

        def complete(**kwargs):
            calls.append(kwargs)
            return json.dumps(valid_fill(self.snapshot))

        director.generate_director_draft(legacy, complete)
        self.assertEqual(calls[0]["messages"][1]["content"], self.snapshot["model_message"])

    def test_empty_field_gets_one_targeted_repair_without_rewriting_the_film(self):
        calls = []
        filled = valid_fill(self.snapshot)
        filled["segments"]["S1"]["intent"] = ""
        def complete(**kwargs):
            calls.append(kwargs)
            return json.dumps(filled if len(calls) == 1 else {
                "segments.S1.intent": "讲清开场要点。"})
        draft = director.generate_director_draft(self.snapshot, complete)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["response_schema"]["required"], ["segments.S1.intent"])
        self.assertIn("仅补填", calls[1]["messages"][-1]["content"])
        self.assertEqual(calls[0]["messages"][:2], calls[1]["messages"][:2])
        self.assertEqual(len(draft["attempts"]), 2)
        self.assertIn("validation_error", draft["attempts"][0])
        self.assertEqual(draft["result"]["segments"][1]["brief"],
                         filled["segments"]["S2"]["brief"])
        self.assertEqual(draft["result"]["segments"][0]["intent"], "讲清开场要点。")

    def test_one_english_field_repairs_only_that_field_and_keeps_brand_names(self):
        filled = valid_fill(self.snapshot)
        filled["segments"]["S1"]["brief"] = "Introduce Qwen naturally."
        filled["segments"]["S2"]["brief"] = "介绍 Qwen 的新能力，保持自然姿态。"
        calls = []
        def complete(**kwargs):
            calls.append(kwargs)
            return json.dumps(filled if len(calls) == 1 else {
                "segments.S1.brief": "介绍 Qwen，轻轻点头。"})
        result = director.generate_director_draft(self.snapshot, complete)["result"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["response_schema"]["required"], ["segments.S1.brief"])
        self.assertEqual(result["segments"][1]["brief"], filled["segments"]["S2"]["brief"])

    def test_entire_english_fill_fails_without_whole_film_retry(self):
        def english(schema):
            return "Keep the pose." if schema["type"] == "string" else {
                key: english(value) for key, value in schema["properties"].items()}
        calls = []
        def complete(**kwargs):
            calls.append(kwargs)
            return json.dumps(english(kwargs["response_schema"]))
        with self.assertRaisesRegex(director.DirectorValidationError, "不自动重写全片"):
            director.generate_director_draft(self.snapshot, complete)
        self.assertEqual(len(calls), 1)

    def test_failed_repair_and_runtime_failure_do_not_modify_the_snapshot(self):
        before = copy.deepcopy(self.snapshot)
        calls = []
        filled = valid_fill(self.snapshot)
        filled["segments"]["S1"]["brief"] = ""
        def invalid(**kwargs):
            calls.append(kwargs)
            return json.dumps(filled) if len(calls) == 1 else "{}"
        with self.assertRaisesRegex(director.DirectorValidationError, "repair failed") as failure:
            director.generate_director_draft(self.snapshot, invalid)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(failure.exception.attempts), 2)
        self.assertTrue(all("validation_error" in item for item in failure.exception.attempts))
        self.assertEqual(failure.exception.attempts[0]["messages"], calls[0]["messages"])
        self.assertEqual(self.snapshot, before)
        for failure in (TimeoutError("timeout"), RuntimeError("context budget exceeded")):
            calls.clear()
            def broken(**kwargs):
                calls.append(kwargs)
                raise failure
            with self.assertRaises(type(failure)):
                director.generate_director_draft(self.snapshot, broken)
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.snapshot, before)

    def test_invalid_or_empty_frame_never_retries_the_whole_film(self):
        for text in ("not JSON", '{"segments":', "{}", '{"unexpected":1}',
                     '{"global_contract":"one","global_contract":"two"}'):
            calls = []
            def complete(**kwargs):
                calls.append(kwargs)
                return text
            with self.subTest(text=text), self.assertRaises(director.DirectorValidationError):
                director.generate_director_draft(self.snapshot, complete)
            self.assertEqual(len(calls), 1)

    def test_local_repair_cannot_overwrite_an_accepted_field(self):
        filled = valid_fill(self.snapshot)
        filled["segments"]["S1"]["brief"] = ""
        replies = iter([json.dumps(filled), json.dumps({
            "segments.S1.brief": "Fixed brief", "segments.S2.brief": "Unrequested change"})])
        with self.assertRaisesRegex(director.DirectorValidationError, "fixed-slot"):
            director.generate_director_draft(self.snapshot, lambda **kwargs: next(replies))

    def test_storage_marks_async_source_conflict_stale_and_preserves_source(self):
        first = director.draft_for_storage(self.snapshot, self.result,
                                            current_source_fingerprint="source-v1")
        stale = director.draft_for_storage(self.snapshot, self.result,
                                            current_source_fingerprint="source-v2")
        self.assertEqual(first["status"], "draft")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["source_fingerprint"], "source-v1")
        self.assertFalse(stale["approved"])
        self.assertEqual(stale["result"], first["result"])
        self.assertNotEqual(stale["id"], first["id"])

    def test_invalid_draft_cannot_be_saved_and_output_budget_is_bounded(self):
        self.result["segments"].clear()
        with self.assertRaises(director.DirectorValidationError):
            director.draft_for_storage(self.snapshot, self.result)
        with self.assertRaises(director.DirectorValidationError):
            director.generate_director_draft(self.snapshot, lambda **kwargs: "{}", max_output_tokens=99999)


class FixedSlotTests(unittest.TestCase):
    def snapshot(self, cues=None):
        plan = sample_plan()
        plan["segments"][0]["speech_cues"] = cues if cues is not None else [
            {"cue_index": 0, "start_seconds": 0.08, "end_seconds": 4.12,
             "text": "第一条完整台词。"},
            {"cue_index": 1, "start_seconds": 4.54, "end_seconds": 9.98,
             "text": "第二条完整台词。"},
        ]
        return director.build_input_snapshot(plan)

    def test_fixed_slots_cover_gaps_and_tail_without_a_second_call(self):
        snapshot = self.snapshot()
        calls = []
        def complete(**kwargs):
            calls.append(kwargs)
            return json.dumps(valid_fill(snapshot))
        draft = director.generate_director_draft(snapshot, complete)
        beats = draft["result"]["segments"][0]["beats"]
        self.assertEqual(len(calls), 1)
        self.assertEqual([beat["speech_cue_indices"] for beat in beats], [[], [0], [], [1], []])
        self.assertEqual(beats[0]["start_seconds"], 0)
        self.assertEqual(beats[-1]["end_seconds"], 10)
        self.assertTrue(all(left["end_seconds"] == right["start_seconds"]
                            for left, right in zip(beats, beats[1:])))
        self.assertTrue(all(beat["camera"] == "locked-off" for beat in beats))
        timeline = director.render_h3_timeline(draft["result"]["segments"][0],
                    {**snapshot["source"]["segments"][0], "sample_rate": 100})
        self.assertNotIn("台词：", timeline)
        self.assertNotIn("<d>", timeline)
        self.assertNotIn("speech_cue_indices", json.dumps(calls[0]["response_schema"]))

    def test_motion_budget_downgrades_excess_gestures_without_second_call(self):
        snapshot = self.snapshot()
        filled = valid_fill(snapshot)
        for segment in filled["segments"].values():
            for cue in segment["cues"].values():
                cue["motion"] = director.MOTION_MINIMAL_GESTURE
                cue["shot_description"] = "轻轻抬手一次，随后保持姿态。"
        calls = []

        def complete(**kwargs):
            calls.append(kwargs)
            return json.dumps(filled, ensure_ascii=False)

        result = director.generate_director_draft(snapshot, complete)["result"]
        beats = [beat for row in result["segments"] for beat in row["beats"]]
        gestures = [beat for beat in beats
                    if beat["motion"] == director.MOTION_MINIMAL_GESTURE]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(gestures), 1)  # 20 seconds of source => one gesture.
        self.assertTrue(all("轻轻抬手" not in beat["shot_description"]
                            for beat in beats if beat["motion"] == director.MOTION_HOLD))
        self.assertTrue(all(beat["shot_description"] == director.HOLD_SHOT_DESCRIPTION
                            for beat in beats if beat["motion"] == director.MOTION_HOLD))
        self.assertEqual(
            [beat["speech_cue_indices"] for beat in result["segments"][0]["beats"]],
            [[], [0], [], [1], []],
        )

    def test_motion_strategy_is_visible_and_round_trips_through_timeline(self):
        snapshot = self.snapshot()
        result = director.generate_director_draft(
            snapshot, lambda **kwargs: json.dumps(valid_fill(snapshot), ensure_ascii=False))
        segment = copy.deepcopy(result["result"]["segments"][0])
        segment["beats"][1]["motion"] = director.MOTION_MINIMAL_GESTURE
        segment["beats"][1]["shot_description"] = "轻轻抬手一次。"
        source = {**snapshot["source"]["segments"][0], "sample_rate": 100}
        timeline = director.render_h3_timeline(segment, source, "speaking")
        self.assertIn("动作策略：单次轻微手势", timeline)
        parsed = director.parse_h3_timeline(timeline, source, "speaking")
        self.assertEqual(parsed["beats"][1]["motion"], director.MOTION_MINIMAL_GESTURE)
        self.assertEqual(parsed["beats"][0]["motion"], director.MOTION_HOLD)

    def test_disallowed_or_compound_minimal_gesture_is_downgraded_locally(self):
        snapshot = self.snapshot()
        filled = valid_fill(snapshot)
        first = filled["segments"]["S1"]["cues"]["C0"]
        first.update(motion=director.MOTION_MINIMAL_GESTURE,
                     shot_description="挥手并指向镜头。")
        calls = []
        result = director.generate_director_draft(
            snapshot,
            lambda **kwargs: calls.append(kwargs) or json.dumps(filled, ensure_ascii=False),
        )["result"]
        beat = next(item for item in result["segments"][0]["beats"]
                    if item["speech_cue_indices"] == [0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(beat["motion"], director.MOTION_HOLD)
        self.assertEqual(beat["shot_description"], director.HOLD_SHOT_DESCRIPTION)

    def test_authored_motion_policy_rejects_two_gestures_in_one_segment(self):
        beat = {
            "motion": director.MOTION_MINIMAL_GESTURE,
            "shot_description": "轻轻抬手一次。",
            "pose": "人物居中站立。",
        }
        with self.assertRaisesRegex(director.DirectorValidationError, "第 1 段"):
            director.assert_motion_policy(
                [{"beats": [copy.deepcopy(beat), copy.deepcopy(beat)]}],
                "speaking", 60)

    def test_timeline_validation_uses_confirmed_content_duration_without_row_sample_rate(self):
        snapshot = self.snapshot()
        result = director.generate_director_draft(
            snapshot, lambda **kwargs: json.dumps(valid_fill(snapshot)))
        source = dict(snapshot["source"]["segments"][0])
        source.pop("sample_rate", None)
        timeline = director.render_h3_timeline(result["result"]["segments"][0], source)
        parsed = director.parse_h3_timeline(timeline, source)
        self.assertEqual(parsed["beats"][-1]["end_seconds"], 10)

    def test_legacy_dialogue_rows_are_ignored_when_parsing_authored_timeline(self):
        snapshot = self.snapshot()
        result = director.generate_director_draft(
            snapshot, lambda **kwargs: json.dumps(valid_fill(snapshot)))
        source = {**snapshot["source"]["segments"][0], "sample_rate": 100}
        timeline = director.render_h3_timeline(result["result"]["segments"][0], source)
        authored = timeline.replace(
            "画面：", "台词：<d>[Chinese]旧台词。</d>\n画面：", 1)
        parsed = director.parse_h3_timeline(authored, source)
        self.assertEqual(len(parsed["beats"]), len(result["result"]["segments"][0]["beats"]))
        self.assertNotIn("台词", director.strip_timeline_dialogue(authored))

    def test_empty_cues_use_local_hold_without_invented_silence(self):
        snapshot = self.snapshot([])
        result = director.generate_director_draft(
            snapshot, lambda **kwargs: json.dumps(valid_fill(snapshot)))
        beat = result["result"]["segments"][0]["beats"][0]
        self.assertEqual(beat["speech_cue_indices"], [])
        self.assertEqual((beat["start_seconds"], beat["end_seconds"]), (0, 10))
        self.assertEqual(beat["shot_description"], director.HOLD_SHOT_DESCRIPTION)
        self.assertNotIn("闭唇", beat["shot_description"])

    def test_overlapping_cues_fail_before_inference(self):
        snapshot = self.snapshot()
        snapshot["source"]["segments"][0]["speech_cues"][1]["start_seconds"] = 3
        def forbidden(**kwargs):
            self.fail("invalid local timing must not start Qwen")
        with self.assertRaisesRegex(director.DirectorValidationError, "overlap"):
            director.generate_director_draft(snapshot, forbidden)


if __name__ == "__main__":
    unittest.main()
