# ComfyUI-H3-TY-LongVideo

**TY Shot Planner 视频分段与角色替换节点，预发布版 `v0.1.0-beta.1`。**

**本项目基于 [Zzz1phyrf/ComfyUI-H3-LongVideo](https://github.com/Zzz1phyrf/ComfyUI-H3-LongVideo) 二次开发，感谢原作者的长音频分段与顺序生成实现。** TY 分支增加了源视频分段、上传截取、SelfLift 角色替换和逐段生成管理，保留原有长音频数字人功能。

将带声音的源视频分析为可编辑分段，通过 SelfLift Avatar 和 MiniMax H3 逐段替换人物；可以先生成一段，再批量生成、局部重做和合成。当前面向已经能运行 H3 的 Windows 环境，其他平台尚未验证。

## 安装

### 1. 安装节点

在目标 ComfyUI 的 `custom_nodes` 目录打开终端：

```powershell
git clone https://github.com/supart/ComfyUI-H3-TY-LongVideo.git ComfyUI-H3-TY-LongVideo
cd ComfyUI-H3-TY-LongVideo
```

也可下载 `v0.1.0-beta.1` 预发布附件，或在仓库的 `main` 分支选择 **Code → Download ZIP**。解压后的插件目录须命名为 `ComfyUI-H3-TY-LongVideo`，并直接包含 `__init__.py`、`nodes.py`、`web` 等，避免双层同名目录。

已有 TY 插件时，等任务结束并关闭 ComfyUI，把旧目录备份到 `custom_nodes` 之外，再替换新版，避免两份节点同时加载。详细更新和回滚步骤见[安装与迁移](docs/安装与迁移.md)。

### 2. 安装 Python 依赖

在插件目录使用**目标 ComfyUI 实际使用的 Python**，将占位路径改为便携版或虚拟环境的 Python：

```powershell
$ComfyPython = "<目标 ComfyUI 使用的 python.exe 完整路径>"
& $ComfyPython -m pip install -r .\requirements.txt
```

依赖为 `faster-whisper==1.2.1` 和 SoundFile；Torch、torchaudio、NumPy、Pillow、aiohttp 复用现有 ComfyUI 环境。插件不会自动安装缺失的 Python 包；不要使用系统 Python 代替目标环境，也不要直接升级已有 Torch/CUDA。

### 3. 确认运行依赖

| 依赖 | 用途 |
| --- | --- |
| 已能运行 H3 的 ComfyUI 与对应 H3、LoRA、VAE、CLIP 权重 | 实际视频生成的基础环境，权重自行安装并在工作流选择。 |
| [VideoHelperSuite（VHS）](https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite) | 当前 TY 的单段/批量生成使用 `VHS_VideoCombine` 保存输出。 |
| [SelfLift Avatar](https://github.com/slmonker/selflift-Avatar)、[BFSNodes](https://github.com/alisson-anjos/ComfyUI-BFSNodes)、[KJNodes](https://github.com/kijai/ComfyUI-KJNodes) | 随附 SelfLift 示例的额外依赖，并非全部 TY 功能都需要。 |
| FFmpeg 与 ffprobe | 视频探测、音轨提取、截取与合成；两个工具都必须可用。 |

确保启动 ComfyUI 的环境能找到 `ffmpeg`、`ffprobe`，或分别配置 `FFMPEG_PATH`、`FFPROBE_PATH` 为对应程序路径或所在目录。缺少工具时，从 [FFmpeg 下载页](https://ffmpeg.org/download.html)的 Windows 构建入口下载并解压。参考版本、检查命令和缓存位置见[安装与迁移](docs/安装与迁移.md)。上传与截取功能由本插件提供，无需另装一个 TY 插件。

### 4. 导入示例

重启 ComfyUI 并刷新浏览器，导入 [SelfLift 预发布示例](examples/TY_ShotPlanner_SelfLift_迁移版_20261025.json)，重新选择本机模型、参考图和带音轨视频，先分析并只生成一段。示例保留了排版、连线、提示词和生成参数，已清空素材选择、旧项目绑定及历史预览。

首次分析会按需下载 ASR `large-v3-turbo`；唱歌模式未提供独立人声时还会下载约 319 MiB 的 HDEMUCS。后续复用本机缓存。仓库不包含模型、FFmpeg、llama.cpp 二进制、素材或项目。

## Shot Planner 使用流程

### 上传与分析

在 **TY Shot Planner** 节点点击“上传视频”或“上传并截取视频”。源视频必须带音轨；截取时选择范围并确认，关闭或取消不会应用未确认的截取。

首次点击“分析并分段”只建立分段项目，不运行完整 H3 生成。ASR 首次使用可能下载 `large-v3-turbo`；唱歌模式可能额外下载人声分离模型。请预留网络、磁盘和分析时间。

成功应用新视频后，插件会清空旧项目绑定，并把按钮恢复为“分析并分段”。已有项目绑定时显示“重新分析并分段”；重新分析会创建新项目，旧项目和已生成文件仍保留。按钮修复的验证范围见[阶段说明](docs/阶段说明.md)。

### 检查分段

在审核面板预览源视频、试听原音频或人声，检查自动切点。可以拖动切点，或在波形内定位后切割；选中内部切点后可删除并合并相邻段。手动分段最长 15 秒，不能合并为超过限制的片段。修改后点“保存并确认”。

参考图可沿用画布输入，也可在“图像素材库”上传并为各段选择。编号与素材说明保持一致；需要更改参考图或切点时，先保存，再生成受影响的片段。

| 参数 | 含义 |
| --- | --- |
| `source_video` | 当前输入视频，由上传或选择输入文件设置。 |
| `mode` | `speaking` 口播、`singing` 唱歌、`speech_light` 居中演讲。 |
| `target_seconds` / `max_seconds` | 自动分段的目标时长与最长时长。 |
| `generation_megapixels` | 按源画幅比例确定生成尺寸的像素预算。 |
| `fixed_prompt` | 当前工作流的全局完整提示词，可通过连线输入。 |
| `project_id` / `segment_index` | 项目与当前分段状态，由插件管理；新素材无需填写旧值。 |

Shot Planner 使用本地分段规则，直接使用 `fixed_prompt` 完整提示词（`final` 路径），无需额外安装 Qwen GGUF 或 llama.cpp。H3 工作流自身的 CLIP/文本编码器仍须按 H3 要求安装，二者并非同一依赖。

### 生成、重做与合成

- **仅生成本段**：首次在目标机使用时优先点击某段卡片的此按钮，检查模型、输出和参考图。
- **批量生成**：依次生成各段，成功完成后合成并配回原音频。
- **重新生成本段**：使用当前画布设置重做选中片段。新版本成功前保留旧成功版本；最新 SelfLift 种子修复尚需重载后实机专项验收。
- **当前段完成后暂停 / 停止后续生成**：让当前片段完成，不继续提交后续片段。
- **恢复上一版 / 仅重新合成**：恢复成功的历史片段，或用已有片段重新合成；无需重新分析整个视频。

切点、素材或工作流发生修改后，先确认保存的状态，避免把旧版本片段当作新结果。人物一致性、动作质量与段间连续性仍需人工检查，插件不保证模型逐帧一致或无缝长镜头。

## 保存位置

| 内容 | 默认位置 |
| --- | --- |
| 成片 | `ComfyUI/output/TYH3LongVideo/final_videos` |
| 分段项目、源素材副本、生成片段与状态 | `ComfyUI/output/TYH3LongVideo/projects` |
| 用户规则 | `ComfyUI/user/default/TYH3LongVideo` |
| ASR 缓存 | `ComfyUI/models/faster-whisper` |
| 人声分离权重 | `ComfyUI/models/TYH3LongVideo/hdemucs_high_trained.pt` |

仓库不包含这些运行数据。需要继续旧项目时，应单独备份并迁移完整项目及其依赖；复制插件本身不会恢复旧项目。

## 文档与验证

- [安装、升级、回滚与常见问题](docs/安装与迁移.md)
- [预发布功能与验证边界](docs/阶段说明.md)
- [长音频数字人功能及可选 Qwen 运行时](docs/长音频功能.md)
- [验证摘要](验证摘要.md)
- [更新日志](CHANGELOG.md)

本分支的三个节点标识为 `TY_ShotPlanner`、`TY_H3LVUnified`、`TY_H3LVDirectorTask`。它与上游当前版本的功能和接口不同：当前 TY 不提供上游新增的外部 API 提示词生成器或原生视频保存链路，请按本仓库说明接线。

本预发布版的运行代码与 2026-10-25 已验证阶段版本相同，沿用该日期的验证记录；最新 SelfLift 重生成修复仍待后端重载后的真实采样验收。安装后先分析并生成一段，完整边界见[验证摘要](验证摘要.md)。

## 许可

当前主项目未附整体开源许可证，本次预发布不新增开源授权。上游也未提供整体开源许可证；仓库公开可见不等于获得代码再分发、修改或商用授权。请保留 [llama.cpp 第三方许可](third_party/llama.cpp-LICENSE)；它不等于本插件或模型的授权。模型及外部节点按各自来源的许可使用。
