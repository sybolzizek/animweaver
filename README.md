# AnimWeaver

本地 MCP 插件。以一张或多张图片为材料，制作可编辑的裁片场景和动画。原始像素、裁片归属、运动和绘制顺序分开保存。适合分层合成、局部动作和遮挡重排；不是视频分割、3D 重建或剪辑软件。遮罩可以来自任意能输出 PNG 的工具；插件不附带或要求安装模型。

## 安装并接入 Agent

发布渠道是 npm。需要 Node.js 22（含 npm）和 Python 3.11+；MP4 另需带 `libx264` 的 FFmpeg。npm 包提供 CLI 和 Playwright，`setup` 单独准备图像处理所需的 Python 环境，不会在 npm 安装阶段执行脚本，也不安装模型。

从仓库试装（发到 npm 后，将 `.tgz` 换成包名）：

```sh
npm ci
npm pack
npm install -g ./reference-materials-mcp-0.2.0.tgz
reference-materials setup --client codex --workspace "/absolute/path/to/materials"
```

- 建议使用持久的 npm 安装目录：客户端登记的是已安装包的位置，不要用随缓存清理而消失的临时 `npx` 目录。`setup` 的 Python 环境和浏览器存在用户缓存中，不会写进全局 npm 包目录。
- Claude Code 把 `--client codex` 换成 `--client claude`（只为该工作目录注册）；Codex 添加的是本机用户 MCP 条目，其读写范围仍限定于指定的素材目录。
- 其他支持本地 MCP stdio 的客户端可用 `--client config`（默认值）。命令把可导入的 `mcpServers` JSON 输出到 stdout，不修改客户端配置。
- 素材目录必须已存在。多个目录用不同的 `--name`，不要把整个个人目录当成素材目录。有 Chrome 或 Chromium 时，加 `--browser "<浏览器可执行文件的绝对路径>"` 跳过浏览器下载；仅处理图片和遮罩可用 `--materials-only`。
- 配置完成后新开会话。首次使用可让 Agent「用 Reference Materials 登记这张图，做一个裁片动作，先给我一帧预览」。不必再粘贴整份教程。

交给 Agent 安装时可以说：

> 从这个仓库 `npm pack` 后安装 Reference Materials，运行 `reference-materials setup`，使用我指定的客户端和素材目录，不改其他 MCP 条目；有本机 Chrome 就复用。新会话里确认能看到 `register_original` 和 `render_cutout_scene`，然后用我选的素材试渲一张 PNG。

### 手动接入

在仓库目录用 Python 3.11+ 安装依赖：`python -m pip install -r requirements.txt`。虚拟环境、Node 和浏览器请在运行客户端的机器上准备。

客户端通过 MCP stdio 启动 `server.py`。以下为采用 `mcpServers` 配置格式的示例；不同客户端的配置位置可能不同。素材目录需已存在，三个路径建议使用绝对路径。Windows 的 JSON 路径可使用正斜杠。

```json
{
  "mcpServers": {
    "reference-materials": {
      "command": "/absolute/path/to/venv/bin/python",
      "args": ["/absolute/path/to/reference-materials/server.py", "--workspace", "/absolute/path/to/materials"]
    }
  }
}
```

输入与输出以素材目录为边界，记录写入 `.reference-materials/`。这是受信任的本地工具，不是多人服务。

## 渲染

```sh
npm ci
npm run build
npm run install-browser
```

渲染需要 Node.js（建议 22 LTS）和可用的 Chromium。也可以在客户端启动环境中用 `REFERENCE_BROWSER_EXECUTABLE` 指定机器上已有的 Chrome 或 Chromium。桌面客户端的 PATH 不同时，可用 `REFERENCE_NODE` 和 `REFERENCE_FFMPEG` 指定可执行文件。没有带 `libx264` 的 FFmpeg 仍可导出 PNG。透明 PNG 可以继续合成，导出 MP4 前需把画面补成不透明。

### 第一次运行

```sh
python examples/paper-signal.py paper-signal-output --video
```

输出目录必须尚不存在。没有 FFmpeg 时去掉 `--video`。示例生成自己的静图、两张 PNG 遮罩和干净背景，然后通过 MCP 建场景并渲染；`rest.png`、`crossing.png`、`clear.png` 分别对应原位、穿过栏架后方、离开遮挡。脚本打印渲染用的 `scene.json` 路径；要改动画，可调整示例里的关键帧并输出到新目录。无需素材下载、模型或 API key。

`render_cutout_scene` 导出 PNG 时直接返回文件记录，导出 MP4 时返回任务 ID。用 `inspect_cutout_video` 查看状态和成片路径。重新连接后仍可用原任务 ID 查询；若宿主结束了渲染进程，会显示中断。重复提交同一场景、输出路径和帧率会返回原任务，不会自动重跑。

裁片树各自保留图片坐标。场景可同时使用多棵树的裁片，也可直接放入带透明度的图片；放置位置、骨骼和图层前后关系都可独立修改。

连续网格的附件可以绑定到变形表面；裁片可附加不会改变原图像素归属的底层补片。外观调整支持指定区域采样、随时间变化的局部光照，以及限定在指定承影层上的软阴影。使用原则见 [Agent 指南](AGENT-GUIDE.md)，也可通过 MCP 资源 `reference-materials://guide/compositing` 读取。

在两块单独运动的裁片之间，`seams` 可以用两条对应的源图像素线生成随两侧变形的接缝。PNG 回执返回该帧的接缝状态，MP4 回执汇总异常帧及首次出现时间。`windows` 用于仅在指定时间段连接，不替代作品的遮挡关系或骨骼绑定。

`underlap` 在裁片背后补全遮挡区；`paintover` 用带透明度的登记素材在指定源图坐标覆画局部纹理。两者都不改原素材或裁片归属。`mattes` 引用随动的遮罩裁片，只对指定目标做 `keep` 或 `erase`；遮罩可以标成 `matte_only`，不参与正常绘制。

连续表面的独立绘图层可以用同一 `mesh.domain` 固定网格取样坐标。使用相同的步长、偏移和骨端绑定才会得到相同的变形；裁片之间仍可独立绘制和修改。

`refine_image_split_edges` 产出的 ID 可以通过 `revise_cutout_scene.edge_mattes` 回接原裁片，或在创建时填入图层的 `edge_matte`。柔边只改变绘制表面，不重新分配像素或绑骨。底图上的裁片需要干净底板才能移动硬边之外的柔边；蒙皮在原网格覆盖范围内绘制，若需要预留空间则在绑定时明确 `mesh.domain`。导出的场景记录会报告被裁掉的柔边像素。

`revise_cutout_scene.motions` 按名称替换一个或多个组／裁片的动作轨，不动其余轨道。空数组清空指定轨道；可另传 `duration`，已有关键帧和作用区间若超出新片长会报错，不会偷偷改时序。

## 参考动作与自动检查

插件可以把外部跟踪器或 Agent 整理的二维点轨迹映射到现有场景节点。它不绑定姿态识别模型；上游只需输出工作区内的 JSON：

```json
{
  "schema": 1,
  "width": 1920,
  "height": 1080,
  "duration": 1.5,
  "tracks": {
    "Hip": [
      {"time": 0, "x": 840, "y": 620, "confidence": 0.98},
      {"time": 1.5, "x": 910, "y": 600, "confidence": 0.96}
    ],
    "Shoulder": [
      {"time": 0, "x": 830, "y": 430},
      {"time": 1.5, "x": 850, "y": 420}
    ],
    "Wrist": [
      {"time": 0, "x": 970, "y": 440},
      {"time": 1.5, "x": 850, "y": 300}
    ]
  }
}
```

`register_motion_reference` 会校验时间顺序、尺寸、置信度和片长，并在素材库中保存规范化快照。`retarget_motion_reference` 的绑定支持：

- `position`：相对于首个采样点的局部位移；
- `direction: [from, to]`：方向向量的旋转和可选长度缩放；
- `relative_direction`：减去父级或另一参考向量的旋转，得到子骨骼的局部转角；
- `fit`、时间区间、目标片长、低置信度插值和可选平滑。

映射只替换被点名节点的运动轨道，原裁片、网格、遮挡、外观和未映射动作保持不变。场景记录会保存动作参考 ID、绑定和低置信度修复数量。

已有姿态跟踪结果可直接用 `register_pose_reference` 转换：

- `format: "mediapipe"`：读取一个含 `frames` 的 JSON，每帧使用 `pose_landmarks` 或 `landmarks`，支持 33 点单人结果或多人列表；可指定归一化坐标或像素坐标。
- `format: "openpose"`：读取一个按文件名排序的逐帧 JSON 目录，支持 BODY_25 和 COCO_18 的 `pose_keypoints_2d`。

两种适配器都输出同一份动作格式。缺失人物或关键点会写成零置信度观测，后续映射会明确记录插值数量，不把缺帧伪装成可靠动作。MediaPipe 的时间可来自 `time`、`timestamp_ms` 或帧率；OpenPose 使用目录帧序和指定帧率。

完成映射后，用 `audit_cutout_scene` 在关键帧和固定采样点实际渲染检查。报告包含接缝跨度、关键事件的露底／未填充像素、无底板透明输出，以及基于相邻帧差分的单帧跳变候选。它不修改场景；Agent 根据报告定位对应轨道或合成关系，再调用现有修订工具。

## 可迁移场景包

`export_animweaver_scene` 把场景及完整素材依赖闭包写入新的 `.animweaver` 文件，包括源图快照、裁片选择、补充素材、动作参考、场景记录和运行时文件。包内 manifest 记录根场景、素材 ID、每个文件的大小和 SHA-256；导出不会覆盖已有文件。

`import_animweaver_scene` 在写入素材库前验证路径、文件声明、校验值、素材记录、依赖闭包和 ID 冲突。相同素材按字节复用，不重新写入；不同内容不能占用同一个素材 ID。导入返回可继续调用 `revise_cutout_scene`、`audit_cutout_scene` 和渲染工具的根场景，以及一条独立的导入回执。

`.animweaver` 是带完整性清单的 ZIP 容器，不依赖原工作区的绝对路径。包中保留登记时的工作区相对来源说明，但实际编辑和渲染使用随包保存的素材快照。

场景结构、版本补丁和时间线规则由 TypeScript 实现；浏览器读取同一份编译后的时间线。只修改动作或片长时，新版本复用原来的 PNG 和网格，不重新读原素材。Python 保留 MCP 协议适配、像素处理和资源落盘。npm 包已经包含编译结果，安装包的用户不需要 TypeScript 编译器。

工具参数由 MCP schema 提供。安装 `pytest` 后在本目录运行 `python -m pytest -q`；浏览器测试用 `REFERENCE_BROWSER_DEPS` 和 `REFERENCE_BROWSER_EXECUTABLE` 指定本机运行时。
