"""SQLite task persistence and the single-server process lock."""

from __future__ import annotations

import base64
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Literal

from .contracts import TaskInput, TaskRecord, TaskStatus


schema_version = 1


@contextmanager
def _connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(db_path, timeout=5, isolation_level=None)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
    finally:
        connection.close()


def _json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _task_record(row: sqlite3.Row) -> TaskRecord:
    return {
        "id": row["id"],
        "status": row["status"],
        "request": json.loads(row["request_json"]),
        "execution": json.loads(row["execution_json"]) if row["execution_json"] is not None else None,
        "inputs": json.loads(row["inputs_json"]),
        "pid": row["pid"],
        "processCreateTime": row["process_create_time"],
        "phase": row["phase"],
        "progressPercent": row["progress_percent"],
        "exitCode": row["exit_code"],
        "errorMessage": row["error_message"],
        "createdAt": row["created_at"],
        "startedAt": row["started_at"],
        "finishedAt": row["finished_at"],
    }


def initialize(db_path: Path) -> None:
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if sqlite3.sqlite_version_info < (3, 38, 0):
        raise RuntimeError("SQLite 3.38 or newer is required")
    with _connection(db_path) as connection:
        connection.execute("SELECT json_valid('{}'), json_type('{}')")
        connection.execute("PRAGMA journal_mode = WAL")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != 0 and version != schema_version:
            raise RuntimeError(f"unsupported database schema version: {version}")
        if version == 0:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
            ).fetchone():
                raise RuntimeError("unversioned tasks table already exists")
            try:
                connection.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE tasks (
                      id TEXT PRIMARY KEY,
                      status TEXT NOT NULL CHECK (status IN ('preparing','running','succeeded','failed')),
                      request_json TEXT NOT NULL
                        CHECK (json_valid(request_json) AND json_type(request_json) = 'object'),
                      execution_json TEXT
                        CHECK (execution_json IS NULL OR (json_valid(execution_json) AND json_type(execution_json) = 'object')),
                      inputs_json TEXT NOT NULL DEFAULT '[]'
                        CHECK (json_valid(inputs_json) AND json_type(inputs_json) = 'array'),
                      pid INTEGER,
                      process_create_time REAL,
                      phase TEXT,
                      progress_percent INTEGER NOT NULL DEFAULT 0,
                      exit_code INTEGER,
                      error_message TEXT,
                      created_at TEXT NOT NULL,
                      started_at TEXT,
                      finished_at TEXT
                    ) STRICT;
                    CREATE INDEX tasks_status_created ON tasks(status, created_at DESC, id DESC);
                    CREATE INDEX tasks_created ON tasks(created_at DESC, id DESC);
                    PRAGMA user_version = 1;
                    COMMIT;
                """)
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        else:
            table = connection.execute(
                "SELECT wr, strict FROM pragma_table_list WHERE name = 'tasks'"
            ).fetchone()
            if table is None or table["strict"] != 1:
                raise RuntimeError("tasks table is missing or is not STRICT")
    if os.name == "posix":
        os.chmod(db_path, 0o600)


class ServerLock:
    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor = self._descriptor
        self._descriptor = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def acquire_server_lock(db_dir: Path) -> ServerLock:
    db_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(db_dir / "server.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\x00")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(descriptor)
        raise RuntimeError("another server instance holds the database lock") from exc
    return ServerLock(descriptor)


def claim_task(db_path: Path, task_id: str, max_concurrent_jobs: int, created_at: str) -> bool:
    if max_concurrent_jobs < 1:
        raise ValueError("max_concurrent_jobs must be positive")
    with _connection(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            active = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('preparing','running')"
            ).fetchone()[0]
            if active >= max_concurrent_jobs:
                connection.execute("ROLLBACK")
                return False
            connection.execute(
                "INSERT INTO tasks (id, status, request_json, created_at) "
                "VALUES (?, 'preparing', '{}', ?)",
                (task_id, created_at),
            )
            connection.execute("COMMIT")
            return True
        except BaseException:
            connection.execute("ROLLBACK")
            raise


def remove_preparing(db_path: Path, task_id: str) -> None:
    with _connection(db_path) as connection:
        result = connection.execute(
            "DELETE FROM tasks WHERE id = ? AND status = 'preparing'", (task_id,)
        )
        if result.rowcount != 1:
            raise ValueError("task is not preparing")


def save_prepared(
    db_path: Path,
    task_id: str,
    request: dict,
    execution: dict,
    inputs: list[TaskInput],
) -> None:
    request_text = _json_text(request)
    execution_text = _json_text(execution)
    inputs_text = _json_text(inputs)
    with _connection(db_path) as connection:
        result = connection.execute(
            "UPDATE tasks SET request_json = ?, execution_json = ?, inputs_json = ? "
            "WHERE id = ? AND status = 'preparing' AND request_json = '{}'",
            (request_text, execution_text, inputs_text, task_id),
        )
        if result.rowcount != 1:
            raise ValueError("task is not awaiting prepared inputs")


def mark_running(
    db_path: Path, task_id: str, pid: int, process_create_time: float, started_at: str
) -> None:
    with _connection(db_path) as connection:
        result = connection.execute(
            "UPDATE tasks SET status = 'running', pid = ?, process_create_time = ?, "
            "started_at = ? WHERE id = ? AND status = 'preparing' AND execution_json IS NOT NULL",
            (pid, process_create_time, started_at, task_id),
        )
        if result.rowcount != 1:
            raise ValueError("task is not ready to run")


def update_progress(db_path: Path, task_id: str, phase: str, progress_percent: int) -> None:
    if isinstance(progress_percent, bool) or not 0 <= progress_percent <= 100:
        raise ValueError("progress_percent must be between 0 and 100")
    with _connection(db_path) as connection:
        result = connection.execute(
            "UPDATE tasks SET phase = ?, progress_percent = ? "
            "WHERE id = ? AND status = 'running'",
            (phase, progress_percent, task_id),
        )
        if result.rowcount != 1:
            raise ValueError("task is not running")


def complete_task(
    db_path: Path,
    task_id: str,
    status: Literal["succeeded", "failed"],
    exit_code: int | None,
    error_message: str | None,
    finished_at: str,
) -> None:
    if status not in ("succeeded", "failed"):
        raise ValueError("invalid terminal status")
    with _connection(db_path) as connection:
        result = connection.execute(
            "UPDATE tasks SET status = ?, exit_code = ?, error_message = ?, finished_at = ?, "
            "progress_percent = CASE WHEN ? = 'succeeded' THEN 100 ELSE progress_percent END "
            "WHERE id = ? AND status IN ('preparing','running')",
            (status, exit_code, error_message, finished_at, status, task_id),
        )
        if result.rowcount != 1:
            raise ValueError("task is already complete or does not exist")


def get_task(db_path: Path, task_id: str) -> TaskRecord | None:
    with _connection(db_path) as connection:
        row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _task_record(row) if row is not None else None


def _encode_cursor(row: TaskRecord) -> str:
    payload = _json_text([row["createdAt"], row["id"]]).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        if not cursor or len(cursor) > 4096:
            raise ValueError
        raw = base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        data = json.loads(raw)
        if not isinstance(data, list) or len(data) != 2 or not all(
            isinstance(part, str) and part for part in data
        ):
            raise ValueError
        return data[0], data[1]
    except (UnicodeError, ValueError, base64.binascii.Error) as exc:
        raise ValueError("invalid task cursor") from exc


def list_tasks(
    db_path: Path, status: TaskStatus | None, limit: int, cursor: str | None
) -> tuple[list[TaskRecord], str | None]:
    if status is not None and status not in ("preparing", "running", "succeeded", "failed"):
        raise ValueError("invalid task status")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be positive")
    clauses: list[str] = []
    values: list[object] = []
    if status is not None:
        clauses.append("status = ?")
        values.append(status)
    if cursor is not None:
        created_at, task_id = _decode_cursor(cursor)
        clauses.append("(created_at, id) < (?, ?)")
        values.extend((created_at, task_id))
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM tasks" + where + " ORDER BY created_at DESC, id DESC LIMIT ?",
            (*values, limit + 1),
        ).fetchall()
    items = [_task_record(row) for row in rows[:limit]]
    next_cursor = _encode_cursor(items[-1]) if len(rows) > limit else None
    return items, next_cursor


def list_pending(db_path: Path) -> list[TaskRecord]:
    with _connection(db_path) as connection:
        rows = connection.execute(
            "SELECT * FROM tasks WHERE status IN ('preparing','running') "
            "ORDER BY created_at ASC, id ASC"
        ).fetchall()
    return [_task_record(row) for row in rows]
