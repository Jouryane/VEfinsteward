# VE4 资产配置战术模块

> 本模块独立于 VE4/（应用代码）和 userdata/（用户隐私数据），与两者平级存放。

## 目录结构

- `fundamental/` -- 基本面分析法
  - RAG 向量知识库 + SQL 结构化存储
  - 研报/新闻解析 -> 时间排序 + 置信度分层
  - Agent/Skill 通过知识库指导大模型学习分析
- `quantitative/` -- 量化分析法
  - 数据源 API 接入（tushare 等）
  - 策略代码生成 + CodeSandbox 验证
  - 程序输出（文本 + 图片）-> 大模型最终解析
- `shared/` -- 共享基础设施
  - Agent 基类、Orchestrator 编排器、数据模型
- `config/` -- 数据源 API 密钥配置
