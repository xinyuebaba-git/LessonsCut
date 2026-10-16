# LessonsCut

课程视频批量剪辑应用：统一片头、生成精简标题页、保留连续授课与原声，输出可重建工程、MP4 和复核报告。

- [设计文档入口](docs/course-video-app/README.md)
- [智能体技能](skills/course-video-batch-edit/SKILL.md)
- [开发环境决策](docs/course-video-app/development-environment.md)
- [0.2开发版本：启动、测试与已知限制](docs/DEVELOPMENT.md)
- [0.2自动切点与本地语音识别](docs/AUTOMATION.md)
- [初轮：1号自动交付与3号异常拦截验证](docs/automation-checks-20261015.md)
- [3号收束语定格与实际成片验证](docs/ending-hold-checks-20261015.md)
- [批量工作台：模型配置、登录与逐课标题生成](docs/workbench-20261016.md)

开发环境采用现有 RTX 3090 24GB 和 Qwen3.8 多模态服务，MiniMax 官方 M3 API 负责任务监督与图片复核，阿里云百炼 `qwen-image-3.0` 负责标题生图；未来可迁移至 RTX 4090。凭据不进入版本库。

0.2在基础管理、队列与媒体验证上增加本地词级ASR、相邻帧切点精查、独立事实复核和正常任务自动推进；异常进入人工确认。生产部署与未支持素材类型见开发说明。
