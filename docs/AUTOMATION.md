# 0.2 自动切点与本地语音识别

目标：证据充分且复核无冲突的课程自动完成，异常才人工确认。基础内容规则不变：6秒批准片头、1.5秒具体课程标题、连续授课与原声、独立1080p MP4；不新增字幕或去logo。

## 运行分工

- 现有3090上的Qwen3.8：画面分类、标题/姓名证据选择、相邻帧精查。
- 本地faster-whisper large-v3-turbo：CPU/int8全片转写，输出段落/词级时间戳、语言与识别质量值；不把音频送云端。
- MiniMax M3：先独立读取必要帧和相关转写事实，再对照本地计划检查，最后复核实际MP4抽帧。
- 阿里qwen-image-3.0：按批准文字和原片标题参考图生成标题，最多两轮有依据的修正。
- 程序：验证证据引用与直接文字依据，校验时间、生成确定性计划、按整数源帧裁切、验证音频与发布。

实查开发服务器3090显存已占用约20.8GB，因此本次ASR用服务器CPU，未停止/切换Qwen，也未增加公开ASR端口。ASR由本机进程或认证SSH启动独立CPU进程，音频经stdin传输，stdout返回JSON。服务器CPU计算可与3090视觉识别并行；同一任务ASR缓存受文件锁保护。

## 配置

```dotenv
LESSONSCUT_AUTOMATIC_PLAN_APPROVAL=true
LESSONSCUT_MAX_CALLS_PER_JOB=160
ASR_BACKEND=ssh
ASR_SSH_HOST=root@192.168.14.12
ASR_PYTHON=/mnt/sdc/apps/lessonscut-asr/venv/bin/python
ASR_WORKER_PATH=/mnt/sdc/apps/lessonscut-asr/asr_worker.py
ASR_MODEL_PATH=/mnt/sdc/models/lessonscut-asr/large-v3-turbo
ASR_THREADS=16
```

在服务器本身运行应用时用`ASR_BACKEND=local`并安装`uv sync --extra asr`；SSH开发模式无需把模型下载到Mac。单独ASR脚本兼容服务器现有Python3.10；应用主程序仍要求Python≥3.11。本次服务器专用venv只安装faster-whisper，不动其他环境。

批次POST `/api/v1/batches`新增`intro_asset_id`和`approval_policy`（默认`exceptions_only`，可用`always`要求逐条确认）。必须选择登记的批准片头；也可配置`LESSONSCUT_DEFAULT_INTRO_ASSET_ID`。没有必需ASR或片头时进入异常，不猜测。

## 决策流程

1. 读取实际源帧PTS与time_base，检查CFR，抽帧用源PTS精确选择，不以四舍五入后的秒数定位图片。
2. 头尾各60秒每2秒抽样，主体每30秒抽样。ASR并行生成全片带时间戳转写，使用源/音频哈希绑定证据。
3. 发现首次主讲人、最后有效授课及片尾候选区间。课件/演示保留，不能把人物消失当结束。相同品牌卡可在开头或结尾出现：单帧识别的品牌身份与时序角色分开；位于最终教学之后的品牌卡只作为片尾候选，还要独立精查。
4. 在单一变化的候选区间内分段收窄，直至前后两帧相邻；再次独立询问边界前后帧。类别变化不单调、模型改判、低置信或样本覆盖错误，立即转异常，不按估算秒数裁切。
5. 课名必须由对应OCR或明确ASR文字直接支持；概括标题、无法辨认的姓名及多证据冲突转人工。简繁规范化只用于比对，原始OCR/ASR保留。词级语音横跨切点且无法合理对齐也转异常。
6. MiniMax先不看本地通过结论，独立提取事实；事实一致后，检查相邻帧、语音上下文与批准片头。每个必需检查都通过且没有未解决issues才写入不可变自动计划。
7. 标题生成和实际合成首中末帧复核通过后才发布。授课内部保持单一连续源区间；输出按整数源帧trim，音轨按同一源时钟裁切。

云端只接必要帧与相关文字；不声称M3听过原音频。ASR置信度也不是正确性的证明，自动通过仍依赖直接证据、独立事实及结构检查。

## 缓存、重试与恢复

模型返回非数字confidence、缺字段等格式问题最多要求两次格式修复，不能自行将high映射成任意数值。语义冲突不会通过格式修复自动洗成通过。JSON响应和调用usage保留。

相同任务/修订内，模型请求以端点、模型、完整输入及图片内容哈希缓存，复测不重复调用已经成功的相同请求；失败/未知请求不会伪造成功缓存。ASR结果绑定源哈希、音频哈希、模型路径和实现版本，缓存保留词级结果。更换权重时使用新模型目录或配置版本，不能原地替换后继续沿用旧缓存。

渲染完成后保存计划/标题哈希检查点，网络复核失败可复用同一成片；依赖变更必须创建新修订。真正语义异常仍待人工。开发修复后重跑的历史原因保存在审计里，不把最初失败删掉。

## 验证与查询

```bash
uv run pytest -q
uv run python -m scripts.e2e_courses --source-dir '/path/to/course-videos'
```

真实验证脚本通过素材入库与批次API创建任务，然后执行同一Worker。它不会将历史标题/姓名/切点输入模型；只有处理结束后才读历史基线比较结果。转写、PTS、识别、边界、生成页、模型响应、渲染工程和复核报告留在被忽略的data目录，公开报告另作脱敏摘要。

新增只读接口：`/jobs/{id}/transcript`、`/jobs/{id}/report`（均位于`/api/v1`并需管理员认证）。报告包含抽样范围和异常，不把“全片解码”称为“全片语义审核”。

## ASR权重来源与复现

faster-whisper支持large-v3-turbo的CTranslate2模型。开发服务器无法直连HuggingFace时，本次使用ModelScope镜像：`pengzhendong/faster-whisper-large-v3-turbo`，固定版本`c312f538745ce1237d0b0c8eef0b81d91ea1fce6`。下载config/tokenizer/vocabulary/preprocessor与model.bin，逐文件匹配仓库清单SHA256后发布；模型主文件SHA256：`e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da`。来源清单保存在服务器模型目录provenance.json。本次不把第三方模型权重提交项目。

资料：[faster-whisper实现与CPU int8](https://github.com/SYSTRAN/faster-whisper)、[模型清单](https://github.com/SYSTRAN/faster-whisper/blob/master/faster_whisper/utils.py)、[ModelScope镜像](https://modelscope.cn/models/pengzhendong/faster-whisper-large-v3-turbo)。

本轮1号自动交付、3号语音跨片尾转人工的真实结果见[验证记录](automation-checks-20261015.md)。

仍有限制：VFR/HDR/隔行/多声道、无法确认的声画交叠和多课程文件走人工。没有人物或课尾没有可确认片尾候选的素材也保守转人工；不因本轮两条样本通过就宣称适配所有课程。完整分布式恢复、金额预算与生产部署仍按原设计后续实施。
