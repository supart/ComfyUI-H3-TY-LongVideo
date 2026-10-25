import asyncio
import copy
import io
import json
import re

from . import controller
from . import director_rules
from . import director_review
from .core import (LOCK, UNSET, archive_take, audio_file, edit_plan, fingerprint, inside, preview_bounds,
                   collect_reference_library, output_preview, project_path, read_plan, read_project_transcript,
                   rebuild_speech_timeline, reference_directory, remove_reference, request_regeneration, segmentation,
                   reference_asset_items, replace_with_retry, state_file, store_default_reference, store_reference_asset, write_plan)
from .nodes import data_root, rules_path, storage_root

RANGE_HEADER = re.compile(r"\s*bytes=(\d*)-(\d*)\s*")


def byte_range(header, total):
    """Parse an HTTP Range header into an inclusive (start, end) pair.

    Returns None when the header is absent, malformed or unsatisfiable, so the
    caller serves the whole resource. Without this, browsers treat the audio as
    non-seekable and the player's progress bar cannot be dragged.
    """
    if total <= 0:
        return None
    match = RANGE_HEADER.fullmatch(str(header or ""))
    if not match or (not match.group(1) and not match.group(2)):
        return None
    if match.group(1):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else total - 1
    else:
        length = int(match.group(2))
        if length <= 0:
            return None
        start, end = max(0, total - length), total - 1
    if start >= total or start > end:
        return None
    return start, min(end, total - 1)


def register_routes():
    from aiohttp import web
    from server import PromptServer
    server = PromptServer.instance
    routes = server.routes

    def endpoint(fn):
        async def wrapped(request):
            try:
                return await fn(request)
            except FileNotFoundError:
                return web.json_response({"error": "项目文件不完整或已被移动，请重新运行音频分析。"}, status=400)
            except (ValueError, KeyError, IndexError) as exc:
                return web.json_response({"error": str(exc)}, status=400)
            except Exception as exc:
                return web.json_response({"error": str(exc)}, status=500)
        return wrapped

    @routes.get("/ty-h3lv/rules")
    @endpoint
    async def get_rules(request):
        return web.json_response(director_rules.public_rules(rules_path()))

    @routes.post("/ty-h3lv/rules")
    @endpoint
    async def save_rules(request):
        payload = await request.json()
        return web.json_response(director_rules.write_rules(rules_path(), payload))

    @routes.post("/ty-h3lv/rules/reset")
    @endpoint
    async def reset_rules(request):
        payload = await request.json()
        return web.json_response(director_rules.reset_rules(
            rules_path(), payload.get("mode")))

    @routes.get("/ty-h3lv/projects")
    @endpoint
    async def projects(request):
        result = []
        root = data_root()
        if root.exists():
            for file in root.glob("*/state/segments.json"):
                try:
                    plan = read_plan(root, file.parent.parent.name)
                    result.append({"id": plan["id"], "created": plan["created"], "duration": plan["duration"],
                                   "count": len(plan["segments"]), "status": plan["run_status"],
                                   "mode": plan.get("mode", "singing"),
                                   "clip_origin": plan.get("clip_origin"),
                                   "project_kind": plan.get("project_kind", "long_video")})
                except (ValueError, OSError):
                    continue
        return web.json_response(sorted(result, key=lambda p: p["created"], reverse=True))

    @routes.get("/ty-h3lv/final-folder")
    @endpoint
    async def final_folder(request):
        path = storage_root()/"final_videos"
        path.mkdir(parents=True, exist_ok=True)
        return web.json_response({"path": str(path)})

    @routes.get("/ty-h3lv/project/{project_id}")
    @endpoint
    async def get_project(request):
        root = data_root()
        project_id = request.match_info["project_id"]
        controller.reconcile_orphaned_jobs(root, project_id, server)
        plan = read_plan(root, project_id)
        plan["reference_library"] = collect_reference_library(plan)
        plan["reference_assets"] = reference_asset_items(plan["reference_library"])
        plan["controller_active"] = plan["id"] in controller.TASKS
        plan["final_preview"] = output_preview(storage_root().parent, plan.get("final_video"))
        for row in plan["segments"]:
            job = row.get("job") or {}
            row["video_preview"] = output_preview(storage_root().parent, job.get("video"))
        return web.json_response(plan)

    @routes.get("/ty-h3lv/project/{project_id}/analysis")
    @endpoint
    async def get_analysis(request):
        root, pid = data_root(), request.match_info["project_id"]
        directory = project_path(root, pid)
        path = state_file(directory, "analysis.json")
        if path.is_file():
            result = json.loads(path.read_text(encoding="utf-8"))
        else:
            def legacy_analysis():
                import soundfile as sf
                plan = read_plan(root, pid)
                mix, sr = sf.read(audio_file(directory, "source.wav"), dtype="float32", always_2d=True)
                voice, voice_sr = sf.read(audio_file(directory, "vocals.wav"), dtype="float32", always_2d=True)
                if voice_sr != sr or len(voice) != len(mix):
                    raise ValueError("旧项目的原曲与人声文件长度不一致，无法绘制诊断波形。")
                transcript, reconstructed = read_project_transcript(directory, plan)
                _, value = segmentation(voice, sr, transcript, plan["mode"], plan["max_seconds"],
                                        plan["target_seconds"], mix_audio=mix, return_analysis=True)
                value.update(legacy=True,
                    legacy_notice=("旧项目缺少识别缓存，已用现有分段文本恢复预览；不会改写原分段。"
                                   if reconstructed else
                                   "旧项目诊断数据为打开面板时临时计算；不会改写原分段。"))
                return value
            result = await asyncio.to_thread(legacy_analysis)
        result["available"] = True
        return web.json_response(result)

    @routes.get("/ty-h3lv/project/{project_id}/director")
    @endpoint
    async def get_director(request):
        return web.json_response(director_review.director_state(
            data_root(), request.match_info["project_id"], server))

    @routes.post("/ty-h3lv/project/{project_id}/director/configure")
    @endpoint
    async def configure_director(request):
        return web.json_response(director_review.configure_director(
            data_root(), request.match_info["project_id"], await request.json(), server))

    @routes.post("/ty-h3lv/project/{project_id}/director/plan")
    @endpoint
    async def plan_director(request):
        return web.json_response(await director_review.queue_director(
            data_root(), request.match_info["project_id"], await request.json(), server))

    @routes.post("/ty-h3lv/project/{project_id}/director/preview")
    @endpoint
    async def preview_director(request):
        return web.json_response(director_review.preview_director(
            data_root(), request.match_info["project_id"], await request.json(), server))

    @routes.post("/ty-h3lv/project/{project_id}/director/approve")
    @endpoint
    async def approve_director(request):
        return web.json_response(director_review.approve_director(
            data_root(), request.match_info["project_id"], await request.json(), server))

    @routes.post("/ty-h3lv/project/{project_id}/director/output-fields")
    @endpoint
    async def save_director_output_fields(request):
        return web.json_response(director_review.save_external_qwen_fields(
            data_root(), request.match_info["project_id"], await request.json(), server))

    @routes.post("/ty-h3lv/project/{project_id}/edit")
    @endpoint
    async def edit(request):
        payload = await request.json()
        root, pid = data_root(), request.match_info["project_id"]
        with LOCK:
            plan = read_plan(root, pid)
            if int(payload.get("revision", -1)) != plan["revision"]:
                raise ValueError("方案已被另一个窗口修改，请刷新。")
            if plan.get("project_kind") == "shot_planner" and pid in controller.TASKS:
                raise ValueError("请等待生成或合成停止后再保存切点。")
            before = json.dumps(plan, ensure_ascii=False, indent=2)
            old_revision = plan["revision"]
            transcript, _ = read_project_transcript(project_path(root, pid), plan)
            plan = edit_plan(plan, payload["segments"], project_path(root, pid),
                             payload.get("reference_default_count", UNSET),
                             payload.get("reference_defaults", UNSET))
            rebuild_speech_timeline(plan["segments"], transcript, plan["sample_rate"])
            if plan.get("project_kind") == "shot_planner":
                backup = project_path(root, pid) / "state" / f"segments.before-edit-r{old_revision}.json"
                if not backup.exists():
                    temporary = backup.with_suffix(".json.tmp")
                    try:
                        temporary.write_text(before, encoding="utf-8")
                        replace_with_retry(temporary, backup)
                    finally:
                        temporary.unlink(missing_ok=True)
            write_plan(root, plan)
        return web.json_response(plan)

    @routes.post("/ty-h3lv/project/{project_id}/approve")
    @endpoint
    async def approve(request):
        payload = await request.json()
        root, pid = data_root(), request.match_info["project_id"]
        with LOCK:
            plan = read_plan(root, pid)
            if payload.get("revision") != plan["revision"]:
                raise ValueError("请刷新后重新确认。")
            plan.update(approved=True, approved_fingerprint=fingerprint(plan))
            director_review.adopt_manual_prompt_edits(root, plan)
            write_plan(root, plan)
        return web.json_response(plan)

    @routes.post("/ty-h3lv/project/{project_id}/clip")
    @endpoint
    async def clip(request):
        from .clip_project import create_clip_project
        payload = await request.json()
        plan = await asyncio.to_thread(
            create_clip_project, data_root(), request.match_info["project_id"], payload)
        return web.json_response(plan)

    @routes.post("/ty-h3lv/source/preview")
    @endpoint
    async def source_preview(request):
        from .clip_project import preview_source
        payload = await request.json()
        return web.json_response(await asyncio.to_thread(preview_source, payload))

    @routes.post("/ty-h3lv/source/clip")
    @endpoint
    async def source_clip(request):
        from .clip_project import clip_source
        payload = await request.json()
        return web.json_response(await asyncio.to_thread(clip_source, payload))

    @routes.post("/ty-h3lv/project/{project_id}/refs")
    @endpoint
    async def upload_reference(request):
        root, pid = data_root(), request.match_info["project_id"]
        directory = project_path(root, pid)
        reader = await request.multipart()
        index, filename, content = None, "", b""
        while (part := await reader.next()) is not None:
            if part.name == "index":
                index = int((await part.text()).strip() or 0)
            elif part.name == "image":
                filename = part.filename or ""
                content = await part.read(decode=False)
        if not filename:
            raise ValueError("没有收到参考图文件。")
        with LOCK:
            plan = read_plan(root, pid)
            if plan.get("run_status") in {"running", "pausing", "stopping", "merging"}:
                raise ValueError("生成任务正在运行，请先停止再修改参考图。")
            if index is not None and not 0 <= index < len(plan["segments"]):
                raise ValueError("分段编号超出范围。")
            name = store_reference_asset(directory, filename, content)
            library = collect_reference_library(plan)
            library.append(name)
            plan["reference_library"] = library
            write_plan(root, plan)
        return web.json_response({"name": name, "index": index,
                                  "assets": reference_asset_items(library)})

    @routes.post("/ty-h3lv/project/{project_id}/defaults")
    @endpoint
    async def upload_default_reference(request):
        root, pid = data_root(), request.match_info["project_id"]
        directory = project_path(root, pid)
        reader = await request.multipart()
        slot, filename, content = 0, "", b""
        while (part := await reader.next()) is not None:
            if part.name == "slot":
                slot = int((await part.text()).strip() or 0)
            elif part.name == "image":
                filename = part.filename or ""
                content = await part.read(decode=False)
        if not filename:
            raise ValueError("没有收到项目默认参考图文件。")
        with LOCK:
            plan = read_plan(root, pid)
            if plan.get("run_status") in {"running", "pausing", "stopping", "merging"}:
                raise ValueError("生成任务正在运行，请先停止再修改默认参考图。")
            name = store_default_reference(directory, slot, filename, content)
            library = collect_reference_library(plan)
            if name not in library:
                library.append(name)
            plan["reference_library"] = library
            write_plan(root, plan)
        return web.json_response({"name": name, "slot": slot,
                                  "assets": reference_asset_items(library)})

    @routes.get("/ty-h3lv/project/{project_id}/refs/{name}")
    @endpoint
    async def reference_file(request):
        root, pid = data_root(), request.match_info["project_id"]
        directory = project_path(root, pid)
        folder = reference_directory(directory)
        path = inside(folder, folder/request.match_info["name"])
        if not path.is_file():
            raise FileNotFoundError(str(path))
        return web.FileResponse(path, headers={"Cache-Control": "no-store"})

    @routes.post("/ty-h3lv/project/{project_id}/refs/remove")
    @endpoint
    async def delete_reference(request):
        payload = await request.json()
        root, pid = data_root(), request.match_info["project_id"]
        directory = project_path(root, pid)
        with LOCK:
            plan = read_plan(root, pid)
            if plan.get("run_status") in {"running", "pausing", "stopping", "merging"}:
                raise ValueError("生成任务正在运行，请先停止再修改参考图。")
            name = str(payload.get("name") or "").strip()
            if not name:
                raise ValueError("缺少要删除的参考图。")
            if any(name in (row.get("refs") or []) for row in plan["segments"]):
                raise ValueError("该参考图仍被某个分段使用，请先在分段卡片中移除。")
            if any(name == item.get("name") for item in plan.get("reference_defaults") or []):
                raise ValueError("该参考图仍是项目默认图，请先在项目默认图设置中替换或移除。")
            if any(name == item.get("name")
                   for row in plan["segments"]
                   for item in row.get("reference_slots") or []
                   if item.get("source") == "custom"):
                raise ValueError("该参考图仍被某个双图槽位使用，请先在分段卡片中移除。")
            remove_reference(directory, name)
            if "reference_library" in plan:
                plan["reference_library"] = [item for item in plan["reference_library"]
                                              if (item.get("name") if isinstance(item, dict)
                                                  else str(item)) != name]
                write_plan(root, plan)
        return web.json_response({"removed": name})

    @routes.get("/ty-h3lv/project/{project_id}/audio")
    @endpoint
    async def audio(request):
        import soundfile as sf
        plan = read_plan(data_root(), request.match_info["project_id"])
        a, b = preview_bounds(plan, request.query.get("index", 0),
            request.query.get("start"), request.query.get("end"), request.query.get("boundary") == "1")
        name = "vocals.wav" if request.query.get("vocals") == "1" else "source.wav"
        path = audio_file(project_path(data_root(), plan["id"]), name)
        def render():
            samples, sr = sf.read(path, start=a, stop=b, dtype="float32", always_2d=True)
            data = io.BytesIO()
            sf.write(data, samples, sr, format="WAV", subtype="PCM_16")
            return data.getvalue()
        body = await asyncio.to_thread(render)
        headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
        span = byte_range(request.headers.get("Range"), len(body))
        if span is None:
            return web.Response(body=body, content_type="audio/wav", headers=headers)
        start, end = span
        headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
        return web.Response(body=body[start:end+1], status=206,
                            content_type="audio/wav", headers=headers)

    @routes.post("/ty-h3lv/project/{project_id}/run")
    @endpoint
    async def run(request):
        payload = await request.json()
        controller.start(data_root(), request.match_info["project_id"], payload, server)
        return web.json_response({"started": True})

    @routes.post("/ty-h3lv/project/{project_id}/pause")
    @endpoint
    async def pause(request):
        root, pid = data_root(), request.match_info["project_id"]
        with LOCK:
            plan = read_plan(root, pid)
            plan.update(pause_requested=True)
            if pid in controller.TASKS and plan["run_status"] == "running":
                plan["run_status"] = "pausing"
            write_plan(root, plan)
        return web.json_response({"message": "当前片段完成后暂停；不会中断其他任务。"})

    @routes.post("/ty-h3lv/project/{project_id}/stop")
    @endpoint
    async def stop(request):
        root, pid = data_root(), request.match_info["project_id"]
        with LOCK:
            plan = read_plan(root, pid)
            if plan.get("run_status") == "merging":
                raise ValueError("正在合并最终视频，请等待合并完成。")
            plan["stop_requested"] = True
            if pid in controller.TASKS:
                plan["run_status"] = "stopping"
            else:
                plan["run_status"] = "stopped"
            write_plan(root, plan)
        return web.json_response({"message": "停止请求已记录；当前片段完成后不会再提交后续片段。"})

    @routes.post("/ty-h3lv/project/{project_id}/retry")
    @endpoint
    async def retry(request):
        payload = await request.json()
        root, pid = data_root(), request.match_info["project_id"]
        with LOCK:
            if pid in controller.TASKS:
                raise ValueError("请等待当前任务停止。")
            plan = read_plan(root, pid)
            row = plan["segments"][int(payload["index"])]
            if row.get("job", {}).get("prompt_id") in controller.queued_ids(server):
                raise ValueError("该段仍在队列中，不能重复提交。")
            request_regeneration(row, "用户要求重新生成本段")
            plan.update(run_status="paused", error="")
            if plan.get("final_video"):
                plan["final_stale"] = True
            write_plan(root, plan)
        return web.json_response(plan)

    @routes.post("/ty-h3lv/project/{project_id}/regenerate")
    @endpoint
    async def regenerate(request):
        payload = await request.json()
        root, pid = data_root(), request.match_info["project_id"]
        with LOCK:
            if pid in controller.TASKS:
                raise ValueError("请等待当前任务停止。")
            plan = read_plan(root, pid)
            row = plan["segments"][int(payload["index"])]
            if row.get("job", {}).get("prompt_id") in controller.queued_ids(server):
                raise ValueError("该段仍在队列中，不能重复提交。")
            request_regeneration(row, "用户要求重新生成本段")
            plan.update(run_status="paused", error="")
            if plan.get("final_video"):
                plan["final_stale"] = True
            write_plan(root, plan)
        payload["replace_snapshot"] = True
        payload["only_segment_index"] = int(payload["index"])
        controller.start(root, pid, payload, server)
        return web.json_response({"started": True, "index": int(payload["index"])})

    @routes.post("/ty-h3lv/project/{project_id}/restore")
    @endpoint
    async def restore(request):
        payload = await request.json()
        root, pid = data_root(), request.match_info["project_id"]
        with LOCK:
            if pid in controller.TASKS:
                raise ValueError("请等待当前任务停止。")
            plan = read_plan(root, pid)
            row = plan["segments"][int(payload["index"])]
            takes = row.setdefault("takes", [])
            if not takes:
                raise ValueError("该段没有可恢复的旧版本。")
            previous = takes.pop()
            current = row.get("job")
            if current and current.get("status") == "completed":
                archive_take(row, current)
            row["job"] = copy.deepcopy(previous)
            row.pop("needs_regeneration", None)
            row.pop("regeneration_reason", None)
            row.pop("replacement_previous_job", None)
            plan.update(run_status="paused", final_stale=True, error="")
            write_plan(root, plan)
        return web.json_response(plan)

    @routes.post("/ty-h3lv/project/{project_id}/assemble")
    @endpoint
    async def assemble_only(request):
        root, pid = data_root(), request.match_info["project_id"]
        with LOCK:
            if pid in controller.TASKS:
                raise ValueError("仍有生成任务运行中。")
            plan = read_plan(root, pid)
            if (plan.get("full_regeneration_pending")
                    or any(row.get("needs_regeneration")
                           or row.get("job", {}).get("status") != "completed"
                           for row in plan["segments"])):
                raise ValueError("仍有片段需要生成，暂不能只合成。")
            plan["run_status"] = "merging"
            write_plan(root, plan)
        try:
            final = await asyncio.to_thread(controller.assemble, root, pid)
            with LOCK:
                plan = read_plan(root, pid)
                previous_final = plan.get("final_video")
                if previous_final and previous_final != final:
                    versions = plan.setdefault("final_versions", [])
                    if previous_final not in versions:
                        versions.append(previous_final)
                plan.update(run_status="completed", final_video=final, final_stale=False, error="")
                plan.pop("full_regeneration_pending", None)
                plan.pop("full_regeneration_reason", None)
                write_plan(root, plan)
            preview = output_preview(storage_root().parent, final)
            plan["final_preview"] = preview
            if preview and callable(getattr(server, "send_sync", None)):
                server.send_sync("ty-h3lv-final", {"project_id": pid, "preview": preview})
            return web.json_response(plan)
        except Exception:
            with LOCK:
                plan = read_plan(root, pid)
                plan["run_status"] = "failed"
                write_plan(root, plan)
            raise

    @routes.get("/ty-h3lv/project/{project_id}/final")
    @endpoint
    async def final(request):
        plan = read_plan(data_root(), request.match_info["project_id"])
        file = inside(storage_root()/"final_videos", plan["final_video"])
        return web.FileResponse(file, headers={"Cache-Control": "no-store"})

    @routes.post("/ty-h3lv/project/{project_id}/reveal-final")
    @endpoint
    async def reveal_final(request):
        plan = read_plan(data_root(), request.match_info["project_id"])
        file = inside(storage_root()/"final_videos", plan["final_video"])
        revealed = await asyncio.to_thread(controller.reveal_file, file)
        return web.json_response({"path": revealed})
