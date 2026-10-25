"""Loopback-only administration page and browser session routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

from server.auth import AuthManager
from server.config import ServerConfig
from server.contracts import ApiError


static_dir = Path(__file__).resolve().parent / "static"
asset_types = {"admin.js": "text/javascript", "admin.css": "text/css"}


def create_admin_router(_config: ServerConfig, auth: AuthManager) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    async def page() -> FileResponse:
        return FileResponse(static_dir / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    @router.get("/assets/{name}")
    async def asset(name: str) -> FileResponse:
        media_type = asset_types.get(name)
        if media_type is None:
            raise ApiError(404, "not_found", "Asset not found")
        return FileResponse(static_dir / name, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.post("/admin/session")
    async def create_session(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError as exc:
            raise ApiError(400, "invalid_request", "Expected a JSON token") from exc
        if not isinstance(body, dict) or not isinstance(body.get("token"), str):
            raise ApiError(400, "invalid_request", "Token is required")
        cookie, csrf = auth.create_session(body["token"], request)
        response = JSONResponse({"authenticated": True, "csrfToken": csrf}, headers={"Cache-Control": "no-store"})
        response.set_cookie(auth.cookie_name, cookie, path="/", httponly=True, samesite="strict")
        return response

    @router.get("/admin/session")
    async def get_session(request: Request) -> JSONResponse:
        authenticated = auth.get_session(request)
        return JSONResponse(
            {"authenticated": authenticated, "csrfToken": auth.get_csrf_token(request) if authenticated else None},
            headers={"Cache-Control": "no-store"},
        )

    @router.delete("/admin/session")
    async def delete_session(request: Request) -> JSONResponse:
        auth.require_task_auth(request, write=True)
        auth.delete_session(request)
        response = JSONResponse({"authenticated": False}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(auth.cookie_name, path="/")
        return response

    return router
