"""Shared "call the judge/extraction LLM directly" helpers.

Extracted verbatim from ``routers/knowledge.py``. They live here rather than in
that router because non-router modules need them -- the Phase 12 knowledge-graph
extractor in ``retrieval/knowledge_graph.py`` in particular. That module sits
under ``retrieval/``, which ``routers/knowledge.py`` already imports, so
importing the router back would be circular.

``routers/knowledge.py`` keeps its original private names via aliased imports, so
every existing call site is untouched.
"""

from __future__ import annotations

import json
import logging
import os
import re

import httpx
from fastapi import HTTPException, status
from open_webui.config import (
    OPENAI_API_BASE_URL,
    OPENAI_API_KEY,
    RAG_OPENAI_API_BASE_URL,
    RAG_OPENAI_API_KEY,
)

log = logging.getLogger(__name__)


def resolve_llm_api() -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)。优先 .env 直读（对齐 exec_workflow），回退全局配置，默认 DeepSeek。"""
    # config.* 常量在模块导入时取值，可能早于 env.py 加载 .env 而为空；
    # 这里与 exec_workflow 一致：显式 load_dotenv(override=True) 后直读 os.getenv。
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ImportError:
        pass
    base = (
        os.getenv('RAG_OPENAI_API_BASE_URL')
        or os.getenv('OPENAI_API_BASE_URL')
        or RAG_OPENAI_API_BASE_URL
        or OPENAI_API_BASE_URL
        or 'https://api.deepseek.com/v1'
    ).rstrip('/')
    key = os.getenv('RAG_OPENAI_API_KEY') or os.getenv('OPENAI_API_KEY') or RAG_OPENAI_API_KEY or OPENAI_API_KEY or ''
    model = os.getenv('EVAL_MODEL', 'deepseek-chat')
    return base, key, model


def extract_json(text: str) -> dict:
    """从 LLM 输出稳健抽取 JSON（容忍 markdown 围栏 / 前后缀）。"""
    text = (text or '').strip()
    fence = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find('{'), text.rfind('}')
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f'no JSON object in judge output: {text[:200]}')
    return json.loads(text[start : end + 1])


async def chat_completion(
    messages: list[dict],
    temperature: float = 0.0,
    model: str | None = None,
    timeout: float = 120,
) -> str:
    """调 DeepSeek（OpenAI 兼容协议）拿一段文本输出。

    ``model`` / ``timeout`` default to the previous hard-coded behaviour, so
    callers that predate Phase 12 are unaffected.
    """
    base, key, default_model = resolve_llm_api()
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='No API key configured for faithfulness evaluation (set OPENAI_API_KEY or RAG_OPENAI_API_KEY)',
        )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            base + '/chat/completions',
            headers={'Authorization': f'Bearer {key}'},
            json={
                'model': model or default_model,
                'messages': messages,
                'temperature': temperature,
                'stream': False,
            },
        )
        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f'LLM call failed: HTTP {resp.status_code}'
            )
        data = resp.json()
        return (data.get('choices', [{}])[0].get('message', {}).get('content') or '').strip()
