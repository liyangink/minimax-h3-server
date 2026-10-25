"""Validated, immutable server configuration."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ServerConfig:
    api_host: str
    api_port: int
    admin_port: int
    max_request_bytes: int
    h3_binary: Path
    model_dir: Path
    h3_cwd: Path
    db_dir: Path
    upload_temp_dir: Path
    artifact_dir: Path
    log_dir: Path
    tokens: tuple[str, ...]
    max_concurrent_jobs: int
    log_level: int
    hardware_args: tuple[str, ...]
    render_width: int | None
    render_height: int | None
    task_defaults: dict

    @property
    def db_path(self) -> Path:
        return self.db_dir / "tasks.sqlite"


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _port(value: object, name: str) -> int:
    port = _positive_int(value, name)
    if port > 65535:
        raise ValueError(f"{name} must be at most 65535")
    return port


def _absolute_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def _task_defaults(value: object) -> dict:
    defaults = _object(value, "h3.taskDefaults")
    allowed = {
        "width", "height", "layers", "steps", "reuse", "coreReuse",
        "seed", "frames", "seconds",
    }
    if set(defaults) - allowed:
        raise ValueError("h3.taskDefaults contains unknown fields")
    required = allowed - {"frames", "seconds"}
    if not required.issubset(defaults):
        raise ValueError("h3.taskDefaults is missing a required field")
    if ("frames" in defaults) == ("seconds" in defaults):
        raise ValueError("h3.taskDefaults needs exactly one of frames or seconds")
    for key in ("width", "height"):
        number = _positive_int(defaults[key], f"h3.taskDefaults.{key}")
        if number < 32 or number % 32:
            raise ValueError(f"h3.taskDefaults.{key} must be a multiple of 32")
    if defaults["width"] * defaults["height"] > 768 * 1344:
        raise ValueError("h3.taskDefaults output area is too large")
    for key, minimum, maximum in (
        ("layers", 35, 50), ("steps", 2, 1000),
        ("reuse", 1, 3), ("coreReuse", 1, 6),
    ):
        number = _positive_int(defaults[key], f"h3.taskDefaults.{key}")
        if not minimum <= number <= maximum:
            raise ValueError(f"h3.taskDefaults.{key} must be {minimum}..{maximum}")
    if defaults["reuse"] > 1 and defaults["coreReuse"] > 1:
        raise ValueError("reuse and coreReuse cannot both be greater than 1")
    seed = defaults["seed"]
    if not isinstance(seed, str) or not seed.isascii() or not seed.isdecimal():
        raise ValueError("h3.taskDefaults.seed must be a decimal string")
    if int(seed) > 2**64 - 1:
        raise ValueError("h3.taskDefaults.seed exceeds uint64")
    if "frames" in defaults:
        frames = _positive_int(defaults["frames"], "h3.taskDefaults.frames")
        aligned = 5 + 17 * max(1, (frames - 5 + 16) // 17)
        if not 22 <= aligned <= 362:
            raise ValueError("h3.taskDefaults.frames is outside the supported range")
    else:
        seconds = defaults["seconds"]
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("h3.taskDefaults.seconds must be a positive number")
        try:
            duration = Decimal(str(seconds))
        except InvalidOperation as exc:
            raise ValueError("h3.taskDefaults.seconds must be a positive number") from exc
        if not duration.is_finite() or duration <= 0:
            raise ValueError("h3.taskDefaults.seconds must be a positive number")
        requested_frames = int((duration * 24).to_integral_value(rounding=ROUND_HALF_UP))
        aligned = 5 + 17 * max(1, (requested_frames - 5 + 16) // 17)
        if not 22 <= aligned <= 362:
            raise ValueError("h3.taskDefaults.seconds is outside the supported range")
    return dict(defaults)


def _hardware_args(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("h3.hardwareArgs must be a string array")
    controlled = {
        "-d", "--model-dir", "-p", "--prompt", "-o", "--output",
        "--width", "--height", "--frames", "--seconds", "--steps",
        "--reuse", "--layers", "--core-reuse", "--seed",
        "--first-frame", "--last-frame", "--ref-image", "--ref-video",
        "--ref-silent-video", "--ref-video-audio", "--ref-audio",
        "--ref-image-size", "--frames-dir", "--show", "--zoom",
        "--info", "-h", "--help", "--",
    }
    for item in value:
        if "\x00" in item:
            raise ValueError("h3.hardwareArgs may not contain NUL")
        if (item in controlled
                or any(item.startswith(option + "=") for option in controlled)
                or item.startswith(("-d", "-p", "-o"))):
            raise ValueError(f"h3.hardwareArgs may not override {item}")
    flags = set(value)
    if "--ssd-streaming" in flags and "--use-int8-row-fc2" in flags:
        raise ValueError("--ssd-streaming conflicts with --use-int8-row-fc2")
    if "--use-int8-row-fc2" in flags and "--use-slower-bf16-mlp" in flags:
        raise ValueError("--use-int8-row-fc2 conflicts with --use-slower-bf16-mlp")
    return tuple(value)


def _render_size(args: tuple[str, ...], option: str) -> int | None:
    values: list[str] = []
    for index, item in enumerate(args):
        if item == option:
            if index + 1 >= len(args):
                raise ValueError(f"{option} needs a value")
            values.append(args[index + 1])
        elif item.startswith(option + "="):
            values.append(item[len(option) + 1:])
    if len(values) > 1:
        raise ValueError(f"{option} may appear only once")
    if not values:
        return None
    value = values[0]
    if not value.isascii() or not value.isdecimal():
        raise ValueError(f"{option} must be a positive multiple of 32")
    number = int(value)
    if number < 32 or number % 32:
        raise ValueError(f"{option} must be a positive multiple of 32")
    return number


def load_config(path: Path) -> ServerConfig:
    config_path = Path(path)
    if not config_path.is_file():
        raise ValueError(f"configuration file does not exist: {config_path}")
    if os.name == "posix" and config_path.stat().st_mode & 0o077:
        raise ValueError("configuration file must have mode 0600 or stricter")
    try:
        root = _object(json.loads(config_path.read_text(encoding="utf-8")), "config")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read configuration: {exc}") from exc
    api = _object(root.get("api"), "api")
    admin = _object(root.get("admin"), "admin")
    http = _object(root.get("http", {}), "http")
    paths = _object(root.get("paths"), "paths")
    auth = _object(root.get("auth"), "auth")
    resources = _object(root.get("resources"), "resources")
    logging_config = _object(root.get("logging"), "logging")
    h3 = _object(root.get("h3"), "h3")
    api_host = api.get("host")
    if not isinstance(api_host, str) or not api_host or api_host.strip() != api_host:
        raise ValueError("api.host must be a nonempty IP address or hostname")
    api_port = _port(api.get("port"), "api.port")
    admin_port = _port(admin.get("port"), "admin.port")
    if api_port == admin_port:
        raise ValueError("api.port and admin.port must differ")
    tokens = auth.get("tokens")
    if not isinstance(tokens, list) or not tokens or not all(
        isinstance(token, str) and token for token in tokens
    ):
        raise ValueError("auth.tokens must be a nonempty string array")
    log_level_name = logging_config.get("level")
    if not isinstance(log_level_name, str) or log_level_name.upper() not in logging.getLevelNamesMapping():
        raise ValueError("logging.level is invalid")
    directory_keys = ("dbDir", "uploadTempDir", "artifactDir", "logDir")
    directories = {key: _absolute_path(paths.get(key), f"paths.{key}") for key in directory_keys}
    if len(set(directories.values())) != len(directories):
        raise ValueError("data directories must be distinct")
    h3_binary = _absolute_path(paths.get("h3Binary"), "paths.h3Binary")
    model_dir = _absolute_path(paths.get("modelDir"), "paths.modelDir")
    h3_cwd = _absolute_path(paths.get("h3Cwd"), "paths.h3Cwd")
    if not h3_binary.is_file() or not os.access(h3_binary, os.X_OK):
        raise ValueError("paths.h3Binary is not executable")
    if not model_dir.is_dir():
        raise ValueError("paths.modelDir is not a directory")
    if not h3_cwd.is_dir() or not (h3_cwd / "h3_shaders.metal").is_file():
        raise ValueError("paths.h3Cwd must contain h3_shaders.metal")
    hardware_args = _hardware_args(h3.get("hardwareArgs", []))
    render_width = _render_size(hardware_args, "--render-width")
    render_height = _render_size(hardware_args, "--render-height")
    if (render_width is None) != (render_height is None):
        raise ValueError("--render-width and --render-height must be set together")
    task_defaults = _task_defaults(h3.get("taskDefaults"))
    if render_width is not None:
        if (render_width > task_defaults["width"] or render_height > task_defaults["height"]
                or render_width * task_defaults["height"] != render_height * task_defaults["width"]):
            raise ValueError("h3.taskDefaults output must fit fixed internal render size and aspect")
    return ServerConfig(
        api_host=api_host,
        api_port=api_port,
        admin_port=admin_port,
        max_request_bytes=_positive_int(http.get("maxRequestBytes", 536870912), "http.maxRequestBytes"),
        h3_binary=h3_binary,
        model_dir=model_dir,
        h3_cwd=h3_cwd,
        db_dir=directories["dbDir"],
        upload_temp_dir=directories["uploadTempDir"],
        artifact_dir=directories["artifactDir"],
        log_dir=directories["logDir"],
        tokens=tuple(tokens),
        max_concurrent_jobs=_positive_int(resources.get("maxConcurrentJobs"), "resources.maxConcurrentJobs"),
        log_level=logging.getLevelNamesMapping()[log_level_name.upper()],
        hardware_args=hardware_args,
        render_width=render_width,
        render_height=render_height,
        task_defaults=task_defaults,
    )
