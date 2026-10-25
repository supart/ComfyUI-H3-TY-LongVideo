"""Compatibility checks for the explicitly selected segment prompt kind."""
import copy
import hashlib
import json
import tempfile
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from test_plugin import core, nodes, sample_plan


class PromptKindTests(unittest.TestCase):
    def test_legacy_and_explicit_brief_have_identical_fingerprints(self):
        plan = sample_plan()
        explicit = copy.deepcopy(plan)
        for row in plan["segments"]:
            row.pop("prompt_kind", None)
        for row in explicit["segments"]:
            row["prompt_kind"] = "brief"
        self.assertEqual(core.fingerprint(plan), core.fingerprint(explicit))
        for before, after in zip(plan["segments"], explicit["segments"]):
            self.assertEqual(core.segment_fingerprint(before), core.segment_fingerprint(after))
        legacy = [{"start_sample": row["start_sample"], "end_sample": row["end_sample"],
                   "prompt": row["prompt"]} for row in plan["segments"]]
        self.assertEqual(core.fingerprint(explicit),
                         hashlib.sha256(json.dumps(legacy, ensure_ascii=False).encode()).hexdigest())

    def test_prompt_kind_changes_both_fingerprints(self):
        plan = sample_plan()
        row = plan["segments"][0]
        original = core.fingerprint(plan), core.segment_fingerprint(row)
        row["prompt_kind"] = "final"
        self.assertNotEqual(original[0], core.fingerprint(plan))
        self.assertNotEqual(original[1], core.segment_fingerprint(row))
        row["prompt_kind"] = "brief"
        self.assertEqual(original, (core.fingerprint(plan), core.segment_fingerprint(row)))

    def test_final_is_exact_text_without_material_prefix_or_format_inference(self):
        text = "\n  User-authored final prompt.\n镜头方案：one field is literal text\n  "
        row = {"prompt_kind": "final", "prompt": text, "material_note": "This is not a prefix."}
        self.assertEqual(core.validate_segment_prompt(row), text)
        self.assertEqual(core.brief_text(row), text)
        brief = {"prompt": "subject_definitions:\nA complete prompt is still an explicit brief.",
                 "material_note": "Keep this legacy note."}
        self.assertEqual(core.brief_text(brief),
                         "素材说明：Keep this legacy note.\n" + brief["prompt"])

    def test_invalid_kinds_and_empty_final_are_rejected(self):
        for value in ("auto", "FINAL", 1, [], {}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.normalize_prompt_kind(value)
        with self.assertRaises(ValueError):
            core.validate_segment_prompt({"prompt_kind": "final", "prompt": " \n"})

    def test_decorate_never_regenerates_an_explicit_final(self):
        plan = sample_plan()
        row = plan["segments"][0]
        row.update(prompt_kind="final", prompt="  Keep the exact authored prompt. \n")
        original = row["prompt"]
        core.decorate(plan, regenerate_prompts=True)
        self.assertEqual(row["prompt"], original)
        self.assertEqual(row["prompt_kind"], "final")

    def test_boundary_edit_preserves_final_and_invalidates_approval(self):
        plan = sample_plan()
        row = plan["segments"][0]
        row.update(prompt_kind="final", prompt="  Keep the original prompt.\n")
        plan["approved"] = True
        original = row["prompt"]
        submitted = [{"end": item["end"], "prompt": item["prompt"]}
                     for item in plan["segments"]]
        submitted[0]["end"] = 9
        core.edit_plan(plan, submitted)
        self.assertEqual(row["prompt"], original)
        self.assertEqual(row["prompt_kind"], "final")
        self.assertFalse(plan["approved"])
        self.assertIn(0, plan["changed_segments"])
        self.assertTrue(any("最终提示词已保留" in warning for warning in row["warnings"]))

    def test_explicit_kind_edit_is_not_inferred_or_reset_by_legacy_client(self):
        plan = sample_plan()
        submitted = [{"end": row["end"], "prompt": row["prompt"]}
                     for row in plan["segments"]]
        submitted[1].update(prompt_kind="final", prompt="Explicit final.")
        core.edit_plan(plan, submitted)
        self.assertEqual(plan["segments"][1]["prompt_kind"], "final")
        legacy = [{"end": row["end"], "prompt": row["prompt"]}
                  for row in plan["segments"]]
        core.edit_plan(plan, legacy)
        self.assertEqual(plan["segments"][1]["prompt_kind"], "final")
        self.assertEqual(plan["segments"][1]["prompt"], "Explicit final.")

    def test_loader_outputs_exact_final_and_keeps_five_output_slots(self):
        import soundfile as sf
        with tempfile.TemporaryDirectory() as root, patch.object(nodes, "data_root", return_value=root):
            plan = sample_plan()
            plan["approved"] = True
            row = plan["segments"][0]
            row.update(prompt_kind="final", prompt=" \nExact final text.\n ",
                       material_note="Must not be prefixed.")
            directory = core.project_path(root, plan["id"])
            (directory / "audio").mkdir(parents=True)
            audio = np.zeros((plan["samples"], 2), dtype=np.float32)
            for name in ("source.wav", "vocals.wav"):
                sf.write(core.audio_file(directory, name), audio, plan["sample_rate"])
            core.write_plan(root, plan)
            fake_torch = types.SimpleNamespace(from_numpy=lambda value: MagicMock())
            with patch.dict(sys.modules, {"torch": fake_torch}):
                result = nodes.LoadSegment().load(plan["id"], 0)
            self.assertEqual(len(result), 5)
            self.assertEqual(result[2], row["prompt"])


if __name__ == "__main__":
    unittest.main()
