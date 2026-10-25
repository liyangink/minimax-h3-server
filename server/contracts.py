"""Shared data shape and HTTP error contract for service modules."""

from __future__ import annotations

from typing import Literal, TypedDict


TaskStatus = Literal["preparing", "running", "succeeded", "failed"]
ReferenceKind = Literal[
    "image", "silentVideo", "video", "audio", "videoWithAudio"
]


class TaskInput(TypedDict):
    role: str
    kind: str
    originalName: str
    path: str
    referenceIndex: int | None


class TaskRecord(TypedDict):
    id: str
    status: TaskStatus
    request: dict
    execution: dict | None
    inputs: list[TaskInput]
    pid: int | None
    processCreateTime: float | None
    phase: str | None
    progressPercent: int
    exitCode: int | None
    errorMessage: str | None
    createdAt: str
    startedAt: str | None
    finishedAt: str | None


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}
