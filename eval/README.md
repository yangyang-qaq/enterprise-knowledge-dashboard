# RAG 离线评测（Faithfulness + 检索召回 A/B）

两件**互相独立**的评测，共用一套配置与黄金集目录：

1. **Faithfulness（忠实度）** —— 生成答案里的每条主张是否真的被检索片段支持。接 GitHub Actions 后每个 PR 自动跑，低于门槛即失败。
2. **检索召回 A/B（Phase 12）** —— 开图 vs 不开图，top-K 里命中了哪些**文档**。不调 LLM，秒级到分钟级。见下方独立章节。

## 目录

```
eval/
├── golden_set.json           # Faithfulness 黄金集（当前 40 条，来自 4 个真实技术文档，待人工核实后扩到 50~200）
├── golden_set.schema.json    # JSON Schema 校验
├── golden_set_demo_kb.json   # 召回 A/B 黄金集（16 题 = 8 跨文档 + 8 单文档，手写 gold_files）
├── rag_template.txt          # 固化的 RAG 模板（含引用约束，需与 config.py 同步）
├── config.py                 # 集中配置（env 可覆盖）
├── generate.py               # 渲染 RAG 模板 + DeepSeek 生成答案
├── faithfulness.py           # LLM-as-judge：拆主张 + 判定 + 打分
├── retrieve.py               # full 档：HTTP 调真实混合检索+重排
├── run_eval.py               # Faithfulness 主入口（--mode judge-only|full，门槛退出码）
├── retrieval_eval.py         # 召回 A/B 主入口（--verify-golden-set / --compare 回归门）
├── bootstrap_golden.py       # 从问答对生成 golden_set 骨架
├── gen_questions.py          # 从文档自动生成问答草稿（人工核实后入库）
├── reports/                  # 两类报告都写这里：report-*.md（忠实度）、retrieval-graph-*.md（召回 A/B）
└── requirements.txt
```

## 两档模式

| 档 | 片段来源 | 依赖 | 触发 |
|---|---|---|---|
| `judge-only`（轻量） | `golden_set.json` 里预存的 `context_chunks` | 仅 DeepSeek key | 每个 PR |
| `full`（完整） | 真实「混合检索+重排」（HTTP 调 `/evaluate/query`） | 后端 + 模型 + 登录账号 | nightly / 手动 |

两档共用 `generate.py`（生成）+ `faithfulness.py`（判定），差异只在片段来源。

## Faithfulness 算法

1. 用 RAG 模板（含「严格基于上下文回答，不足则拒答」约束）生成答案（`temperature=0`）。
2. LLM-as-judge 把答案拆成原子主张，逐条判 `supported ∈ {yes,no,insufficient}`。
3. `faithfulness = #yes / #claims`；拒答（空主张）视为 1.0。

## 本地运行

```bash
# 安装依赖（建议复用 backend venv，或新建轻量 venv）
pip install -r requirements.txt

# 1) judge-only 档（需 DEEPSEEK_API_KEY）
export DEEPSEEK_API_KEY=sk-...
python run_eval.py --mode judge-only --limit 5

# 2) full 档（需后端在 127.0.0.1:8080 跑 + 登录账号 + KB id）
export EVAL_USER_EMAIL=eval@example.com
export EVAL_USER_PASSWORD=xxx
export EVAL_KB_ID=<your-kb-id>
python run_eval.py --mode full --limit 5 --save-context   # --save-context 回填 context_chunks
```

报告输出到 `eval/reports/report-<时间戳>.json` / `.md`。

## 黄金集整理流程

1. 拿到原始文档内容：可从知识库导出（见 `gen_questions.py` 文件头示例）或直接写 `{文件名: 全文}` 的 JSON。
2. 生成草稿（二选一）：
   - 自动：`python gen_questions.py --docs docs.json --kb-id <ID> --per-doc 10`（从文档提取问答对）；
   - 手动：`python bootstrap_golden.py --input my_qa.json --kb-id <ID>`（从已有问答对生成骨架）。
3. 逐条人工核实 `golden_answer` 正确、且在 KB 内**可答**（否则忠实度无意义）。
4. 跑 `run_eval.py --mode full --save-context` 一次性回填真实检索片段（`context_chunks`）。
5. 提交 `golden_set.json`。之后 PR 走 judge-only 档即用这些预存片段。

> 当前 `golden_set.json` 的 40 条来自知识库「新的test1」的 4 个真实技术文档（AI 医疗 / 微服务 / Python 后端 / LLM 综述），由 DeepSeek 依据原文生成、`source_file` 已标注出处。**上线前务必逐条人工核实 `golden_answer` 与原文一致**，再据此扩到 50~200 条。

## 模板同步（重要）

`rag_template.txt` 是 `open-webui/backend/open_webui/config.py` 里 `DEFAULT_RAG_TEMPLATE` 的固话副本。若后者改动，须同步更新这里，否则 judge-only 档测的不是生产行为。生产环境可用 `RAG_TEMPLATE` env 覆盖（`config.py:load_rag_template` 优先读 env）。

## CI 门禁

- `rag-eval.yml`：PR / push master → judge-only 档，低于 `EVAL_THRESHOLD`（默认 0.8）`exit 1` 使 build 失败。
- `rag-eval-full.yml`：`schedule`（每天 03:17 UTC）+ `workflow_dispatch` → full 档（自托管 runner，需后端）。

**仓库需配置的 Secrets**（Settings → Secrets and variables → Actions）：

| Name | 档位 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 两档 | DeepSeek 密钥（生成 + judge 共用） |
| `EVAL_USER_EMAIL` | full / graph | 评测专用登录账号邮箱 |
| `EVAL_USER_PASSWORD` | full / graph | 该账号密码 |
| `EVAL_TOKEN` | graph | 已登录的 JWT，**可选但推荐**：有它就不必再 signin，绕开 signin 限流（15 次/180s/email） |

**仓库需配置的 Variables**（可选，均有默认值）：

| Name | 默认 | 说明 |
|---|---|---|
| `EVAL_THRESHOLD` | `0.8` | 聚合 Faithfulness 门槛 |
| `EVAL_KB_ID` | — | full 档 / graph 档目标知识库 id |
| `EVAL_GRAPH_K` | `3` | 图谱 A/B 的 K（**刻意不是 10**，理由见下） |

> 建议单独建一个只读的 `eval` 账号跑 full 档，避免频繁 signin 触发限流（15 次/180s/email）。

## 检索召回 A/B（`retrieval_eval.py`，Phase 12）

与上面的 Faithfulness 评测是**两件独立的事**：这里不生成、不判分、不调 LLM，只量
「开图 vs 不开图，top-K 里命中了哪些文档」。ground truth 是 `gold_files`（哪些文档能
回答这个问题），秒级到分钟级。

```bash
export EVAL_TOKEN=<已登录的 JWT>          # 或 EVAL_USER_EMAIL/PASSWORD
python retrieval_eval.py --verify-golden-set          # 先验 ground truth 是否属实
python retrieval_eval.py --mode compare --compare     # A/B + 回归门
python retrieval_eval.py --mode compare --set-config GRAPH_HUB_DEGREE_CAP=5
```

- **`--verify-golden-set` 必须先跑**。`gold_files` 是人工判定的，判错了整个 A/B 就变成
  「跑出来数字很好看但没测到东西」。它逐条检查 `evidence` 里的子串是否真的出现在声明的
  文件里，16 条全过才继续。
- **默认 K=3，不是 `EVAL_RETRIEVAL_K=10`**。4~6 文档的语料上 base 在 k≥5 就覆盖全集，
  文件级 recall 已饱和，Δ 必然是 0 —— 那是指标饱和，不是图谱没生效。细节见脚本 docstring。
- **`graph_added` 必须一起看**。它在大 k 小 k 都不为 0，是「图确实在工作」的直接证据；
  文件级 Δ 才是「图有没有用」。
- `--compare` 只看跨文档子集，低于 `-EVAL_REGRESSION_TOLERANCE`（默认 0.02）退出码 1。

  **这条门禁断言的是「图没有把召回搞差」，不是「图有提升」——别把这两件事混起来。**
  在 4 文档 demo KB 上跨文档 Δ 实测为 **+0.0000**：`append` 融合让 base 顺序原封不动，
  graph chunk 只追加在后，所以文件级 top-K 指标一条不变，而 `graph_added` 仍是 2.00。
  于是门禁是绿的，而且**恰好绿在 0 上**——这不是「图有用」的证据，是「图没有害」的证据。

  一旦有人把 `rag.graph_merge_mode` 改回 `rerank` 或调大 `rag.graph_bonus`，Δ 会转负
  （实测默认 rerank + 0.05 时跨文档 Δ = **−0.1458**），门禁立刻红。**这正是它存在的意义**：
  它守的是回归，不是收益。工作流见 `../.github/workflows/rag-eval-graph.yml`。

`golden_set_demo_kb.json` 是本仓库 4 份文档那个演示 KB 的 16 题（8 跨文档 + 8 单文档）。
`tier` 字段决定分层报告：全量会被单文档题稀释，跨文档子集才是图谱机制该发力的地方。
