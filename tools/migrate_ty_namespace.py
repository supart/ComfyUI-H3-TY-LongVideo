"""Copy LongVideo workflows or data into the TY namespace without editing sources."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


LEGACY_NODE_TYPES = {
    "H3LVUnified": "TY_H3LVUnified",
    "H3LVDirectorTask": "TY_H3LVDirectorTask",
}
LEGACY_REPOSITORY = "Zzz1phyrf/ComfyUI-H3-LongVideo"
TY_REPOSITORY = "supart/ComfyUI-H3-TY-LongVideo"
LEGACY_STORAGE = "H3LongVideo"
TY_STORAGE = "TYH3LongVideo"
LEGACY_MIRROR = "H3LV"
TY_MIRROR = "TYH3LV"

PATH_KEYS = {
    "filename_prefix",
    "final_video",
    "final_versions",
    "video",
    "subfolder",
    "image",
    "path",
    "reference_path",
}


def _path_value(value: str) -> str:
    replacements = (
        (f"{LEGACY_STORAGE}/", f"{TY_STORAGE}/"),
        (f"{LEGACY_STORAGE}\\", f"{TY_STORAGE}\\"),
        (f"/{LEGACY_STORAGE}/", f"/{TY_STORAGE}/"),
        (f"\\{LEGACY_STORAGE}\\", f"\\{TY_STORAGE}\\"),
        (f"{LEGACY_MIRROR}/", f"{TY_MIRROR}/"),
        (f"{LEGACY_MIRROR}\\", f"{TY_MIRROR}\\"),
        (f"/{LEGACY_MIRROR}/", f"/{TY_MIRROR}/"),
        (f"\\{LEGACY_MIRROR}\\", f"\\{TY_MIRROR}\\"),
    )
    for old, new in replacements:
        if old in value:
            value = value.replace(old, new)
    return value


def _migrate(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        result = {}
        for name, child in value.items():
            target_name = f"ty_{name}" if name.startswith("h3lv_") else name
            if target_name in result and target_name != name:
                continue
            result[target_name] = _migrate(child, target_name)
        return result
    if isinstance(value, list):
        return [_migrate(child, key) for child in value]
    if isinstance(value, str):
        if key in {"class_type", "type", "Node name for S&R"}:
            return LEGACY_NODE_TYPES.get(value, value)
        if key == "aux_id" and value == LEGACY_REPOSITORY:
            return TY_REPOSITORY
        if key == "title":
            return {
                "H3 长视频 · 音频分析与顺序生成": "TY H3 长视频 · 音频分析与顺序生成",
                "H3 长视频 · Qwen 全片导演任务": "TY H3 长视频 · Qwen 全片导演任务",
            }.get(value, value)
        if key in {"ty_h3lv_note", "ty_h3lv_manual_test", "runtime_test"}:
            return (value.replace("H3LVUnified", "TY_H3LVUnified")
                         .replace("H3LVDirectorTask", "TY_H3LVDirectorTask")
                         .replace("H3LongVideo", "TYH3LongVideo"))
        if key in PATH_KEYS or any(marker in value for marker in (
                LEGACY_STORAGE, LEGACY_MIRROR)):
            return _path_value(value)
    return value


def migrate_json(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}")
    payload = json.loads(source.read_text(encoding="utf-8"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(_migrate(payload), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def migrate_tree(source: Path, destination: Path) -> None:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_dir():
        raise NotADirectoryError(source)
    if destination.exists():
        raise FileExistsError(f"Refusing to overwrite {destination}")
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.suffix.casefold() == ".json":
                migrate_json(item, target)
            else:
                shutil.copy2(item, target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    workflow = subparsers.add_parser("workflow", help="copy and migrate one JSON workflow")
    workflow.add_argument("source", type=Path)
    workflow.add_argument("destination", type=Path)

    tree = subparsers.add_parser("tree", help="copy a directory and migrate JSON files")
    tree.add_argument("source", type=Path)
    tree.add_argument("destination", type=Path)

    args = parser.parse_args()
    if args.command == "workflow":
        migrate_json(args.source, args.destination)
    else:
        migrate_tree(args.source, args.destination)
    print(f"Migrated {args.source} -> {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
