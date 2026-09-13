"""full 档检索：HTTP 调 /evaluate/query 拿真实「混合检索 + 重排」片段。

复用 loadtest 的 signin 模式（登录一次拿 Bearer token）。
仅 full 档需要后端在跑；judge-only 档不 import 本模块。
"""

import os

import requests

import config


def signin() -> str:
    # EVAL_TOKEN 优先：本地/CI 已经有一个有效 JWT 时就不必再 signin（也绕开
    # signin 限流）。没有配 EMAIL/PASSWORD 又没给 TOKEN 时才真的失败。
    token = os.getenv("EVAL_TOKEN", "")
    if token:
        return token
    url = f"{config.BACKEND_URL}{config.API}/auths/signin"
    r = requests.post(
        url,
        json={"email": config.EVAL_USER_EMAIL, "password": config.EVAL_USER_PASSWORD},
        timeout=30,
    )
    r.raise_for_status()
    token = r.json().get("token", "")
    if not token:
        raise RuntimeError(f"signin 未返回 token: {r.text[:200]}")
    return token


def retrieve_chunks(
    question: str,
    kb_id: str,
    token: str,
    k: int | None = None,
    use_graph: bool | None = None,
) -> list[dict]:
    """调 /evaluate/query（已定制返回完整 k 条），映射成统一 context_chunk 结构。

    ``use_graph`` 为 None 时跟随后端全局配置（rag.enable_graph_retrieval），
    显式 True/False 则强制覆盖——这是图谱 A/B 能成立的前提：两条臂走同一个函数、
    同一个 k、同一个 reranker，唯一的差别就是这个布尔值。

    返回的 chunk 带 ``from_graph`` / ``beyond_k``：图谱扩展会把结果撑到 k 条以上
    （多出来的标 ``beyond_k=True``），文件级 recall@K 必须只看 ``rank <= k`` 的前缀，
    否则开图那一臂会白拿额外名额。``from_graph`` 是「图确实在工作」的直接证据，
    它在大 k 小 k 都不为 0——而文件级指标在小语料上会饱和（见 retrieval_eval.py 的说明）。
    """
    k = k or config.RETRIEVAL_K
    url = f"{config.BACKEND_URL}{config.API}/knowledge/{kb_id}/evaluate/query"
    payload: dict = {"query": question, "k": k}
    if use_graph is not None:
        payload["use_graph"] = use_graph
    r = requests.post(
        url,
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
        timeout=180,
    )
    r.raise_for_status()
    chunks = []
    for item in r.json().get("results", []):
        meta = item.get("metadata") or {}
        chunks.append(
            {
                "text": item.get("text", ""),
                "source": meta.get("name") or meta.get("source") or meta.get("file_id", ""),
                "file_id": meta.get("file_id", ""),
                "score": item.get("score"),
                "rank": item.get("rank"),
                "from_graph": bool(meta.get("graph")),
                "beyond_k": bool(item.get("beyond_k")),
            }
        )
    return chunks
