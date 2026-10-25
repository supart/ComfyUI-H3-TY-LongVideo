import json
import hashlib
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import uuid

from . import director_rules
from . import qwen_adapter
from .core import (audio_file, brief_text, decorate, fingerprint, inside, project_path, read_plan,
                   resolve_reference_images, segmentation, validate_segment_prompt, write_plan)
from .director_task import DirectorTask


HDEMUCS_MODEL_URL = "https://download.pytorch.org/torchaudio/models/hdemucs_high_trained.pt"
HDEMUCS_MODEL_SIZE = 334_697_255
HDEMUCS_MODEL_SHA256 = "a004b2790d73ffeaa535db458a1a79b539dfdbafbccc31f275d07e632ebd7816"
HDEMUCS_DOWNLOAD_LOCK = threading.Lock()
HDEMUCS_VERIFIED_SIGNATURE = None
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SHOT_PLANNER_PROMPT = (
    "body_swap:  subject_definitions: <Subject 1> is the {details} shown in <Picture 1>, whose identity, "
    "face, head, hair or fur, skin, body shape, proportions and clothing define the character in the target "
    "video. The guide video provides the scene and the performance: its background, environment, lighting, "
    "camera, framing, body pose, hand and arm movement, head rotation, gaze direction, lip movement and facial "
    "expression, frame by frame; only the main subject of the guide video is replaced: its face, hair, body and "
    "clothing become <Subject 1>.  summary: [video generation + reference] Replace the guide subject with "
    "<Subject 1>, keeping the scene, camera and the complete motion and performance of the guide video.  "
    "detailed_description: [Shot 1] <Subject 1> takes the exact place of the guide subject and moves with the "
    "same timing, body pose, gestures, head motion, gaze, speech and facial expressions, while keeping the "
    "complete look from <Picture 1>: head, face, skin, hair or fur, body shape and clothing stay stable in every "
    "frame, with no trace of the original subject's face, hair or clothing. The background, lighting, colors and "
    "camera of the guide video are preserved. Prevent identity drift, clothing changes, texture crawling and "
    "flicker. The result is a real, photographic video."
)


def storage_root():
    import folder_paths
    return Path(folder_paths.get_output_directory()) / "TYH3LongVideo"


def data_root():
    return storage_root() / "projects"


def final_root():
    return storage_root() / "final_videos"


def user_data_root():
    import folder_paths
    get_user = getattr(folder_paths, "get_user_directory", None)
    root = Path(get_user()) if get_user else Path(folder_paths.base_path)/"user"
    return root/"default"/"TYH3LongVideo"


def rules_path():
    return user_data_root()/"rules"


def asr_model_root():
    import folder_paths
    root = Path(getattr(folder_paths, "models_dir", Path(folder_paths.base_path)/"models"))
    return root/"faster-whisper"


def hdemucs_model_path():
    import folder_paths
    root = Path(getattr(folder_paths, "models_dir", Path(folder_paths.base_path)/"models"))
    return root/"TYH3LongVideo"/"hdemucs_high_trained.pt"


def legacy_hdemucs_model_path():
    import folder_paths
    root = Path(getattr(folder_paths, "models_dir", Path(folder_paths.base_path)/"models"))
    return root/"H3LongVideo"/"hdemucs_high_trained.pt"


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _valid_hdemucs_model(path):
    global HDEMUCS_VERIFIED_SIGNATURE
    path = Path(path)
    if not path.is_file():
        return False
    stat = path.stat()
    if stat.st_size != HDEMUCS_MODEL_SIZE:
        return False
    signature = (str(path.resolve()), stat.st_size, stat.st_mtime_ns, HDEMUCS_MODEL_SHA256)
    if signature == HDEMUCS_VERIFIED_SIGNATURE:
        return True
    if _sha256(path) != HDEMUCS_MODEL_SHA256:
        return False
    HDEMUCS_VERIFIED_SIGNATURE = signature
    return True


def _download_notice(state, downloaded=0):
    try:
        server_module = sys.modules.get("server")
        if server_module is None:
            return
        PromptServer = server_module.PromptServer
        server = PromptServer.instance
        if callable(getattr(server, "send_sync", None)):
            server.send_sync("ty-h3lv-model-download", {
                "state": state, "downloaded": int(downloaded),
                "total": HDEMUCS_MODEL_SIZE,
            })
    except (ImportError, AttributeError, RuntimeError):
        pass


def _copy_legacy_hdemucs(source, target, partial):
    if not _valid_hdemucs_model(source):
        return False
    shutil.copyfile(source, partial)
    os.replace(partial, target)
    _download_notice("completed", HDEMUCS_MODEL_SIZE)
    return True


def _download_hdemucs(target, partial):
    import comfy.model_management as mm
    try:
        from comfy.utils import ProgressBar
        progress = ProgressBar(HDEMUCS_MODEL_SIZE)
    except ImportError:
        progress = None

    downloaded = partial.stat().st_size if partial.is_file() else 0
    if downloaded >= HDEMUCS_MODEL_SIZE:
        partial.unlink(missing_ok=True)
        downloaded = 0
    headers = {"User-Agent": "ComfyUI-H3-TY-LongVideo/1.0"}
    if downloaded:
        headers["Range"] = f"bytes={downloaded}-"
    request = Request(HDEMUCS_MODEL_URL, headers=headers)
    _download_notice("resuming" if downloaded else "started", downloaded)
    try:
        with urlopen(request, timeout=30) as response:
            status = int(getattr(response, "status", 200))
            if downloaded and status != 206:
                downloaded = 0
            mode = "ab" if downloaded and status == 206 else "wb"
            if progress:
                progress.update_absolute(downloaded)
            with partial.open(mode) as handle:
                while True:
                    mm.throw_exception_if_processing_interrupted()
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    handle.write(block)
                    downloaded += len(block)
                    if progress:
                        progress.update_absolute(downloaded)
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            "人声分离模型下载失败，已保留临时文件供下次续传。"
            f"请检查网络和磁盘空间后重试。临时文件：{partial}；原因：{exc}") from exc

    actual_size = partial.stat().st_size if partial.is_file() else 0
    if actual_size != HDEMUCS_MODEL_SIZE:
        raise RuntimeError(
            f"人声分离模型下载不完整（{actual_size}/{HDEMUCS_MODEL_SIZE} 字节），"
            "已保留临时文件，下次运行会自动续传。")
    if _sha256(partial) != HDEMUCS_MODEL_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError("人声分离模型校验失败，损坏的临时文件已删除，请重新运行。")
    os.replace(partial, target)
    _download_notice("completed", HDEMUCS_MODEL_SIZE)


def ensure_hdemucs_model():
    target = hdemucs_model_path()
    with HDEMUCS_DOWNLOAD_LOCK:
        if _valid_hdemucs_model(target):
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        partial = target.with_name(target.name + ".part")
        if _copy_legacy_hdemucs(legacy_hdemucs_model_path(), target, partial):
            return target
        _download_hdemucs(target, partial)
        return target


def audio_array(audio):
    import numpy as np
    x = audio["waveform"].detach().cpu().float()
    if x.ndim != 3 or x.shape[0] != 1:
        raise ValueError("请一次输入一首音频，不支持 AUDIO batch。")
    x = x[0].T.numpy().copy()
    if not len(x) or not np.isfinite(x).all():
        raise ValueError("音频为空或无效。")
    return x, int(audio["sample_rate"])


def input_video_files():
    try:
        import folder_paths
        root = Path(folder_paths.get_input_directory()).resolve()
    except (ImportError, AttributeError, OSError):
        return [""]
    values = sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                    if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS)
    return values or [""]


def source_video_path(name):
    import folder_paths
    root = Path(folder_paths.get_input_directory()).resolve()
    path = (root / str(name or "")).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("请选择 ComfyUI input 目录中的有效视频文件。")
    return path


def _ratio(value):
    try:
        numerator, denominator = str(value or "0/1").split("/", 1)
        return float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0.0


def parse_source_video_metadata(data):
    streams = list(data.get("streams") or [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video:
        raise ValueError("源文件没有视频轨道。")
    if not audio:
        raise ValueError("源视频没有音轨，TY Shot Planner 需要带声音的视频。")
    duration = float(video.get("duration") or (data.get("format") or {}).get("duration") or 0)
    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    fps = _ratio(video.get("avg_frame_rate")) or _ratio(video.get("r_frame_rate"))
    if duration <= 0 or width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("无法读取源视频的时长、尺寸或帧率。")
    return {"width": width, "height": height, "source_fps": fps, "duration": duration,
            "audio_sample_rate": int(audio.get("sample_rate") or 0),
            "audio_channels": int(audio.get("channels") or 0)}


def probe_source_video(path):
    from .controller import command, media_tool
    ffprobe = media_tool("ffprobe")
    if not ffprobe:
        raise ValueError("未找到 ffprobe，请配置现有 FFmpeg 到 PATH。")
    data = json.loads(command([ffprobe, "-v", "error", "-show_entries",
        "stream=codec_type,width,height,r_frame_rate,avg_frame_rate,duration,sample_rate,channels:format=duration",
        "-of", "json", str(path)]))
    return parse_source_video_metadata(data)


def align_audio_length(audio, samples):
    import numpy as np
    audio = np.asarray(audio, dtype=np.float32)
    if audio.ndim == 1:
        audio = audio[:, None]
    if audio.ndim != 2 or not len(audio):
        raise ValueError("源视频音轨为空或无法解码。")
    if len(audio) >= samples:
        return audio[:samples].copy()
    return np.pad(audio, ((0, samples - len(audio)), (0, 0)))


def audio_from_video(path, duration):
    import soundfile as sf
    import torch
    from .controller import command, media_tool
    ffmpeg = media_tool("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 FFmpeg，请配置已有 FFmpeg 到 PATH。")
    with tempfile.TemporaryDirectory(prefix="ty-shot-audio-") as temporary:
        wav = Path(temporary) / "source.wav"
        command([ffmpeg, "-v", "error", "-y", "-i", str(path), "-map", "0:a:0", "-vn",
                 "-ac", "2", "-ar", "48000", "-af", "apad", "-t", f"{float(duration):.9f}",
                 "-c:a", "pcm_f32le", str(wav)], timeout=600)
        audio, sr = sf.read(wav, dtype="float32", always_2d=True)
    audio = align_audio_length(audio, round(float(duration) * sr))
    return {"waveform": torch.from_numpy(audio.T.copy()).unsqueeze(0), "sample_rate": int(sr)}


def generation_size(width, height, megapixels, multiple=32):
    scale = math.sqrt(float(megapixels) * 1_000_000 / (int(width) * int(height)))
    aligned = lambda value: max(multiple, int(round(value * scale / multiple)) * multiple)
    return aligned(width), aligned(height)


def segment_frame_range(row, sample_rate):
    start = round(int(row["start_sample"]) / int(sample_rate) * 24)
    end = round(int(row["end_sample"]) / int(sample_rate) * 24)
    if end <= start:
        raise ValueError("视频分段量化到 24fps 后不足一帧。")
    return start, end


def pad_video_frames(frames, total):
    if frames.shape[0] <= 0:
        raise ValueError("没有解码出当前视频分段。")
    if frames.shape[0] >= total:
        return frames[:total]
    return __import__("torch").cat(
        [frames, frames[-1:].expand(total - frames.shape[0], -1, -1, -1)], 0)


def decode_segment_frames(path, start_frame, edit_frames, width, height):
    import numpy as np
    import torch
    from .controller import media_tool
    ffmpeg = media_tool("ffmpeg")
    if not ffmpeg:
        raise ValueError("未找到 FFmpeg，请配置已有 FFmpeg 到 PATH。")
    result = subprocess.run(
        [ffmpeg, "-v", "error", "-ss", f"{start_frame / 24:.9f}", "-i", str(path),
         "-vf", f"fps=24:round=near,scale={int(width)}:{int(height)}:flags=lanczos",
         "-frames:v", str(int(edit_frames)), "-an", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode:
        raise RuntimeError("视频分段解码失败：" + result.stderr.decode("utf-8", "replace")[-2000:])
    frame_bytes = int(width) * int(height) * 3
    if not result.stdout or len(result.stdout) % frame_bytes:
        raise RuntimeError("视频分段解码返回了不完整的 RGB 帧。")
    count = len(result.stdout) // frame_bytes
    array = np.frombuffer(result.stdout, dtype=np.uint8).reshape(count, int(height), int(width), 3).copy()
    return torch.from_numpy(array).float().div_(255)


def separate(audio, sr):
    import numpy as np
    import torch
    from torchaudio.models import hdemucs_high
    from torchaudio.functional import resample
    import comfy.model_management as mm
    checkpoint = ensure_hdemucs_model()
    device = mm.get_torch_device()
    x = torch.from_numpy(audio.T.copy())
    if x.shape[0] == 1:
        x = x.repeat(2, 1)
    if x.shape[0] != 2:
        raise ValueError("人声分离支持单声道或双声道。")
    if sr != 44100:
        x = resample(x, sr, 44100)
    model = hdemucs_high(sources=["drums", "bass", "other", "vocals"])
    model.load_state_dict(torch.load(checkpoint, weights_only=True, map_location="cpu"))
    model.eval().to(device)
    mean, std = x.mean(0).mean(), x.mean(0).std().clamp_min(1e-8)
    normalized = (x-mean)/std
    output, weights = torch.zeros_like(x), torch.zeros(x.shape[1])
    chunk, overlap = 8*44100, 44100
    try:
        with torch.inference_mode():
            for start in range(0, x.shape[1], chunk-overlap):
                mm.throw_exception_if_processing_interrupted()
                end = min(start+chunk, x.shape[1])
                value = model(normalized[:, start:end].unsqueeze(0).to(device))[0, 3].cpu()
                w = torch.ones(end-start)
                fade = min(overlap, len(w))
                if start:
                    w[:fade] *= torch.linspace(1/(fade+1), 1, fade)
                if end < x.shape[1]:
                    w[-fade:] *= torch.linspace(1, 1/(fade+1), fade)
                output[:, start:end] += value*w
                weights[start:end] += w
                if end == x.shape[1]:
                    break
        output = output/weights.clamp_min(1e-8)*std+mean
        if sr != 44100:
            output = resample(output, 44100, sr)
        output = output[:, :len(audio)]
        if output.shape[1] < len(audio):
            output = torch.nn.functional.pad(output, (0, len(audio)-output.shape[1]))
        return output.T.numpy().astype(np.float32)
    finally:
        del model
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


def run_asr(project):
    import importlib.util
    import comfy.model_management as mm
    if importlib.util.find_spec("faster_whisper") is None:
        raise ValueError(
            "当前 ComfyUI Python 缺少 faster-whisper。请通过 ComfyUI Manager 重新安装本插件依赖，"
            "或在当前 ComfyUI Python 中安装 requirements.txt 后重启。")
    model_root = asr_model_root()
    model_root.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-B", str(Path(__file__).with_name("worker.py")),
               "--model", "large-v3-turbo", "--download-root", str(model_root),
               "--audio", str(audio_file(project, "vocals.wav")),
               "--output", str(project/"state"/"transcript.json"), "--device", "auto"]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    with (project/"state"/"asr.log").open("w", encoding="utf-8") as log:
        child = subprocess.Popen(command, stdout=log, stderr=log, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=="nt" else 0)
        try:
            deadline = time.monotonic()+3600
            while child.poll() is None:
                mm.throw_exception_if_processing_interrupted()
                if time.monotonic() > deadline:
                    raise TimeoutError("语音识别超过一小时，已停止本次分析进程。")
                time.sleep(.2)
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
            raise
    if child.returncode:
        raise RuntimeError(
            "语音识别失败。首次使用可能正在下载模型；请检查网络、磁盘空间和运行环境。"
            f"详细日志：{project/'state'/'asr.log'}")
    return json.loads((project/"state"/"transcript.json").read_text(encoding="utf-8"))


class Analyze:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"audio": ("AUDIO",), "mode": (["singing", "speaking", "speech_light"],),
            "max_seconds": ("FLOAT", {"default": 15, "min": 5, "max": 15, "step": .1}),
            "target_seconds": ("FLOAT", {"default": 11, "min": 5, "max": 15, "step": .1}),
            "asr_python": ("STRING", {"default": ""}), "asr_model": ("STRING", {"default": ""}),
            "asr_device": (["auto", "cuda", "cpu"],),
            "camera_activity": (["auto", "moderate", "dynamic"],),
            "widest_framing": (["medium close-up", "medium shot", "full shot", "close-up"],),
            # Hidden and ignored compatibility slot. Keeping its position prevents
            # older workflow widget values from shifting project_id/segment_index.
            "director_mode": ("STRING", {"default": "本地规则"}),
            }, "optional": {"vocals": ("AUDIO",)}}
    RETURN_TYPES = ("STRING", "INT")
    RETURN_NAMES = ("project_id", "segment_count")
    FUNCTION = "analyze"
    CATEGORY = "TY/H3 长视频"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def analyze(self, audio, mode, max_seconds, target_seconds, asr_python, asr_model,
                asr_device="auto", camera_activity="auto", widest_framing="medium close-up",
                director_mode="本地规则", vocals=None, qwen_config=None):
        import soundfile as sf
        import numpy as np
        mix, sr = audio_array(audio)
        audio_seed = hashlib.sha256(np.ascontiguousarray(mix).tobytes()).hexdigest()[:16]
        rules = director_rules.read_rules(rules_path())
        if vocals is not None:
            voice, vsr = audio_array(vocals)
            if vsr != sr or len(voice) != len(mix):
                raise ValueError("原曲与人声必须采样率、样本数一致；请提供同版本未裁切的人声。")
        else:
            voice = separate(mix, sr) if mode=="singing" else mix.copy()
        project_id = uuid.uuid4().hex
        root = data_root()
        directory = project_path(root, project_id)
        for name in ("state", "audio", "takes", "cache", "work"):
            (directory/name).mkdir(parents=True, exist_ok=True)
        sf.write(audio_file(directory, "source.wav"), mix, sr, subtype="FLOAT")
        sf.write(audio_file(directory, "vocals.wav"), voice, sr, subtype="FLOAT")
        transcript = run_asr(directory)
        rows, analysis = segmentation(voice, sr, transcript, mode, float(max_seconds),
                                      float(target_seconds), mix_audio=mix, return_analysis=True)
        analysis_temp = directory/"state"/f".analysis-{uuid.uuid4().hex}.tmp"
        analysis_temp.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        analysis_temp.replace(directory/"state"/"analysis.json")
        rhythm = analysis.get("rhythm") or {}
        audio_structure = {
            "tempo_bpm": rhythm.get("tempo_bpm"),
            "tempo_confidence": rhythm.get("confidence"),
            "sections": list(analysis.get("sections") or [])[:32],
        }
        plan = {"id": project_id, "schema": 4, "analysis_schema": 2,
            "revision": 1, "created": time.time(),
            "mode": mode, "max_seconds": float(max_seconds), "target_seconds": float(target_seconds),
            "sample_rate": sr, "samples": len(mix), "duration": len(mix)/sr,
            "director": {"mode": "rule", "performance_intensity": "auto",
                         "camera_activity": camera_activity, "widest_framing": widest_framing,
                         "note": "", "rule_config": rules["config"],
                         "schedule_seed": audio_seed, "rule_revision": rules["revision"]},
            "audio_structure": audio_structure,
            "qwen_config": dict(qwen_config or {}),
            "segments": rows, "approved": False,
            "run_status": "draft", "warnings": ["ASR 文字和时间戳未经校对；请试听风险切点。",
            "气口、拖音和无人声段是声学估计；无文字不代表无人声。"]}
        plan = decorate(plan)
        reconstructed = np.concatenate([mix[r["start_sample"]:r["end_sample"]] for r in plan["segments"]])
        if not np.array_equal(reconstructed, mix):
            raise AssertionError("音频覆盖检查失败。")
        plan["sample_coverage_verified"] = True
        write_plan(root, plan)
        return {"ui": {"ty_h3lv_project": [project_id], "text": [f"分析完成：{len(rows)} 段。点击分段预览检查后生成。"]},
                "result": (project_id, len(rows))}


class LoadSegment:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"project_id": ("STRING", {"default": ""}),
                             "segment_index": ("INT", {"default": 0, "min": 0, "max": 10000})}}
    RETURN_TYPES = ("AUDIO", "AUDIO", "STRING", "INT", "STRING")
    RETURN_NAMES = ("original_audio_padded", "vocals_padded", "segment_brief", "generation_frames", "filename_prefix")
    FUNCTION = "load"
    CATEGORY = "TY/H3 长视频"

    @classmethod
    def IS_CHANGED(cls, project_id, segment_index):
        try:
            plan = read_plan(data_root(), project_id)
            return fingerprint(plan)+str(plan.get("approved", False))
        except (ValueError, FileNotFoundError):
            return float("nan")

    def load(self, project_id, segment_index):
        return _load_segment(project_id, segment_index, include_images=False)


def _normalize_image(value):
    if value is None:
        return None
    import torch
    image = value.detach().cpu().float() if hasattr(value, "detach") else torch.as_tensor(value).float()
    if image.ndim == 3:
        image = image.unsqueeze(0)
    if image.ndim != 4 or image.shape[-1] not in (3, 4) or not image.numel():
        raise ValueError("参考图输入必须是有效的 IMAGE 张量。")
    if image.shape[-1] == 4:
        image = image[..., :3]
    if not torch.isfinite(image).all():
        raise ValueError("参考图输入包含无效像素。")
    return image.clamp(0, 1).contiguous()


def _load_image_file(path):
    from PIL import Image
    import numpy as np
    import torch
    with Image.open(path) as source:
        pixels = np.asarray(source.convert("RGB"), dtype=np.float32).copy() / 255.0
    return torch.from_numpy(pixels).unsqueeze(0)


def _empty_image():
    import torch
    return torch.zeros((1, 64, 64, 3), dtype=torch.float32)


def _image_or_empty(value):
    image = _normalize_image(value)
    return image if image is not None else _empty_image()


def _load_segment(project_id, segment_index, default_performer_image=None,
                  default_background_image=None, default_image_3=None,
                  default_image_4=None, default_image_5=None, default_image_6=None,
                  include_images=True, prompt_override=None):
    import numpy as np
    import soundfile as sf
    if not str(project_id).strip():
        raise ValueError("尚未分析音频。请先运行 H3 音频分析节点并确认分段。")
    plan = read_plan(data_root(), project_id)
    if not plan.get("approved"):
        raise ValueError("音频已经分析，但分段尚未确认。请先在分段审核界面点击“保存并确认”。")
    from .director_review import require_director_approval, effective_segment_brief
    require_director_approval(data_root(), plan)
    if not 0 <= segment_index < len(plan["segments"]):
        raise ValueError("分段编号超出范围。")
    row = plan["segments"][segment_index]
    validate_segment_prompt(row if prompt_override is None else
                            {**row, "prompt": prompt_override, "prompt_kind": "final"})
    directory = project_path(data_root(), project_id)
    images = []
    if include_images:
        references = resolve_reference_images(plan, row, directory)
        for reference, fallback in zip(
                references, (default_performer_image, default_background_image,
                             default_image_3, default_image_4, default_image_5,
                             default_image_6)):
            image = (_load_image_file(directory / "refs" / reference["name"])
                     if reference["name"] else _normalize_image(fallback))
            if image is None:
                image = _empty_image()
            images.append(image)
    outputs = []
    for name in ("source.wav", "vocals.wav"):
        audio, sr = sf.read(audio_file(directory, name), start=row["start_sample"], stop=row["end_sample"], dtype="float32", always_2d=True)
        target = math_ceil_samples(row["generation_frames"], sr)
        audio = np.pad(audio, ((0, max(0, target-len(audio))), (0, 0)))
        import torch
        outputs.append({"waveform": torch.from_numpy(audio.T.copy()).unsqueeze(0), "sample_rate": sr})
    text = prompt_override if prompt_override is not None else (
        effective_segment_brief(data_root(), plan, row)
        if plan.get("planning_method", "rules") == "qwen" else brief_text(row))
    result = (*outputs, text, row["generation_frames"],
              f"TYH3LongVideo/projects/{project_id}/takes/seg_{segment_index:04d}")
    return result + tuple(images) if include_images else result


class Unified:
    """Analyze on native partial execution; load one approved segment during controller runs."""
    @classmethod
    def INPUT_TYPES(cls):
        analyze = Analyze.INPUT_TYPES()
        required = dict(analyze["required"])
        required.update(project_id=("STRING", {"default": ""}),
                        segment_index=("INT", {"default": 0, "min": 0, "max": 10000}))
        required.update({
            # These fields are appended after the legacy inputs so old saved
            # workflows keep their widget positions and receive defaults.
            "skill": (["long_video_director"], {"default": "long_video_director"}),
            "duration": ("FLOAT", {"default": 10.0, "min": 1.0, "max": 60.0, "step": 0.1}),
            "llm_model": (list(qwen_adapter.model_options(False)), {
                "default": qwen_adapter.default_model_name(False),
            }),
            "vision_model": (list(qwen_adapter.model_options(True)), {
                "default": qwen_adapter.default_model_name(True),
            }),
            "think_mode": ("BOOLEAN", {"default": False}),
            "reasoning_effort": (["low", "medium", "xhigh"], {"default": "medium"}),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            "max_tokens": ("INT", {"default": 8192, "min": 256, "max": 8192, "step": 128}),
            "video_sample_frames_per_sec": (
                "INT", {"default": 2, "min": 1, "max": 8, "step": 1}),
            "force_unload_model": ("BOOLEAN", {"default": True}),
        })
        optional = dict(analyze.get("optional", {}))
        optional.update({
            "default_performer_image": ("IMAGE",),
            "default_background_image": ("IMAGE",),
            "default_image_3": ("IMAGE",),
            "default_image_4": ("IMAGE",),
            "default_image_5": ("IMAGE",),
            "default_image_6": ("IMAGE",),
        })
        return {"required": required, "optional": optional}

    RETURN_TYPES = LoadSegment.RETURN_TYPES + ("IMAGE",) * 6
    RETURN_NAMES = LoadSegment.RETURN_NAMES + tuple(f"image_{index}" for index in range(1, 7))
    FUNCTION = "process"
    CATEGORY = "TY/H3 长视频"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, project_id="", segment_index=0, **kwargs):
        if str(project_id).strip():
            try:
                plan = read_plan(data_root(), project_id)
                if plan.get("approved"):
                    director_key = ""
                    if plan.get("planning_method", "rules") == "qwen":
                        from .director_review import require_director_approval
                        require_director_approval(data_root(), plan)
                        director_key = ":" + json.dumps(plan.get("director_approval"), sort_keys=True)
                    index = int(segment_index)
                    rows = plan.get("segments", [])
                    nonce = rows[index].get("regeneration_nonce", "") if 0 <= index < len(rows) else ""
                    return f"{fingerprint(plan)}:{index}:{nonce}{director_key}"
            except (ValueError, FileNotFoundError):
                pass
        return float("nan")

    def process(self, audio, mode, max_seconds, target_seconds, asr_python, asr_model,
                asr_device="auto", camera_activity="auto", widest_framing="medium close-up",
                director_mode="本地规则", project_id="", segment_index=0,
                skill="long_video_director", duration=10.0, llm_model="",
                vision_model="", think_mode=False, reasoning_effort="medium", seed=0,
                 max_tokens=8192, video_sample_frames_per_sec=2,
                 force_unload_model=True, vocals=None, default_performer_image=None,
                 default_background_image=None, default_image_3=None,
                 default_image_4=None, default_image_5=None, default_image_6=None):
        if not str(llm_model).strip():
            llm_model = qwen_adapter.default_model_name(False)
        if not str(vision_model).strip():
            vision_model = qwen_adapter.default_model_name(True)
        qwen_config = {
            "skill": skill,
            "duration": float(duration),
            "llm_model": str(llm_model),
            "vision_model": str(vision_model),
            "think_mode": bool(think_mode),
            "reasoning_effort": reasoning_effort,
            "seed": int(seed),
            "max_tokens": int(max_tokens),
            "video_sample_frames_per_sec": int(video_sample_frames_per_sec),
            "force_unload_model": bool(force_unload_model),
        }
        if str(project_id).strip():
            try:
                plan = read_plan(data_root(), project_id)
                if plan.get("approved"):
                    return _load_segment(project_id, int(segment_index),
                                         default_performer_image, default_background_image,
                                         default_image_3, default_image_4, default_image_5,
                                         default_image_6,
                                         include_images=True)
            except FileNotFoundError:
                pass
        analyzed = Analyze().analyze(audio, mode, max_seconds, target_seconds,
                                     asr_python, asr_model, asr_device, camera_activity,
                                     widest_framing, director_mode, vocals, qwen_config)
        # Native Run targets only this node. These placeholders are not sent downstream;
        # the approved controller run replaces them with the selected segment outputs.
        return {"ui": analyzed["ui"],
                "result": (audio, vocals if vocals is not None else audio, "", 0, "",
                            _image_or_empty(default_performer_image),
                            _image_or_empty(default_background_image),
                            _image_or_empty(default_image_3),
                            _image_or_empty(default_image_4),
                            _image_or_empty(default_image_5),
                            _image_or_empty(default_image_6))}


def _shot_reference_outputs(plan, row, directory, images, fallbacks):
    refs, names = [], []
    for item, image, fallback in zip(resolve_reference_images(plan, row, directory), images, fallbacks):
        if item["source"] == "ignore" or (not item.get("name") and fallback is None):
            continue
        refs.append(image[:1])
        names.append(item.get("name") or f"image_{item['slot'] + 1}")
    return refs, names


def load_shot_segment(project_id, segment_index, fallbacks, fixed_prompt=None):
    plan = read_plan(data_root(), project_id)
    directory = project_path(data_root(), project_id)
    row = plan["segments"][int(segment_index)]
    latest_prompt = str(fixed_prompt or "")
    if not latest_prompt.strip():
        latest_prompt = str(plan.get("fixed_prompt") or "")
    if not latest_prompt.strip():
        raise ValueError("请在工作流中填写或连接 fixed_prompt，全局提示词不能为空。")
    values = list(_load_segment(project_id, int(segment_index), *fallbacks,
                                include_images=True, prompt_override=latest_prompt))
    values[2] = latest_prompt
    metadata = plan.get("source_video_metadata") or {}
    width, height = generation_size(
        metadata.get("width", 0), metadata.get("height", 0), plan.get("generation_megapixels", .5))
    start_frame, end_frame = segment_frame_range(row, plan["sample_rate"])
    edit_frames = end_frame - start_frame
    if edit_frames != int(row["edit_frames"]):
        raise ValueError("项目视频帧边界与音频切点不一致，请重新分析。")
    source = inside(directory, directory / str(plan.get("source_video") or ""))
    if not source.is_file():
        raise ValueError("项目源视频已丢失，请重新分析视频。")
    frames = decode_segment_frames(source, start_frame, edit_frames, width, height)
    frames = pad_video_frames(frames, int(row["generation_frames"]))
    images = values[5:11]
    refs, ref_names = _shot_reference_outputs(plan, row, directory, images, fallbacks)
    shot = {
        "index": int(row["index"]), "count": len(plan["segments"]),
        "start": start_frame, "end": end_frame, "length": edit_frames,
        "gen_length": int(row["generation_frames"]), "head": 0, "fps": 24,
        "cut_before": int(row["index"]) > 0, "width": width, "height": height,
        "frames": frames, "refs": refs, "ref": (refs + [None])[0],
        "ref2": (refs + [None, None])[1], "ref_names": ref_names,
        "prompt": values[2], "audio": values[0],
        "crop": None, "full_frames": None, "mask_src": None,
        "inpaint": False, "inpaint_strength": 1.0, "paste": False, "paste_text": "",
        "color_guide": False, "color_guide_marks": [],
        "chain": "off", "queue": False, "run_mode": "ty-shot-planner",
        "prev_length": 0, "history": None, "history_src": None, "head_frames": None,
    }
    return tuple(values) + (shot,)


class ShotPlanner:
    @classmethod
    def INPUT_TYPES(cls):
        required = {
            "source_video": (input_video_files(), {"default": input_video_files()[0]}),
            "mode": (["speaking", "singing", "speech_light"], {"default": "speaking"}),
            "max_seconds": ("FLOAT", {"default": 10.0, "min": 5.0, "max": 15.0, "step": .1}),
            "target_seconds": ("FLOAT", {"default": 8.0, "min": 5.0, "max": 15.0, "step": .1}),
            "generation_megapixels": ("FLOAT", {"default": .5, "min": .1, "max": 4.0, "step": .05}),
            "fixed_prompt": ("STRING", {"default": SHOT_PLANNER_PROMPT, "multiline": True}),
            "asr_python": ("STRING", {"default": ""}), "asr_model": ("STRING", {"default": ""}),
            "asr_device": (["auto", "cuda", "cpu"],),
            "project_id": ("STRING", {"default": ""}),
            "segment_index": ("INT", {"default": 0, "min": 0, "max": 10000}),
        }
        optional = {
            "default_performer_image": ("IMAGE",), "default_background_image": ("IMAGE",),
            "default_image_3": ("IMAGE",), "default_image_4": ("IMAGE",),
            "default_image_5": ("IMAGE",), "default_image_6": ("IMAGE",),
        }
        return {"required": required, "optional": optional}

    RETURN_TYPES = LoadSegment.RETURN_TYPES + ("IMAGE",) * 6 + ("BFS_SHOT",)
    RETURN_NAMES = (
        "original_audio_padded", "vocals_padded", "segment_prompt",
        "generation_frames", "filename_prefix",
    ) + tuple(f"image_{index}" for index in range(1, 7)) + ("shot",)
    FUNCTION = "process"
    CATEGORY = "TY/H3 长视频"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, project_id="", segment_index=0, **kwargs):
        return Unified.IS_CHANGED(project_id=project_id, segment_index=segment_index, **kwargs)

    def process(self, source_video, mode, max_seconds, target_seconds, generation_megapixels,
                fixed_prompt, asr_python="", asr_model="", asr_device="auto", project_id="",
                segment_index=0, default_performer_image=None, default_background_image=None,
                default_image_3=None, default_image_4=None, default_image_5=None, default_image_6=None):
        fallbacks = (default_performer_image, default_background_image, default_image_3,
                     default_image_4, default_image_5, default_image_6)
        if str(project_id).strip():
            try:
                plan = read_plan(data_root(), project_id)
                if plan.get("approved"):
                    return load_shot_segment(
                        project_id, int(segment_index), fallbacks, fixed_prompt)
            except FileNotFoundError:
                pass
        source = source_video_path(source_video)
        metadata = probe_source_video(source)
        audio = audio_from_video(source, metadata["duration"])
        analyzed = Analyze().analyze(
            audio, mode, max_seconds, target_seconds, asr_python, asr_model, asr_device,
            "auto", "medium close-up", "本地规则", None, None)
        created_id = analyzed["result"][0]
        root = data_root()
        directory = project_path(root, created_id)
        source_dir = directory / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        stored = source_dir / ("source" + source.suffix.lower())
        shutil.copy2(source, stored)
        plan = read_plan(root, created_id)
        plan.update(project_kind="shot_planner", planning_method="rules", fps=24,
                    generation_megapixels=float(generation_megapixels), fixed_prompt=str(fixed_prompt),
                    source_video=stored.relative_to(directory).as_posix(),
                    source_video_input=str(source_video), source_video_metadata=metadata)
        for row in plan["segments"]:
            row.update(prompt=str(fixed_prompt), prompt_kind="final")
            validate_segment_prompt(row)
        write_plan(root, plan)
        return {"ui": analyzed["ui"],
                "result": (audio, audio, "", 0, "", *(_image_or_empty(value) for value in fallbacks), {})}


def math_ceil_samples(frames, sr):
    return (frames*sr+23)//24


NODE_CLASS_MAPPINGS = {
    "TY_H3LVUnified": Unified,
    "TY_ShotPlanner": ShotPlanner,
    "TY_H3LVDirectorTask": DirectorTask,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "TY_H3LVUnified": "TY H3 长视频 · 音频分析与顺序生成",
    "TY_ShotPlanner": "TY Shot Planner",
    "TY_H3LVDirectorTask": "TY H3 长视频 · Qwen 全片导演任务",
}
