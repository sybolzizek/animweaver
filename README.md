# AnimWeaver

本地 MCP 插件。以一张或多张图片为材料，制作可编辑的裁片场景和动画。原始像素、裁片归属、运动和绘制顺序分开保存。适合分层合成、局部动作和遮挡重排；不是视频分割、3D 重建或剪辑软件。遮罩可以来自任意能输出 PNG 的工具；插件不附带或要求安装模型。

## 安装并接入 Agent

发布渠道是 npm。需要 Node.js 22（含 npm）和 Python 3.11+；MP4 另需带 `libx264` 的 FFmpeg。npm 包提供 CLI 和 Playwright，`setup` 单独准备图像处理所需的 Python 环境，不会在 npm 安装阶段执行脚本，也不安装模型。

从仓库试装（发到 npm 后，将 `.tgz` 换成包名）：

```sh
npm ci
npm pack
npm install -g ./reference-materials-mcp-0.3.0.tgz
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

组和裁片可以设置 `local_time`，把父级时钟映射成该节点及其后代读取关键帧时使用的时钟。映射公式为 `offset + (parent_time - start) * scale`；`scale` 可以为零或负数，分别用于冻结和倒放。默认 `free` 不限制结果；`clamp`、`repeat`、`pingpong` 必须提供正 `duration`，分别用于钳制、循环和往返。嵌套映射按父到子依次计算，因而可以先循环整个动作组，再单独调整某个后代的速度。镜头、接缝窗口和局部遮罩窗口仍使用场景全局时间。

图层还可以用 `scene` 引用另一个可编辑场景。子场景保留自己的画布、节点、动作与素材依赖，在 Three.js 后端中先渲染为透明目标纹理，再像普通平面一样进入父场景；`offset` 放置子画布原点，父图层的变换、透明度和深度顺序继续作用于完整子场景。父图层求出的 `local_time` 就是该实例传给子场景的时间，因此同一个场景可重复引用而不共享播放头，也可以继续嵌套。依赖必须是有效的 `cutout_scene` 且不得递归成环；场景源不能使用只适用于单张栅格的柔边、网格、补片、覆画、Matte-only 或外观字段。Canvas2D 会明确拒绝这种图层，不会静默拍平。

`revise_cutout_scene.local_times` 只替换指定层或组的时钟映射并复用现有栅格；传 `null` 恢复为直接继承父级时钟。拥有局部时钟的节点轨道使用局部时间，因此关键帧可以超过场景片长；移除最后一个局部时钟后，超出全局片长的关键帧会重新被拒绝。

浏览器运行时先由 `scene-evaluate.mjs` 求出节点局部时间、姿势、世界矩阵、透明度、深度、局部光照和镜头状态，再由具体后端完成绘制。Canvas2D 与 Three 后端消费同一套纹理、遮罩、接缝、阴影与离屏合成契约；表面挂点通过后端提供的几何查询接入求值器，因此时间和层级语义不依赖具体绘制实现。新增后端可以复用同一求值结果，不需要改变场景工具接口。

`render_cutout_scene.backend` 默认为 `canvas2d`，也可以设为 `three`。Three.js 后端使用透明纹理平面与真正的 `THREE.PerspectiveCamera`，消费同一份场景求值结果；当前支持普通父子变换、完整镜头轨、平面透视深度、嵌套局部时间、可递归的可编辑场景源、图层透明度、显式绘制顺序，以及由组透明度或组深度触发的嵌套隔离合成，PNG 与 MP4 使用同一路径。相机通过离轴视锥保留任意 `pivot` 和屏幕空间 `move`，并直接消费 `focal`、`truck`、`dolly`、`zoom`、旋转和确定性 shake；图层和隔离组的 `plane_depth` 是 Three 空间中的真实 Z 坐标。离屏组使用另一台零深度严格保持 1:1 的阶段相机，只计算组内直接单元的相对透视，再由父级相机投影组表面，避免重复应用全局镜头。

每个普通图层、场景实例与隔离组都会先生成同规格的预乘透明 GPU 表面，再进入统一的全屏合成 Pass。合成器使用两张 ping-pong 累积目标和一张可复用源目标，按确定的绘制顺序执行 `normal`、`add`、`multiply` 或 `screen`；图层和组共用同一套 Shader 运算，不按素材名称或场景用途分支。阶段合成完成后，最终相机只投影一次完整结果。目标纹理在采样时校正 framebuffer 方向，透明度同时缩放 RGB 与 alpha，因此不会在嵌套时翻转或重复衰减颜色。同一子场景的多个引用拥有独立求值器、目标纹理、相机和局部播放头。

Matte 使用同一套表面协议。目标先生成预乘颜色表面；遮罩按自身动作、深度和图层透明度生成 Alpha 表面；`keep` 或 `erase` Shader 同时缩放目标 RGB 与 Alpha，过滤后的目标再进入常规混合 Pass。多个 Matte 按声明顺序执行，`windows` 继续读取场景全局时间；读取遮罩时使用其未被其他 Matte 修改的原始 Alpha。`matte_only` 遮罩只参与遮罩 Pass，不进入正常绘制。目标与遮罩仍须位于同一隔离范围，这一约束在建场景时验证。

蒙皮图层把已经编译的规则网格直接构造成带索引的 `THREE.BufferGeometry`。基础顶点保持在源图像素坐标，UV、三角形拓扑和逐顶点完整权重在创建后不再重建；权重存入浮点数据纹理，每帧只更新另一张骨骼仿射矩阵纹理。顶点 Shader 对场景声明的全部骨骼求和，因此不受常见四骨骼顶点属性限制。变形后的顶点继续使用图层自身变换和真实 `plane_depth`，并作为普通预乘表面进入 Matte、混合、隔离组和最终相机。普通平面与网格统一校正向下增长的舞台坐标和纹理 UV，不会因后端不同上下翻转。

平面与蒙皮网格共用同一个表面点查询。规则平面直接映射源像素；网格先在编译出的三角形内做重心插值，再使用与顶点 Shader 相同的完整骨骼权重，最后依次应用图层变换、图层深度、隔离祖先深度和最终相机。运行时 `surfacePoint` 返回最终画面坐标；场景求值器内部使用最终相机之前的阶段点，使表面挂点能够从两个查询点得到位置与方向，同时保持附件自身的尺寸和深度。

接缝也只消费这套表面查询。Canvas2D 与 Three 共用同一个跨度、交叉和窗口求值器；Three 把求出的条带三角形写入可复用的动态 `BufferGeometry`，并在绘制顺序中插到两侧图层之前。接缝纹理、两侧较低透明度、共同混合模式、`max_span`、交叉拓扑和逐帧状态报告继续保持原契约。嵌套子场景的接缝纹理会随场景依赖递归装载。

局部光照在普通纹理平面和蒙皮网格上使用同一个片元函数：先把 sRGB 解码到线性空间，再按 `exposure` 与裁片 UV 上的轴向 `gradient` 调整亮度，最后编码回 sRGB。它不读取场景法线，也不自动创建光源。软阴影先把投影层经过动作、深度、透明度、网格和 Matte 后的真实 Alpha 渲染成 GPU 表面，再围绕声明的 `origin` 做偏移和缩放，以可分离高斯 Pass 模糊，并通过 source-atop 等价运算限定在指定承影层的 Alpha 内。背景承影在背景与可选 backing 合成后执行；阴影与承影对象仍须处于同一隔离范围。阴影完成后才进入组深度和最终相机，因此镜头只投影一次。

Three 后端当前只拒绝其余混合模式；请求包含这些模式时会明确失败，不会忽略字段或改用近似结果。Canvas2D 仍保留完整混合模式集合。纹理、Shader 合成和输出暂时保持与 Canvas2D 相同的直接 8 位通道约定，未来的全局线性光工作流应作为显式后端选项加入。

### 当前能力边界

AnimWeaver 是给 Agent 调用的可编辑 2.5D 合成工具箱，不负责替 Agent 完成美术设计，也不把低质量素材自动变成高质量镜头。当前边界是：

- Three 后端使用真正的透视相机、深度平面和 GPU 合成，但普通素材仍是二维纹理平面，不提供体积几何、三维重建或物理布料。
- 子场景会先渲染到独立透明表面，再作为一个平面进入父场景；子场景内部深度不会成为父场景可交叉遮挡的独立几何。
- 原生渲染暂不提供景深、运动模糊、体积光或粒子系统。Agent 可以登记外部生成的透明序列或图像组件，但插件不会按效果名称生成预设。
- Three 后端只支持 `normal`、`add`、`multiply` 和 `screen`；其他混合模式必须使用 Canvas2D 或在调用前改写合成方案。
- 最终相机投影固定尺寸舞台表面。明显推远、滚转或冲击可能露出舞台外透明区域；应预留构图余量并以 `audit_cutout_scene` 和实际 MP4 导出验证。
- `local_time` 作用于节点自身及全部后代，包括该节点的动作、透明度和深度轨。需要“内部动作循环、外部按全局时间登场”时，应把循环放在子节点，把全局位移和显隐放在不带局部时钟的父组。

这些限制属于稳定契约，不会静默近似。需要超出边界的镜头时，Agent 应把 AnimWeaver 用作可编辑合成阶段，而不是假装它是完整 DCC 或特效软件。

## 镜头与动作节奏

场景可以使用一条全局 `camera` 轨道控制画面位移、缩放和旋转。`pivot` 默认是画布中心；`move` 是不产生视差的屏幕空间构图偏移。`truck` 是场景空间的横向／纵向镜头位移，`dolly` 是向前或向后的镜头位移。裁片的 `plane_depth` 表示它相对背景平面的深度，正值更靠近镜头；镜头按 `focal` 做薄透视投影，未提供时焦距为画布较长边的两倍。由此可以让二维平面产生确定的大小关系和视差，而不引入三维网格、材质或预设场景。投影平面不能穿过镜头。

每个裁片或组都可以使用动画 `opacity`、`blend_mode` 和 `plane_depth`。混合模式直接对应有限白名单中的标准合成运算，包括 `normal`、`add`、`multiply`、`screen`、`overlay`、明暗与颜色分量模式。这些字段只是原始渲染操作，不会自动添加辉光、闪光、粒子或任何特效模板。遮罩会读取遮罩层动画后的透明度；接缝跟随两侧较低的透明度。`revise_cutout_scene` 的 `opacities`、`blend_modes` 和 `plane_depths` 可以只替换指定节点并复用原有栅格。

组在没有合成字段时仍是零开销的变换节点。只要组拥有透明度轨、非 `normal` 混合模式或非零平面深度，运行时就会把它的完整子树先绘制到透明离屏画布，再把结果作为一个原子表面合成到父级。离屏组可以嵌套；组级透明度因此不会在重叠子层上重复计算。为了保持确定的绘制顺序，一个离屏组的全部后代必须在图层列表中占据连续区间。阴影、接缝、局部遮罩和表面挂点不能跨越离屏边界；这类关系应放在同一组内或在组外完成。

最终的构图偏移、旋转和基础投影仍作用于完整画面；裁片深度投影在局部绘制、遮罩、表面点和接缝计算中保持一致。推远、平移和旋转可能露出画布外的透明区域，`audit_cutout_scene` 会检查全片采样、全局关键事件和冲击时刻，MP4 导出仍会拒绝透明帧。局部循环的关键帧目前由常规逐帧采样覆盖，不会被错误地当成同秒数的全局事件。

节点动作和镜头关键帧都支持 `linear`、`smooth`、`ease_in`、`ease_out`、`hold`。缓动属于从该关键帧开始的区间；`hold` 会保持当前值直到下一关键帧，用于蓄力、命中停顿或明确的姿势切换。`camera.shakes` 在基础镜头上叠加有时长、频率、衰减和种子的二维冲击，同一场景同一时刻会得到完全相同的画面，适合可复现审计和逐帧导出。它不移动场景节点，也不替代角色自身的后坐、重心和余势。

`revise_cutout_scene.camera` 替换整条镜头轨和冲击列表，并与只改动作、片长一样复用已经编译好的裁片素材。传入空的镜头对象可回到恒等构图；未提供该参数则保留原镜头。镜头 `zoom` 是没有视差的光学式整体缩放，`dolly` 才会依据各层深度改变相对大小。

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

`export_animweaver_scene` 把场景及完整素材依赖闭包写入新的 `.animweaver` 文件，包括源图快照、裁片选择、补充素材、动作参考、递归引用的子场景、场景记录和运行时文件。包内 manifest 记录根场景、素材 ID、每个文件的大小和 SHA-256；导出不会覆盖已有文件。

`import_animweaver_scene` 在写入素材库前验证路径、文件声明、校验值、素材记录、依赖闭包和 ID 冲突。相同素材按字节复用，不重新写入；不同内容不能占用同一个素材 ID。导入返回可继续调用 `revise_cutout_scene`、`audit_cutout_scene` 和渲染工具的根场景，以及一条独立的导入回执。

`.animweaver` 是带完整性清单的 ZIP 容器，不依赖原工作区的绝对路径。包中保留登记时的工作区相对来源说明，但实际编辑和渲染使用随包保存的素材快照。

## 多镜头影片工程

`register_audio` 把工作区内的 PCM WAV 登记成不可变音频素材，记录采样率、声道、片长和 SHA-256。`create_film_project` 按顺序组合多个可编辑场景；每个镜头使用稳定键、源场景起点和精确帧数，因而剪辑点不依赖浮点秒数。第一版影片工程使用连续硬切，并要求所有镜头具有相同画布尺寸。

影片可以记录 BPM、每小节拍数和逐镜头事件。事件使用镜头内帧号，并在编译时得到全片帧号；它们可用于动作、拟音和后续声音工具，不会被当作界面文案。`inspect_film_timeline` 返回镜头边界、节拍偏移、全局事件和音画长度警告。

`render_film_project` 异步逐镜头渲染同一条帧时间线，拒绝透明 MP4 帧，检查总帧数，汇总每个镜头的接缝状态，并在存在登记音轨时完成混流。用 `inspect_film_video` 查询任务。`revise_film_project` 会创建新的不可变时间线版本，并复用未修改的场景。当前影片工程仍走 Canvas2D 完整合成路径，因此包含 `scene` 图层的镜头应先用 Three 后端单独导出；影片级后端选择尚未加入，不会自动拍平子场景。

`export_animweaver_project` 把影片、音频和所有场景的完整素材依赖闭包写入 `.animweaver`；用 `import_animweaver_project` 导入后返回 `project`。单场景继续使用 `export_animweaver_scene` 和 `import_animweaver_scene`。

场景结构、版本补丁和时间线规则由 TypeScript 实现；浏览器读取同一份编译后的时间线。只修改动作或片长时，新版本复用原来的 PNG 和网格，不重新读原素材。Python 保留 MCP 协议适配、像素处理和资源落盘。npm 包已经包含编译结果，安装包的用户不需要 TypeScript 编译器。

工具参数由 MCP schema 提供。安装 `pytest` 后在本目录运行 `python -m pytest -q`；浏览器测试用 `REFERENCE_BROWSER_DEPS` 和 `REFERENCE_BROWSER_EXECUTABLE` 指定本机运行时。
