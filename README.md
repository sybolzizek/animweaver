# AnimWeaver

AnimWeaver is a local MCP toolkit for turning still images into editable cutout animations. It keeps source pixels, cutout ownership, motion and draw order separate. Bring your own PNG masks; no model, API key or cloud service is required. The included paper-signal example creates its own assets and can render PNG or MP4.

本地 MCP 插件。以一张或多张图片为材料，制作可编辑的裁片场景和动画。原始像素、裁片归属、运动和绘制顺序分开保存。适合分层合成、局部动作和遮挡重排；不是视频分割、3D 重建或剪辑软件。遮罩可以来自任意能输出 PNG 的工具；插件不附带或要求安装模型。

## 安装并接入 Agent

需要 Node.js 22 或更新版本（含 npm）和 Python 3.11+；MP4 另需带 `libx264` 的 FFmpeg。npm 安装包提供 CLI 和 Playwright，`setup` 单独准备图像处理所需的 Python 环境，不会在 npm 安装阶段执行脚本，也不安装模型。

从 GitHub 克隆后，在仓库目录用 npm 打包安装（无需 npm 账号）：

```sh
git clone https://github.com/sybolzizek/animweaver.git
cd animweaver
npm ci
npm pack
npm install -g ./reference-materials-mcp-0.1.0.tgz
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

场景结构、版本补丁和时间线规则由 TypeScript 实现；浏览器读取同一份编译后的时间线。只修改动作或片长时，新版本复用原来的 PNG 和网格，不重新读原素材。Python 保留 MCP 协议适配、像素处理和资源落盘。npm 包已经包含编译结果，安装包的用户不需要 TypeScript 编译器。

工具参数由 MCP schema 提供。安装 `pytest` 后在本目录运行 `python -m pytest -q`；浏览器测试用 `REFERENCE_BROWSER_DEPS` 和 `REFERENCE_BROWSER_EXECUTABLE` 指定本机运行时。
