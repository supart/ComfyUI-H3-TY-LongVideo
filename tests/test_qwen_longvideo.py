"""LongVideo-owned Qwen controls and multimodal path tests."""

import importlib
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from test_plugin import nodes


adapter = importlib.import_module("h3lv_test.qwen_adapter")
engine = importlib.import_module("h3lv_test.qwen_h3_engine")


class Lock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeServer:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.requests = []

    def _request(self, method, path, payload=None, timeout=None):
        self.requests.append((method, path, payload, timeout))
        if path == "/apply-template":
            return {"prompt": "formatted"}
        if path == "/tokenize":
            return {"tokens": [1, 2, 3]}
        if path == "/props":
            return {"default_generation_settings": {"n_ctx": 32768}}
        raise AssertionError(path)

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return next(self.responses), {"total_tokens": 1}


class FakeManager:
    def __init__(self, server):
        self.server = server
        self.releases = 0

    def acquire(self, model, mmproj, context_size):
        return self.server, False

    def release(self):
        self.releases += 1


class QwenLongVideoTests(unittest.TestCase):
    def test_node_exposes_longvideo_model_controls_in_appended_order(self):
        required = nodes.Unified.INPUT_TYPES()["required"]
        names = list(required)
        appended = [
            "skill", "duration", "llm_model", "vision_model", "think_mode",
            "reasoning_effort", "seed", "max_tokens",
            "video_sample_frames_per_sec", "force_unload_model",
        ]
        self.assertEqual(names[-len(appended):], appended)
        self.assertEqual(
            required["llm_model"][1]["default"],
            "Qwen3.8-27B-UD-Q4_K_M.gguf",
        )
        self.assertEqual(required["vision_model"][1]["default"], "mmproj-F16.gguf")

    def test_config_rejects_skill_and_truncated_or_unsafe_model_names(self):
        base = {
            "llm_model": "Qwen3.8-27B-UD-Q4_K_M.gguf",
            "vision_model": "mmproj-F16.gguf",
        }
        with self.assertRaises(adapter.QwenAdapterConfigurationError):
            adapter.normalize_qwen_config({**base, "skill": "h3-prompt-writing"})
        for field, value in (
            ("llm_model", "Qwen3.8-27B-UD-Q4_K_"),
            ("llm_model", "Qwen3.8-27B-UD-Q4_K_M.gguf"),
            ("vision_model", "../mmproj-F16.gguf"),
        ):
            config = dict(base)
            config[field] = value
            if value == base[field]:
                with tempfile.TemporaryDirectory() as directory:
                    config[field] = str(Path(directory) / value)
                    with self.assertRaises(adapter.QwenAdapterConfigurationError):
                        adapter.normalize_qwen_config(config)
            else:
                with self.assertRaises(adapter.QwenAdapterConfigurationError):
                    adapter.normalize_qwen_config(config)

    def test_mode_recognition_is_local_and_does_not_route_skills(self):
        self.assertEqual(engine._detect_mode(0, 0), "t2va")
        self.assertEqual(engine._detect_mode(3, 0), "ref2va")
        self.assertIsNone(engine._detect_mode(1, 0))
        self.assertIsNone(engine._detect_mode(2, 0))
        self.assertEqual(engine._parse_mode_selection("i2va", 1), "i2va")
        self.assertEqual(engine._parse_mode_selection("unknown", 1), "ref2va")
        self.assertIn("图像输入模式", engine._mode_router_messages("request", 1)[0]["content"])

    def test_internal_output_is_chinese_but_h3_tokens_and_authored_dialogue_are_unchanged(self):
        prompt = "[Shot 1] 连续镜头\nAt 00:00.000–00:01.000\n台词：<d>[Chinese]原文 Qwen</d>\n素材说明：原始内容"
        instruction = engine._h3_output_instruction("ref2va", 2, 0, prompt)
        self.assertIn("简体中文", instruction)
        self.assertIn("subject_definitions:", instruction)
        self.assertIn("<d>[Chinese]原文 Qwen</d>", instruction)
        self.assertNotIn("素材说明", engine._timeline_contract(prompt))
        english = ("integrated_multimodal_description:\n[Shot 1] At 00:00.000–00:01.000 "
                   "(S1) speaks <d>[Chinese]原文 Qwen</d>\n"
                   "overall_soundscape: Audio\nnon_diegetic_music: N/A")
        self.assertIn("台词以外的自然语言描述必须使用简体中文。",
                      engine._validate_h3_output(english, prompt, "t2va", 0, 0))
        chinese = english.replace("(S1) speaks", "(S1) 保持居中，自然讲述")
        self.assertEqual(engine._validate_h3_output(chinese, prompt, "t2va", 0, 0), [])

    def test_direct_engine_uses_thinking_sampling_and_repairs_empty_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            projector = root / "mmproj.gguf"
            model.write_bytes(b"model")
            projector.write_bytes(b"projector")
            valid_repair = (
                "integrated_multimodal_description:\n"
                "[Shot 1] 首帧与 <Picture 1> 对齐，人物保持已批准的连续姿态。\n"
                "overall_soundscape: 由提供的音频驱动表演。\n"
                "non_diegetic_music: 无"
            )
            server = FakeServer(["i2va", "", valid_repair])
            manager = FakeManager(server)
            released = []
            module = types.SimpleNamespace(
                MODEL_DIR=root,
                _resolve_model=lambda name: root / name,
                _release_node_resources=lambda: released.append(True),
            )
            binding = types.SimpleNamespace(
                module=module, inference_lock=Lock(), server_manager=manager)
            config = {
                "skill": "long_video_director",
                "llm_model": model.name,
                "vision_model": projector.name,
                "think_mode": True,
                "reasoning_effort": "xhigh",
                "seed": 123,
                "max_tokens": 1024,
                "video_sample_frames_per_sec": 2,
                "force_unload_model": True,
            }
            fake_image = types.SimpleNamespace(shape=(1, 8, 8, 3))
            with patch.object(engine.qwen_adapter, "discover_qwen_runtime",
                              return_value=binding), \
                 patch.object(engine, "image_content",
                              return_value={"type": "image_url"}):
                result = engine.QwenH3Engine(config).generate(
                    "Approved segment direction",
                    images=[fake_image], videos=[], duration=11.25)
            self.assertEqual(result, valid_repair)
            self.assertEqual(len(server.calls), 3)
            self.assertFalse(server.calls[0][1]["think_mode"])
            self.assertEqual(server.calls[0][1]["max_tokens"], 16)
            self.assertTrue(server.calls[1][1]["think_mode"])
            self.assertEqual(server.calls[1][1]["reasoning_effort"], "xhigh")
            self.assertEqual(server.calls[1][1]["seed"], 123)
            self.assertTrue(server.calls[2][1]["think_mode"])
            self.assertEqual(manager.releases, 0)
            self.assertEqual(len(released), 1)


if __name__ == "__main__":
    unittest.main()
