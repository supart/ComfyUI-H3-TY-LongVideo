import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ty_namespace_migration", ROOT / "tools" / "migrate_ty_namespace.py")
MIGRATION = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MIGRATION)


class TyNamespaceMigrationTests(unittest.TestCase):
    def test_workflow_copy_changes_only_namespace_fields(self):
        payload = {
            "nodes": [{
                "type": "H3LVUnified",
                "properties": {"h3lv_project": "abc", "h3lv_qwen_source": "254"},
                "_meta": {"title": "H3 长视频 · 音频分析与顺序生成"},
                "widgets_values": ["H3LongVideo/projects/abc/takes/seg_0000"],
            }],
            "prompt": {
                "1": {"class_type": "H3LVDirectorTask", "inputs": {
                    "image": "H3LV/abc/person.png",
                    "filename_prefix": ["H3LongVideo/projects/abc/takes/seg_0000", 4],
                }},
            },
            "extra": {"aux_id": "Zzz1phyrf/ComfyUI-H3-LongVideo",
                      "h3lv_note": "Use H3LVUnified.",
                      "h3lv_manual_test": {"runtime_test": "Use H3LVUnified."}},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            destination = root / "copy.json"
            source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            before = source.read_bytes()

            MIGRATION.migrate_json(source, destination)

            self.assertEqual(source.read_bytes(), before)
            migrated = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(migrated["nodes"][0]["type"], "TY_H3LVUnified")
            self.assertEqual(migrated["nodes"][0]["properties"]["ty_h3lv_project"], "abc")
            self.assertEqual(migrated["nodes"][0]["properties"]["ty_h3lv_qwen_source"], "254")
            self.assertEqual(migrated["nodes"][0]["_meta"]["title"], "TY H3 长视频 · 音频分析与顺序生成")
            self.assertEqual(migrated["prompt"]["1"]["class_type"], "TY_H3LVDirectorTask")
            self.assertEqual(migrated["prompt"]["1"]["inputs"]["image"], "TYH3LV/abc/person.png")
            self.assertEqual(
                migrated["prompt"]["1"]["inputs"]["filename_prefix"][0],
                "TYH3LongVideo/projects/abc/takes/seg_0000",
            )
            self.assertEqual(migrated["extra"]["aux_id"], "supart/ComfyUI-H3-TY-LongVideo")
            self.assertEqual(migrated["extra"]["ty_h3lv_note"], "Use TY_H3LVUnified.")
            self.assertEqual(migrated["extra"]["ty_h3lv_manual_test"]["runtime_test"],
                             "Use TY_H3LVUnified.")

    def test_absolute_output_paths_are_rebased_without_touching_source(self):
        payload = {
            "final_video": r"H:\ComfyUI\output\H3LongVideo\final_videos\clip.mp4",
            "final_versions": [r"H:\ComfyUI\output\H3LongVideo\final_videos\old.mp4"],
            "video": r"H:\ComfyUI\output\H3LongVideo\projects\abc\takes\seg.mp4",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.json"
            destination = root / "copy.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            before = source.read_bytes()
            MIGRATION.migrate_json(source, destination)
            migrated = json.loads(destination.read_text(encoding="utf-8"))
            self.assertEqual(source.read_bytes(), before)
            self.assertTrue(all("\\H3LongVideo\\" not in value
                                for value in [migrated["final_video"],
                                              migrated["final_versions"][0],
                                              migrated["video"]]))
            self.assertIn("\\TYH3LongVideo\\", migrated["final_video"])

    def test_tree_copy_preserves_non_json_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "destination"
            (source / "state").mkdir(parents=True)
            (source / "state" / "note.txt").write_bytes(b"keep")
            (source / "state" / "plan.json").write_text(
                json.dumps({"class_type": "H3LVUnified"}), encoding="utf-8")

            MIGRATION.migrate_tree(source, destination)

            self.assertEqual((destination / "state" / "note.txt").read_bytes(), b"keep")
            self.assertEqual(
                json.loads((destination / "state" / "plan.json").read_text(encoding="utf-8"))["class_type"],
                "TY_H3LVUnified",
            )


if __name__ == "__main__":
    unittest.main()
