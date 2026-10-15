# LessonsCut 0.1 开发与验证

当前为第一阶段可运行开发版本。设计中的完整异常自动处理、精确边界自动识别、本地语音识别、VFR渲染和生产恢复尚未完成；不将0.1称为32项验收全部通过的应用。

## 已有能力

- React/TypeScript中文管理界面：视频/标题图片上传、批量入队、任务进度、证据抽帧、切点与标题确认、新修订及成片下载。
- FastAPI API：管理员令牌、授权目录入库、持久化任务、提交幂等、修订冲突检查、旧复核失效、独立输出。
- 单Worker：Qwen图片分批识别提出候选，MiniMax M3独立看源抽样，人工确认后生成标题、渲染、机械验证和实际输出复核。
- 阿里云`qwen-image-3.0`：DashScope原生同步协议，1920×1080标题，图片URL即时下载，图片计量单列。
- FFmpeg：CFR/SDR/mono或stereo素材，6秒片头＋量化标题＋连续授课，H.264/AAC，完整解码、首中尾音频相关性、源哈希与无覆盖发布。
- SQLAlchemy：本机默认SQLite，支持PostgreSQL连接。当前运行模式为单Worker，不自动管理或切换服务器Qwen模型。

## 本地启动

要求Python≥3.11、Node.js≥20、FFmpeg/ffprobe和uv。根目录执行：

```bash
uv sync --frozen
cp .env.example .env
```

填写管理令牌、本地Qwen端点、MiniMax和百炼按量API Key。`DASHSCOPE_API_KEY`不能使用Coding Plan密钥；地域与端点必须匹配。已有本地.env时不执行cp覆盖。凭据存宿主配置，不提交Git。

```bash
cd frontend
npm ci
npm run build
cd ..
uv run uvicorn lessonscut.api:create_app --factory --host 127.0.0.1 --port 18765
```

另一个终端启动Worker：

```bash
uv run python -m lessonscut.worker
```

浏览器访问`http://127.0.0.1:18765`，填管理令牌。上传课程与已批准片头；只勾选课程建任务。识别结束进入待核对，逐条确认标题、源起止秒、输出帧率及片头。可以上传已批准标题图，或按识别到的原风格调用Qwen-Image生成。

**0.1所有计划均需人工确认**。候选来自稀疏抽样，不是最终精确切点。人工需要查看原视频并核实对应帧，不能直接点确认把候选当结论。API文档为`/docs`；业务请求需Bearer令牌。

开发可以在Mac运行后端并远程调用现有3090模型；本次未向服务器安装应用。Linux运行使用相同配置。若使用PostgreSQL，配置`LESSONSCUT_DATABASE_URL=postgresql+psycopg://...`，并确保数据库及所有Worker共享同一data目录。当前不允许横向扩展Worker。

## 模型接口

| 角色 | 默认服务 | 路径 |
|---|---|---|
| 本地识别 | Qwen3.8 / 现有3090 | `/v1/chat/completions`，接口实际模型ID配置化 |
| 监督评估 | MiniMax官方M3 | `/v1/chat/completions`，图片输入 |
| 标题生成 | 阿里云qwen-image-3.0 | `/api/v1/services/aigc/multimodal-generation/generation` |

官方参考：[MiniMax多模态](https://platform.minimax.cn/docs/api-reference/text-openai-api)、[Qwen图像API](https://help.aliyun.com/zh/model-studio/qwen-image-generation-and-editing-api-reference)。阿里返回的usage是图片计量；请求ID、usage和原始响应保存在本地data/work与数据库，不包含凭据。

可选真实联调（会产生模型调用/图片费用，仅使用合成测试图）：

```bash
uv run python -m scripts.probe_services --qwen --supervisor
uv run python -m scripts.probe_services --image
```

脚本没有自动重试付费生成。状态未知要先查调用记录，不能重发直到成功。正常应用任务也不会无限重试。

## 测试与限制

```bash
uv run pytest -q
cd frontend
npm run build
```

单元/集成测试使用临时数据库、HTTP mock和FFmpeg合成片，不调用真实云API，不依赖私人课程。测试范围涵盖认证、幂等、修订、worker fencing、调用预算、图片接口契约、用量、路径越界、实际视频渲染/解码/音频映射和损坏文件。

0.1与完整设计之间仍有明确差距：

1. 所有切点人工确认；还没有本地ASR及自动边界逐帧精查，也未完成云端盲审后与本地结果的自动冲突裁决。
2. VFR/HDR/隔行/多声道素材转待人工，不自动转换。当前渲染为CPU libx264默认，h264_nvenc只有配置入口，未在3090实测。
3. 只有一个Worker可持有data目录锁；失联租约不自动重跑，要先核对旧进程和供应商状态。跨容器分布式GPU租约、生产数据库迁移与高级恢复后续实现。
4. 自动质量返工尚未启用，失败进入人工；调用预算限制调用数，尚未实现按实时价格预留金额。上传硬上限20GiB，磁盘配额与长期保留策略还未实现。
5. 当前API为0.1开发子集，未实现设计中的SSE、全部分页/配置管理与工程下载界面。每次渲染已在工作目录写project.json/report.json；跨机重建还需相同哈希原素材和生成图。
6. 源/输出只做首中尾静态帧复核，不是全片逐帧视觉或完整语音语义审核；机械完整解码并不能代替内容审核。
7. PostgreSQL驱动已接入但本轮未运行真实PostgreSQL集成测试。CI流程已编写，远端运行结果需以GitHub为准。
8. 模型配置目前通过服务端.env提供；任务级完整配置快照/预算版本、前端模型配置管理和原始PTS工程契约尚未完成。0.1确认计划使用源秒数，且只接受已校验的CFR；不能声称已经实现设计中的完整VFR/PTS接口。

本轮实际结果见[开发验证记录](development-checks-20261015.md)。

## 后续里程碑

先完成原始PTS索引、边界精查、可插拔本地ASR和证据冲突处理，再恢复默认异常才人工的策略。之后补齐预算、返工、恢复、VFR、GPU渲染和目标服务器压测；最后按设计A01–A32逐项验收。字幕生成与去logo仍不进入首版基础范围。
