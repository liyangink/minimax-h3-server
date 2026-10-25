# minimax-h3-server

独立 Python 3.11+ 本地 h3 视频生成服务。设计见 [design.md](design.md)，模块契约见 [moduleContracts.md](moduleContracts.md)。

安装与启动：

```sh
uv venv --python 3.11
uv pip install --python .venv/bin/python -e .
cp config.example.json config.json
# 编辑 config.json 中的绝对路径与 token，设文件权限为 0600
chmod 600 config.json
.venv/bin/minimax-h3-server --config config.json
```

启动前需要 h3.c 可执行文件、模型目录、工作目录中的 `h3_shaders.metal`，以及 `PATH` 中的 FFmpeg 和 FFprobe。API 监听地址由配置决定；后台只监听 `127.0.0.1`。两个监听器共用任务协调器与 SQLite；停服时正在烧制的 h3 进程继续运行，重启后恢复状态与制品结算。

API 使用配置文件中的 Bearer Token；本机后台在 `http://127.0.0.1:8788/`（或配置的 `admin.port`）用同一 Token 登录。创建任务用 `POST /v1/tasks` multipart，包含一个 JSON `request` part 和按 `fileKey` 命名的素材 part。`GET /v1/capabilities` 提供参数范围与默认值，其他接口和请求结构见 [设计文档](design.md)。

首次验收已用 256×256、22 帧、35 layers、2 steps 串行跑通纯文本、首帧、首尾帧、有序双参考图；四条任务均生成含视频与音轨的 MP4，HTTP 下载与落盘文件一致。首尾帧任务在服务正常停服时继续运行，重启后恢复并结算成功；后台也已用真实制品验证预览和下载。

当前上传解析为严格校验 `request` JSON 的 UTF-8，使用了 Starlette 1.10.0 的 `MultiPartParser` 内部字段。因此依赖固定在该版本；升级 Starlette 时须手动验证 multipart 上传和请求大小限制。
