# LessonsCut 0.2 开发与验证

当前为第二阶段开发版本，已接入相邻帧自动精查和本地词级ASR，证据与监督复核全部通过的任务自动推进，异常转人工。完整生产恢复、VFR与设计A01–A32全部验收仍未完成。详细流程见[自动剪辑说明](AUTOMATION.md)。

## 已有能力

- React/TypeScript中文管理界面：视频/标题图片上传、批量入队、任务进度、证据抽帧、切点与标题确认、新修订及成片下载。
- FastAPI API：管理员令牌、授权目录入库、持久化任务、提交幂等、修订冲突检查、旧复核失效、独立输出。
- 单Worker：Qwen分段识别与逐帧精查，本地ASR并行转写；MiniMax分开独立读取标题/姓名并审查起止边界。正常任务自动生成标题、渲染和复核，异常才人工确认。
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

浏览器访问`http://127.0.0.1:18765`，用管理令牌对应的「访问密码」登录。在模型配置页保存三个角色，在剪辑模板配置已批准片头；一次选择多个课程，确认后点击「开始批量处理」。标题图逐课自动生成，人工图片仅作异常备用。按[AUTOMATION.md](AUTOMATION.md)配置服务器本地ASR；异常可核对证据后创建新修订。完整操作和配置版本规则见[工作台接入记录](workbench-20261016.md)。

默认`exceptions_only`只要求异常人工确认；批次`approval_policy=always`可要求每条确认。低置信、推断课名、姓名分歧、语音跨切点、复核不通过等均不会静默发布。API文档为`/docs`；业务请求需Bearer令牌。

开发可以在Mac运行后端并远程调用现有3090模型；本次仅向服务器隔离安装CPU ASR运行环境和权重，未部署管理应用、未改变Qwen服务。Linux运行使用相同配置。若使用PostgreSQL，配置`LESSONSCUT_DATABASE_URL=postgresql+psycopg://...`，并确保数据库及所有Worker共享同一data目录。当前不允许横向扩展Worker。

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

0.2与完整设计之间仍有明确差距：

1. 自动剪辑要求可证明的单一连续授课区间。无真人、无可确认结尾、复杂交叉语音或同层级证据冲突仍需人工；不会因基准样本少量通过而推断通用正确率。
2. VFR/HDR/隔行/多声道素材转待人工，不自动转换。当前渲染为CPU libx264默认，h264_nvenc只有配置入口，未在3090实测。
3. 只有一个Worker可持有data目录锁；失联租约不自动重跑，要先核对旧进程和供应商状态。跨容器分布式GPU租约、生产数据库迁移与高级恢复后续实现。
4. 标题支持最多两轮有依据修正，其他语义失败转人工；调用预算限制调用数，尚未实现按实时价格预留金额。上传硬上限20GiB，磁盘配额与长期保留策略还未实现。
5. 当前API仍为开发子集，已接入配置管理，未实现设计中的SSE、全部分页与工程下载界面。每次渲染已在工作目录写project.json/report.json；跨机重建还需相同哈希原素材和生成图。
6. 起止边界做局部相邻帧复核，授课内部仅抽样，本地ASR虽全片转写，仍不是全片逐帧视觉或完整语音语义审核；机械完整解码并不能代替内容审核。
7. PostgreSQL驱动已接入但本轮未运行真实PostgreSQL集成测试。CI流程已编写，远端运行结果需以GitHub为准。
8. 三个模型角色及模板已实现不可变版本，批次保存配置引用，人工修订保持原版本；明确重新分析使用新版本。角色超时、调用量预算已接入，金额预算仍未实现。ASR/部署设置仍由.env配置。0.2保存原始PTS、time_base、相邻帧与源帧区间，但确认计划公开接口仍用源秒数，只接受已校验CFR，完整VFR工程接口后续实现。

0.1记录见[基础开发验证](development-checks-20261015.md)；0.2记录见[自动精查与ASR真实验证](automation-checks-20261015.md)。

## 后续里程碑

接下来补齐预算、更多质量返工、生产恢复、VFR、GPU渲染和目标服务器压测；最后按设计A01–A32逐项验收。字幕生成与去logo仍不进入首版基础范围。
