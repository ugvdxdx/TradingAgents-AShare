"""
研报知识系统 (Research Knowledge System)
=========================================

五层架构:
  L1. Collector  — 数据采集层 (小鹅通圈子爬虫 + 增量更新)
  L2. Cleaner    — 数据清洗与标准化层
  L3. Extractor  — 知识提取层 (LLM 结构化提取)
  L4. Store      — 知识存储层 (SQLite + 双层知识库)
  L5. Service    — 知识服务层 (API + 检索 + 回测)

与选股系统集成点:
  - 辩论阶段: Service.query() 提供行业/个股知识
  - 增量采集: Collector 增量拉取新帖子
  - 回测: Service.snapshot() 提供历史知识状态
"""
