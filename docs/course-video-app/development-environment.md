# 开发环境决策：2026-10-15

本条是后续用户明确决定，对原先4090目标部署配置的开发阶段补充。

| 角色 | 开发配置 | 说明 |
|---|---|---|
| GPU | 已有RTX 3090 24GB | 调用常驻识别服务，不自动卸载或切换现有模型 |
| 内容识别 | 已有Qwen3.8多模态API | 通过配置读取base URL和实际model ID，启动时验证 |
| 云端监督/视觉复核 | MiniMax官方MiniMax-M3 | 使用原生图片输入，独立检查抽帧；未验证可用前不自动回退 |
| 标题生图 | 阿里云百炼qwen-image-3.0 | 用户后续指定；DashScope原生multimodal-generation接口，依本地提取的原标题风格说明 |
| 最终目标GPU | RTX 4090 24GB | 后续部署和压测，不将3090速度当作4090性能 |

MiniMax官方笔记中的国内域名为`api.minimaxi.com`；当前官方文档示例为`api.minimax.cn`。endpoint独立配置，鉴权测试记录实际使用域名。密钥仅保存本地被git忽略的.env，或部署时挂载的密钥文件；任何诊断输出禁止打印Authorization或密钥正文。

官方资料（2026-10-15查阅）：

- [OpenAI兼容接口及M3图片输入](https://platform.minimax.cn/docs/api-reference/text-openai-api)
- [图像生成](https://platform.minimax.cn/docs/api-reference/image-generation-t2i)
- [模型目录](https://platform.minimax.cn/docs/guides/models-intro)
- [Qwen-Image-3.0产品页](https://www.qianwenai.com/models/qwen-image-3.0)
- [阿里图像生成与编辑API](https://help.aliyun.com/zh/model-studio/qwen-image-generation-and-editing-api-reference)

M3不能直接接收音频。需要音频证据时使用本地语音识别适配器；尚未配置则进入人工核对，不以视觉判断代替语音判断。标题生图已从最初MiniMax方案改为阿里云qwen-image-3.0；生成与监督分别使用各自平台的密钥。默认用官方推荐DashScope原生同步协议，参数size使用星号；OpenAI兼容模式使用x且当前官方示例要求业务空间域名，旧域名兼容路径实测404，不作为默认。返回图片URL后即时下载，图片计量不算文本token。

本次仓库只接收技能和设计文件，不上传历史课程视频、私人Obsidian笔记或凭据。历史基线中外部证据路径需要在本地绑定，克隆仓库不等于获得媒体素材。
