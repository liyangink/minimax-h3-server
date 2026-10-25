# minimax-h3-server

独立 Python 3.11+ 本地 h3 视频生成服务。设计见 [design.md](design.md)，模块契约见 [moduleContracts.md](moduleContracts.md)。

安装与启动（实施完成后）：

```sh
uv venv --python 3.11
uv pip install -e .
cp config.example.json config.json
# 编辑 config.json 中的绝对路径与 token，设文件权限为 0600
chmod 600 config.json
uv run --no-sync minimax-h3-server --config config.json
```

启动前需要 h3.c 可执行文件、模型目录、工作目录中的 `h3_shaders.metal`，以及 `PATH` 中的 FFmpeg 和 FFprobe。API 监听地址由配置决定；后台只监听 `127.0.0.1`。
