# Enterprise Knowledge Base Management Dashboard

> A comprehensive RAG observability and management layer built on top of [Open WebUI](https://github.com/open-webui/open-webui) (128K+ Stars)
> Author: Guyang | 2026-07-20 ~ 2026-07-21

---

## Overview

This project adds an enterprise-grade knowledge base management dashboard to Open WebUI, giving users full visibility and control over their RAG (Retrieval-Augmented Generation) pipeline. It introduces 5 new tabs in the knowledge base detail page, 5 new database tables, and 26 new API endpoints.

## Feature Modules

| Tab | Feature | Description |
|---|---|---|
| 📁 Files | File Management + "Unlink Only" | Remove a file from a KB without physically deleting it, enabling safe snapshot rollbacks |
| 🧩 Chunks | Chunk Preview / Merge / Split / Reindex | Visualize how documents are split, manually adjust chunk boundaries, and rebuild vector indexes |
| ⏳ Processing | Real-time Progress Monitoring | SSE push + 3-second polling fallback for reliable progress tracking |
| 📊 Evaluate | Retrieval Quality Assessment + Prompt Config | Query → Retrieve → Annotate → compute recall/precision/MRR; custom RAG prompt templates |
| 📸 Snapshots | Version Management | Create, rollback, compare, and delete KB snapshots |

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│              Browser (SvelteKit)                 │
│   Tab Nav: Files │ Chunks │ Processing │ Eval │ Snap │
├─────────────────────────────────────────────────┤
│               FastAPI Backend                     │
│  Knowledge Router │ Retrieval Router             │
│  26 new endpoints  │ Prompt Template Injection   │
│        │                    │                    │
│   ┌────┴────┐  ┌──────────┐  ┌───────────┐     │
│   │ SQLite  │  │ ChromaDB │  │ LLM API   │     │
│   │(5 tables)│  │ (vectors)│  │(Inference)│     │
│   └─────────┘  └──────────┘  └───────────┘     │
└─────────────────────────────────────────────────┘
```

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Python / FastAPI (async ASGI) |
| **Frontend** | SvelteKit / TypeScript / Tailwind CSS |
| **Database** | SQLite (dev) / PostgreSQL (prod), SQLAlchemy ORM + Alembic |
| **Vector DB** | ChromaDB |
| **Document Processing** | LangChain Text Splitters |
| **Real-time** | SSE (Server-Sent Events) |
| **AI/LLM** | DeepSeek API / RAG / Prompt Engineering |

---

## Project Structure

```
├── README.md
├── DESIGN.md                           # Design document
├── backend/
│   ├── models/knowledge.py             # 5 new tables + Pydantic models
│   ├── routers/knowledge.py            # 26 new API endpoints
│   ├── routers/files.py                # Upload pipeline progress hooks
│   ├── middleware.py                   # Prompt template injection in chat pipeline
│   └── migrations/                     # Alembic migration
├── frontend/
│   ├── components/                     # 7 Svelte components
│   │   ├── ChunkManager.svelte
│   │   ├── ProcessingDashboard.svelte
│   │   ├── EvaluatePanel.svelte
│   │   ├── SnapshotManager.svelte
│   │   ├── KnowledgeBase.svelte
│   │   └── Files.svelte
│   ├── routes/                         # 5 pages + layout
│   │   └── workspace/knowledge/[id]/
│   │       ├── +layout.svelte
│   │       ├── +page.svelte
│   │       ├── chunks/+page.svelte
│   │       ├── processing/+page.svelte
│   │       ├── evaluate/+page.svelte
│   │       └── snapshots/+page.svelte
│   └── apis/                           # API client functions
└── test-docs/                          # 4 Chinese test documents
```

---

## Database Design (5 New Tables)

| Table | Purpose |
|---|---|
| `knowledge_chunk` | Individual chunk tracking with content hash, token count, and metadata |
| `knowledge_processing_task` | Per-file processing status (pending → chunking → embedding → completed/failed) |
| `knowledge_batch_task` | Batch processing summary for multi-file uploads |
| `knowledge_relevance_judgment` | Ground truth annotations for retrieval evaluation |
| `knowledge_snapshot` | KB version snapshots storing file associations and chunk metadata as JSON |

---

## API Endpoints (26 New)

All endpoints are mounted under `/api/v1/knowledge/{id}/`:

### Chunks
| Method | Path | Description |
|---|---|---|
| `POST` | `/chunks/preview` | Preview chunking results without writing to vector DB |
| `GET` | `/files/{fileId}/chunks` | Get all chunks for a file |
| `GET` | `/chunks/{chunkId}` | Get a single chunk's full content |
| `POST` | `/chunks/merge` | Merge consecutive chunks |
| `POST` | `/chunks/split` | Split a chunk at a specified position |
| `POST` | `/chunks/reindex` | Rebuild vector index from current chunks |

### Processing
| Method | Path | Description |
|---|---|---|
| `GET` | `/progress` | Get processing status for all files in the KB |
| `GET` | `/progress/stream` | SSE stream for real-time progress updates |
| `GET` | `/progress/batch` | Batch task progress summary |

### Evaluate
| Method | Path | Description |
|---|---|---|
| `POST` | `/evaluate/query` | Execute a test query and return Top-K results |
| `POST` | `/evaluate/annotate` | Annotate a result as relevant/not-relevant |
| `GET` | `/evaluate/judgments` | View all annotations grouped by query |
| `DELETE` | `/evaluate/judgments/{query}` | Delete annotations for a query |

### Snapshots
| Method | Path | Description |
|---|---|---|
| `POST` | `/snapshots` | Create a new snapshot |
| `GET` | `/snapshots` | List all snapshots (reverse chronological) |
| `POST` | `/{sid}/rollback` | Rollback to a snapshot |
| `POST` | `/snapshots/compare` | Compare two snapshots (added/removed/modified files) |
| `DELETE` | `/{sid}` | Delete a snapshot |

### Prompt
| Method | Path | Description |
|---|---|---|
| `GET` | `/prompt` | Get the KB's custom RAG prompt template |
| `PATCH` | `/prompt` | Update the KB's custom RAG prompt template |

---

## Key Design Decisions

1. **Progress Tracking**: DB-persisted status + in-memory store for SSE. Polling every 3s as fallback ensures reliability even when SSE connections fail.
2. **Snapshots**: Store file_id associations + chunk metadata as JSON. Rollback restores associations only — vectors are re-indexed on demand via the Chunks tab.
3. **Evaluation Metrics**: Server-side computation of recall@K, precision@K, and MRR from stored annotations.
4. **Chunk Persistence**: Chunks are written to the `knowledge_chunk` table during `process_file()`, enabling instant preview without reloading documents.
5. **Dual-channel Reliability**: SSE provides real-time updates; polling provides a guaranteed fallback.
6. **Prompt Template Injection**: KB-level custom RAG prompt templates with `{query}`, `{context}`, `{kb_name}` variable substitution, injected via the chat middleware pipeline.

---

## Integration Points

### File Upload Pipeline
```
add_file_to_knowledge_by_id()
  → _update_processing_progress(status='chunking')
  → process_file() → LangChain load → split → embed
  → write chunks to knowledge_chunk table
  → _update_processing_progress(status='completed')
```

### Chat Pipeline (Prompt Injection)
```
apply_source_context_to_messages()  (in utils/middleware.py)
  → Check if source is a knowledge base
  → Look up KB.meta.rag_prompt_template
  → Use custom template if available, otherwise fall back to global rag.template
  → Substitute {query}, {context}, {kb_name} variables
  → Inject into system message
```

---

## Upstream Contributions

| PR | Description |
|---|---|
| [#27222](https://github.com/open-webui/open-webui/pull/27222) | fix: knowledge_fs grep splits on literal backslash-n instead of newline |
| [#27249](https://github.com/open-webui/open-webui/pull/27249) | fix: mutable default argument in generate_function_chat_completion |

---

## Local Setup

Merge the files from this repository into an Open WebUI (v0.10.2) project:

```bash
# backend/  → open-webui/backend/open_webui/
# frontend/ → open-webui/src/lib/ and src/routes/

cd open-webui
npm run build
cd backend
WEBUI_SECRET_KEY="your-key" \
  python -m uvicorn open_webui.main:app --host 127.0.0.1 --port 8080
```




## License

This project follows the same license as Open WebUI.
