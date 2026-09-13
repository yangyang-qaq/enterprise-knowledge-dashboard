# Phase 12 工作记录 —— 知识图谱增强 RAG（多跳扩展召回）

> 2026-09-12 | 记录 Phase 12 从 S0 到 S8 的**实际执行过程**：做了什么、实测到什么、什么没做。
> 设计规格见 [开发文档.md](开发文档.md) §12，踩坑过程见 [问题记录.md](问题记录.md) #36–41。

---

## 一、这个 Phase 在做什么

当前检索是「向量 + BM25 → RRF → rerank」的**单轮**召回，每个 chunk 独立被检索。当答案需要**跨文档**拼出来时（问 A 文档里的实体，答案在 B 文档），单轮向量检索召回不全——两份文档在语义空间里可能离得很远，但通过某个共同实体是强相关的。

Phase 12 做的事：**用 LLM 从 chunk 里抽「实体—关系—实体」三元组建图；检索时先用现有混合检索拿到种子 chunk，从种子 chunk 的实体在图上多跳游走，把关联 chunk（常常来自完全不同的文档）一起召回，过同一个 reranker 后与基础结果融合。**

三条已拍板的边界：

1. 定位是第 12 个 Phase，规模控制在几十个文档
2. 抽取 = LLM 抽三元组，**离线批处理**
3. 检索用法 = **多跳扩展召回**（local search）。**不做**社区检测 / 全局摘要 / GraphRAG global search / 图上 LLM 推理——理由写进了 `enterprise-kb/DESIGN.md` 的 Non-Goals

---

## 二、交付与状态

| 步 | 内容 | 状态 | 验证方式 |
|---|---|---|---|
| ★ S0 | 5 张表 + Model + 迁移 + config key | ✅ | SQLite 上 `upgrade`/`downgrade` 通过 |
| ★ S1 | LLM 客户端下沉 `utils/llm_client.py` | ✅ | faithfulness 评测分数与改动前一致 |
| ★ S2 | 抽取器 + CLI（`retrieval/knowledge_graph.py`） | ✅ | 演示 KB 抽完 162 chunk、807 实体、534 边、0 失败 |
| ★ S3 | 图查询 + 遍历 + 融合（`retrieval/graph_retrieval.py`） | ✅ | 39 个纯函数单测（手搓 in-memory 结构，不碰 DB） |
| ★ S4 | `query_collection` 注入 + 建图 API + 挂载 | ✅ | `/graph/build` → completed；`/graph/search` 见 `from:'graph'` |
| ★ S5 | 检索链路打通（混检 + 重排 + 图谱开关） | ✅ | API 层已验证；浏览器端见 §八 |
| ★ S6 | 前端（Tab + GraphPanel + 3 组件 + 7 API） | ✅ | 编译通过、路由 200；交互端见 §八 |
| ★ S7 | 评测 `retrieval_eval.py` | ✅ | 16 题 A/B 出表；**写回一个真 bug 并修掉**（见 §六） |
| ★ S8 | 文档与镜像同步 | ✅ | `scripts/sync_backend.sh` 39 文件 0 漂移 |

**合计 33 个真实改动文件**（18 改 + 15 新增，不含生成出来的镜像副本）。

---

## 三、文件清单

### A. `open-webui/` 源码树 —— 改动 9 个

| 文件 | 改了什么 |
|---|---|
| `backend/open_webui/config.py` | 7 个图谱 config key + `DEFAULT_CONFIG` 注册 |
| `backend/open_webui/main.py` | 挂载 `knowledge_graph.router`（必须在 `knowledge.router` **之前**） |
| `backend/open_webui/models/knowledge.py` | 5 个表模型 + Form/Response model |
| `backend/open_webui/retrieval/utils.py` | `query_collection` 加 `knowledge_id=`；`get_sources_from_items` 透传 |
| `backend/open_webui/routers/knowledge.py` | `evaluate/query` 加 `use_graph`；3 个 LLM helper 改为从 `llm_client` import |
| `backend/open_webui/routers/retrieval.py` | 检索路径透传 `knowledge_id` |
| `src/lib/apis/knowledge/index.ts` | 追加 7 个图谱 API 函数 |
| `src/lib/components/workspace/Knowledge/EvaluatePanel.svelte` | `图谱:跟随配置/关/开` 下拉 + 结果上「图谱召回」徽章 |
| `src/routes/(app)/workspace/knowledge/[id]/+layout.svelte` | 第 8 个 Tab `🕸️ 图谱` |

### B. `open-webui/` 源码树 —— 新增 11 个

| 文件 | 作用 |
|---|---|
| `backend/open_webui/migrations/versions/d2e3f4a5b6c7_add_knowledge_graph_tables.py` | 建 5 张表，幂等守卫，`downgrade` 可跑 |
| `backend/open_webui/retrieval/knowledge_graph.py` | 抽取器（prompt + 后验校验 + 并发 + 增量对账），46KB |
| `backend/open_webui/retrieval/graph_retrieval.py` | 图查询 + 多跳遍历 + 融合，26KB |
| `backend/open_webui/routers/knowledge_graph.py` | 9 个 API，27KB |
| `backend/open_webui/utils/llm_client.py` | 从 `routers/knowledge.py` 下沉的 LLM 调用（本 Phase 唯一一处重构既有代码） |
| `backend/tests/test_graph_retrieval.py` | 39 个用例 |
| `backend/tests/conftest.py` | pytest 路径配置 |
| `…/Knowledge/graph/GraphPanel.svelte` | 主面板：统计卡 + 画布 + 抽屉 + 对比检索 |
| `…/Knowledge/graph/GraphCanvas.svelte` | Svelte Flow 封装 |
| `…/Knowledge/graph/EntityNode.svelte` | 实体节点（保留 `<Handle>`，否则边不画） |
| `src/routes/(app)/workspace/knowledge/[id]/graph/+page.svelte` | 路由页 |

### C. 评测（`enterprise-kb/eval/`）

新增 `retrieval_eval.py`（免 LLM 的文件级召回 A/B + 回归门）、`golden_set_demo_kb.json`（16 题手写 ground truth）；改动 `config.py`（`GRAPH_EVAL_K` / `REGRESSION_TOLERANCE`）、`retrieve.py`、`README.md`。

### D. 仓库基建

新增 `enterprise-kb/.github/workflows/rag-eval-graph.yml`（self-hosted runner）、`scripts/sync_backend.sh`（镜像同步，四种模式）。

### E. 文档

改动 `开发文档.md`（+§12.1–12.10）、`项目总结.md`、`问题记录.md`（+问题 36–41）、`read.md`、`enterprise-kb/README.md`、`enterprise-kb/DESIGN.md`。

> ⚠️ `enterprise-kb/` 是本仓库的**扁平镜像**（独立 git 仓库）。上面 A/B/C 的文件在那边都有一份对应副本，改完必须跑 `bash scripts/sync_backend.sh apply` 才会同步。映射关系在脚本的 MAP 表里。

---

## 四、四个关键设计决策

### 决策 1：抽取源是 ChromaDB，不是 `knowledge_chunk` 表

`knowledge_chunk` 只有用户点开「分块」Tab 才写入，用户直接上传文件的话这张表**一行都没有**，但 Chroma 里已经有数据了——而检索命中的是 Chroma。所以抽取源走 `ASYNC_VECTOR_DB_CLIENT.get(collection_name=kb_id)` 全量对账。

**收益**：`save_docs_to_vector_db` 和 `knowledge_chunk` 两条写入路径**都不需要挂抽取钩子**，增量靠「当前 Chroma 的 hash 集合 − 已成功抽取的 hash 集合」算出来。

### 决策 2：图的 join key = `_content_hash(chunk文本)`

`retrieval/utils.py` 已有的 `_content_hash(text) = sha256(text)`，其结果以 `metadata['_chunk_hash']` 形式**一定会出现在最终检索结果里**。图的 `chunk_hash` 列直接用同一个函数算 → **图的 join key 和检索时的 key 天然一致**，不动任何写入路径。

已知不一致风险：检索侧是 `sha256(sanitize_text_for_db(text))`，SQL 侧是 `sha256(raw)`。含 `\x00` 的文档两侧会分叉 → 计数上报 `stats.graph_join_miss`，不静默丢。

### 决策 3：只在 rerank 分数空间里做融合（score-space guard）

已知坑：Chroma 的 `distances` 是**余弦距离（小=好）**，rerank 后 `metadata['score']` 是 **CrossEncoder 分数（大=好）**，而 `merge_and_sort_query_results` 统一按降序排——对纯向量路径是反的。

**解法**：图扩展的唯一入口守卫是「必须开了混合检索 **且** 配了 reranker」，图 chunk 一律**过同一个 reranker** 落回同一分数空间。这样图谱永远进不了那个被搞反的纯向量空间。

**代价**：功能依赖混检 + 重排。`ENABLE_RAG_HYBRID_SEARCH` 代码默认 `false`，所以 `GET /{id}/graph/stats` **必须**返回 `retrieval_ready` + 原因（前端显示提示条）——不做的话演示当天会静默什么都不发生。

### 决策 4：融合模式默认 `append`，不是 `rerank` ← **这是被实测逼出来的**

原设计是 `rerank`（图谱块与 base 块同空间公平竞争），实测证明它**让结果变差**，详见 §六。改成 `append` 后 base 顺序原封不动、图谱块追加在后。

---

## 五、实测数据

演示 KB：`bfa8f7fa-3b8e-4181-a55d-addedd49bfc3`（4 份文档），建图完成：**162 chunk / 807 实体 / 534 边 / 0 失败**。

评测：16 题（8 跨文档 + 8 单文档，手写 ground truth + 机械校验 16/16 通过），**K=3**（4 文档语料上 k≥5 文件级指标就饱和了，必须用小 k，否则测的是指标饱和不是图谱）。

### 当前默认配置（`append` 融合）

报告：`eval/reports/retrieval-graph-20260912-191038.{json,md}`

| 档位 | base recall@3 | graph recall@3 | Δ | graph_added |
|---|---|---|---|---|
| 全量（16 题） | 0.7292 | 0.7292 | **+0.0000** | 2.00 |
| 跨文档（8 题） | 0.7083 | 0.7083 | **+0.0000** | 2.00 |
| 单文档（8 题） | 0.7500 | 0.7500 | **+0.0000** | 2.00 |

回归门 `PASS`。逐题看，**每一题的 recall 数值两臂完全相同**——这正是 `append` 的干净签名：base 顺序没被碰过，图谱块全部落在 top-k 之外。

### 修复前的默认配置（`rerank` + 0.05 bonus）

报告：`eval/reports/retrieval-graph-20260912-114116.{json,md}`（保留作「before」证据）

| 档位 | Δ | graph_added |
|---|---|---|
| 全量 | **−0.0730** | 2.00 |
| 跨文档 | **−0.1458** | 2.00 |
| 单文档 | +0.0000 | 2.00 |

### 扫参数时试过的其它配置

| 配置 | 全量 Δ | 跨文档 Δ | 单文档 Δ |
|---|---|---|---|
| `GRAPH_HOPS=2` | −0.0104 | −0.1458 | **+0.1250** |
| `GRAPH_HUB_DEGREE_CAP=5` | +0.0625 | 0.0000 | +0.1250 |
| hops=2 + hub_cap=5 | +0.0312 | −0.0625 | +0.1250 |
| **`GRAPH_MERGE_MODE=append`** | **0.0000** | **0.0000** | **0.0000** |

`graph_added` 在**每一组、每一题**都是 2——图一直在召回，问题从来不在「召不回来」。

---

## 六、负结果（这部分比正结果重要）

### 6.1 图谱在 rerank 模式下会挤掉 base，让结果变差

单题实测（xd02，gold = {read.md, 项目总结.md}）：

```
base : rank1 项目总结 0.9978 | rank2 项目总结 0.9956 | rank3 read 0.9886   → recall 1.0
graph: rank1 开发文档 1.0473* | rank2 项目总结 1.0146* | rank3 项目总结 0.9978
       rank4 项目总结 0.9956(beyond_k) | rank5 read 0.9886(beyond_k)       → recall 0.5
       * = 图谱块，>1.0 是因为 rerank 分 + 0.05
```

**根因**：`graph_bonus = 0.05` 是按 logit 尺度标定的（原意是「只在分数接近时起 tiebreak」），但 `bge-reranker-base` 走 `CrossEncoder.predict` 会**默认套 Sigmoid**，top 结果全挤在 0.99+。0.05 在那个尺度上不是 tiebreak，是**强行提拔**。把 bonus 去掉，两块会是 0.9973 / 0.9646，第二块根本挤不掉 read.md。

**为什么之前没发现**：早期用 `POST /graph/search` 扫出来的「小 k 有 12/30 胜」量的是 `distinct_files` 在**整个返回列表**（`k + graph_added` 条）上的变化——图谱块算**新增**。而 `retrieval_eval.py` 量的是 **top-K 前缀**。两个都对，含义不同：

- 图谱能**扩大召回池**（beyond_k）✅
- 图谱**不能改进 top-K 排序**，默认下还会变差 ❌

聊天链路用的是固定 k，`beyond_k` 会被丢掉 —— 这就是「池子变大但用户看不到」的原因。

**修法**：融合默认从 `rerank` 改成 `append`。负收益归零（Δ 从 −0.1458 → +0.0000），图谱贡献保留（`graph_added` 仍 2.00）。

> ⚠️ 一句注释曾经骗过我：`graph_retrieval.py` 里写着「merge at k + len(graph_items) 保证 every base hit survives」。它**只保证 base 项还在合并列表里，不保证还在 top-K 里**——`rerank` 是把图谱块**排进** base 序列，消费者照样丢。这是「注释说修了、实测没修」的典型，已在代码和测试里更正。

### 6.2 评测工具本身是错的：ground truth 循环论证

文件级 A/B 第一次跑就暴露自己的 ground truth 是**循环论证**——「跨文档」档用检索输出定义 gold_files，导致 40/40 条全部入选，该档位毫无区分度（看起来还很漂亮）。

**修法**：改成手写 ground truth + 机械校验（`--verify-golden-set`：每条 evidence 必须是声明文件里的原文子串），16/16 通过才继续跑。**这类错误不会报错，只会让数字好看。**

### 6.3 诚实的结论

**图谱把召回池扩大了 2 个 chunk/query，但不改变 top-K 排序。没有任何参数配置能让跨文档 Δ 转正。** 这是一个中性偏保守的结论，不该包装成「提升」。

---

## 七、收尾阶段做了什么（S7 / S8）

### S7 收尾

- 改 `merge_mode` 默认值后，5 个单测挂了（它们隐式依赖 rerank 是默认值）→ 显式传 `mode='rerank'` 修复
- 新增 `test_default_mode_is_append_not_rerank` 守住新默认值
- 改写 `TestAdditiveMerge` 的 docstring（原文断言了一个已被实测推翻的「修法」）
- 测试结果：**83 passed**（分块策略 44 + 图谱检索 39）

### S8 收尾

- **建 `scripts/sync_backend.sh`**：显式 MAP 表 + 四种模式（`check` / `apply` / `list` / `share`），当前 39 文件 0 漂移
  - 坑 1：diff 必须**忽略行尾**。`EvaluatePanel.svelte` 原始 diff 811 行，按行尾归一后只剩 23 行真差异——有 5 个文件是纯 CRLF 差异
  - 坑 2：`share` 模式最初把未跟踪的新文件误报成 0% 二开（`git diff` 对未跟踪文件返回空），改用 `git ls-files --error-unmatch` 判断
- **发现「文档镜像」这一半从来没做过**：`enterprise-kb/` 里的 `开发文档.md` / `问题记录.md` 等 4 份落后了 10 个问题和好几个 Phase（41 vs 31 个问题）。跨仓库所以完全看不出来。已补进 MAP 一起同步
- **全量核对文档数字**，逐项回仓库验证：12 张表（5+2+5）、39 个端点（图谱 9 个）、8 Tab + 13 组件、41 个问题、83 个测试
- 修掉一处我自己写错的文档结论：`eval/README.md` 曾说「回归门按默认配置必然红」，`append` 修复后它是**绿**的

---

## 八、前端验收步骤

> ⚠️ **必须在 http://127.0.0.1:5173 上验收，不要在 8080。**
> `open-webui/build/` 里是 **8 月 14 日**的旧构建产物，没有图谱 Tab；8080 服务的是它。
> 5173 是 Vite dev server（直接读源码），dev 模式下前端会把 API 请求发到
> `http://<当前主机名>:8080/api/v1`，CORS 是 `*`，两个服务现在都在跑。
> 想在 8080 上看，得先 `npm run build`。

**目标知识库**：`图谱演示知识库技术文档`，id `bfa8f7fa-3b8e-4181-a55d-addedd49bfc3`
（已建好图：162 chunk / 807 实体 / 534 边）

### 验收 1：图谱 Tab 与画布（对应验收标准 #6 的静态部分）

1. 打开 http://127.0.0.1:5173 → 进 `工作空间 → 知识库 → 图谱演示知识库技术文档`
2. 点最右边的第 8 个 Tab **🕸️ 图谱**
3. **期望**：顶部 4 张统计卡有数（实体 807 / 边 534 / 跨文档边 / 覆盖率），画布上出现节点和边
4. **拖拽**几个节点，连线跟着动 → `nodeTypes` 引用稳定、子组件没被重挂载
5. **期望顶部出现黄色提示条**「⚠️ 聊天检索不会启用图谱扩展：…」，因为
   `rag.enable_graph_retrieval` 当前是 **false**。**这条提示是设计要求的**，不是 bug —— 它就是「图扩展静默不生效」这个最危险演示风险的防线
6. 点画布上一个**度数较高**的节点 → 右侧 380px 抽屉展开：类型 / 别名 / 度数 + **关联 chunk 列表** + 邻居关系；点邻居能聚焦跳过去

### 验收 2：对比检索（对应验收标准 #3）—— **这是演示主力，不需要开全局开关**

1. 拉到底部「对比检索」面板
2. 输入一个**跨文档**问题，例如：`知识图谱这个功能是在哪个 Phase 做的，配套的评测脚本叫什么？`
3. 点 **对比检索**
4. **期望**：左右两列 `基础混合检索` vs `+ 图谱扩展`
   - graph 列多出带绿色 **「图谱召回」** 徽章的条目
   - 底部显示 `图谱新增文件：N 个`，**N > 0**
   - base 列的结果在 graph 列里**顺序不变**（`append` 融合的直接体现）
5. 这个面板内部**强制** `use_graph=True`（绕过全局开关），所以无论第 5 步的提示条在不在，它都能出效果——它只用于对照，不影响聊天

### 验收 3：评估 Tab 的 A/B 开关（对应验收标准 #6 的开关部分）

1. 进 `评估` Tab
2. 右上角下拉 `图谱:跟随配置` / `图谱:关` / `图谱:开`
3. 同一个查询分别用 `关` 和 `开` 跑一次
4. **期望**：`开` 的结果里出现带 `图谱召回` 徽章的条目（鼠标悬停显示「N 跳、命中 M 个实体」），两次结果不同

### 验收 4：聊天跨文档召回（对应验收标准 #4）—— **需要先打开全局开关**

前三步都不需要动配置，这一步需要，因为聊天链路读的是全局开关，而它默认是 `false`。

**关键坑**：`ENABLE_GRAPH_RETRIEVAL=true` 写进 `.env` **没用**。启动时 `Config.seed_defaults()`
只插入**缺失**的 key，**已存在的 DB 行优先**——`rag.enable_graph_retrieval` 已经以 `false` 落库了。
而且这组 `rag.*` key 目前**没有管理后台 UI**。

**正确做法**（以管理员身份，`POST /api/v1/configs/import` 会 upsert 这一行）：

```bash
curl -X POST http://127.0.0.1:8080/api/v1/configs/import \
  -H "Authorization: Bearer <管理员 token>" \
  -H "Content-Type: application/json" \
  -d '{"config": {"rag.enable_graph_retrieval": true}}'
```

`Config.get_many` 每次调用都读 DB、不缓存，**改完立刻生效，不用重启**。

然后：

1. 回到图谱 Tab，黄色提示条消失、`检索就绪` 变成 **就绪**
2. 新建/打开一个聊天，**在模型选择处挂上这个知识库**
3. 问一个**答案要跨两份文档**的问题（比如一个概念的定义在一份文档、它的实现细节在另一份）
4. **期望**：回答下方的 `sources` 引用里出现**另一份文档**的来源，不是只有一份

> 若想还原：把上一条命令里的 `true` 改成 `false` 再发一次即可。

### 验收 5：重建图谱进度条（对应验收标准 #6 的进度部分）

1. 图谱 Tab 右上角 **🔄 重建图谱**（旁边还有「强制全量」）
2. **期望**：进度条走起来、数字在涨；因为当前 162 个 chunk 都已抽取过，**增量模式应当直接跳过全部**
   （`待抽取 0`），很快结束——这本身就是「增量对账生效」的验证
3. 点「取消」应能中断

---

## 九、验收标准逐条对照

| # | 标准 | 状态 |
|---|---|---|
| 1 | `coverage_pct` > 60%、关系类型不全是「其他」 | ✅ |
| 2 | 人工抽查 10 条三元组，精确率 ≥ 70% | ❌ **未做**（需要人看，我做不了） |
| 3 | `/graph/search` 的 `distinct_files_graph > distinct_files_base` | ✅ |
| 4 | 聊天里 `sources` 出现另一份文档 | ⚠️ API 层验证过，浏览器端待你按 §八 验收 4 走 |
| 5 | `retrieval_eval.py --compare` 跨文档 recall@K 提升 > 0 | ⚠️ **未达成**：实测 Δ = +0.0000（打平，不是 +）。见 §6.3 |
| 6 | 前端能拖拽、点节点出 chunk、重建进度条正常 | ⚠️ 编译/路由已验证，交互待你在浏览器里确认 |
| 7 | 迁移在 SQLite 与 PG 上各跑通 | ⚠️ SQLite 通过；**PG 未验证** |

---

## 十、未决事项（需要拍板，我没有自行改默认值）

1. **`GRAPH_HUB_DEGREE_CAP` 默认 20 → 5**：有实测支撑（跨文档 Δ 从 −0.1458 拉回 0），但 n=16、单 KB，且 `hub_cap=5` 的语义是「被 >5 个 chunk 提到的实体就停止扩展」，大库上可能过严。**建议先加语料复测再改。**
2. **`graph_bonus = 0.05` 不该是常数**：它打在 sigmoid 分数上不是 tiebreak 而是提拔。要么调小，要么改成「只在分数接近时生效」的真 tiebreak。
3. **图谱在 `rerank` 模式下仍会挤占 base**：目前是靠「默认 `append`」规避的，`rerank` 模式本身没有修。要不要一起修，取决于还想不想保留公平竞争这个模式。
4. **`merge_and_sort_query_results` 的降序 bug**：纯向量路径下 `distances` 是余弦距离（小=好）却被按降序排。本期**只记录未修**（改了影响所有现网路径，无法回归验证），记在 `问题记录.md` #41。修掉可提上游 PR。
5. **镜像里 3 个低二开占比文件**：`config.py`(1%)、`routers/retrieval.py`(1%)、`retrieval/utils.py`(7%) 贡献了 8k 行上游代码（镜像 +33%）。已按既有惯例保留（`middleware.py` 5%、`files.py` 3% 本来就在里面，且 `retrieval/utils.py` 正是图谱接入点），要撤是改 MAP 表一行的事。

---

## 附：一句话总结

**这个 Phase 做出了一个能跑、能演示、能被测的知识图谱召回，然后诚实地测出它「扩大了召回池但不改进排序」——并且把第一版默认配置的负收益（−0.1458）定位到根因（bonus 打在 sigmoid 分数上）、修掉、留了回归门防复发。**
