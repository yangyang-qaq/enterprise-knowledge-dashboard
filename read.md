# 基于 Open WebUI 的企业级知识库管理后台

> 在开源项目 [Open WebUI](https://github.com/open-webui/open-webui)（128K+ Star）基础上二次开发，新增可视化的知识库管理仪表板。
> **全 12 个 Phase 已完成 ✅** | 2026-07-20 ~ 2026-08-14（Phase 12 于 2026-09-12 补完） | 41 个问题 | 2 个上游 PR | 后端全链路验证通过

---

## 项目简介

Open WebUI 是一个自托管的 AI 对话平台，支持接入 ChatGPT、DeepSeek、Ollama 等大模型。本人在其已有的 RAG 能力之上，独立设计并实现了企业级知识库管理后台：

| 模块 | 说明 |
|---|---|
| 🧩 **分块预览与手动调整** | 文档上传后自动展示分块结果，支持合并/拆分分块，调整后重建向量 |
| ⏳ **向量化进度可视化** | 实时展示文档处理进度（pending→chunking→embedding→completed），SSE 流式推送 |
| 📊 **检索质量评估面板** | 输入测试查询，展示 Top-K 检索结果与分数，支持人工标注计算 recall/precision/MRR |
| 📸 **知识库版本管理** | 创建知识库快照，支持回滚和快照间差异对比 |
| 📝 **Prompt 模板配置** | 知识库级 RAG Prompt 模板，支持变量替换，已接入聊天管道 |
| 🔪 **多策略文档分块** | 7 种分块算法 + jieba 关键词提取 + 问题自动生成 |
| 🤖 **多 Agent 工作流编排** | Agent 角色预设、知识库绑定、LLM 真实调用、检索溯源、结构化报告输出 |
| 🧪 **单元测试** | 83 个 pytest 用例（44 个分块策略 + 39 个图谱检索） |
| 🔥 **并发压力测试** | Locust 100 并发 + Mock LLM + PostgreSQL 多 worker，定位 ChromaDB 写锁 |
| 🎯 **Faithfulness 离线评测** | 40 条黄金集 + LLM-as-judge + CI 门禁，每个 PR 自动跑 |
| 🕸️ **知识图谱扩展召回** | LLM 抽「实体—关系—实体」三元组建图（5 张表）+ 多跳游走融合召回，让答案能跨文档拼出来；配套文件级召回 A/B 与回归门 |

---

## 技术栈

| 层级 | 技术 |
|---|---|
| **后端框架** | Python / FastAPI（异步 ASGI） |
| **前端框架** | SvelteKit + TypeScript + Tailwind CSS |
| **数据库** | SQLite（开发）/ PostgreSQL（生产），SQLAlchemy ORM + Alembic 迁移 |
| **向量数据库** | ChromaDB |
| **AI/LLM** | DeepSeek API / Prompt Engineering / RAG |
| **文档处理** | LangChain Text Splitters |
| **实时通信** | SSE (Server-Sent Events) |

---

## 系统架构

```
┌─────────────────────────────────────────────────┐
│                  浏览器 (SvelteKit)               │
│  8-Tab: Files│Chunks│Processing│Eval│Faithful    │
│         Snapshots│Agents│Graph                   │
├─────────────────────────────────────────────────┤
│                  FastAPI 后端                     │
│  Knowledge Router │ Retrieval Router │ Graph     │
│  39 个新增端点     │ Prompt 模板注入   │ 图谱召回  │
│         │                   │                    │
│    ┌────┴────┐  ┌──────────┐  ┌───────────┐    │
│    │ SQLite  │  │ ChromaDB │  │DeepSeek API│    │
│    │(12 新表)│  │(向量库)  │  │ (LLM 推理) │    │
│    └─────────┘  └──────────┘  └───────────┘    │
└─────────────────────────────────────────────────┘
```

---

## 开发统计

| 指标 | 数据 |
|---|---|
| 新增数据库表 | 12 张 |
| 新增 API 端点 | 39 个 |
| 新增前端页面 | 8 个 Tab + 13 个子组件 |
| Phase 12 改动文件 | 33 个（18 改 + 15 新增，不含镜像副本） |
| 解决的问题 | 41 个 |
| 开发周期 | 2026-07-20 ~ 2026-08-14（Phase 12 补于 2026-09-12） |
| 上游 PR | 2 个 bug fix PR 已提交 |
| 离线评测 | Faithfulness 40 条黄金集 + 召回 A/B 16 条 + CI 门禁与图谱回归门 |
| 单元测试 | 83 个 pytest 用例 |

---

## 上游贡献

| PR | 内容 |
|---|---|
| [#27222](https://github.com/open-webui/open-webui/pull/27222) | fix: knowledge_fs grep splits on literal backslash-n instead of newline |
| [#27249](https://github.com/open-webui/open-webui/pull/27249) | fix: mutable default argument in generate_function_chat_completion |

---

## 简历描述

**项目名称**：基于 Open WebUI 的企业级知识库管理后台

**项目描述**：在开源项目 Open WebUI（128K+ Star）的 FastAPI + SvelteKit 架构上进行二次开发，独立设计并实现了可视化的 RAG 知识库管理仪表板，涵盖文档分块管理、向量化进度监控（SSE）、检索质量评估（recall/precision/MRR）、知识库版本快照与回滚、知识库级 Prompt 模板配置。深度参与 RAG 全链路优化，Prompt 模板已接入聊天管道。

**技术栈**：Python / FastAPI / SQLAlchemy / ChromaDB / LangChain / SvelteKit / TypeScript / Tailwind CSS / DeepSeek API / SSE / Prompt Engineering

**主要工作**：
- 深入 RAG pipeline：文档加载→LangChain 分块→ChromaDB 向量化→检索评估→Prompt 模板注入，全链路参与
- 设计 12 张新数据库表 + Alembic 迁移，遵循 SQLAlchemy 异步架构，兼容 SQLite/PostgreSQL
- 新增 39 个 RESTful API 端点，复用 JWT 认证和 RBAC 权限体系
- 实现文档分块可视化预览，支持合并/拆分后重建向量索引
- 使用 SSE + 轮询双通道实现向量化进度实时推送
- 设计检索质量评估流程：查询→Top-K→人工标注→recall/precision/MRR 自动计算
- 实现知识库级 RAG Prompt 模板配置，支持变量替换，已接入聊天管道实现闭环
- 实现知识库版本快照功能，支持元数据回滚和差异对比
- 基于 SvelteKit + Tailwind CSS 构建 8-Tab 管理界面
- 搭建 RAG Faithfulness 离线评测体系（40 条黄金集 + LLM-as-judge 忠实度判定 + GitHub Actions CI 门禁，低于门槛自动 build 失败），首次运行即发现并修复「文件处理状态 completed 但向量未入库」的真实 bug
- 实现知识图谱增强 RAG：LLM 抽「实体—关系—实体」三元组建图（5 张表）+ 多跳游走融合召回，并配套文件级召回 A/B 与回归门；实测定位并修复了融合策略的负收益（跨文档 recall Δ 由 −0.1458 转为 0.0000）
- 向 Open WebUI 上游提交 2 个 bug fix PR，独立排查并解决 41 个技术问题
