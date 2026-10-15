# 接口与数据契约 v1.0

本文件定义待实现接口，不宣称已有 API。字段是应用规范化结构；供应商协议由适配器转换，不能将供应商某次输出直接当数据库类型。所有记录包含 `schema_version: "1.0"`，未知主版本拒绝执行；扩展仅增可选字段。

## 通用类型

| 类型 | 定义 |
|---|---|
| Rational | `{num: integer, den: positive integer}`，约分保存；帧率 num 必须正数 |
| MediaTime | `{pts: decimal_integer_string, time_base: Rational}`，字符串避免浏览器整数精度丢失；允许源负 PTS |
| SourceRange | `{start: MediaTime, end: MediaTime}`，同一流 time_base，左闭右开且 end > start |
| OutputRange | `{start_frame: nonnegative integer, end_frame: positive integer}`，左闭右开，属于本计划 output_fps |
| AssetRef | `{asset_id: string, sha256: hex64}`，物理路径仅由服务器资产登记表解析 |
| EvidenceRef | `{evidence_id, asset_ref, stream_index, source_range, sample_pts[], media_artifact_id, media_sha256}`；转写证据另外包含 text、识别服务和 call_id |
| Decision | `pass / fail / uncertain / not_applicable`，后者需 reason；总通过不能忽略必需项 |

整型帧数只能与明确 output_fps 配对使用。API 展示秒数是派生显示值，不作为编辑的唯一保存精度。音频映射使用音频流 time_base，显式记录与视频流起点的偏移。

## 模型配置与适配器

`ModelProfile`：profile_id、revision、role（recognizer/supervisor/title_generator）、provider_adapter、endpoint、model、credential_ref、locality、capabilities、limits、timeouts、budget_policy、lifecycle。

capabilities 声明 image_input、audio_input、video_input、structured_output、async_jobs、status_query、idempotency，并通过能力探测记录 `verified_at / evidence_id`。limits 至少包含 max_images、max_image_pixels、max_context_tokens、max_output_tokens、concurrency。不能把未支持的能力声明成 true 以通过检查。

核心适配器方法：

| 方法 | 输入 | 返回 |
|---|---|---|
| probe_capabilities | profile | 实际 model_id、能力、限制、健康结果 |
| recognize | call_context、evidence[]、policy | RecognitionResult |
| review | call_context、evidence[]、rubric、comparison_phase | ReviewResult |
| generate_title | call_context、approved_text、style_evidence[]、canvas | TitleAsset 或异步 provider_request_id |
| query_request / cancel_request | provider_request_id | 完成/运行/失败/未知；取消只对支持的服务生效 |

若需要本地 ASR，使用识别阶段可选 `transcriber_profile`，输入本地音频，输出带时间戳转写；不发送原音频到云端。生图工具可接图片或仅文字风格描述，必须明确是哪种，不声称未发送参考图时做过图像编辑。

错误统一为 `invalid_request / authentication / unsupported_capability / transient / rate_limited / timeout_unknown / out_of_memory / malformed_output / budget_exhausted / quality_failure`，附 retryable、provider_request_id 和原始错误引用。retryable 由确定性策略复核，不接受模型自行决定无限重试。

## 核心对象

### RecognitionResult

字段：job_id、revision、source_ref、model_profile_ref、call_ids、samples[]、title_candidates[]、presenter_candidates[]、boundary_candidates[]、uncertainties[]。

sample 包含 evidence_id、category、visible_text、confidence（可空）、reason。category 枚举 `intro_match / intro_other / lesson_title / series_or_credits / presenter_teaching / teaching_slide / teaching_demo / transition / outro / other / uncertain`。

candidate 包含 value、origin（visible_text/explicit_speech/inferred/human）、evidence_ids、conflicts。边界候选同时记录前后样本、候选源时间、音频上下文是否核实，不能只返回一个小数秒。语义输出无证据引用、漏样本、时间越界或 inferred 未人工确认时，不得生成已批准计划。

### EditPlan

字段：plan_id、job_id、revision、rule_profile_ref、model_profile_refs、source_refs、approved_title、presenter（可空）、title_origin、approval_events、output_spec、segments[]、audio_policy、evidence_ids、validation_policy_ref、dependencies_hash、plan_hash。

每个 segment 包含 kind（intro/title/lecture）、asset_ref、source_range（静态图为空）、output_range、fit、audio_mapping。title 还包含目标/实际时长、generation_asset_ref。lecture 可以在工程内部拆成相邻片段，但所有源范围必须构成单一连续区间、速度为 1，且对应音频没有重复或遗漏。

output_spec 包含 canvas、fps Rational、CFR、video_codec、pixel_format、audio_codec、sample_rate、channel_policy、color_policy。单段映射保存输出时刻到源时刻的线性时间关系及 CFR 重采样规则；不能用反复浮点相加生成边界。原计划不可原地修改，人工操作创建 revision+1。

### ReviewResult

字段：review_id、job_id、revision、stage、subject_refs（含 plan_hash 与 render_hash，渲染前 render_hash 为空）、reviewer_profile_ref（机械检查为空）、checks[]、overall_decision、sample_manifest、coverage_limits、repair_target、raw_response_ref。

check 包含 code、required、decision、evidence_ids、measured_value、threshold、reason。机械检查失败优先阻止发布，不由模型评分覆盖。云端原证据初审与本地结论对照保存为两份独立 review，comparison_phase 为 blind / compared；用户已确认的标题是检查目标，可在 blind 初审中提供，本地 pass 结论不可提供。

### ModelCall / Usage

字段：call_id、logical_request_id、job_id、revision、stage、role、profile_ref、attempt（1–3）、provider_request_id、status、started_at、duration_ms、input_evidence_hashes、request_ref、raw_response_ref、error、usage、cost。

usage：input_tokens、output_tokens、total_tokens、cached_tokens 均可为 null；另含 image_count、generated_seconds、gpu_seconds、usage_status（reported/partial/missing）、provider_units。reported 数值来源必须为供应商返回，估算放独立字段。cost 包含 currency、amount（可空）、pricing_version、basis（actual/estimated/unknown）。缓存计入规则保留在 provider_adapter，汇总必须显示未知调用数。

### Artifact / Delivery

artifact：artifact_id、kind、job_id、revision、stage、relative_storage_path、sha256、bytes、created_at、producer_version、dependency_hashes、status（temporary/approved/obsolete/published）。发布对象的 sha256 必须等于已通过复核对象。

delivery：delivery_id、job_id、revision、render_ref、report_ref、project_manifest_ref、destination_ref、published_filename、published_sha256、source_hashes_verified、exceptions[]、published_at。DELIVERED 必须有真实 delivery 记录，不能只凭模型响应置位。

## 首版 HTTP API

API 前缀 `/api/v1`，时间用 UTC ISO8601，界面按用户时区显示。所有写入需认证，长任务返回 202 与任务 ID，不同步等待模型。列表支持 cursor 分页。错误格式 `{code,message,details,request_id}`；校验 422、修订/幂等冲突 409、权限 403、资源缺失 404。

| 接口 | 用途与关键约束 |
|---|---|
| POST /assets/uploads | 创建上传会话，返回 upload_id 与受限上传目标 |
| POST /assets/uploads/{id}/complete | 上传完成后计算哈希与入库；未完整上传不可创建任务 |
| POST /assets/imports | 接受 authorized_root_id＋相对路径，不接受任意宿主绝对路径 |
| POST /model-profiles；POST /model-profiles/{id}/probe | 建立新配置修订与显式测试，凭据只返回引用 |
| POST /batches | asset_ids、rule_profile_ref、model_profile_refs、output_root_id、budget、approval_policy；需 Idempotency-Key |
| GET /batches/{id}；GET /jobs/{id} | 汇总状态或任务、当前修订、异常、进度、artifact 引用 |
| GET /jobs/{id}/events | SSE 事件流，支持 Last-Event-ID 恢复；数据库是状态真相 |
| GET /jobs/{id}/revisions/{rev}/plan | 计划、证据、源/输出时间、校验结果 |
| POST /jobs/{id}/revisions | base_revision、修改的结构化字段、reason；旧修订冲突返回 409 |
| POST /jobs/{id}/decisions | expected_revision、decision（confirm/accept_exception/exclude）、reason、证据；例外不能跳过文件/解码硬门槛 |
| POST /jobs/{id}/resume | expected_revision，验证阻塞条件已解除后重新派发；不清空历史计数 |
| POST /jobs/{id}/cancel | 停止本任务，新状态由执行器确认，不假称供应商已取消 |
| GET /jobs/{id}/usage；GET /jobs/{id}/delivery | 分角色用量或真实已发布文件与工程/报告 |
| GET /artifacts/{id}/content | 鉴权后读取登记对象，支持视频 Range；禁止任意路径/URL 代理 |

以上为开发契约，后续代码需生成 OpenAPI 与请求/响应模型并按这里的用例验收；本次不生成假运行 API 或应用代码。

## 配置样本的用法

[default-profile.json](examples/default-profile.json) 是设计模板，null 字段必须在现场绑定。提交批次时合并管理员配置、模板和批次覆盖，顺序为管理员允许范围内的批次覆盖优先于模板；用户未明确覆盖时不继承上一批。最终展开快照写入 revision，生成 hash 后使用。

首版不内置具体云厂商、权重名称和价格，避免把可配置方案锁定到历史环境。三个角色可以指向不同协议适配器；只有能力验证通过的配置才能入队。凭据不能随工程包导出。
