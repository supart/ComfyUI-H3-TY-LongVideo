import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from test_plugin import core, controller, nodes
from h3lv_test import clip_project


class ClipProjectTests(unittest.TestCase):
    def plan(self):
        return core.decorate({
            "id": "a"*32, "revision": 3, "created": 1, "project_kind": "shot_planner",
            "mode": "speaking", "max_seconds": 10, "target_seconds": 5,
            "sample_rate": 48000, "samples": 192000, "duration": 4,
            "fixed_prompt": "global", "generation_megapixels": .5,
            "approved": True, "run_status": "completed", "final_video": "old.mp4",
            "source_video": "source/source.mp4",
            "source_video_metadata": {"width": 96, "height": 160, "source_fps": 24, "duration": 4},
            "reference_library": ["ref.png"],
            "reference_defaults": [{"slot": 0, "name": "ref.png"}],
            "segments": [
                {"start_sample": 0, "end_sample": 96000, "prompt": "global", "prompt_kind": "final",
                 "job": {"status": "completed", "video": "old.mp4"}},
                {"start_sample": 96000, "end_sample": 192000, "prompt": "manual", "prompt_kind": "final",
                 "refs": ["ref.png"], "needs_regeneration": True, "takes": [{"video": "old.mp4"}]},
            ]}, regenerate_prompts=False)

    def test_bounds_reject_invalid_or_stale_requests(self):
        plan = self.plan()
        for start, end in ((-1, 2), (1, 5), (2, 2), (3, 2), (float("nan"), 2), (1, 1.001)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                clip_project.clip_bounds(plan, {
                    "revision": 3, "start_seconds": start, "end_seconds": end})
        with self.assertRaisesRegex(ValueError, "变化"):
            clip_project.clip_bounds(plan, {"revision": 2})
        with self.assertRaisesRegex(ValueError, "只有"):
            clip_project.clip_bounds({**plan, "project_kind": "long_video"}, {"revision": 3})
        self.assertEqual(clip_project.clip_bounds(plan, {
            "revision": 3, "start_seconds": 1.01, "end_seconds": 3.01}), (24, 72))

    def test_transcript_clips_and_rebases_words(self):
        result = clip_project.shifted_transcript({
            "segments": [{"start": .5, "end": 2.5, "text": "hello",
                          "words": [{"start": .5, "end": 1.5, "word": "hello"}]}],
            "words": [{"start": 3, "end": 4, "word": "outside"}]}, 1, 3)
        self.assertEqual(result["segments"][0]["start"], 0)
        self.assertEqual(result["segments"][0]["end"], 1.5)
        self.assertEqual(result["segments"][0]["words"][0]["end"], .5)
        self.assertEqual(result["words"], [])

    @unittest.skipUnless(controller.media_tool("ffmpeg") and controller.media_tool("ffprobe"), "FFmpeg unavailable")
    def test_real_clip_preserves_parent_and_assembles_only_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)/"projects"
            root.mkdir()
            parent = self.plan()
            directory = core.project_path(root, parent["id"])
            for folder in ("audio", "state", "source", "refs"):
                (directory/folder).mkdir(parents=True)
            mix = np.column_stack([np.linspace(-.5, .5, parent["samples"], dtype=np.float32)]*2)
            sf.write(core.audio_file(directory, "source.wav"), mix, 48000, subtype="FLOAT")
            sf.write(core.audio_file(directory, "vocals.wav"), mix*.5, 48000, subtype="FLOAT")
            (directory/"refs"/"ref.png").write_bytes(b"reference-content")
            (directory/"state"/"transcript.json").write_text(json.dumps({
                "segments": [{"start": 0, "end": 2, "text": "first"},
                             {"start": 2, "end": 4, "text": "second"}]}), encoding="utf-8")
            controller.command([controller.media_tool("ffmpeg"), "-v", "error", "-n",
                "-f", "lavfi", "-i", "testsrc2=size=96x160:rate=24:duration=4",
                "-i", str(core.audio_file(directory, "source.wav")), "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-t", "4", str(directory/"source"/"source.mp4")])
            core.write_plan(root, parent)
            before = {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in directory.rglob("*") if path.is_file()}
            payload = {"revision": 3, "start_seconds": 1, "end_seconds": 3}
            child = clip_project.create_clip_project(root, parent["id"], payload)
            stored = core.project_path(root, child["id"])
            self.assertFalse(child["approved"])
            self.assertNotIn("final_video", child)
            self.assertEqual(child["duration"], 2)
            self.assertEqual([row["edit_frames"] for row in child["segments"]], [24, 24])
            self.assertEqual([row["prompt"] for row in child["segments"]], ["global", "manual"])
            self.assertEqual(child["segments"][1]["refs"], ["ref.png"])
            self.assertEqual((stored/"refs"/"ref.png").read_bytes(), b"reference-content")
            for row in child["segments"]:
                self.assertTrue({"job", "takes", "needs_regeneration"}.isdisjoint(row))
            cropped, rate = sf.read(core.audio_file(stored, "source.wav"), dtype="float32")
            np.testing.assert_array_equal(cropped, mix[48000:144000])
            self.assertEqual(rate, 48000)
            self.assertEqual(int(controller.probe_video(stored/"source"/"source.mp4")["nb_read_frames"]), 48)
            core.edit_plan(child, [{"end": end} for end in (.5, 1, 2)], stored)
            self.assertEqual(len(child["segments"]), 3)
            core.edit_plan(child, [{"end": end} for end in (.5, 2)], stored)
            self.assertEqual([row["edit_frames"] for row in child["segments"]], [12, 36])
            for row in child["segments"]:
                take = stored/"takes"/f"{row['index']}.mp4"
                controller.command([controller.media_tool("ffmpeg"), "-v", "error", "-n",
                    "-ss", str(row["start"]), "-i", str(stored/"source"/"source.mp4"),
                    "-frames:v", str(row["edit_frames"]), "-an", "-c:v", "libx264", str(take)])
                row["job"] = {"status": "completed", "video": str(take)}
            core.write_plan(root, child)
            final = Path(controller.assemble(root, child["id"]))
            info = controller.probe_video(final)
            self.assertEqual(int(info["nb_read_frames"]), 48)
            self.assertEqual(info["r_frame_rate"], "24/1")
            self.assertEqual(before, {
                str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in directory.rglob("*") if path.is_file()})
            existing = set(root.iterdir())
            with patch.object(clip_project, "command", side_effect=RuntimeError("encode failed")):
                with self.assertRaisesRegex(RuntimeError, "encode failed"):
                    clip_project.create_clip_project(root, parent["id"], payload)
            self.assertEqual(set(root.iterdir()), existing)
            tiny = clip_project.create_clip_project(root, parent["id"], {
                "revision": 3, "start_seconds": 1, "end_seconds": 1+1/24})
            self.assertEqual(tiny["segments"][0]["edit_frames"], 1)


class SourceClipTests(unittest.TestCase):
    def test_bounds_and_paths(self):
        self.assertEqual(clip_project.source_clip_bounds(40, {
            "start_seconds": 1.01, "end_seconds": 30.01}), (24, 720))
        for start, end in ((-1, 2), (1, 41), (2, 2), (float("inf"), 4), (1, 1.001)):
            with self.assertRaises(ValueError):
                clip_project.source_clip_bounds(40, {"start_seconds": start, "end_seconds": end})
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, {
                "folder_paths": types.SimpleNamespace(get_input_directory=lambda: temporary)}):
            for value in ("../private.mp4", "C:/private.mp4", "", None):
                with self.assertRaises(ValueError):
                    clip_project.preview_source({"source_video": value})

    @unittest.skipUnless(controller.media_tool("ffmpeg") and controller.media_tool("ffprobe"), "FFmpeg unavailable")
    def test_source_clip_waveform_audio_and_analysis_only_use_selected_range(self):
        import torch
        with tempfile.TemporaryDirectory() as temporary, patch.dict(sys.modules, {
                "folder_paths": types.SimpleNamespace(get_input_directory=lambda: temporary)}):
            root = Path(temporary)
            source = root / "中文 视频.mp4"
            sample_rate = 48000
            time_axis = np.arange(4*sample_rate)/sample_rate
            audio = np.column_stack([.4*np.sin(2*np.pi*220*time_axis)*(1+time_axis/4)]*2)
            sf.write(root/"original.wav", audio, sample_rate, subtype="FLOAT")
            controller.command([controller.media_tool("ffmpeg"), "-v", "error", "-n",
                "-f", "lavfi", "-i", "testsrc2=size=96x160:rate=30:duration=4",
                "-i", str(root/"original.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-t", "4", str(source)])
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            payload = {"source_video": source.name, "start_seconds": 1, "end_seconds": 3}
            with patch.object(nodes.Analyze, "analyze", side_effect=AssertionError("preview ran ASR")):
                preview = clip_project.preview_source(payload)
                result = clip_project.clip_source(payload)
            self.assertEqual(preview["duration"], 4)
            self.assertEqual(preview["source_fps"], 30)
            self.assertEqual(len(preview["waveform"]["original"]), 200)
            cropped = nodes.source_video_path(result["source_video"])
            self.assertEqual(result["frames"], 48)
            self.assertEqual(controller.probe_video(cropped)["r_frame_rate"], "24/1")
            self.assertEqual(result["duration"], 2)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            for label, path in (("before", source), ("after", cropped)):
                controller.command([controller.media_tool("ffmpeg"), "-v", "error", "-n",
                    "-i", str(path), "-vn", "-ar", str(sample_rate), "-ac", "2",
                    "-c:a", "pcm_f32le", str(root/f"{label}.wav")])
            original_audio, _ = sf.read(root/"before.wav", dtype="float32", always_2d=True)
            clip_audio, _ = sf.read(root/"after.wav", dtype="float32", always_2d=True)
            self.assertAlmostEqual(len(clip_audio)/sample_rate, 2, delta=.03)
            np.testing.assert_allclose(clip_audio[2000:94000], original_audio[50000:142000], atol=.045)
            # Execute the actual ShotPlanner source path with only expensive inference replaced.
            projects = root/"projects"
            plan = ClipProjectTests().plan()
            plan.update(id="b"*32, sample_rate=sample_rate, samples=96000, duration=2,
                        segments=[{"start_sample": 0, "end_sample": 96000, "prompt": "global", "prompt_kind": "final"}])
            directory = core.project_path(projects, plan["id"])
            (directory/"state").mkdir(parents=True)
            core.write_plan(projects, plan)
            captured = []
            def analyze(selected_audio, *args):
                self.assertIsInstance(selected_audio["waveform"], torch.Tensor)
                captured.append(selected_audio["waveform"].shape[-1])
                return {"result": (plan["id"],), "ui": {"ty_h3lv_project": [plan["id"]]}}
            with patch.object(nodes, "data_root", return_value=projects), \
                 patch.object(nodes.Analyze, "analyze", side_effect=analyze):
                nodes.ShotPlanner().process(result["source_video"], "speaking", 10, 8, .5, "global")
            saved = core.read_plan(projects, plan["id"])
            self.assertEqual(captured, [96000])
            self.assertEqual(saved["source_video_input"], result["source_video"])
            self.assertEqual(saved["source_video_metadata"]["duration"], 2)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
            existing = set((root/"TYH3LongVideo"/"clips").iterdir())
            with patch.object(clip_project, "command", side_effect=RuntimeError("encode failed")):
                with self.assertRaisesRegex(RuntimeError, "encode failed"):
                    clip_project.clip_source(payload)
            self.assertEqual(set((root/"TYH3LongVideo"/"clips").iterdir()), existing)
            tiny = clip_project.clip_source({**payload, "end_seconds": 1+1/24})
            self.assertEqual(tiny["frames"], 1)
            silent = root/"silent.mp4"
            controller.command([controller.media_tool("ffmpeg"), "-v", "error", "-n",
                "-i", str(source), "-an", "-c:v", "copy", str(silent)])
            with self.assertRaisesRegex(ValueError, "没有音轨"):
                clip_project.preview_source({"source_video": silent.name})


if __name__ == "__main__":
    unittest.main()
