# 企业级知识库管理后台

> 基于 [Open WebUI](https://github.com/open-webui/open-webui) (128K+ Star) 二次开发
> 开发者：顾扬 | 2026-07-20 ~ 2026-07-21

## 功能模块

| Tab | 功能 |
|---|---|
| 📁 Files | 文件管理 + Unlink Only 按钮 |
| 🧩 Chunks | 分块预览 / 合并 / 拆分 / 重建向量 |
| ⏳ Processing | 实时进度监控（SSE + 轮询） |
| 📊 Evaluate | 检索质量评估（recall/precision/MRR）+ Prompt 模板配置 |
| 📸 Snapshots | 版本管理（快照 / 回滚 / 差异对比） |

## 技术栈

Python / FastAPI / SQLAlchemy / ChromaDB / LangChain / SvelteKit / TypeScript / Tailwind CSS / DeepSeek API / SSE / Prompt Engineering

## 项目结构

```
├── backend/
│   ├── models/knowledge.py          # 数据模型（5 张新表）
│   ├── routers/knowledge.py         # API 路由（26 个新端点）
│   ├── routers/files.py             # 文件处理进度集成
│   └── migrations/                  # Alembic 迁移
├── frontend/
│   ├── components/                  # 7 个 Svelte 组件
│   ├── routes/                      # 5 个页面路由
│   └── apis/                        # API 客户端
├── docs/                            # 开发文档 / 问题记录
├── 测试文档/                        # 4 份测试数据
└── README.md
```

## 统计数据

| 指标 | 数据 |
|---|---|
| 新增数据库表 | 5 |
| 新增 API 端点 | 26 |
| 新增前端页面 | 5 |
| 新增前端组件 | 8 |
| 解决的问题 | 19 |
| 上游 PR | 2 |
| 开发周期 | 2 天 |

## 上游贡献

- [#27222](https://github.com/open-webui/open-webui/pull/27222) fix: knowledge_fs grep splits on literal backslash-n
- [#27249](https://github.com/open-webui/open-webui/pull/27249) fix: mutable default argument in generate_function_chat_completion

## 部署方式

将 `backend/` 和 `frontend/` 目录中的文件合并到 Open WebUI 项目对应位置，然后：
```bash
cd open-webui
npm run build
cd backend
WEBUI_SECRET_KEY="your-key" python -m uvicorn open_webui.main:app --host 127.0.0.1 --port 8080
```
