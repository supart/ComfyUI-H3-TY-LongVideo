# Codex 安装指引

帮助用户安装本插件时，先阅读 [README](README.md) 和 [安装与迁移](docs/安装与迁移.md)，确认目标是 Windows、现有 ComfyUI 已能运行 H3，并找到它实际使用的 `python.exe`。

- 在目标 ComfyUI 的 `custom_nodes` 中只保留一个 `ComfyUI-H3-TY-LongVideo` 目录；已有版本先在服务停止后备份到 `custom_nodes` 外。
- 用目标 Python 检查 Torch、torchaudio 与 CUDA，再安装 `requirements.txt`；不要擅自替换现有 GPU 环境。
- 检查 `ffmpeg.exe` 和 `ffprobe.exe`；示例另需 VHS、SelfLift Avatar、BFSNodes、KJNodes，以及用户自己的 H3 模型、视频和参考图。依赖和模型必须按实际工作流核对。
- 重启目标 ComfyUI、刷新前端后，先验证节点与模型，再分析一段带音轨视频，最后只生成一段做验收。ASR 和唱歌人声分离模型可能首次联网下载。
- 不把离线测试当作目标机运行成功；不要声称 Codex 读取仓库即可自动配齐模型或保证工作流通过。

本仓库没有整体开源许可证。上游许可仍需单独确认；不要替用户推断再分发或商用权利。
