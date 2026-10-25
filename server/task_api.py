"""Authenticated task API and multipart upload handling."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import shutil
import stat
import subprocess
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import MalformedRangeHeader, RangeNotSatisfiable

from server import task_store
from server.auth import AuthManager
from server.config import ServerConfig
from server.contracts import ApiError, TaskInput, TaskRecord
from server.task_runner import TaskRunner


logger = logging.getLogger(__name__)
parameter_limits = {
    "layers": (35, 50),
    "steps": (2, 1000),
    "reuse": (1, 3),
    "coreReuse": (1, 6),
}
parameter_names = set(parameter_limits) | {
    "width", "height", "seed", "frames", "seconds",
}
reference_kinds = {"image", "silentVideo", "video", "audio", "videoWithAudio"}
inline_media_types = {
    "image/png", "image/jpeg", "image/webp", "image/gif",
    "video/mp4", "video/webm", "video/quicktime",
    "audio/mpeg", "audio/mp4", "audio/wav", "audio/x-wav",
    "audio/aac", "audio/ogg", "audio/webm",
}


class TaskMultipartParser(MultiPartParser):
    def on_part_end(self) -> None:
        if self._current_part.field_name == "request" and self._current_part.file is None:
            self.items.append(("request", self._current_part.data))
            return
        super().on_part_end()


def _bad(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message)


def _number(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise _bad(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _aligned_frames(requested: int) -> int:
    return 5 + 17 * max(1, (requested - 5 + 16) // 17)


def _execution(config: ServerConfig, parameters: object) -> dict:
    if not isinstance(parameters, dict) or set(parameters) - parameter_names:
        raise _bad("parameters must contain only supported fields")
    if "frames" in parameters and "seconds" in parameters:
        raise _bad("frames and seconds are mutually exclusive")
    values = dict(config.task_defaults)
    if "frames" in parameters:
        values.pop("seconds", None)
    if "seconds" in parameters:
        values.pop("frames", None)
    values.update(parameters)

    for name in ("width", "height"):
        value = _number(values[name], name, 32, 768 * 1344)
        if value % 32:
            raise _bad(f"{name} must be a multiple of 32")
    if values["width"] * values["height"] > 768 * 1344:
        raise _bad("output canvas exceeds 768x1344 pixels")
    render_width, render_height = config.render_width, config.render_height
    if render_width is not None and render_height is not None:
        if (values["width"] < render_width or values["height"] < render_height or
                values["width"] * render_height != values["height"] * render_width):
            raise _bad("output canvas must match and contain the internal render canvas")

    for name, (minimum, maximum) in parameter_limits.items():
        _number(values[name], name, minimum, maximum)
    if values["reuse"] > 1 and values["coreReuse"] > 1:
        raise _bad("reuse and coreReuse cannot both exceed 1")
    seed = values["seed"]
    if (not isinstance(seed, str) or not seed or len(seed) > 20 or
            not seed.isascii() or not seed.isdecimal() or int(seed) > 2**64 - 1):
        raise _bad("seed must be a decimal string in uint64 range")

    if "frames" in values:
        requested_frames = _number(values["frames"], "frames", 1, 362)
    else:
        seconds = values["seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise _bad("seconds must be a positive number")
        try:
            duration = Decimal(str(seconds))
            if not duration.is_finite() or duration <= 0 or duration > 16:
                raise ValueError
            requested_frames = int((duration * 24).to_integral_value(rounding=ROUND_HALF_UP))
        except (InvalidOperation, ValueError) as exc:
            raise _bad("seconds must be a positive duration within the model range") from exc
        if requested_frames < 1:
            raise _bad("seconds is too short to produce a frame")
    frames = _aligned_frames(requested_frames)
    if not 22 <= frames <= 362:
        raise _bad("requested duration exceeds 362 frames")
    return {
        "width": values["width"], "height": values["height"],
        "layers": values["layers"], "steps": values["steps"],
        "reuse": values["reuse"], "coreReuse": values["coreReuse"],
        "seed": seed, "frames": frames, "actualSeconds": frames / 24,
        "hardwareArgs": list(config.hardware_args),
    }


def _input_specs(conditioning: object, frames: int) -> list[tuple[str, str, str, int | None]]:
    if not isinstance(conditioning, dict):
        raise _bad("conditioning must be an object")
    if set(conditioning) - {"firstFrameFileKey", "lastFrameFileKey", "references"}:
        raise _bad("conditioning contains unsupported fields")
    has_frames = any(key in conditioning for key in ("firstFrameFileKey", "lastFrameFileKey"))
    has_refs = "references" in conditioning
    if has_frames and has_refs:
        raise _bad("frame anchors and references are mutually exclusive")
    specs: list[tuple[str, str, str, int | None]] = []
    if has_frames:
        for name, role in (("firstFrameFileKey", "firstFrame"),
                           ("lastFrameFileKey", "lastFrame")):
            if name in conditioning:
                specs.append((role, "image", conditioning[name], None))
    if has_refs:
        references = conditioning["references"]
        if not isinstance(references, list) or not 1 <= len(references) <= 12:
            raise _bad("references must contain 1 to 12 entries")
        images = videos = audio = 0
        for index, reference in enumerate(references):
            if not isinstance(reference, dict) or reference.get("kind") not in reference_kinds:
                raise _bad(f"reference {index + 1} has an unsupported kind")
            kind = reference["kind"]
            expected = {"kind", "fileKey"} | ({"audioFileKey"} if kind == "videoWithAudio" else set())
            if set(reference) != expected:
                raise _bad(f"reference {index + 1} has invalid fields")
            specs.append(("reference", kind, reference["fileKey"], index))
            if kind == "videoWithAudio":
                specs.append(("referenceAudio", "audio", reference["audioFileKey"], index))
            images += kind == "image"
            videos += kind in ("silentVideo", "video", "videoWithAudio")
            audio += kind in ("audio", "video", "videoWithAudio")
            if kind in ("video", "videoWithAudio") and frames < 56:
                raise _bad("references with sound require at least 56 output frames")
        if images > 9 or videos > 3 or audio > 3:
            raise _bad("references exceed 9 images, 3 videos, or 3 audio inputs")
        if images + videos == 0:
            raise _bad("audio references require an image or video reference")
    keys = [key for _, _, key, _ in specs]
    if any(not isinstance(key, str) or not key for key in keys) or len(keys) != len(set(keys)):
        raise _bad("fileKey values must be distinct nonempty strings")
    return specs


def _decode_request(raw: bytes | bytearray) -> dict:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise _bad("request must use valid UTF-8") from exc
    try:
        payload = json.loads(text, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (json.JSONDecodeError, ValueError) as exc:
        raise _bad("request must contain valid JSON") from exc
    if not isinstance(payload, dict) or set(payload) - {"prompt", "parameters", "conditioning"}:
        raise _bad("request must contain only prompt, parameters, and conditioning")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt or "\x00" in prompt:
        raise _bad("prompt must be a nonempty string without NUL")
    try:
        prompt.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise _bad("prompt must contain valid Unicode characters") from exc
    return payload


async def _parse_upload(request: Request, max_bytes: int) -> tuple[dict, dict[str, UploadFile], FormData]:
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        raise _bad("Content-Type must be multipart/form-data")
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > max_bytes:
                raise ApiError(413, "request_too_large", "Request exceeds maxRequestBytes")
        except ValueError as exc:
            raise _bad("Invalid Content-Length") from exc
    consumed = 0
    receive = request.receive

    async def limited_receive() -> dict:
        nonlocal consumed
        message = await receive()
        if message["type"] == "http.request":
            consumed += len(message.get("body", b""))
            if consumed > max_bytes:
                raise ApiError(413, "request_too_large", "Request exceeds maxRequestBytes")
        return message

    bounded_request = Request(request.scope, limited_receive)
    parser = TaskMultipartParser(
        bounded_request.headers, bounded_request.stream(), max_part_size=max_bytes,
    )
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise _bad(exc.message) from exc
    fields: dict[str, object] = {}
    for key, value in form.multi_items():
        if key in fields:
            await form.close()
            raise _bad(f"Duplicate multipart field: {key}")
        fields[key] = value
    raw = fields.pop("request", None)
    if not isinstance(raw, (bytes, bytearray)):
        await form.close()
        raise _bad("request must be a UTF-8 JSON form field")
    try:
        payload = await asyncio.to_thread(_decode_request, raw)
        files: dict[str, UploadFile] = {}
        for key, value in fields.items():
            if not isinstance(value, UploadFile):
                raise _bad(f"{key} must be a file part")
            files[key] = value
        return payload, files, form
    except BaseException:
        await form.close()
        raise


def _suffix(filename: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    return suffix if 1 < len(suffix) <= 12 and suffix[1:].isascii() and suffix[1:].isalnum() else ".bin"


def _copy_uploads(
    directory: Path,
    specs: list[tuple[str, str, str, int | None]],
    files: dict[str, UploadFile],
) -> list[TaskInput]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    inputs: list[TaskInput] = []
    for index, (role, kind, key, reference_index) in enumerate(specs):
        upload = files[key]
        filename = f"input{index:02d}{_suffix(upload.filename)}"
        path = directory / filename
        with path.open("xb") as target:
            shutil.copyfileobj(upload.file, target, length=1024 * 1024)
        inputs.append({
            "role": role, "kind": kind,
            "originalName": upload.filename or key,
            "path": filename, "referenceIndex": reference_index,
        })
    return inputs


def _probe_duration(path: Path, kind: str) -> Decimal:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,duration:format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or "FFprobe could not read the file")
        data = json.loads(result.stdout)
        streams = [stream for stream in data.get("streams", [])
                   if stream.get("codec_type") == kind]
        if not streams:
            raise ValueError(f"missing {kind} stream")
        durations = []
        for stream in streams:
            try:
                value = Decimal(str(stream["duration"]))
                if value.is_finite() and value > 0:
                    durations.append(value)
            except (KeyError, InvalidOperation):
                pass
        if durations:
            return max(durations)
        duration = Decimal(str(data.get("format", {}).get("duration")))
        if not duration.is_finite() or duration <= 0:
            raise ValueError("media duration is unavailable")
        return duration
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError,
            InvalidOperation, ValueError) as exc:
        logger.warning("Media duration probe failed for %s: %s", path, exc)
        raise _bad(f"Cannot determine {kind} duration") from exc


def _aligned_video_frames(path: Path, max_frames: int) -> int:
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0", "-an",
             "-vf", "fps=24,scale=32:32:flags=lanczos,setsar=1",
             "-frames:v", str(max_frames), "-f", "rawvideo", "-pix_fmt", "rgb24",
             "pipe:1"],
            capture_output=True, timeout=120, check=False,
        )
        frame_bytes = 32 * 32 * 3
        if result.returncode or len(result.stdout) % frame_bytes:
            raise ValueError("FFmpeg could not decode bounded video")
        count = len(result.stdout) // frame_bytes
        if count < 5:
            raise ValueError("reference videos require at least 5 decoded frames")
        return count - (count - 5) % 17
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        logger.warning("Video frame probe failed for %s: %s", path, exc)
        raise _bad("Cannot determine aligned reference video frames") from exc


def _validate_audio_inputs(directory: Path, inputs: list[TaskInput], frames: int) -> None:
    audio_by_reference = {
        item["referenceIndex"]: item for item in inputs
        if item["role"] == "referenceAudio"
    }
    total_duration = Decimal(0)
    for item in inputs:
        if item["role"] != "reference" or item["kind"] not in (
            "audio", "video", "videoWithAudio",
        ):
            continue
        media_path = directory / item["path"]
        if item["kind"] == "videoWithAudio":
            audio_item = audio_by_reference[item["referenceIndex"]]
            audio_path = directory / audio_item["path"]
        else:
            audio_path = media_path
        audio_duration = _probe_duration(audio_path, "audio")
        if item["kind"] == "audio":
            effective = audio_duration
        else:
            aligned_frames = _aligned_video_frames(media_path, frames)
            if aligned_frames < 56:
                raise _bad("Video references with sound need at least 56 aligned input frames")
            effective = min(audio_duration, Decimal(aligned_frames) / 24)
        if effective < 2 or (item["kind"] == "audio" and effective > 15):
            raise _bad("Audio references must contain 2 to 15 seconds after video trimming")
        total_duration += effective
    if total_duration > 15:
        raise _bad("Combined reference audio exceeds 15 seconds")


def _public_task(task: TaskRecord, *, detailed: bool) -> dict:
    result = dict(task)
    result.pop("pid", None)
    result.pop("processCreateTime", None)
    if detailed:
        result["inputs"] = [
            {**item, "url": f"/v1/tasks/{task['id']}/inputs/{index}"}
            for index, item in enumerate(task["inputs"])
        ]
        result["artifactUrl"] = (
            f"/v1/tasks/{task['id']}/artifact" if task["status"] == "succeeded" else None
        )
    else:
        result.pop("inputs", None)
    return result


async def _task(config: ServerConfig, task_id: str) -> TaskRecord:
    task = await asyncio.to_thread(task_store.get_task, config.db_path, task_id)
    if task is None:
        raise ApiError(404, "task_not_found", "Task not found")
    return task


async def _file_response(request: Request, path: Path, media_type: str) -> FileResponse:
    try:
        file_stat = await asyncio.to_thread(path.stat)
    except FileNotFoundError as exc:
        raise ApiError(410, "file_gone", "Task file is missing") from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise ApiError(410, "file_gone", "Task file is missing")
    byte_range = request.headers.get("range")
    if byte_range and not request.headers.get("if-range"):
        try:
            FileResponse._parse_range_header(byte_range, file_stat.st_size)
        except MalformedRangeHeader as exc:
            raise ApiError(400, "invalid_range", "Invalid Range header") from exc
        except RangeNotSatisfiable as exc:
            raise ApiError(416, "range_not_satisfiable", "Range is outside the file") from exc
    response = FileResponse(path, media_type=media_type, stat_result=file_stat)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def create_task_router(config: ServerConfig, auth: AuthManager, runner: TaskRunner) -> APIRouter:
    router = APIRouter(prefix="/v1")

    @router.get("/health")
    async def health() -> dict:
        return {"ready": True, "version": "0.1.0"}

    @router.get("/capabilities")
    async def capabilities(request: Request) -> dict:
        auth.require_task_auth(request)
        pending = await asyncio.to_thread(task_store.list_pending, config.db_path)
        return {
            "taskDefaults": config.task_defaults,
            "parameterRanges": {
                "width": {"min": 32, "multiple": 32},
                "height": {"min": 32, "multiple": 32},
                "maxPixels": 768 * 1344,
                **{name: {"min": low, "max": high} for name, (low, high) in parameter_limits.items()},
                "seed": {"min": "0", "max": str(2**64 - 1)},
                "frames": {"min": 22, "max": 362, "step": 17},
                "seconds": {"minExclusive": 0, "max": 362 / 24},
            },
            "conditioningLimits": {"references": 12, "images": 9, "videos": 3, "audio": 3},
            "maxConcurrentJobs": config.max_concurrent_jobs,
            "activeJobs": len(pending),
        }

    @router.post("/tasks", status_code=202)
    async def create_task(request: Request) -> dict:
        auth.require_task_auth(request, write=True)
        task_id = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()
        admitted = await asyncio.to_thread(
            task_store.claim_task, config.db_path, task_id,
            config.max_concurrent_jobs, created_at,
        )
        if not admitted:
            raise ApiError(429, "capacity_reached", "Maximum concurrent jobs reached")

        directory = config.upload_temp_dir / task_id
        form: FormData | None = None
        try:
            payload, files, form = await _parse_upload(request, config.max_request_bytes)
            execution = _execution(config, payload.get("parameters", {}))
            specs = _input_specs(payload.get("conditioning", {}), execution["frames"])
            expected = {key for _, _, key, _ in specs}
            if set(files) != expected:
                raise _bad("Uploaded file parts must match all fileKey values exactly")
            inputs = await asyncio.to_thread(_copy_uploads, directory, specs, files)
            await asyncio.to_thread(_validate_audio_inputs, directory, inputs, execution["frames"])
            await asyncio.to_thread(
                task_store.save_prepared, config.db_path, task_id, payload, execution, inputs,
            )
            await form.close()
            form = None
        except BaseException as exc:
            try:
                await asyncio.to_thread(task_store.remove_preparing, config.db_path, task_id)
            except Exception:
                logger.exception("Failed to remove rejected task %s", task_id)
            try:
                await asyncio.to_thread(shutil.rmtree, directory)
            except FileNotFoundError:
                pass
            except OSError:
                logger.exception("Failed to clean up rejected upload for %s", task_id)
            if isinstance(exc, OSError):
                raise ApiError(507, "storage_error", "Could not save task inputs") from exc
            raise
        finally:
            if form is not None:
                try:
                    await form.close()
                except Exception:
                    logger.exception("Failed to close upload form for %s", task_id)

        try:
            await asyncio.to_thread(runner.start_task, task_id)
        except Exception as exc:
            logger.exception("Failed to start h3 task %s", task_id)
            raise ApiError(500, "launch_failed", f"Task {task_id} failed to start") from exc
        return {
            "id": task_id, "status": "running",
            "statusUrl": f"/v1/tasks/{task_id}",
            "actualFrames": execution["frames"],
            "actualSeconds": execution["actualSeconds"],
        }

    @router.get("/tasks")
    async def list_tasks(request: Request) -> dict:
        auth.require_task_auth(request)
        status = request.query_params.get("status")
        cursor = request.query_params.get("cursor")
        raw_limit = request.query_params.get("limit", "50")
        try:
            limit = int(raw_limit)
            if not 1 <= limit <= 100:
                raise ValueError
            items, next_cursor = await asyncio.to_thread(
                task_store.list_tasks, config.db_path, status, limit, cursor,
            )
        except ValueError as exc:
            raise _bad("Invalid status, limit, or cursor") from exc
        return {"items": [_public_task(item, detailed=False) for item in items],
                "nextCursor": next_cursor}

    @router.get("/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> dict:
        auth.require_task_auth(request)
        return _public_task(await _task(config, task_id), detailed=True)

    @router.get("/tasks/{task_id}/artifact")
    async def get_artifact(request: Request, task_id: str) -> FileResponse:
        auth.require_task_auth(request)
        task = await _task(config, task_id)
        if task["status"] != "succeeded":
            raise ApiError(409, "task_not_complete", "Task has no completed artifact")
        return await _file_response(request, config.artifact_dir / task_id / "video.mp4", "video/mp4")

    @router.get("/tasks/{task_id}/inputs/{index}")
    async def get_input(request: Request, task_id: str, index: int) -> FileResponse:
        auth.require_task_auth(request)
        task = await _task(config, task_id)
        if index < 0 or index >= len(task["inputs"]):
            raise ApiError(404, "input_not_found", "Input not found")
        item = task["inputs"][index]
        directory = (config.upload_temp_dir / task_id).resolve()
        path = (directory / item["path"]).resolve()
        if not path.is_relative_to(directory):
            raise ApiError(410, "file_gone", "Task file is missing")
        media_type = mimetypes.guess_type(item["originalName"])[0] or "application/octet-stream"
        category = "audio" if item["role"] == "referenceAudio" else (
            "image" if item["kind"] == "image" else
            "audio" if item["kind"] == "audio" else "video"
        )
        inline = media_type in inline_media_types and media_type.startswith(category + "/")
        response = await _file_response(
            request, path, media_type if inline else "application/octet-stream",
        )
        if not inline:
            response.headers["Content-Disposition"] = "attachment"
        return response

    @router.get("/tasks/{task_id}/log")
    async def get_log(request: Request, task_id: str) -> dict:
        auth.require_task_auth(request)
        await _task(config, task_id)
        raw_tail = request.query_params.get("tailBytes", "65536")
        try:
            tail_bytes = int(raw_tail)
            if not 1 <= tail_bytes <= 1048576:
                raise ValueError
        except ValueError as exc:
            raise _bad("tailBytes must be between 1 and 1048576") from exc
        path = config.log_dir / "tasks" / f"{task_id}.log"

        def read_tail() -> str:
            try:
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - tail_bytes))
                    return stream.read().decode("utf-8", errors="replace")
            except FileNotFoundError:
                return ""

        return {"text": await asyncio.to_thread(read_tail)}

    return router
