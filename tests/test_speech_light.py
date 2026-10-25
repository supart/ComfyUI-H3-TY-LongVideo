import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from test_plugin import ROOT, controller, core, director_rules, nodes, sample_plan


class SpeechLightTests(unittest.TestCase):
    def plan(self, count=8, **controls):
        plan = sample_plan()
        plan.update(mode="speech_light", samples=count * 1000, duration=count * 10)
        plan["segments"] = [
            {"start_sample": i * 1000, "end_sample": (i + 1) * 1000,
             "energy_db": -40 + (i % 3) * 20, "text": "演讲"}
            for i in range(count)]
        plan["director"].update(controls)
        return core.decorate(plan)

    def test_mode_is_available_without_reordering_original_choices(self):
        self.assertEqual(nodes.Unified.INPUT_TYPES()["required"]["mode"][0],
                         ["singing", "speaking", "speech_light"])
        script = (ROOT / "web/ty-h3lv.js").read_text(encoding="utf-8")
        self.assertIn('speech_light: "居中演讲"', script)
        self.assertIn("modeLabels[project.mode]", script)
        self.assertIn("fullConfig[selectedMode]", script)

    def test_old_config_and_selected_mode_reset_preserve_user_rules(self):
        legacy = director_rules.default_config()
        legacy.pop("speech_light")
        legacy["singing"]["allowed_framings"] = ["medium shot"]
        legacy["speaking"]["framing"] = "medium shot"
        original = copy.deepcopy(legacy)
        normalized = director_rules.validate_config(legacy)
        self.assertEqual(legacy, original)
        self.assertEqual(normalized["singing"], legacy["singing"])
        self.assertEqual(normalized["speaking"], legacy["speaking"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "director_config.json"
            path.write_text(json.dumps(legacy), encoding="utf-8")
            raw = path.read_bytes()
            public = director_rules.public_rules(directory)
            self.assertEqual(path.read_bytes(), raw)
            config = json.loads(public["config_text"])
            config["speech_light"]["framing"] = "medium close-up"
            saved = director_rules.write_rules(directory, {"config_text": json.dumps(config)})
            self.assertEqual(json.loads(saved["config_text"])["speaking"], original["speaking"])
            reset = json.loads(director_rules.reset_rules(directory, "speech_light")["config_text"])
            self.assertEqual(reset["singing"], original["singing"])
            self.assertEqual(reset["speaking"], original["speaking"])
            self.assertEqual(reset["speech_light"], normalized["speech_light"])

    def test_each_allowed_framing_stays_fixed_across_all_segments(self):
        for framing in ("medium close-up", "medium shot"):
            config = director_rules.default_config()
            config["speech_light"]["framing"] = framing
            states = core.camera_sequence("speech_light", [{}] * 12, {"rule_config": config})
            self.assertTrue(all(row["camera_start"] == row["camera_end"] == framing for row in states))
            self.assertTrue(all(row["camera_move_type"] == "steady" for row in states))

    def test_invalid_speech_framings_and_movement_are_rejected(self):
        for key, value in (
            ("framing", "close-up"), ("framing", "full shot"), ("framing", ""),
            ("angle", "front three-quarter right"), ("angle", ""),
            ("movement", "dolly_in"), ("movement", "dolly_out"),
            ("movement", "arc_left"), ("movement", "truck_right"),
            ("movement", "micro_reframe"),
        ):
            with self.subTest(key=key, value=value):
                config = director_rules.default_config()
                config["speech_light"][key] = value
                with self.assertRaises(ValueError):
                    director_rules.validate_config(config)
        for invalid in (None, [], "steady"):
            config = director_rules.default_config()
            config["speech_light"] = invalid
            with self.assertRaises(ValueError):
                director_rules.validate_config(config)

    def test_default_camera_is_centered_bounded_and_continuous(self):
        plan = self.plan()
        rows = plan["segments"]
        self.assertEqual([row["camera_move_type"] for row in rows], ["steady"] * 8)
        for index, row in enumerate(rows):
            self.assertIn(row["camera_start"], {"medium close-up", "medium shot"})
            self.assertIn(row["camera_end"], {"medium close-up", "medium shot"})
            self.assertEqual(row["camera_start"], row["camera_end"])
            self.assertEqual(row["camera_start_angle"], "front")
            self.assertEqual(row["camera_end_angle"], "front")
            self.assertEqual(row["entry_motion_state"], "still")
            self.assertEqual(row["exit_motion_state"], "still")
            self.assertIn("centered", row["composition_anchor"])
            self.assertIn("without walking, lateral drift", row["composition_anchor"])
            self.assertIn("人物始终固定在画面中央", row["prompt"])
            self.assertIn("模式：居中演讲（speech_light）", row["prompt"])
            self.assertIn("禁止推进、拉远、变焦", row["prompt"])
            self.assertEqual(core.validate_segment_brief(row["prompt"]), row["prompt"].strip())
            if index:
                self.assertEqual(row["camera_start"], rows[index - 1]["camera_end"])
            self.assertIn("locked-off", row["camera_move"])
            self.assertIn("no dolly, zoom", row["camera_move"])

    def test_singing_controls_cannot_introduce_closeups_or_dynamic_motion(self):
        expected = self.plan()["segments"]
        for activity in ("auto", "moderate", "dynamic"):
            for widest in ("close-up", "medium close-up", "medium shot", "full shot"):
                actual = self.plan(camera_activity=activity, widest_framing=widest)["segments"]
                self.assertEqual(actual, expected)

    def test_legacy_camera_entry_point_is_not_treated_as_singing(self):
        self.assertEqual(core.camera_plan("speech_light", 0, 4, 0, 0)[:2],
                         ("medium shot", "medium shot"))
        self.assertIn("centered", core.camera_plan("speech_light", 0, 4, 0, 0)[2])

    def test_references_and_h3_prompt_describe_centered_speech_not_singing(self):
        plan = self.plan()
        references = core.normalize_reference_manifest(plan)
        self.assertIn("说话参考", core.reference_brief_lines(references, "speech_light"))
        self.assertIn("服装与可见穿戴配饰沿用参考图", plan["segments"][0]["prompt"])
        for row in plan["segments"]:
            prompt = core.compose_h3_prompt(plan, row)
            self.assertIn("spoken delivery", prompt)
            self.assertIn("<Subject 1> speaks", prompt)
            self.assertIn("centered on the same vertical screen axis", prompt)
            self.assertIn("no dolly, zoom", prompt)
            self.assertNotIn("singing performance", prompt)
            self.assertNotIn("music-driven", prompt)
            core.validate_h3_prompt(prompt, references)
        template = (ROOT / "ref2va_performance_rule.txt").read_text(encoding="utf-8")
        self.assertIn("Speech_light mode only:", template)
        self.assertIn("without walking, lateral drift or body repositioning", template)

    def test_audio_analysis_keeps_original_audio_and_skips_singing_separation(self):
        audio = np.zeros((600, 2), dtype=np.float32)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(nodes, "audio_array", return_value=(audio, 100)), \
                 patch.object(nodes, "data_root", return_value=root / "projects"), \
                 patch.object(nodes, "rules_path", return_value=root / "rules"), \
                 patch.object(nodes, "run_asr", return_value={}), \
                 patch.object(nodes, "separate") as separate:
                result = nodes.Analyze().analyze(
                    {}, "speech_light", 15, 11, "", "", camera_activity="dynamic",
                    widest_framing="full shot")
            separate.assert_not_called()
            plan = core.read_plan(root / "projects", result["result"][0])
            self.assertEqual(plan["mode"], "speech_light")
            self.assertTrue(plan["sample_coverage_verified"])
            self.assertIn("人物始终固定在画面中央", plan["segments"][0]["prompt"])
            self.assertEqual(plan["segments"][0]["camera_start"], "medium shot")

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg unavailable")
    def test_assembly_labels_new_mode_without_reclassifying_as_singing(self):
        import soundfile as sf
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "TYH3LongVideo/projects"
            plan = self.plan(count=1)
            plan.update(sample_rate=48000, samples=48000, duration=1)
            plan["segments"][0].update(start_sample=0, end_sample=48000)
            core.decorate(plan)
            project = core.project_path(root, plan["id"])
            core.audio_file(project, "source.wav").parent.mkdir(parents=True)
            sf.write(core.audio_file(project, "source.wav"), np.zeros((48000, 2)), 48000)
            path = project / "takes" / "test.mp4"
            path.parent.mkdir()
            controller.command([shutil.which("ffmpeg"), "-v", "error", "-f", "lavfi",
                                "-i", "testsrc2=s=160x96:r=24", "-frames:v",
                                str(plan["segments"][0]["generation_frames"]),
                                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
            plan["segments"][0]["job"] = {"status": "completed", "video": str(path)}
            core.write_plan(root, plan)
            final = controller.assemble(root, plan["id"])
            self.assertIn("_speech_light_", Path(final).name)
            self.assertEqual(int(controller.probe_video(final)["nb_read_frames"]), 24)


if __name__ == "__main__":
    unittest.main()
