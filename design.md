# minimax-h3-server 方案设计

状态：设计稿。本阶段只定义首版服务，不编写实现。`minimax-h3-server` 是独立 Python 项目，Toonflow 后续调用它；VedioWork 只作参考。

## 1. 职责与运行方式

一个 Python 进程管理 SQLite、任务协调器和两个 HTTP 监听器。任务 API 按配置的 IP/端口监听；管理后台固定监听 `127.0.0.1`，端口从配置读取。两个独立 FastAPI 应用在同一事件循环中运行，共用数据库和协调器。后台路由只在本机监听器注册。任一监听器启动失败，整个服务退出。使用 Python 3.11+、FastAPI、Uvicorn、`python-multipart`、`psutil`，以及标准库的 `sqlite3`、`subprocess`、`logging`；不启用 Uvicorn 多 worker 或自动重载。

任务 API 使用配置文件中的 Bearer token。后台用同一 token 登录，换取仅保存在内存中的本机会话。首版只允许同时烧制配置的任务数；超额创建立即返回 429，不排队。任务、上传引用、原始日志和生成视频默认保留，供后台查看历史。

## 2. 配置与参数

启动时读取 `config.json`；变更配置后重启生效。配置文件含 token，应设为 `0600`，且不提交仓库。示例：

```json
{
  "api": { "host": "127.0.0.1", "port": 8787 },
  "admin": { "port": 8788 },
  "http": { "maxRequestBytes": 536870912 },
  "paths": {
    "h3Binary": "/absolute/path/to/h3",
    "modelDir": "/absolute/path/to/minimax-h3",
    "h3Cwd": "/absolute/path/to/h3.c",
    "dbDir": "/absolute/path/to/db",
    "uploadTempDir": "/absolute/path/to/uploads",
    "artifactDir": "/absolute/path/to/artifacts",
    "logDir": "/absolute/path/to/logs"
  },
  "auth": { "tokens": ["replace-with-random-token"] },
  "resources": { "maxConcurrentJobs": 1 },
  "logging": { "level": "INFO" },
  "h3": {
    "hardwareArgs": ["--ssd-streaming"],
    "taskDefaults": {
      "width": 512,
      "height": 512,
      "layers": 50,
      "steps": 20,
      "reuse": 1,
      "coreReuse": 1,
      "seed": "42",
      "frames": 107
    }
  }
}
```

`api.host/api.port`、`admin.port`、四个数据目录和日志级别均由配置控制；后台 IP 固定为 `127.0.0.1`。启动时拒绝两个监听地址的端口冲突。`h3Cwd` 是 h3 子进程工作目录，需包含 `h3_shaders.metal`。`http.maxRequestBytes` 是唯一按字节计的可调 HTTP 上限，默认 512 MiB，用于限制整个上传请求；启动时确认它为正数。没有逐文件字节配置、元数据字节配置、每任务磁盘预算、最低空闲磁盘或烧制运行时长配置。磁盘写满属于 I/O 失败：服务记录错误、清理未完成上传，运行中任务在进程退出后记为失败。

`h3.hardwareArgs` 是本机硬件相关 CLI 参数数组，服务按顺序直接透传，不由任务覆盖。服务拒绝其中出现自己控制的 `-d/-p/-o`、时长、逐任务参数、首尾帧、引用等选项，并校验已知互斥组合。新增 h3 硬件开关只需修改配置；未知开关若被 h3.c 拒绝，该任务失败并保留日志。`h3.taskDefaults` 提供逐任务默认值；请求只覆盖显式传入的项。保存请求原值和执行值，配置变化不改历史任务。

| 逐任务字段 | 传给 h3.c | 首版校验 |
| --- | --- | --- |
| `width`, `height` | `--width`, `--height` | 整数、至少 32、是 32 的倍数，面积不超过 `768×1344`；若本机固定内部渲染尺寸，输出须同长宽比且不小于内部尺寸。 |
| `layers` | `--layers` | 整数 35–50。 |
| `steps` | `--steps` | 整数 2–1000。 |
| `reuse` | `--reuse` | 整数 1–3。 |
| `coreReuse` | `--core-reuse` | 整数 1–6。 |
| `seed` | `--seed` | 十进制字符串，范围 `0..2^64-1`，避免 JavaScript 整数精度损失。 |
| `frames` | `--frames` | 整数；对齐到 h3.c 的 `5+17n` 后在 22–362 帧内。 |
| `seconds` | 换算为 `--frames` | 正数，与 `frames` 二选一；24 fps 换算并按 h3.c 规则对齐。 |

`reuse` 和 `coreReuse` 是两种互斥的加速方式：**二者不能同时大于 1**。值为 1 表示该方式关闭，因此默认值 `reuse=1, coreReuse=1` 有效；例如 `reuse=2` 时 `coreReuse` 必须为 1。默认时长只设 `frames` 或 `seconds` 之一；任务若显式给另一个字段，就覆盖默认时长。所有参数在创建任务时校验，不能等进程启动后才发现基本输入错误。h3.c 当前始终输出带音轨的 MP4，不提供关闭音频的任务参数。

`seconds` 先按 24 fps 换算请求帧数，再向上对齐到 `5+17n`；例如请求 10 秒实际得到 243 帧，即 10.125 秒。响应和历史详情均显示实际帧数/时长。

提示词必填且必须是有效 UTF-8，不允许内嵌 NUL（C 字符串会在这里截断）；服务不另设提示词字节上限，也不截断。当前 h3.c 的 tokenizer 不限制单独提示词长度；模型配置的 `max_position_embeddings/model_max_length` 为 **262,144 个 token**，但当前 C 文本编码器因 `UINT32_MAX / TEXT_INTERMEDIATE` 的检查，最多接受 **167,772 个输入 token**。在首尾帧或参考图场景，输入序列还包含视觉与标记 token，因此 167,772 不是提示词单独可用的额度；实际可运行长度还受内存约束。服务无法用字节数等价校验 token 数，交由 h3.c 对完整输入执行原生检查。`request` JSON 只包含已定义的字段和最多 12 个引用；multipart 解析器的非文件字段上限设为 `http.maxRequestBytes`，避免默认值暗中截断提示词。不单设元数据或单文件字节上限，整个请求统一受 `maxRequestBytes` 约束。引用个数、类型、音频时长、最少输出帧等按 h3.c 实际能力校验。

## 3. 目录与数据库

四个数据目录分别配置，不要求在同一根目录下：

```text
dbDir/
  tasks.sqlite                 # SQLite 及其 WAL/SHM
  server.lock                  # 单服务实例的文件锁
uploadTempDir/
  <taskId>/...                 # 上传暂存和保留的原始引用
artifactDir/
  <taskId>/output.part.mp4     # h3 正在写入
  <taskId>/video.mp4           # 结算后的制品
logDir/
  service.log                  # 服务日志
  tasks/<taskId>.log          # h3 原始 stderr
```

启动时执行 `os.umask(0o077)`；目录权限 `0700`，文件权限 `0600`。上传解析器的临时文件也落在 `uploadTempDir`，不能先写系统默认临时目录。任务路径全部由服务根据 UUID 拼接，不接受客户端给出的本机路径；原始文件名只用于后台展示。上传临时目录与制品目录可能位于不同文件系统，转存与结果结算不能假设跨目录 `rename` 原子；结果只在同一任务制品目录中从 `output.part.mp4` 原子改名为 `video.mp4`。更改四个数据目录前应迁移已有数据并保持任务 ID 子目录结构，服务不自动迁移历史文件。

首版只需一张 SQLite 任务表；输入清单和参数以 JSON 文本快照保存，列表页需要的状态、进度和时间单独成列。SQLite 没有独立的 JSON 列类型；使用 `TEXT` 类型和 `json_valid/json_type` 约束，避免声明为 `JSON` 时得到 NUMERIC 类型亲和性。表采用 `STRICT` 类型检查，启动预检确认 SQLite ≥3.38 且 JSON 函数可用。数据库启用 WAL、`busy_timeout`，并用 `PRAGMA user_version` 管理 schema 版本：

```sql
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
  process_create_time REAL,           -- 用于识别 PID 复用
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
```

`preparing` 记录先以 `{}` 写入 `request_json` 占名额；上传解析成功后替换为用户提交的完整 JSON，包括提示词。`execution_json` 保存真正使用的逐任务参数与固定硬件开关，两者足以复盘烧制，无须再存一份完整 argv。`inputs_json` 是按原顺序的素材清单，含角色、类型、原文件名和服务生成的相对文件路径；最多 12 个参考，首版不需要独立输入表。制品和日志路径可由配置目录加任务 ID 推导，不在表里重复存储；文件缺失时下载返回 410，历史任务状态仍保持成功。原方案的事件表由服务日志承担，单实例所有权由 `dbDir/server.lock` 的操作系统文件锁承担，无须额外建表。

SQLite 3.45+ 另有以 BLOB 存储的 JSONB，读取和解析可能更快，但首版每任务只有几份小快照，主要按状态与时间查询；JSONB 不提供本场景需要的索引收益，还提高运行版本要求，因此使用可直接查看和迁移的 `TEXT` JSON。只在需要筛选的普通列上建索引，不给这些快照预建 JSON 表达式索引。

## 4. HTTP 接口

两个监听器共用 `/v1` 任务逻辑。任务 API 的 `/v1/*` 只接受 Bearer token；本机后台的 `/v1/*` 接受登录会话，写操作额外检查 CSRF。FastAPI 文档路由关闭。JSON 错误统一为 `{ "error": { "code": "...", "message": "..." } }`；全局异常处理器将框架默认验证和解析错误映射到这个格式。

| 方法与路径 | 请求 | 成功响应 | 用途 |
| --- | --- | --- | --- |
| `GET /v1/health` | 无 | 200 `{ready,version}` | 进程健康状态。 |
| `GET /v1/capabilities` | 鉴权 | 200 `{taskDefaults,parameterRanges,maxConcurrentJobs,activeJobs}` | 后台表单及 Toonflow 读取能力。 |
| `POST /v1/tasks` | 鉴权；multipart `request` JSON part 和文件 parts | 202 `{id,status,statusUrl,actualFrames,actualSeconds}` | 创建烧制任务；满载返回 429。 |
| `GET /v1/tasks` | 鉴权；`status?`, `limit?`, `cursor?` | 200 `{items,nextCursor}` | 当前及历史任务，按创建时间分页。 |
| `GET /v1/tasks/{id}` | 鉴权 | 200 任务详情 | 状态、进度、提示词、原始/执行参数、引用和错误。 |
| `GET /v1/tasks/{id}/artifact` | 鉴权；可带 `Range` | 200/206 `video/mp4` | 在线预览或下载视频。 |
| `GET /v1/tasks/{id}/inputs/{index}` | 鉴权；可带 `Range` | 200/206 原始媒体 | 预览该任务的首尾帧或参考素材。 |
| `GET /v1/tasks/{id}/log` | 鉴权；`tailBytes?` | 200 `{text}` | 查看受限长度的日志尾部。 |

后台监听器还提供 `GET /` 和 `GET /assets/{name}`（静态页面）、`POST /admin/session`（token 登录）、`GET /admin/session`（读取登录状态）、`DELETE /admin/session`（注销）。后台页面与 API 共享创建任务逻辑，不另造任务格式。常用状态码：400 参数无效、401 未认证、404 不存在、409 任务未完成、410 制品已丢失、413 整个请求超过上限、429 并发已满、507 存储写入失败。

### 创建任务请求与引用

`POST /v1/tasks` 一次提交提示词、可选参数和全部引用文件；无需单独上传接口。`request` part 为 UTF-8 JSON，文件 part 的名字由 `fileKey` 引用。示例：

```json
{
  "prompt": "The person in Picture 1 turns toward the camera.",
  "parameters": { "layers": 45, "reuse": 2, "seconds": 4 },
  "conditioning": {
    "references": [
      { "kind": "image", "fileKey": "ref1" },
      { "kind": "silentVideo", "fileKey": "ref2" }
    ]
  }
}
```

提示词始终必填；`conditioning` 可以不传。传入时在首尾帧 `{firstFrameFileKey?,lastFrameFileKey?}` 与有序参考 `{references:[...]}` 之间二选一。参考类型为 `image`、`silentVideo`、`video`（含原音轨）、`audio`、`videoWithAudio`（视频加独立替换音轨）；后一种引用有 `audioFileKey`，占两个文件 part。服务按数组顺序生成 `--ref-*` 命令参数。参考最多 12 个，图片最多 9、视频最多 3、音频输入最多 3；有声视频所需的输出帧数至少 56，音频时长按 h3.c 的 2–15 秒约束。扩展名和 MIME 不作为媒体有效性的最终依据；h3.c/FFmpeg 读取失败时，任务记为失败并保留错误日志。

创建路由只接收原始 `Request`，先验证 token、检查并发名额，再解析 multipart，避免满载时先收大文件。ASGI 接收层按字节统计整个请求并实施 `http.maxRequestBytes`；上传到达上限后立即返回 413。解析表单时显式把非文件 part 的 `max_part_size` 设为这个总上限，避免 Starlette 默认值另设隐藏的提示词限制。文件在配置的上传目录暂存。解析成功后把原始引用保留在任务目录，写入 `inputs_json`；字段/文件引用不匹配时清理暂存并返回 400。数据库事务只包住名额申请与记录写入，不包住文件上传或模型执行。同步的媒体读取、文件复制和进程检查放入线程池，避免两个 HTTP 入口同时卡住。

任务详情返回 `request`、`execution` 和有序 `inputs`，以及状态、阶段、进度和相对下载地址；不返回 token 或数据库绝对路径。视频和输入素材通过鉴权后的流式响应提供，支持浏览器所需的 Range 请求。

## 5. 任务、子进程与恢复

状态为 `preparing → running → succeeded | failed`。`preparing` 表示名额已占、正在上传或启动，尚未向客户端返回任务 ID；`running` 表示已记录子进程身份。`preparing/running` 都计入并发。烧制任务**没有运行时长上限**；高画质任务运行数小时也继续监控。协调器按固定 2 秒间隔检查进程与新增日志，更新近似阶段/进度；进度只是展示值，完整 MP4 才是成功条件。

服务使用 `subprocess.Popen([...], shell=False, cwd=h3Cwd, start_new_session=True)` 启动 h3，stdin/stdout 指向空设备，stderr 写每任务日志文件。h3 是独立 OS 子进程；**正常停止服务不会主动终止已在运行的烧制**。`start_new_session=True` 让它不依赖服务的进程组，重定向标准流避免服务退出后管道断开；电脑关机或操作系统终止整个任务组仍可能中断烧制。服务重启后按 PID、进程创建时间及包含任务专属输出路径的 argv 识别进程，继续监控。启动写库的短窗口若留下无 PID 的 `preparing` 记录，也按任务专属输出路径搜索进程；找不到时根据制品结果结算或记失败。

正常监控到进程退出时，若返回码为 0 且输出文件存在、非空，使用 FFprobe 校验可读视频和音轨，再在同一制品目录原子改名为 `video.mp4` 并记成功。服务停机期间任务已结束时，新服务通常取不到原子进程退出码，重启后改按输出文件的 FFprobe 校验结算；没有完整制品则记失败。非零退出码及读取/编码错误保留在任务详情和原始日志中。进程不活跃不凭日志停更判定，避免误杀长时间单步计算。服务启动时先取得 `dbDir/server.lock` 文件锁、检查数据库并恢复任务，再开放两个监听器；若另一实例仍持锁则拒绝启动。

磁盘满、输入损坏、FFmpeg 失败等实际错误统一记录日志和任务失败状态；上传阶段出错则返回错误并清理临时文件。服务意外退出且数据库尚未来得及写失败状态时，下次启动按进程与文件恢复。首版不加每任务超时、磁盘预留、自动删历史或复杂重试策略。

## 6. 本机管理后台

页面只在 `http://127.0.0.1:<admin.port>/` 打开。首次打开用配置 token 登录；服务器建立随机内存会话，cookie 使用 `Path=/; HttpOnly; SameSite=Strict`，使本机 `/v1/...` 的视频预览可带会话。登录与后台写请求检查 `Host/Origin`，写请求还需 CSRF 值；重启后重新登录。Token 不放入页面源码、localStorage 或媒体下载 URL。

页面包含进行中任务（参数、状态、进度）、历史任务列表（成功与失败）、任务详情（提示词、原始与执行参数、引用预览、日志、视频播放/下载）和创建任务表单（提示词、首尾帧或有序参考、可选参数）。表单从 `/v1/capabilities` 读取默认值及范围，提交到同一个 `POST /v1/tasks` 接口。历史素材和视频均经本机鉴权接口读取。

## 7. FFmpeg/FFprobe 与 Toonflow 接入边界

FFmpeg 和 FFprobe 是 **h3.c 自身的运行依赖**：它用 FFprobe 读取参考视频信息，用 FFmpeg 解码参考图片、视频和音频，并把生成帧与音频编码为 MP4。服务在任务结算时也调用 FFprobe 检查成品。它们不是 Python 包；首版从服务进程的 `PATH` 查找并在启动预检中检查可执行，不在 `config.json` 再列两个路径。部署到不同机器时通过运行环境的 `PATH` 提供即可。

服务只理解 h3 的素材顺序与提示词，不理解 Toonflow 的 `{{ref N}}`。Toonflow 后续适配需保留视频节点原始有序 `refList` 的编号；编号包括 IMAGE、VIDEO、AUDIO、STRING 等引用，媒体编号可能跳号，不能拆成图片/视频/音频数组后重新从 1 编号。适配器改写提示词中的引用、提交 multipart、轮询任务、下载视频，再交给 Toonflow 原有文件保存链路。首尾帧与参考列表冲突时在适配层提示用户。

## 8. 实施顺序与验证

1. 配置读取、四目录、单实例文件锁、SQLite、双监听与 token/后台会话。
2. 创建任务、参数与引用校验、并发名额、h3 子进程和日志。
3. 状态/进度、查询与 Range 下载、重启恢复、本机管理页面。
4. 实际烧制短任务并验证文本、首尾帧、有序参考；随后再做 Toonflow 适配。

验收重点是高耗时任务不被超时终止、`reuse/coreReuse` 互斥、上传引用与历史预览、服务停止后烧制继续及重启恢复、满载 429、后台本机访问、完整成品下载。编译或 HTTP 启动通过不等于实际烧制验证通过。

## 9. 本地依据

- h3.c [命令行参数](/Users/liyang/Documents/minimax-h3-c/h3.c/main.c)、[生成与参数校验](/Users/liyang/Documents/minimax-h3-c/h3.c/h3.c)、[FFmpeg 调用](/Users/liyang/Documents/minimax-h3-c/h3.c/h3_ffmpeg.c)、[说明文档](/Users/liyang/Documents/minimax-h3-c/h3.c/README.md)
- 提示词长度依据：[tokenizer](/Users/liyang/Documents/minimax-h3-c/h3.c/h3_tokenizer.m)、[文本编码器](/Users/liyang/Documents/minimax-h3-c/h3.c/h3_text_encoder.c)、[模型配置](/Users/liyang/Documents/minimax-h3-c/minimax-h3/FL2VA/text_encoder/config.json)
- Toonflow [视频节点](/Users/liyang/Toonflow-app/packages/nodes/videoGenerationNode/src/index.vue)、[媒体生成链路](/Users/liyang/Toonflow-app/apps/server/src/utils/media/generation.ts)、[引用编号](/Users/liyang/Toonflow-app/packages/nodeShared/src/useNodeReferences.ts)
- SQLite 官方：[JSON 数据类型与 JSONB](https://sqlite.org/json1.html)、[列类型亲和性](https://sqlite.org/datatype3.html)
