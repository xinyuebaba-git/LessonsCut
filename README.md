# LessonsCut

课程视频批量剪辑应用：统一片头、生成精简标题页、保留连续授课与原声，输出可重建工程、MP4 和复核报告。

- [设计文档入口](docs/course-video-app/README.md)
- [智能体技能](skills/course-video-batch-edit/SKILL.md)
- [开发环境决策](docs/course-video-app/development-environment.md)
- [0.1开发版本：启动、测试与已知限制](docs/DEVELOPMENT.md)

开发环境采用现有 RTX 3090 24GB 和 Qwen3.8 多模态服务，MiniMax 官方 M3 API 负责任务监督与图片复核，阿里云百炼 `qwen-image-3.0` 负责标题生图；未来可迁移至 RTX 4090。凭据不进入版本库。

0.1已实现基础管理界面、持久化批量任务、模型适配、人工确认计划及媒体渲染验证；完整自动边界识别与生产部署仍在后续里程碑。
