# 模块契约（阶段 0 冻结）

设计依据为 [design.md](design.md)。以下函数名、参数和返回值供并行模块直接调用；共享契约只由实施主代理维护。Python 模块与函数用 `snake_case`，HTTP JSON 字段沿设计稿保持 `camelCase`。

## 公共对象

- `server.config.load_config(path: Path) -> ServerConfig`：读取并验证配置；`ServerConfig` 的字段为 `api_host`, `api_port`, `admin_port`, `max_request_bytes`, `h3_binary`, `model_dir`, `h3_cwd`, `db_dir`, `upload_temp_dir`, `artifact_dir`, `log_dir`, `tokens`, `max_concurrent_jobs`, `log_level`, `hardware_args`, `task_defaults`。
- `server.contracts.ApiError(status_code, code, message)`：所有业务 HTTP 错误；响应体是 `{"error":{"code":...,"message":...}}`。404、409、410、413、429、507 与设计稿一致。
- `server.contracts.TaskRecord`：任务查询结果使用 API 风格键名 `processCreateTime`, `progressPercent`, `errorMessage`, `createdAt` 等；`request`, `execution`, `inputs` 为解析后的 JSON 值，不向调用方泄漏 SQLite 的 `_json` 列。
- `TaskInput` 的 `path` 是相对于任务上传目录 `<uploadTempDir>/<taskId>/` 的服务生成路径；`role` 为 `firstFrame`、`lastFrame`、`reference`、`referenceAudio`；参考索引从 0 开始并保持用户顺序。原始文件名仅供展示，不能用来构造服务器路径。
- `execution` JSON 含 `width`, `height`, `layers`, `steps`, `reuse`, `coreReuse`, `seed`, `frames`, `actualSeconds`, `hardwareArgs`；`seed` 保持十进制字符串。`frames` 是对齐后的实际帧数。

## DAO：`server/task_store.py`（代理 A）

```python
initialize(db_path: Path) -> None
acquire_server_lock(db_dir: Path) -> ServerLock
claim_task(db_path: Path, task_id: str, max_concurrent_jobs: int, created_at: str) -> bool
remove_preparing(db_path: Path, task_id: str) -> None
save_prepared(db_path: Path, task_id: str, request: dict, execution: dict, inputs: list[TaskInput]) -> None
mark_running(db_path: Path, task_id: str, pid: int, process_create_time: float, started_at: str) -> None
update_progress(db_path: Path, task_id: str, phase: str, progress_percent: int) -> None
complete_task(db_path: Path, task_id: str, status: Literal["succeeded", "failed"], exit_code: int | None, error_message: str | None, finished_at: str) -> None
get_task(db_path: Path, task_id: str) -> TaskRecord | None
list_tasks(db_path: Path, status: TaskStatus | None, limit: int, cursor: str | None) -> tuple[list[TaskRecord], str | None]
list_pending(db_path: Path) -> list[TaskRecord]
```

`ServerLock` 持有 `dbDir/server.lock` 的独占非阻塞 OS 文件锁，`release()` 释放；由 `main.py` 在整个服务生命周期持有。`claim_task` 用短 `BEGIN IMMEDIATE` 原子检查 `preparing/running` 数量并插入 `preparing`，满载返回 `False`。其余非法状态迁移应抛错误，不能静默覆盖终态。每次 DAO 调用独立创建并关闭 SQLite 连接，不在线程间传递连接。`cursor` 为不透明、URL 安全的页游标，按 `(created_at DESC,id DESC)` 稳定翻页。

## 烧制：`server/task_runner.py`（代理 B）

```python
TaskRunner(config: ServerConfig)
TaskRunner.start_task(task_id: str) -> None
TaskRunner.poll_once() -> None
TaskRunner.recover_pending() -> None
```

`start_task` 从 DAO 读取已保存的 `request/execution/inputs`，生成 h3 argv，以 `start_new_session=True` 启动子进程并登记 PID/创建时间。`Popen` 之后的失败由 runner 负责：若 PID 落库失败，停止该进程组并记录失败；无法落库时保留可恢复的日志与专属输出路径。`poll_once` 和 `recover_pending` 均为同步阻塞函数，由主应用用 `asyncio.to_thread` 调度；正常退出服务不终止 h3。服务重启后先恢复，再开放 HTTP 监听。runner 不写 SQL。

## HTTP：`server/task_api.py`（代理 C）

```python
create_task_router(config: ServerConfig, auth: AuthManager, runner: TaskRunner) -> APIRouter
```

两个 FastAPI app 复用同一个 router。路由通过 `auth.require_task_auth(request, write=...)` 识别 API Bearer 或后台 session。`POST /v1/tasks` 仅声明 `Request`，顺序固定为鉴权 → `claim_task` 占槽 → 解析 multipart → 校验/保存输入 → `save_prepared` → `await asyncio.to_thread(runner.start_task, task_id)`。启动前任何错误由 HTTP 删除 `preparing` 记录并清理上传暂存；启动后由 runner 负责。整个请求上限通过按原始 ASGI `receive` 字节计数实施，不能靠 `Content-Length` 或默认 Starlette part 上限替代。读取/写入大文件、媒体元信息操作不能阻塞事件循环。创建返回 202，满载直接返回 429 且不读取请求体。

## 认证、管理页面与装配

- `server.auth.AuthManager(config: ServerConfig)`、`require_task_auth(request: Request, write: bool = False) -> None`、`create_session(token: str, request: Request) -> tuple[cookie_value, csrf_token]`、`get_session(request: Request) -> bool`、`get_csrf_token(request: Request) -> str`、`delete_session(request: Request) -> None`。所有 session 仅在内存；管理 Cookie `Path=/`, `HttpOnly`, `SameSite=Strict`；管理登录和写请求校验 Host/Origin，写操作验证 CSRF。API listener 的 `/v1/*` 只接受 Bearer，admin listener 的 `/v1/*` 只接受 session。`GET /admin/session` 可用 `get_csrf_token` 在页面刷新后取回 CSRF 值。
- `server.admin.create_admin_router(config: ServerConfig, auth: AuthManager) -> APIRouter`；后台只请求本机 app 的 `/v1`，不直读 SQLite，不自行建 session 存储。
- `server.main` 建两个 FastAPI app / 两个 Uvicorn Server，同一事件循环与同一 `TaskRunner/AuthManager`；两个监听器任一失败或退出时联动停止另一个。统一错误处理在两 app 注册。先获取锁、初始化 DB、恢复，再开放监听；正常退出只关服务，不杀 h3。

交付时发现契约冲突，应先向实施主代理说明并由其调整共享文件；不能由专职代理自行改公共契约。
