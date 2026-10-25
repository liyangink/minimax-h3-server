"""Bearer authentication and loopback-only browser sessions."""

from __future__ import annotations

import secrets
from hmac import compare_digest

from fastapi import Request

from server.config import ServerConfig
from server.contracts import ApiError


class AuthManager:
    cookie_name = "h3Session"

    def __init__(self, config: ServerConfig) -> None:
        self._tokens = config.tokens
        self._admin_origin = f"http://127.0.0.1:{config.admin_port}"
        self._admin_host = f"127.0.0.1:{config.admin_port}"
        self._sessions: dict[str, str] = {}

    def _valid_token(self, token: str) -> bool:
        return any(compare_digest(token, valid) for valid in self._tokens)

    def _check_admin_origin(self, request: Request, *, require_origin: bool) -> None:
        if request.headers.get("host") != self._admin_host:
            raise ApiError(403, "invalid_origin", "Invalid admin host")
        origin = request.headers.get("origin")
        if require_origin and origin != self._admin_origin:
            raise ApiError(403, "invalid_origin", "Invalid admin origin")
        if origin is not None and origin != self._admin_origin:
            raise ApiError(403, "invalid_origin", "Invalid admin origin")

    def _session_csrf(self, request: Request) -> str | None:
        cookie = request.cookies.get(self.cookie_name)
        return self._sessions.get(cookie) if cookie else None

    def require_task_auth(self, request: Request, write: bool = False) -> None:
        if request.app.state.listener_kind == "api":
            authorization = request.headers.get("authorization", "")
            if not authorization.startswith("Bearer ") or not self._valid_token(authorization[7:]):
                raise ApiError(401, "unauthorized", "Invalid bearer token")
            return
        self._check_admin_origin(request, require_origin=write)
        csrf = self._session_csrf(request)
        if csrf is None:
            raise ApiError(401, "unauthorized", "Admin session required")
        if write and not compare_digest(request.headers.get("x-csrf-token", ""), csrf):
            raise ApiError(403, "invalid_csrf", "Invalid CSRF token")

    def create_session(self, token: str, request: Request) -> tuple[str, str]:
        self._check_admin_origin(request, require_origin=True)
        if not self._valid_token(token):
            raise ApiError(401, "unauthorized", "Invalid token")
        cookie = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self._sessions[cookie] = csrf
        return cookie, csrf

    def get_session(self, request: Request) -> bool:
        self._check_admin_origin(request, require_origin=False)
        return self._session_csrf(request) is not None

    def get_csrf_token(self, request: Request) -> str:
        self._check_admin_origin(request, require_origin=False)
        csrf = self._session_csrf(request)
        if csrf is None:
            raise ApiError(401, "unauthorized", "Admin session required")
        return csrf

    def delete_session(self, request: Request) -> None:
        self._check_admin_origin(request, require_origin=True)
        cookie = request.cookies.get(self.cookie_name)
        if cookie:
            self._sessions.pop(cookie, None)
