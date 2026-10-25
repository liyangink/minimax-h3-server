"""Single-process service startup and two-listener coordination."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from server.auth import AuthManager
from server.config import ServerConfig, load_config
from server.contracts import ApiError, error_body


logger = logging.getLogger(__name__)


def _prepare_directories(config: ServerConfig) -> None:
    for directory in (
        config.db_dir,
        config.upload_temp_dir,
        config.artifact_dir,
        config.log_dir,
        config.log_dir / "tasks",
    ):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.stat().st_mode & 0o077:
            raise ValueError(f"directory must be private (0700): {directory}")
    tempfile.tempdir = str(config.upload_temp_dir)


def _configure_logging(config: ServerConfig) -> None:
    handler = logging.FileHandler(config.log_dir / "service.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=config.log_level, handlers=[handler, logging.StreamHandler()], force=True)


def _preflight(config: ServerConfig) -> None:
    for program in ("ffmpeg", "ffprobe"):
        if shutil.which(program) is None:
            raise ValueError(f"{program} is not available on PATH")


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def handle_api_error(_request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=error_body(exc.code, exc.message))

    @app.exception_handler(HTTPException)
    async def handle_http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        return JSONResponse(status_code=exc.status_code, content=error_body(code, str(exc.detail)))

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=400, content=error_body("invalid_request", "Invalid request"))

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled HTTP error: %s", exc)
        return JSONResponse(status_code=500, content=error_body("internal_error", "Internal server error"))


def create_app(kind: str, config: ServerConfig, auth: AuthManager, runner: object) -> FastAPI:
    from server.task_api import create_task_router

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.listener_kind = kind
    app.include_router(create_task_router(config, auth, runner))
    if kind == "admin":
        from server.admin import create_admin_router

        app.include_router(create_admin_router(config, auth))
    _install_error_handlers(app)
    return app


async def _poll_loop(runner: object, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await asyncio.to_thread(runner.poll_once)
        except Exception:
            logger.exception("Task polling failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=2)
        except TimeoutError:
            pass


async def run_service(config: ServerConfig) -> None:
    from server import task_store
    from server.task_runner import TaskRunner

    _prepare_directories(config)
    _configure_logging(config)
    _preflight(config)
    lock = task_store.acquire_server_lock(config.db_dir)
    try:
        task_store.initialize(config.db_path)
        runner = TaskRunner(config)
        await asyncio.to_thread(runner.recover_pending)
        auth = AuthManager(config)
        api_app = create_app("api", config, auth, runner)
        admin_app = create_app("admin", config, auth, runner)
        api_server = uvicorn.Server(uvicorn.Config(
            api_app, host=config.api_host, port=config.api_port,
            log_config=None, lifespan="off", workers=1, reload=False,
        ))
        admin_server = uvicorn.Server(uvicorn.Config(
            admin_app, host="127.0.0.1", port=config.admin_port,
            log_config=None, lifespan="off", workers=1, reload=False,
        ))
        stop = asyncio.Event()
        api_task = asyncio.create_task(api_server.serve(), name="api-listener")
        admin_task = asyncio.create_task(admin_server.serve(), name="admin-listener")
        poll_task = asyncio.create_task(_poll_loop(runner, stop), name="task-poller")
        try:
            done, _pending = await asyncio.wait(
                (api_task, admin_task), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
            logger.info("An HTTP listener stopped; shutting down both listeners")
        finally:
            stop.set()
            api_server.should_exit = True
            admin_server.should_exit = True
            await asyncio.gather(api_task, admin_task, poll_task, return_exceptions=True)
    finally:
        lock.release()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local MiniMax H3 generation server")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    args = parser.parse_args()
    os.umask(0o077)
    try:
        asyncio.run(run_service(load_config(args.config)))
    except (OSError, ValueError, RuntimeError) as exc:
        raise SystemExit(f"minimax-h3-server: {exc}") from exc


if __name__ == "__main__":
    main()
