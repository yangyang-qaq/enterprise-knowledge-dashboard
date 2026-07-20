# 企业级知识库管理后台

> 基于 [Open WebUI](https://github.com/open-webui/open-webui) (128K+ Star) 二次开发
> 开发者：顾扬 | 2026-07-20

## 项目简介

在 Open WebUI 的 FastAPI + SvelteKit 架构上进行二次开发，独立设计并实现了可视化的 RAG 知识库管理仪表板。

## 功能模块

| Tab | 功能 | 说明 |
|---|---|---|
| 📁 Files | 文件管理 | 原有功能 + Unlink Only 按钮 |
| 🧩 Chunks | 分块管理 | 预览/合并/拆分/重建向量 |
| ⏳ Processing | 进度监控 | SSE 实时推送 + 轮询 |
| 📊 Evaluate | 检索评估 | 查询→标注→recall/precision/MRR |
| 📸 Snapshots | 版本管理 | 快照/回滚/差异对比 |

## 技术栈

- **后端**：Python / FastAPI / SQLAlchemy / ChromaDB / LangChain
- **前端**：SvelteKit / TypeScript / Tailwind CSS
- **AI**：DeepSeek API / RAG / Sentence Transformers
- **实时**：SSE (Server-Sent Events)

## 项目结构

```
├── backend/
│   ├── models/knowledge.py          # 数据模型 (新增 5 张表)
│   ├── routers/knowledge.py         # API 路由 (新增 24 个端点)
│   ├── routers/files.py             # 文件处理集成
│   └── migrations/                  # Alembic 迁移
├── frontend/
│   ├── components/                  # Svelte 组件 (7 个)
│   ├── routes/                      # 页面路由 (5 个)
│   └── apis/                        # API 客户端
├── docs/                            # 项目文档
│   ├── 开发文档.md                  # 完整技术方案
│   ├── 问题记录.md                  # 15 个技术问题排查
│   ├── 行动清单.md                  # 原始计划
│   └── read.md                      # 简历描述
├── 测试文档/                        # 知识库测试数据
└── README.md
```

## 统计数据

- 新增数据库表：5 张
- 新增 API 端点：24 个
- 新增前端组件：5 页面 + 7 组件
- 修改文件：11 个
- 解决的问题：15 个
- 开发周期：1 天

## 本地运行

```bash
# 原始项目
git clone https://github.com/open-webui/open-webui.git
cd open-webui

# 将本仓库的文件合并进去：
# backend/    → open-webui/backend/open_webui/
# frontend/   → open-webui/src/lib/ 和 src/routes/
# 然后 npm run build && 启动 uvicorn
```
