"""Standalone text-only Qwen bridge for LongVideo storyboard planning.

The director owns model discovery and the llama.cpp runtime. The installed
Qwen H3 node remains an explicit compatibility path only; production calls do
not import its module, node registry, skill router, or model declarations.
"""
from dataclasses import dataclass
import importlib
import importlib.util
import logging
from pathlib import Path
import sys
import threading
import time
import types


NODE_ID = "QwenH3PromptLocal"
DEFAULT_CONTEXT_SIZE = 32768
QWEN_CONFIG_FIELDS = (
    "skill", "duration", "llm_model", "vision_model", "think_mode",
    "reasoning_effort", "seed", "max_tokens", "video_sample_frames_per_sec",
    "force_unload_model",
)
QWEN_CONFIG_DEFAULTS = {
    "skill": "long_video_director",
    "duration": 10.0,
    "llm_model": "",
    "vision_model": "",
    "think_mode": False,
    "reasoning_effort": "medium",
    "seed": 0,
    "max_tokens": 8192,
    "video_sample_frames_per_sec": 2,
    "force_unload_model": True,
}
LOGGER = logging.getLogger(__name__)
_STANDALONE_LOCK = threading.Lock()
_STANDALONE_MANAGER_LOCK = threading.Lock()
_STANDALONE_MANAGER = None


class QwenAdapterUnavailable(RuntimeError):
    """The optional registered Qwen node lacks the compatible runtime API."""


class QwenAdapterConfigurationError(ValueError):
    """The selected model or text request is invalid."""


@dataclass(frozen=True)
class QwenRuntimeBinding:
    node_class: type
    module: object
    inference_lock: object
    server_manager: object


def _runtime_module():
    try:
        if __package__:
            return importlib.import_module(f"{__package__}.runtime")
    except (ImportError, RuntimeError):
        pass
    path = Path(__file__).with_name("runtime.py")
    spec = importlib.util.spec_from_file_location("ty_h3lv_standalone_runtime", path)
    if spec is None or spec.loader is None:
        raise QwenAdapterUnavailable(f"LongVideo runtime module is missing: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _model_root():
    try:
        import folder_paths
    except (ImportError, AttributeError) as error:
        raise QwenAdapterUnavailable(
            "ComfyUI folder_paths is unavailable; restart ComfyUI before using Qwen storyboard planning."
        ) from error
    models_dir = getattr(folder_paths, "models_dir", None)
    if not models_dir:
        raise QwenAdapterUnavailable("ComfyUI models directory is unavailable.")
    return Path(models_dir).resolve() / "LLM" / "Qwen3.8"


def _select_model(root, projector):
    files = [path for path in root.glob("*.gguf") if path.is_file()]
    if projector:
        files = [path for path in files if "mmproj" in path.name.casefold()]
        preferred = ("mmproj-F16.gguf",)
        label = "vision_model"
    else:
        files = [path for path in files if "mmproj" not in path.name.casefold()]
        preferred = ("Qwen3.8-27B-UD-Q4_K_M.gguf", "Qwen3.8-27B-Q4_K_M.gguf")
        label = "llm_model"
    for name in preferred:
        candidate = root / name
        if candidate.is_file():
            return candidate.name
    if files:
        return max(files, key=lambda path: (path.stat().st_size, path.name.casefold())).name
    expected = ", ".join(preferred)
    raise QwenAdapterUnavailable(
        f"未找到 {label} GGUF。请将模型放入共享目录 {root}；优先文件名：{expected}。"
    )


def model_options(projector):
    """Return full GGUF filenames for the LongVideo node's combo widgets."""
    try:
        root = _model_root()
    except QwenAdapterUnavailable:
        return (("mmproj-F16.gguf",) if projector
                else ("Qwen3.8-27B-UD-Q4_K_M.gguf",))
    files = sorted(path.name for path in root.glob("*.gguf") if path.is_file())
    if projector:
        values = [name for name in files if "mmproj" in name.casefold()]
        fallback = "mmproj-F16.gguf"
    else:
        values = [name for name in files if "mmproj" not in name.casefold()]
        fallback = "Qwen3.8-27B-UD-Q4_K_M.gguf"
    return tuple(values or [fallback])


def default_model_name(projector):
    values = model_options(projector)
    preferred = ("mmproj-F16.gguf",) if projector else (
        "Qwen3.8-27B-UD-Q4_K_M.gguf", "Qwen3.8-27B-Q4_K_M.gguf")
    for name in preferred:
        if name in values:
            return name
    return values[0]


def _model_filename(value, label):
    if not isinstance(value, str) or not value.strip():
        raise QwenAdapterConfigurationError(
            f"Invalid or missing {label}: {value}")
    name = value.strip()
    if name != value or Path(name).name != name or "/" in name or "\\" in name:
        raise QwenAdapterConfigurationError(
            f"Invalid or missing {label}: {value}")
    if Path(name).suffix.casefold() != ".gguf":
        raise QwenAdapterConfigurationError(
            f"Invalid or missing {label}: {value}")
    return name


def normalize_qwen_config(config, *, defaults=None):
    """Validate the LongVideo-owned controls without importing an H3 Skill."""
    if not isinstance(config, dict):
        raise QwenAdapterConfigurationError("Qwen 配置必须是对象。")
    values = dict(QWEN_CONFIG_DEFAULTS)
    if isinstance(defaults, dict):
        values.update(defaults)
    values.update({key: config[key] for key in QWEN_CONFIG_FIELDS
                   if key in config})
    if "context_size" in config:
        values["context_size"] = config["context_size"]
    skill = values["skill"]
    if not isinstance(skill, str) or skill.strip() not in {"", "long_video_director"}:
        raise QwenAdapterConfigurationError(
            "LongVideo 只支持自己的 long_video_director 模式，不加载外部 H3 Skill。")
    values["skill"] = "long_video_director"
    duration = values["duration"]
    if (isinstance(duration, bool) or not isinstance(duration, (int, float))
            or not 1 <= float(duration) <= 60):
        raise QwenAdapterConfigurationError("duration 必须在 1 到 60 秒之间。")
    values["duration"] = float(duration)
    values["llm_model"] = _model_filename(values["llm_model"], "llm_model")
    values["vision_model"] = _model_filename(values["vision_model"], "vision_model")
    if type(values["think_mode"]) is not bool:
        raise QwenAdapterConfigurationError("think_mode 必须是布尔值。")
    if values["reasoning_effort"] not in {"low", "medium", "xhigh"}:
        raise QwenAdapterConfigurationError("reasoning_effort 不受支持。")
    if (type(values["seed"]) is not int
            or not 0 <= values["seed"] <= 0xFFFFFFFFFFFFFFFF):
        raise QwenAdapterConfigurationError("seed 必须是非负 64 位整数。")
    if type(values["max_tokens"]) is not int or not 256 <= values["max_tokens"] <= 8192:
        raise QwenAdapterConfigurationError("max_tokens 必须在 256 到 8192 之间。")
    if (type(values["video_sample_frames_per_sec"]) is not int
            or not 1 <= values["video_sample_frames_per_sec"] <= 8):
        raise QwenAdapterConfigurationError(
            "video_sample_frames_per_sec 必须在 1 到 8 之间。")
    if type(values["force_unload_model"]) is not bool:
        raise QwenAdapterConfigurationError("force_unload_model 必须是布尔值。")
    if "context_size" in config or "context_size" in values:
        context_size = values.get("context_size")
        if type(context_size) is not int or not 1024 <= context_size <= 131072:
            raise QwenAdapterConfigurationError("context_size 不受支持。")
        values["context_size"] = context_size
    return values


def _standalone_binding():
    root = _model_root()
    if not root.is_dir():
        raise QwenAdapterUnavailable(
            f"共享 Qwen 模型目录不存在：{root}。请创建目录并放入 GGUF 模型。"
        )
    runtime = _runtime_module()
    try:
        runtime.load_runtime_spec()
    except Exception as error:
        raise QwenAdapterUnavailable(str(error)) from error
    model = _select_model(root, projector=False)
    mmproj = _select_model(root, projector=True)
    global _STANDALONE_MANAGER
    with _STANDALONE_MANAGER_LOCK:
        if _STANDALONE_MANAGER is None:
            _STANDALONE_MANAGER = runtime.LlamaServerManager()
        manager = _STANDALONE_MANAGER

    def resolve_model(name):
        path = (root / str(name)).resolve()
        if path.parent != root or path.suffix.casefold() != ".gguf" or not path.is_file():
            raise FileNotFoundError(str(name))
        return path

    def release_resources():
        manager.release()
        management = sys.modules.get("comfy.model_management")
        empty = getattr(management, "soft_empty_cache", None)
        if callable(empty):
            try:
                empty(force=True)
            except TypeError:
                empty()

    module = types.SimpleNamespace(
        MODEL_DIR=root,
        DEFAULT_MODEL=model,
        DEFAULT_MMPROJ=mmproj,
        INFERENCE_LOCK=_STANDALONE_LOCK,
        SERVER_MANAGER=manager,
        _resolve_model=resolve_model,
        _sampling_settings=lambda thinking: {
            "temperature": 1.0 if thinking else 0.7,
            "top_p": 0.95 if thinking else 0.8,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": 0.0 if thinking else 1.5,
            "repetition_penalty": 1.0,
        },
        _release_node_resources=release_resources,
    )
    return QwenRuntimeBinding(None, module, _STANDALONE_LOCK, manager)


def _registered_binding(registry):
    """Compatibility for older queued projects with an explicit source ID."""

    if not isinstance(registry, dict):
        raise QwenAdapterUnavailable("ComfyUI node registry is unavailable.")
    node_class = registry.get(NODE_ID)
    if node_class is None:
        return None
    module = sys.modules.get(node_class.__module__)
    if module is None:
        raise QwenAdapterUnavailable(f"The registered {NODE_ID} module is not loaded.")
    lock = getattr(module, "INFERENCE_LOCK", None)
    manager = getattr(module, "SERVER_MANAGER", None)
    if not all(callable(getattr(lock, name, None)) for name in ("__enter__", "__exit__")):
        raise QwenAdapterUnavailable(f"{NODE_ID} has no reusable INFERENCE_LOCK.")
    if not all(callable(getattr(manager, name, None)) for name in ("acquire", "release")):
        raise QwenAdapterUnavailable(f"{NODE_ID} has no reusable SERVER_MANAGER.")
    if (not getattr(module, "MODEL_DIR", None) or not all(
        callable(getattr(module, name, None))
        for name in ("_resolve_model", "_sampling_settings")
    )):
        raise QwenAdapterUnavailable(f"{NODE_ID} does not expose a compatible model API.")
    return QwenRuntimeBinding(node_class, module, lock, manager)


def discover_qwen_runtime(registry=None):
    """Return LongVideo's own runtime; use the registered node only explicitly."""
    if registry is not None:
        return _registered_binding(registry)
    return _standalone_binding()


def default_model_config(registry=None):
    """Return actual files from the shared Qwen model directory."""
    binding = discover_qwen_runtime(registry)
    if binding is None:
        raise QwenAdapterUnavailable("No compatible Qwen runtime is available.")
    model = getattr(binding.module, "DEFAULT_MODEL", "")
    mmproj = getattr(binding.module, "DEFAULT_MMPROJ", "")
    if not isinstance(model, str) or not model.strip():
        raise QwenAdapterUnavailable("LongVideo could not select a language model.")
    if not isinstance(mmproj, str) or not mmproj.strip():
        raise QwenAdapterUnavailable("LongVideo could not select a vision projector.")
    return normalize_qwen_config({
        "skill": "long_video_director",
        "llm_model": model.strip(),
        "vision_model": mmproj.strip(),
        "seed": 0,
        "context_size": DEFAULT_CONTEXT_SIZE,
        "reasoning_effort": "medium",
        "think_mode": False,
    })


def validate_shared_model_config(config, *, registry=None):
    """Validate selected GGUF names against the shared model directory."""
    normalized = normalize_qwen_config(config)
    binding = discover_qwen_runtime(registry)
    if binding is None:
        raise QwenAdapterUnavailable("No compatible Qwen runtime is available.")
    normalized["llm_model"] = _model_path(
        binding.module, normalized["llm_model"], "llm_model").name
    normalized["vision_model"] = _model_path(
        binding.module, normalized["vision_model"], "vision_model").name
    return normalized


def _model_path(module, name, label):
    if not isinstance(name, (str, Path)) or not str(name).strip():
        raise QwenAdapterConfigurationError(f"{label} must be an explicit GGUF model name.")
    try:
        # Reuse Qwen's containment policy for names and absolute paths alike.
        path = Path(module._resolve_model(str(name))).resolve()
    except (OSError, ValueError, TypeError) as error:
        raise QwenAdapterConfigurationError(f"Invalid or missing {label}: {name}") from error
    if (path.parent != Path(module.MODEL_DIR).resolve()
        or path.suffix.lower() != ".gguf" or not path.is_file()):
        raise QwenAdapterConfigurationError(f"Invalid or missing {label}: {name}")
    return path


def _positive_int(value, label, maximum=32768):
    if type(value) is not int or not 0 < value <= maximum:
        raise QwenAdapterConfigurationError(f"{label} must be an integer from 1 to {maximum}.")
    return value


def _check_context(server, messages, max_tokens, enable_thinking):
    """Count the complete templated input; never truncate source or repair data."""
    request = getattr(server, "_request", None)
    if not callable(request):
        raise QwenAdapterUnavailable("Qwen runtime cannot preflight the context budget.")
    template = request("POST", "/apply-template", {
        "messages": messages,
        "chat_template_kwargs": {
            "enable_thinking": enable_thinking, "preserve_thinking": False,
        },
    }, timeout=30.0)
    prompt = template.get("prompt") if isinstance(template, dict) else None
    if not isinstance(prompt, str):
        raise QwenAdapterUnavailable("Qwen runtime returned no formatted prompt.")
    tokenized = request("POST", "/tokenize", {
        "content": prompt, "add_special": True, "parse_special": True,
    }, timeout=30.0)
    tokens = tokenized.get("tokens") if isinstance(tokenized, dict) else None
    props = request("GET", "/props", timeout=30.0)
    settings = props.get("default_generation_settings") if isinstance(props, dict) else None
    context_size = settings.get("n_ctx") if isinstance(settings, dict) else None
    if not isinstance(tokens, list) or type(context_size) is not int or context_size <= 0:
        raise QwenAdapterUnavailable("Qwen runtime returned no token count or context capacity.")
    # Leave a small allowance for BOS/template-version differences.
    if len(tokens) + max_tokens + 8 > context_size:
        error = QwenAdapterConfigurationError(
            f"Director context budget exceeded: {len(tokens)} input + {max_tokens} "
            f"output + 8 reserve > {context_size}. The input was not truncated."
        )
        error.context_budget = {"input_tokens": len(tokens), "output_budget": max_tokens,
                                "reserve_tokens": 8, "context_size": context_size}
        raise error
    return {"input_tokens": len(tokens), "output_budget": max_tokens,
            "context_size": context_size}


def _check_interrupted(management):
    check = getattr(management, "throw_exception_if_processing_interrupted", None)
    if callable(check):
        check()


def _cleanup_comfy_memory(management=None):
    """Drop ComfyUI model references before the next GPU-heavy phase."""
    if management is None:
        management = sys.modules.get("comfy.model_management")
    if management is None:
        return
    unload = getattr(management, "unload_all_models", None)
    empty = getattr(management, "soft_empty_cache", None)
    if callable(unload):
        unload()
    if callable(empty):
        try:
            empty(force=True)
        except TypeError:
            empty()


class QwenAdapter:
    """One completion at a time using Qwen's shared lock and manager.

    The model is released after each director call, including its one possible
    repair, so a later H3 task cannot inherit a resident planning model.
    """
    def __init__(self, binding, model, mmproj, *, context_size=DEFAULT_CONTEXT_SIZE,
                 seed=0, reasoning_effort="low"):
        self.binding = binding
        self.model = _model_path(binding.module, model, "llm_model")
        self.mmproj = _model_path(binding.module, mmproj, "vision_model")
        self.context_size = _positive_int(context_size, "context_size")
        if type(seed) is not int or not 0 <= seed <= 0xFFFFFFFFFFFFFFFF:
            raise QwenAdapterConfigurationError("seed must be an unsigned 64-bit integer.")
        if reasoning_effort not in {"low", "medium", "xhigh"}:
            raise QwenAdapterConfigurationError("Unsupported reasoning_effort.")
        self.seed, self.reasoning_effort = seed, reasoning_effort
        self.last_usage = {}
        self.last_metadata = {}
        self.on_phase = None

    @classmethod
    def from_registered(cls, model_config, *, registry=None):
        """Use selected names with LongVideo's standalone runtime."""
        binding = discover_qwen_runtime(registry)
        if not isinstance(model_config, dict):
            raise QwenAdapterConfigurationError("An explicit Qwen model configuration is required.")
        normalized = normalize_qwen_config({
            **model_config,
            "context_size": model_config.get("context_size", DEFAULT_CONTEXT_SIZE),
        })
        return cls(binding, normalized["llm_model"], normalized["vision_model"],
                   context_size=normalized.get("context_size", DEFAULT_CONTEXT_SIZE),
                   seed=normalized["seed"],
                   reasoning_effort=normalized["reasoning_effort"])

    def complete(self, messages, max_tokens, enable_thinking=False, response_schema=None):
        """Callable contract consumed by ``ai_director.generate_director_draft``."""
        if (not isinstance(messages, list) or not messages or any(
            not isinstance(message, dict)
            or message.get("role") not in {"system", "user", "assistant"}
            or not (
                isinstance(message.get("content"), str)
                or (
                    isinstance(message.get("content"), list)
                    and all(isinstance(item, dict) for item in message["content"])
                )
            )
            for message in messages
        )):
            raise QwenAdapterConfigurationError(
                "messages must be non-empty text or multimodal chat messages.")
        max_tokens = _positive_int(max_tokens, "max_tokens")
        if type(enable_thinking) is not bool:
            raise QwenAdapterConfigurationError("enable_thinking must be boolean.")
        if response_schema is not None and not isinstance(response_schema, dict):
            raise QwenAdapterConfigurationError("response_schema must be an object.")
        if max_tokens + 8 >= self.context_size:
            raise QwenAdapterConfigurationError("Output budget leaves no input context.")
        self.last_usage = {}
        self.last_metadata = {}
        started = time.monotonic()
        notify = self.on_phase or (lambda value: None)
        notify("waiting_model")
        with self.binding.inference_lock:
            management = sys.modules.get("comfy.model_management")
            server = None
            try:
                notify("loading_model")
                _check_interrupted(management)
                _cleanup_comfy_memory(management)
                stage_started = time.monotonic()
                server, _ = self.binding.server_manager.acquire(
                    self.model, self.mmproj, self.context_size)
                self.last_metadata["load_seconds"] = time.monotonic() - stage_started
                _check_interrupted(management)
                notify("checking_context")
                stage_started = time.monotonic()
                self.last_metadata.update(_check_context(
                    server, messages, max_tokens, enable_thinking))
                self.last_metadata["preflight_seconds"] = time.monotonic() - stage_started
                _check_interrupted(management)
                notify("generating")
                extra = {"response_schema": response_schema} if response_schema is not None else {}
                stage_started = time.monotonic()
                try:
                    content, usage = server.chat(
                        messages, seed=self.seed, max_tokens=max_tokens,
                        think_mode=enable_thinking, reasoning_effort=self.reasoning_effort,
                        **self.binding.module._sampling_settings(enable_thinking), **extra)
                finally:
                    self.last_metadata["inference_seconds"] = time.monotonic() - stage_started
                    completion = getattr(server, "last_completion_metadata", {})
                    self.last_metadata.update(completion)
                    self.last_usage = dict(completion.get("usage") or {})
                _check_interrupted(management)
                if not isinstance(content, str) or not content.strip():
                    raise RuntimeError("Qwen returned no final answer.")
                self.last_usage = dict(usage or {})
                return content.strip()
            finally:
                # Use Qwen's cleanup hook too; never start or own another runtime.
                active_error = sys.exc_info()[0] is not None
                release_started = time.monotonic()
                try:
                    server = None
                    release = getattr(self.binding.module, "_release_node_resources", None)
                    if callable(release):
                        release()
                    else:
                        self.binding.server_manager.release()
                    _cleanup_comfy_memory(management)
                except Exception:
                    if not active_error:
                        raise
                    LOGGER.exception("Qwen director resource cleanup failed.")
                finally:
                    self.last_metadata["unload_seconds"] = time.monotonic() - release_started
                    self.last_metadata["elapsed_seconds"] = time.monotonic() - started
