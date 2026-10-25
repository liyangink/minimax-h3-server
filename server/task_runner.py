"""Launch, monitor, and recover local h3 generation processes."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

from server import task_store
from server.config import ServerConfig
from server.contracts import TaskRecord


logger = logging.getLogger(__name__)
progress_pattern = re.compile(
    rb"(?:^|[\r\n]) *([A-Za-z][A-Za-z0-9 ]{0,40}?) +([0-9]+)/([0-9]+)"
)
phase_ranges = {
    "tokenizer": (1, 3),
    "Qwen vision": (3, 12),
    "audio VAE encoder": (3, 12),
    "video VAE encoder": (3, 12),
    "text encoder": (12, 23),
    "refine text": (23, 25),
    "load transformer core": (25, 32),
    "precompute AdaLN": (32, 35),
    "denoise enqueue": (35, 85),
    "denoise": (35, 85),
    "preview VAE load": (85, 90),
    "audio VAE": (85, 91),
    "video VAE load": (90, 94),
    "FFmpeg": (94, 99),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskRunner:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._processes: dict[str, subprocess.Popen] = {}
        self._failed_launches: dict[str, tuple[str, subprocess.Popen | None]] = {}
        self._recovery_retries: set[str] = set()
        self._lock = threading.RLock()

    def _paths(self, task_id: str) -> tuple[Path, Path, Path]:
        directory = self.config.artifact_dir / task_id
        return (
            directory / "output.part.mp4",
            directory / "video.mp4",
            self.config.log_dir / "tasks" / f"{task_id}.log",
        )

    def _input_path(self, task_id: str, item: dict) -> str:
        relative = Path(item["path"])
        directory = (self.config.upload_temp_dir / task_id).resolve()
        path = (directory / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(directory):
            raise ValueError("task input path escapes its upload directory")
        return str(path)

    def _argv(self, task: TaskRecord, output: Path) -> list[str]:
        execution = task["execution"]
        if not execution:
            raise ValueError("task has no execution parameters")
        argv = [
            str(self.config.h3_binary), "-d", str(self.config.model_dir),
            "-p", task["request"]["prompt"], "-o", str(output),
        ]
        for key, option in (
            ("width", "--width"), ("height", "--height"),
            ("frames", "--frames"), ("steps", "--steps"),
            ("reuse", "--reuse"), ("layers", "--layers"),
            ("coreReuse", "--core-reuse"), ("seed", "--seed"),
        ):
            argv.extend((option, str(execution[key])))
        argv.extend(execution["hardwareArgs"])

        inputs = task["inputs"]
        frames = [item for item in inputs if item["role"] in ("firstFrame", "lastFrame")]
        references = [item for item in inputs if item["role"] == "reference"]
        if frames and references:
            raise ValueError("frame conditioning and references are mutually exclusive")
        for role, option in (("firstFrame", "--first-frame"), ("lastFrame", "--last-frame")):
            matches = [item for item in frames if item["role"] == role]
            if len(matches) > 1:
                raise ValueError(f"multiple {role} inputs")
            if matches:
                argv.extend((option, self._input_path(task["id"], matches[0])))

        audio_inputs = {
            item["referenceIndex"]: item
            for item in inputs if item["role"] == "referenceAudio"
        }
        for item in sorted(references, key=lambda value: value["referenceIndex"]):
            kind = item["kind"]
            path = self._input_path(task["id"], item)
            if kind == "videoWithAudio":
                audio = audio_inputs.get(item["referenceIndex"])
                if audio is None:
                    raise ValueError("videoWithAudio reference has no audio input")
                argv.extend(("--ref-video-audio", path, self._input_path(task["id"], audio)))
            else:
                option = {
                    "image": "--ref-image",
                    "silentVideo": "--ref-silent-video",
                    "video": "--ref-video",
                    "audio": "--ref-audio",
                }.get(kind)
                if option is None:
                    raise ValueError(f"unsupported reference kind: {kind}")
                argv.extend((option, path))
        return argv

    def start_task(self, task_id: str) -> None:
        try:
            task = task_store.get_task(self.config.db_path, task_id)
        except Exception as exc:
            self._fail_launch(task_id, f"读取待启动任务失败：{exc}")
            raise
        if task is None or task["status"] != "preparing":
            raise ValueError("task is not prepared for launch")
        try:
            output, _, log_path = self._paths(task_id)
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            argv = self._argv(task, output)
            descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                with os.fdopen(descriptor, "ab", buffering=0) as log_file:
                    process = subprocess.Popen(
                        argv,
                        shell=False,
                        cwd=self.config.h3_cwd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=log_file,
                        start_new_session=True,
                        close_fds=True,
                    )
            except Exception:
                # fdopen owns the descriptor once it succeeds.
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
        except Exception as exc:
            self._fail_launch(task_id, f"h3 启动失败：{exc}")
            raise
        try:
            try:
                create_time = psutil.Process(process.pid).create_time()
            except psutil.NoSuchProcess:
                create_time = time.time()
            task_store.mark_running(
                self.config.db_path, task_id, process.pid, create_time, _now()
            )
        except Exception:
            self._fail_launch(
                task_id, "子进程已启动，但登记进程身份失败；已停止该进程组",
                process,
            )
            raise
        with self._lock:
            self._processes[task_id] = process

    def _stop_new_process(self, process: subprocess.Popen) -> bool:
        if process.poll() is not None:
            return True
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=10)
            return True
        except ProcessLookupError:
            return process.poll() is not None
        except Exception:
            logger.exception("could not stop process group %s after registration error", process.pid)
            return False

    def _fail_launch(
        self, task_id: str, message: str, process: subprocess.Popen | None = None
    ) -> None:
        with self._lock:
            self._failed_launches[task_id] = (message, process)
        self._settle_failed_launch(task_id)

    def _settle_failed_launch(self, task_id: str) -> None:
        with self._lock:
            pending = self._failed_launches.get(task_id)
        if pending is None:
            return
        message, process = pending
        if process is not None and not self._stop_new_process(process):
            return
        try:
            task = task_store.get_task(self.config.db_path, task_id)
            if task is not None and task["status"] in ("preparing", "running"):
                task_store.complete_task(
                    self.config.db_path, task_id, "failed",
                    process.poll() if process is not None else None,
                    message, _now(),
                )
        except Exception:
            logger.exception("could not settle failed launch for task %s; will retry", task_id)
            return
        with self._lock:
            self._failed_launches.pop(task_id, None)

    def _matches_process(self, process: psutil.Process, output: Path) -> bool:
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
        args = process.cmdline()
        return any(
            argument == "-o" and index + 1 < len(args) and args[index + 1] == str(output)
            for index, argument in enumerate(args)
        )

    def _find_process(self, output: Path) -> psutil.Process | None:
        for process in psutil.process_iter():
            try:
                if self._matches_process(process, output):
                    return process
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied):
                continue
        return None

    def _saved_process_alive(self, task: TaskRecord, output: Path) -> bool:
        pid = task["pid"]
        create_time = task["processCreateTime"]
        if pid is None or create_time is None:
            return False
        try:
            process = psutil.Process(pid)
            return (
                abs(process.create_time() - create_time) < 0.01
                and self._matches_process(process, output)
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return False

    def _log_progress(self, task: TaskRecord, log_path: Path) -> None:
        try:
            with log_path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                stream.seek(max(0, stream.tell() - 65536))
                chunk = stream.read()
        except FileNotFoundError:
            return
        matches = list(progress_pattern.finditer(chunk))
        for match in reversed(matches):
            phase = match.group(1).decode("ascii").strip()
            bounds = phase_ranges.get(phase)
            total = int(match.group(3))
            if not bounds or total <= 0:
                continue
            completed = min(int(match.group(2)), total)
            estimate = bounds[0] + (bounds[1] - bounds[0]) * completed // total
            percent = max(task["progressPercent"], min(estimate, 99))
            if phase != task["phase"] or percent != task["progressPercent"]:
                task_store.update_progress(self.config.db_path, task["id"], phase, percent)
            return

    def _probe_video(self, path: Path) -> None:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("生成文件不存在或为空")
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type",
             "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            raise ValueError(f"FFprobe 无法读取生成文件：{result.stderr.strip()[:300]}")
        streams = json.loads(result.stdout).get("streams", [])
        kinds = {stream.get("codec_type") for stream in streams}
        if "video" not in kinds or "audio" not in kinds:
            raise ValueError("生成文件缺少视频流或音轨")

    def _finish(self, task: TaskRecord, exit_code: int | None) -> None:
        output, final, _ = self._paths(task["id"])
        try:
            if exit_code not in (None, 0):
                raise ValueError(f"h3 子进程退出码 {exit_code}，详见任务日志")
            artifact = final if final.exists() else output
            self._probe_video(artifact)
            if artifact == output:
                os.replace(output, final)
        except (OSError, ValueError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            status, error_message = "failed", str(exc)
        else:
            status, error_message = "succeeded", None
        task_store.complete_task(
            self.config.db_path, task["id"], status, exit_code, error_message, _now()
        )
        with self._lock:
            self._processes.pop(task["id"], None)

    def poll_once(self) -> None:
        with self._lock:
            failed_launch_ids = list(self._failed_launches)
            recovery_ids = list(self._recovery_retries)
        for task_id in failed_launch_ids:
            self._settle_failed_launch(task_id)
        for task_id in recovery_ids:
            try:
                task = task_store.get_task(self.config.db_path, task_id)
                if task is not None and task["status"] in ("preparing", "running"):
                    self._recover_task(task)
                with self._lock:
                    self._recovery_retries.discard(task_id)
            except Exception:
                logger.exception("could not retry recovery for task %s", task_id)
        for task in task_store.list_pending(self.config.db_path):
            try:
                if task["status"] == "preparing":
                    continue
                output, _, log_path = self._paths(task["id"])
                with self._lock:
                    process = self._processes.get(task["id"])
                if process is not None:
                    exit_code = process.poll()
                    if exit_code is not None:
                        self._finish(task, exit_code)
                        continue
                elif not self._saved_process_alive(task, output):
                    # The original parent may have exited during server downtime.
                    # A process found by task-specific output remains authoritative.
                    if self._find_process(output) is None:
                        self._finish(task, None)
                        continue
                self._log_progress(task, log_path)
            except Exception:
                logger.exception("could not poll task %s", task["id"])

    def recover_pending(self) -> None:
        for task in task_store.list_pending(self.config.db_path):
            try:
                self._recover_task(task)
            except Exception:
                logger.exception("could not recover task %s", task["id"])
                if task["status"] == "preparing":
                    with self._lock:
                        self._recovery_retries.add(task["id"])

    def _recover_task(self, task: TaskRecord) -> None:
        output, _, log_path = self._paths(task["id"])
        process = None
        if task["status"] == "running" and self._saved_process_alive(task, output):
            process = psutil.Process(task["pid"])
        if process is None:
            process = self._find_process(output)
        if process is not None:
            if task["status"] == "preparing":
                task_store.mark_running(
                    self.config.db_path, task["id"], process.pid,
                    process.create_time(), _now(),
                )
            self._log_progress(task, log_path)
        else:
            self._finish(task, None)
