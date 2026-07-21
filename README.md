# 企业级知识库管理后台

> 基于 [Open WebUI](https://github.com/open-webui/open-webui) (128K+ Star) 二次开发
> 开发者：顾扬 | 2026-07-20 ~ 2026-07-21

---

## 功能模块

| Tab | 功能 | 说明 |
|---|---|---|
| 📁 Files | 文件管理 + Unlink Only | 只取消关联不删文件，配合快照回滚 |
| 🧩 Chunks | 分块预览 / 合并 / 拆分 / 重建向量 | 可视化 RAG 分块结果，手动修正 |
| ⏳ Processing | 实时进度监控 | SSE 推送 + 3秒轮询兜底 |
| 📊 Evaluate | 检索评估 + Prompt 配置 | recall/precision/MRR + 自定义 Prompt 模板 |
| 📸 Snapshots | 版本管理 | 快照 / 回滚 / 差异对比 |

---

## 技术栈

**后端**：Python / FastAPI / SQLAlchemy / ChromaDB / LangChain

**前端**：SvelteKit / TypeScript / Tailwind CSS

**AI**：DeepSeek API / RAG / Prompt Engineering / SSE

---

## 项目结构

```
├── README.md
├── backend/
│   ├── models/knowledge.py         # 5 张新表 + Pydantic 模型
│   ├── routers/knowledge.py        # 26 个 API 端点
│   ├── routers/files.py            # 上传流程进度钩子
│   ├── middleware.py               # Prompt 模板聊天管道注入
│   └── migrations/                 # Alembic 迁移
├── frontend/
│   ├── components/                 # 7 个 Svelte 组件
│   ├── routes/                     # 5 个页面 + layout
│   └── apis/                       # API 客户端
└── 测试文档/                       # 4 份中文测试数据
```

---

## 统计数据

| 指标 | 数据 |
|---|---|
| 新增数据库表 | 5 |
| 新增 API 端点 | 26 |
| 新增前端页面/组件 | 13 |
| 修改文件数 | 10 |
| 排查解决问题 | 19 |
| 上游 PR | 2 |
| 开发周期 | 2 天 |

---

## 上游贡献

| PR | 内容 |
|---|---|
| [#27222](https://github.com/open-webui/open-webui/pull/27222) | fix: knowledge_fs grep splits on literal backslash-n |
| [#27249](https://github.com/open-webui/open-webui/pull/27249) | fix: mutable default argument in generate_function_chat_completion |

---

## 本地运行

将此仓库文件合并到 Open WebUI（v0.10.2）项目中：

```bash
# backend/  → open-webui/backend/open_webui/
# frontend/ → open-webui/src/lib/ 和 src/routes/

cd open-webui
npm run build
cd backend
WEBUI_SECRET_KEY="your-key" HF_ENDPOINT="https://hf-mirror.com" \
  python -m uvicorn open_webui.main:app --host 127.0.0.1 --port 8080
```

---

## 核心亮点

- **RAG 全链路**：文档加载→LangChain 分块→ChromaDB 向量化→检索评估→Prompt 注入，全链路参与
- **SQLite UNIQUE 约束**：深入排查 3 种失败方案，最终用原始 SQL + 10000 偏移解决
- **双通道可靠性**：SSE 实时推送 + 3 秒轮询兜底
- **Prompt 闭环**：从模板编辑到聊天管道注入，完整链路打通
