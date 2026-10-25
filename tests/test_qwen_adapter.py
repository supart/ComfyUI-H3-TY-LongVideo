import builtins
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "h3lv_qwen_adapter_test",
    ROOT / "qwen_adapter.py",
)
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)
runtime_spec = importlib.util.spec_from_file_location(
    "h3lv_runtime_test",
    ROOT / "runtime.py",
)
runtime = importlib.util.module_from_spec(runtime_spec)
sys.modules[runtime_spec.name] = runtime
runtime_spec.loader.exec_module(runtime)


class Lock:
    def __init__(self):
        self.events = []

    def __enter__(self):
        self.events.append("enter")
        return self

    def __exit__(self, *_args):
        self.events.append("exit")


class FakeServer:
    def __init__(self):
        self.calls = []
        self.requests = []
        self.token_count = 100
        self.context_size = 32768

    def _request(self, method, path, payload=None, timeout=None):
        self.requests.append((method, path, payload, timeout))
        if path == "/apply-template":
            return {"prompt": str(payload["messages"])}
        if path == "/tokenize":
            return {"tokens": list(range(self.token_count))}
        if path == "/props":
            return {"default_generation_settings": {"n_ctx": self.context_size}}
        raise AssertionError(path)

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return '{"ok":true}', {"total_tokens": 1}


class FakeManager:
    def __init__(self, server):
        self.server = server
        self.calls = []
        self.releases = 0

    def acquire(self, model, mmproj, context_size):
        self.calls.append((model, mmproj, context_size))
        return self.server, False

    def release(self):
        self.releases += 1


class StandaloneManager(FakeManager):
    def __init__(self):
        super().__init__(FakeServer())


def standalone_setup(directory):
    models_dir = Path(directory) / "models"
    model_root = models_dir / "LLM" / "Qwen3.8"
    model_root.mkdir(parents=True)
    (model_root / "Qwen3.8-27B-UD-Q4_K_M.gguf").write_bytes(b"model")
    (model_root / "mmproj-F16.gguf").write_bytes(b"projector")
    runtime = types.SimpleNamespace(
        load_runtime_spec=lambda: object(),
        LlamaServerManager=StandaloneManager,
    )
    return models_dir, model_root, runtime


def fake_qwen_module(root, lock, manager):
    module = types.ModuleType("fake_qwen.node")
    module.MODEL_DIR = root
    module.INFERENCE_LOCK = lock
    module.SERVER_MANAGER = manager
    module.DEFAULT_MODEL = "model.gguf"
    module.DEFAULT_MMPROJ = "mmproj.gguf"

    def resolve_model(name):
        path = (root / name).resolve()
        if path.parent != root.resolve() or not path.is_file():
            raise FileNotFoundError(name)
        return path

    module._resolve_model = resolve_model
    module._sampling_settings = lambda thinking: {
        "temperature": 1.0 if thinking else 0.7,
        "top_p": 0.95 if thinking else 0.8,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0 if thinking else 1.5,
        "repetition_penalty": 1.0,
    }
    node_class = type("QwenH3PromptLocal", (), {"__module__": module.__name__})
    module.QwenH3PromptLocal = node_class
    return module, node_class


class DiscoveryTests(unittest.TestCase):
    def test_import_has_no_optional_runtime_dependencies(self):
        result = subprocess.run([sys.executable, "-S", "-c", (
            "import runpy,sys; runpy.run_path(sys.argv[1]); "
            "assert not any(n in sys.modules for n in "
            "('nodes','torch','comfy','folder_paths','fake_qwen.node'))"
        ), str(ROOT/"qwen_adapter.py")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unavailable_comfyui_is_optional_and_does_not_import_qwen(self):
        with patch.dict(sys.modules, {"folder_paths": None}), self.assertRaises(
            adapter.QwenAdapterUnavailable
        ):
            adapter.discover_qwen_runtime()
        self.assertNotIn("qwen_adapter.node", sys.modules)

    def test_standalone_discovery_uses_shared_ud_model_without_qwen_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            models_dir, model_root, runtime = standalone_setup(directory)
            original_import = builtins.__import__

            def guarded_import(name, *args, **kwargs):
                if name in {"nodes", "torch"} or "ComfyUI_Qwen_H3_Prompt" in name:
                    raise AssertionError(f"standalone path imported {name}")
                return original_import(name, *args, **kwargs)

            with patch.dict(sys.modules, {
                "folder_paths": types.SimpleNamespace(models_dir=str(models_dir))
            }), patch.object(adapter, "_runtime_module", return_value=runtime), \
                 patch.object(adapter, "_STANDALONE_MANAGER", None), \
                 patch("builtins.__import__", guarded_import):
                binding = adapter.discover_qwen_runtime()
                config = adapter.default_model_config()
            self.assertIsNone(binding.node_class)
            self.assertEqual(binding.module.MODEL_DIR, model_root.resolve())
            self.assertEqual(
                config["llm_model"], "Qwen3.8-27B-UD-Q4_K_M.gguf")
            self.assertEqual(config["vision_model"], "mmproj-F16.gguf")
            self.assertEqual(config["context_size"], 32768)

    def test_discovery_does_not_wait_for_inference_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            models_dir, _model_root, runtime = standalone_setup(directory)
            result = []
            with patch.dict(sys.modules, {
                "folder_paths": types.SimpleNamespace(models_dir=str(models_dir))
            }), patch.object(adapter, "_runtime_module", return_value=runtime), \
                 patch.object(adapter, "_STANDALONE_MANAGER", None):
                adapter._STANDALONE_LOCK.acquire()
                try:
                    worker = threading.Thread(
                        target=lambda: result.append(adapter.discover_qwen_runtime()))
                    worker.start()
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
                finally:
                    adapter._STANDALONE_LOCK.release()
                    worker.join(timeout=1)
            self.assertEqual(len(result), 1)

    def test_standalone_adapter_completes_with_shared_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            models_dir, _model_root, runtime = standalone_setup(directory)
            with patch.dict(sys.modules, {
                "folder_paths": types.SimpleNamespace(models_dir=str(models_dir))
            }), patch.object(adapter, "_runtime_module", return_value=runtime), \
                 patch.object(adapter, "_STANDALONE_MANAGER", None):
                config = adapter.default_model_config()
                client = adapter.QwenAdapter.from_registered(config)
                result = client.complete(
                    [{"role": "user", "content": "plan"}], max_tokens=128)
            self.assertEqual(result, '{"ok":true}')
            self.assertEqual(client.binding.server_manager.releases, 1)

    def test_missing_module_is_not_imported_and_incompatible_node_fails(self):
        missing_class = type("Missing", (), {"__module__": "absent_qwen.node"})
        with self.assertRaises(adapter.QwenAdapterUnavailable):
            adapter.discover_qwen_runtime({"QwenH3PromptLocal": missing_class})
        self.assertNotIn("absent_qwen.node", sys.modules)
        broken = types.ModuleType("broken_qwen.node")
        broken_class = type("Broken", (), {"__module__": broken.__name__})
        with patch.dict(sys.modules, {broken.__name__: broken}):
            with self.assertRaises(adapter.QwenAdapterUnavailable):
                adapter.discover_qwen_runtime({"QwenH3PromptLocal": broken_class})

    def test_registered_node_reuses_its_module_objects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.gguf").write_bytes(b"model")
            (root / "mmproj.gguf").write_bytes(b"projector")
            lock = Lock()
            server = FakeServer()
            manager = FakeManager(server)
            module, node_class = fake_qwen_module(root, lock, manager)
            with patch.dict(sys.modules, {module.__name__: module}):
                binding = adapter.discover_qwen_runtime(
                    {"QwenH3PromptLocal": node_class}
                )
                self.assertIs(binding.module, module)
                self.assertIs(binding.inference_lock, lock)
                self.assertIs(binding.server_manager, manager)

    def test_default_model_config_uses_registered_declarations_without_node_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = Lock()
            manager = FakeManager(FakeServer())
            module, node_class = fake_qwen_module(root, lock, manager)
            with patch.dict(sys.modules, {module.__name__: module}):
                config = adapter.default_model_config(
                    {"QwenH3PromptLocal": node_class})
            self.assertEqual(config["llm_model"], "model.gguf")
            self.assertEqual(config["vision_model"], "mmproj.gguf")
            self.assertFalse(config["think_mode"])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ("model.gguf", "mmproj.gguf"):
            (self.root/name).write_bytes(b"model")
        self.lock, self.server = Lock(), FakeServer()
        self.manager = FakeManager(self.server)
        self.module, self.node_class = fake_qwen_module(
            self.root, self.lock, self.manager)
        self.module_patch = patch.dict(sys.modules, {self.module.__name__: self.module})
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        self.config = {"llm_model": "model.gguf", "vision_model": "mmproj.gguf"}
        self.registry = {"QwenH3PromptLocal": self.node_class}
        self.client = adapter.QwenAdapter.from_registered(
            self.config, registry=self.registry)
        self.messages = [{"role": "user", "content": "full confirmed transcript"}]

    def test_complete_validates_models_and_uses_shared_server(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "model.gguf").write_bytes(b"model")
            (root / "mmproj.gguf").write_bytes(b"projector")
            lock = Lock()
            server = FakeServer()
            manager = FakeManager(server)
            module, node_class = fake_qwen_module(root, lock, manager)
            with patch.dict(sys.modules, {module.__name__: module}):
                client = adapter.QwenAdapter.from_registered(
                    {"llm_model": "model.gguf", "vision_model": "mmproj.gguf",
                     "seed": 17},
                    registry={"QwenH3PromptLocal": node_class},
                )
                result = client.complete(
                    [{"role": "user", "content": "plan"}],
                    max_tokens=128,
                )
            self.assertEqual(result, '{"ok":true}')
            self.assertEqual(lock.events, ["enter", "exit"])
            self.assertEqual(len(manager.calls), 1)
            self.assertIs(manager.calls[0][0], client.model)
            self.assertEqual(manager.calls[0][1], client.mmproj)
            self.assertEqual(manager.calls[0][2], 32768)
            kwargs = server.calls[0][1]
            self.assertEqual(kwargs["seed"], 17)
            self.assertFalse(kwargs["think_mode"])
            self.assertEqual(kwargs["max_tokens"], 128)
            self.assertEqual(kwargs["temperature"], 0.7)
            self.assertEqual(manager.releases, 1)
            self.assertEqual(client.last_usage, {"total_tokens": 1})
            self.assertEqual([request[1] for request in server.requests],
                             ["/apply-template", "/tokenize", "/props"])
            self.assertEqual(client.last_metadata["input_tokens"], 100)
            self.assertGreaterEqual(client.last_metadata["elapsed_seconds"], 0)

    def test_skill_router_and_original_node_execute_are_never_called(self):
        def forbidden(*args, **kwargs):
            raise AssertionError("Whole-film planning must not invoke H3 skill/node execution")
        for name in ("system_prompt", "skill_instructions", "router_prompt", "mode_router_prompt"):
            setattr(self.module, name, forbidden)
        self.node_class.execute = forbidden
        for skill in ("h3-prompt-writing", "another-skill"):
            self.module.selected_skill = skill
            self.client.complete(self.messages, max_tokens=256, enable_thinking=False)
        self.assertEqual(len(self.server.calls), 2)
        self.assertEqual(self.server.calls[0], self.server.calls[1])
        self.assertEqual(self.server.calls[0][0], self.messages)

    def test_missing_or_wrong_model_is_rejected_before_acquire(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "mmproj.gguf").write_bytes(b"projector")
            lock = Lock()
            manager = FakeManager(FakeServer())
            module, node_class = fake_qwen_module(root, lock, manager)
            with patch.dict(sys.modules, {module.__name__: module}):
                with self.assertRaises(adapter.QwenAdapterConfigurationError):
                    adapter.QwenAdapter.from_registered(
                        {"llm_model": "missing.gguf", "vision_model": "mmproj.gguf"},
                        registry={"QwenH3PromptLocal": node_class},
                    )
            self.assertEqual(manager.calls, [])

    def test_explicit_model_names_and_model_directory_are_required(self):
        for config in ({}, {"llm_model": "model.gguf"},
                       {"llm_model": "../outside.gguf", "vision_model": "mmproj.gguf"}):
            with self.subTest(config=config):
                with self.assertRaises(adapter.QwenAdapterConfigurationError):
                    adapter.QwenAdapter.from_registered(config, registry=self.registry)
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other)/"outside.gguf"
            outside.write_bytes(b"model")
            with self.assertRaises(adapter.QwenAdapterConfigurationError):
                adapter.QwenAdapter.from_registered(
                    {**self.config, "llm_model": str(outside)}, registry=self.registry)
        self.assertEqual(self.manager.calls, [])

    def test_context_preflight_includes_full_repair_and_stops_without_chat(self):
        phases = []
        self.client.on_phase = phases.append
        self.messages.extend([
            {"role": "assistant", "content": "invalid response"},
            {"role": "user", "content": "validation error: correct every segment"},
        ])
        self.server.token_count = 32700
        with self.assertRaisesRegex(adapter.QwenAdapterConfigurationError, "not truncated") as caught:
            self.client.complete(self.messages, max_tokens=128)
        self.assertEqual(caught.exception.context_budget, {
            "input_tokens": 32700, "output_budget": 128,
            "reserve_tokens": 8, "context_size": 32768})
        self.assertEqual(self.server.requests[0][2]["messages"], self.messages)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.manager.releases, 1)
        self.assertEqual(self.lock.events, ["enter", "exit"])
        self.assertEqual(phases, ["waiting_model", "loading_model", "checking_context"])

    def test_phases_follow_real_model_operations(self):
        phases = []
        self.client.on_phase = phases.append
        self.client.complete(self.messages, max_tokens=128)
        self.assertEqual(phases, ["waiting_model", "loading_model", "checking_context", "generating"])

    def test_failure_and_cancellation_release_inside_shared_lock(self):
        for error in (TimeoutError("timed out"), KeyboardInterrupt("cancelled")):
            def release():
                self.assertEqual(self.lock.events[-1], "enter")
                self.manager.releases += 1
            with self.subTest(error=error), \
                 patch.object(self.server, "chat", side_effect=error), \
                 patch.object(self.manager, "release", side_effect=release):
                with self.assertRaises(type(error)):
                    self.client.complete(self.messages, max_tokens=128)
        self.assertEqual(self.manager.releases, 2)
        self.assertEqual(self.lock.events, ["enter", "exit"] * 2)

    def test_comfy_memory_hooks_and_interrupt_keep_original_cleanup_hook(self):
        events = []
        management = types.SimpleNamespace(
            unload_all_models=lambda: events.append("unload"),
            soft_empty_cache=lambda: events.append("empty_cache"),
            throw_exception_if_processing_interrupted=lambda: events.append("interrupt_check"))
        def release():
            self.assertEqual(self.lock.events[-1], "enter")
            self.manager.release()
            events.append("release")
        self.module._release_node_resources = release
        with patch.dict(sys.modules, {"comfy.model_management": management}):
            self.client.complete(self.messages, max_tokens=128)
        self.assertEqual(events[:3], ["interrupt_check", "unload", "empty_cache"])
        self.assertEqual(events[-3:], ["release", "unload", "empty_cache"])
        self.assertEqual(self.manager.releases, 1)

    def test_cancel_after_acquire_never_calls_chat_and_releases(self):
        checks = iter((False, True))
        def interrupted():
            if next(checks):
                raise RuntimeError("ComfyUI interrupted")
        management = types.SimpleNamespace(
            unload_all_models=lambda: None, soft_empty_cache=lambda: None,
            throw_exception_if_processing_interrupted=interrupted)
        with patch.dict(sys.modules, {"comfy.model_management": management}):
            with self.assertRaisesRegex(RuntimeError, "ComfyUI interrupted"):
                self.client.complete(self.messages, max_tokens=128)
        self.assertEqual(len(self.manager.calls), 1)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.manager.releases, 1)

    def test_acquire_failure_releases_and_preserves_original_exception(self):
        with patch.object(self.manager, "acquire", side_effect=RuntimeError("load failed")):
            with self.assertRaisesRegex(RuntimeError, "load failed"):
                self.client.complete(self.messages, max_tokens=128)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(self.manager.releases, 1)

    def test_legacy_registry_path_and_director_callable_contract(self):
        from test_ai_director import director, sample_plan, valid_fill
        snapshot = director.build_input_snapshot(sample_plan())
        import json
        with patch.dict(sys.modules, {
            "nodes": types.SimpleNamespace(NODE_CLASS_MAPPINGS=self.registry)
        }), patch.object(self.server, "chat", return_value=(
            json.dumps(valid_fill(snapshot)), {"total_tokens": 10}
        )):
            client = adapter.QwenAdapter.from_registered(
                self.config, registry=self.registry)
            draft = director.generate_director_draft(snapshot, client.complete)
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(len(draft["attempts"]), 1)
        self.assertEqual(self.manager.releases, 1)

    def test_schema_and_stage_timings_are_preserved_without_changing_sampling(self):
        schema = {"type": "object", "properties": {}}
        self.client.complete(self.messages, max_tokens=128, response_schema=schema)
        self.assertEqual(self.server.calls[0][1]["response_schema"], schema)
        self.assertEqual(self.server.calls[0][1]["temperature"], 0.7)
        for name in ("load_seconds", "preflight_seconds", "inference_seconds", "unload_seconds"):
            self.assertGreaterEqual(self.client.last_metadata[name], 0)
        self.assertEqual(self.manager.releases, 1)


class RuntimeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.server = runtime.LlamaServer.__new__(runtime.LlamaServer)
        self.requests = []

        def request(method, path, payload=None, timeout=900.0):
            self.requests.append((method, path, payload, timeout))
            return {
                "choices": [{
                    "message": {"content": '{"segments":{}}'},
                    "finish_reason": "stop",
                }],
                "usage": {"completion_tokens": 4},
                "timings": {"predicted_per_second": 2.0},
            }

        self.server._request = request

    def test_schema_is_sent_to_chat_completion_without_changing_sampling(self):
        schema = {
            "type": "object",
            "properties": {"segments": {"type": "object"}},
            "required": ["segments"],
        }
        result, usage = self.server.chat(
            [{"role": "user", "content": "fill"}],
            seed=42, max_tokens=8192, temperature=.7, top_p=.8, top_k=20,
            min_p=0.0, presence_penalty=1.5, repetition_penalty=1.0,
            think_mode=False, reasoning_effort="medium", response_schema=schema)
        payload = self.requests[0][2]
        self.assertEqual(result, '{"segments":{}}')
        self.assertEqual(usage, {"completion_tokens": 4})
        self.assertEqual(payload["response_format"], {
            "type": "json_object", "schema": schema})
        self.assertEqual(payload["temperature"], .7)
        self.assertEqual(payload["seed"], 42)
        self.assertEqual(self.server.last_completion_metadata["finish_reason"], "stop")

    def test_schema_truncation_is_reported_without_returning_partial_json(self):
        self.server._request = lambda *args, **kwargs: {
            "choices": [{
                "message": {"content": '{"segments":'},
                "finish_reason": "length",
            }],
            "usage": {"completion_tokens": 8192},
        }
        with self.assertRaisesRegex(RuntimeError, "no automatic whole-film retry"):
            self.server.chat(
                [{"role": "user", "content": "fill"}],
                seed=42, max_tokens=8192, temperature=.7, top_p=.8, top_k=20,
                min_p=0.0, presence_penalty=1.5, repetition_penalty=1.0,
                think_mode=False, reasoning_effort="medium",
                response_schema={"type": "object"})


if __name__ == "__main__":
    unittest.main()
