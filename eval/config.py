"""eval 集中配置：DeepSeek key/URL、模型、门槛、后端地址等，全部可用环境变量覆盖。

与 loadtest/config.py 同一风格——所有值都有 env 覆盖，CI 里通过 secrets/env 注入。
"""

import os

# ── DeepSeek（OpenAI 兼容协议）──
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("OPENAI_API_KEY", ""))
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
# 生成 + judge 共用的模型（DeepSeek 标准 chat 模型）
EVAL_MODEL = os.getenv("EVAL_MODEL", "deepseek-chat")

# ── 门槛：聚合 faithfulness 低于此值 → run_eval.py 退出码非 0 ──
THRESHOLD = float(os.getenv("EVAL_THRESHOLD", "0.8"))

# ── full 档：后端地址 + 检索 top-k ──
RETRIEVAL_K = int(os.getenv("EVAL_RETRIEVAL_K", "10"))
BACKEND_URL = os.getenv("OWUI_BASE_URL", "http://127.0.0.1:8080")
API = "/api/v1"

# full 档登录（单独配一个 eval 测试账号，避免 signin 限流）
EVAL_USER_EMAIL = os.getenv("EVAL_USER_EMAIL", "")
EVAL_USER_PASSWORD = os.getenv("EVAL_USER_PASSWORD", "")

# golden_set 条目未单独给 kb_id 时的兜底 KB id
DEFAULT_KB_ID = os.getenv("EVAL_KB_ID", "")

# ── 图谱检索 A/B（Phase 12 S7）──
# 文件级召回评测的默认 K。**刻意不用 RETRIEVAL_K=10**：只有 4~6 个文档的语料上，
# base 臂在 k>=5 就基本覆盖全集，文件级 recall 已到天花板，图再加新文件也涨不动，
# Δ 必然为 0 —— 那是指标饱和，不是图谱没生效。小 K（1~3）才是有区分度的区间。
GRAPH_EVAL_K = int(os.getenv("EVAL_GRAPH_K", "3"))
# 回归门容差：graph 的 recall@K 低于 base 超过这个值才判失败
REGRESSION_TOLERANCE = float(os.getenv("EVAL_REGRESSION_TOLERANCE", "0.02"))
# 灵敏度检查用 `--set-config GRAPH_HOPS=2` 临时改后端配置，跑完自动还原 ——
# 故意不在这里放一个 GRAPH_HOPS 默认值：脚本内部改参数证明不了「评测能感知
# 真实参数变化」，必须打到后端配置上才算数。

# ── 路径 ──
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GOLDEN_SET_PATH = os.path.join(BASE_DIR, "golden_set.json")
RAG_TEMPLATE_PATH = os.path.join(BASE_DIR, "rag_template.txt")
REPORT_DIR = os.path.join(BASE_DIR, "reports")


def load_rag_template() -> str:
    """RAG 模板：优先 env RAG_TEMPLATE，否则读 vendored rag_template.txt。

    注意：rag_template.txt 需与 open-webui/backend/open_webui/config.py 的
    DEFAULT_RAG_TEMPLATE 保持同步（见 README「模板同步」）。
    """
    env_template = os.getenv("RAG_TEMPLATE", "")
    if env_template:
        return env_template
    with open(RAG_TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()
