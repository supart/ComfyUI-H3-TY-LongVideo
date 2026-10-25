import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

from test_plugin import core, nodes


def shot_plan(ends=(4, 8, 12)):
    start = 0
    rows = []
    for index, end in enumerate(ends):
        rows.append({"start_sample": start*48000, "end_sample": end*48000,
                     "prompt": "old segment", "prompt_kind": "final", "refs": [],
                     "job": {"status": "completed", "video": f"old-{index}.mp4"}})
        start = end
    return core.decorate({
        "id": "a"*32, "revision": 2, "created": 1, "project_kind": "shot_planner",
        "mode": "speaking", "max_seconds": 10, "target_seconds": 6,
        "sample_rate": 48000, "samples": ends[-1]*48000, "duration": ends[-1],
        "fixed_prompt": "global A", "planning_method": "rules",
        "approved": True, "run_status": "completed", "final_video": "old-final.mp4",
        "segments": rows,
    }, regenerate_prompts=False)


def updates(ends):
    return [{"end": end, "prompt": "ignored client prompt"} for end in ends]


class ShotEditTests(unittest.TestCase):
    def test_split_merge_and_reindex_reuse_only_exact_intervals(self):
        plan = shot_plan()
        core.edit_plan(plan, updates((4, 6, 8, 12)))
        self.assertEqual(plan["changed_segments"], [1, 2])
        self.assertEqual(plan["segments"][3]["job"]["video"], "old-2.mp4")
        self.assertNotIn("job", plan["segments"][1])
        self.assertTrue(plan["final_stale"])
        self.assertFalse(plan["approved"])
        core.edit_plan(plan, updates((6, 8, 12)))
        self.assertEqual(plan["segments"][2]["job"]["video"], "old-2.mp4")
        self.assertNotIn("job", plan["segments"][0])
        self.assertEqual(sum(row["edit_frames"] for row in plan["segments"]), 288)

    def test_merge_inherits_left_references_and_accepts_more_than_auto_limit(self):
        plan = shot_plan((6, 12, 18))
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "refs").mkdir()
            for name in ("left.png", "right.png"):
                (directory / "refs" / name).write_bytes(b"reference")
            plan["segments"][0]["refs"] = ["left.png"]
            plan["segments"][1]["refs"] = ["right.png"]
            core.edit_plan(plan, updates((12, 18)), directory)
            self.assertEqual(plan["segments"][0]["refs"], ["left.png"])
            self.assertEqual(plan["segments"][0]["duration"], 12)
            self.assertEqual(plan["max_seconds"], 10)
            self.assertEqual(core.preview_bounds(plan, 0, 0, 12), (0, 576000))

    def test_validation_is_atomic_and_running_edits_are_rejected(self):
        for ends in ((0, 12), (8, 4, 12), (float("nan"), 12), (4, 11), (16,)):
            plan = shot_plan((8, 16)) if ends == (16,) else shot_plan()
            before = copy.deepcopy(plan)
            with self.subTest(ends=ends), self.assertRaises(ValueError):
                core.edit_plan(plan, updates(ends))
            self.assertEqual(plan, before)
        for status in ("running", "pausing", "stopping", "merging"):
            plan = shot_plan()
            plan["run_status"] = status
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "停止"):
                core.edit_plan(plan, updates((6, 12)))
        plan = shot_plan()
        plan["segments"][0]["job"]["status"] = "queued"
        with self.assertRaisesRegex(ValueError, "停止"):
            core.edit_plan(plan, updates((6, 12)))

    def test_new_cuts_align_to_frames_and_allow_one_frame(self):
        plan = shot_plan()
        core.edit_plan(plan, updates((1/24, 1.02, 4, 8, 12)))
        self.assertEqual(plan["segments"][0]["edit_frames"], 1)
        self.assertEqual(plan["segments"][1]["end_sample"], 48000)
        self.assertEqual(sum(row["edit_frames"] for row in plan["segments"]), 288)
        ordinary = shot_plan()
        ordinary["project_kind"] = "long_video"
        with self.assertRaisesRegex(ValueError, "首版"):
            core.edit_plan(ordinary, updates((6, 12)))

    def test_latest_global_prompt_bypasses_legacy_segment_and_matches_both_outputs(self):
        import torch
        plan = shot_plan((4,))
        plan.update(source_video="source/source.mp4", generation_megapixels=.01,
                    source_video_metadata={"width": 64, "height": 64})
        plan["segments"][0]["prompt"] = ""  # Legacy per-segment text must not be validated or used.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / plan["id"]
            (directory / "source").mkdir(parents=True)
            (directory / "audio").mkdir()
            (directory / "source" / "source.mp4").write_bytes(b"source")
            for name in ("source.wav", "vocals.wav"):
                sf.write(directory / "audio" / name, np.zeros((192000, 2), dtype=np.float32), 48000)
            with patch.object(nodes, "data_root", return_value=root), \
                    patch.object(nodes, "read_plan", return_value=plan), \
                    patch.object(nodes, "decode_segment_frames", return_value=torch.zeros(96, 96, 96, 3)):
                for incoming, expected in (("global B", "global B"), ("  ", "global A")):
                    result = nodes.load_shot_segment(plan["id"], 0, (None,)*6, incoming)
                    self.assertEqual(result[2], expected)
                    self.assertEqual(result[-1]["prompt"], expected)
                plan["fixed_prompt"] = ""
                with self.assertRaisesRegex(ValueError, "fixed_prompt"):
                    nodes.load_shot_segment(plan["id"], 0, (None,)*6, "")


if __name__ == "__main__":
    unittest.main()
