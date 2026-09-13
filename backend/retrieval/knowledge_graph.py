"""Phase 12: LLM-driven entity/relation extraction that builds a knowledge graph
over a knowledge base's chunks.

Why the graph exists
--------------------
Retrieval is vector + BM25 over independently-embedded chunks. When the answer
spans two documents that never share vocabulary, single-round retrieval misses
one of them. Building an explicit entity graph lets retrieval walk from a seed
chunk to related chunks - frequently in a different file.

Two invariants worth stating up front, because everything else follows:

1. The graph is built from **ChromaDB**, not from ``knowledge_chunk``. That
   table is only written when a user opens the Chunks tab, so a KB that was
   uploaded and never inspected has zero rows there while Chroma is full.

2. ``chunk_hash`` is produced by ``retrieval.utils._content_hash`` - the same
   function the retrieval path uses to populate ``metadata['_chunk_hash']``.
   Always import it; never reimplement the digest, or the join silently breaks.

Nothing here imports ``routers/``, so the pure helpers stay unit-testable and
importable without a running app.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field

from sqlalchemy import bindparam
from sqlalchemy import delete as sa_delete
from sqlalchemy import func as sa_func
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError

from open_webui.internal.db import get_async_db
from open_webui.models.knowledge import (
    KnowledgeGraphChunkEntity,
    KnowledgeGraphEdge,
    KnowledgeGraphEntity,
    KnowledgeGraphExtractionLog,
    KnowledgeGraphExtractionTask,
)

log = logging.getLogger(__name__)


####################
# Closed vocabularies
####################

ENTITY_TYPES = ('人物', '组织', '产品', '技术', '指标', '法规', '地点', '其他')
ENTITY_TYPE_FALLBACK = '其他'

# A closed relation set is what makes `uq_knowledge_graph_edge_triple` mean
# something. With free-text relations, "使用" / "采用了" / "基于" become three
# separate edges for one real fact and the uniqueness constraint stops merging.
RELATION_TYPES = (
    '属于',
    '包含',
    '研发',
    '使用',
    '应用于',
    '治疗',
    '位于',
    '导致',
    '提升',
    '降低',
    '优于',
    '合作',
    '定义',
    '其他',
)
RELATION_FALLBACK = '其他'

# Ordered keyword rules mapping free-text relation words into the closed set.
# First match wins, so put the more specific patterns earlier.
_RELATION_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (('研发', '开发', '提出', '发明', '创建', '发布', '推出', '构建', '设计', '训练'), '研发'),
    (('包含', '包括', '含有', '组成', '涵盖', '分为', '收录'), '包含'),
    (('属于', '隶属', '归入'), '属于'),
    (('使用', '采用', '基于', '利用', '依赖', '运用', '通过'), '使用'),
    (('应用于', '用于', '适用', '服务'), '应用于'),
    (('治疗', '治愈', '缓解', '诊断'), '治疗'),
    (('位于', '坐落', '地处', '总部'), '位于'),
    (('导致', '引起', '造成', '引发'), '导致'),
    (('提升', '提高', '增加', '增强', '改善', '优化', '加速'), '提升'),
    (('降低', '减少', '下降', '削弱', '抑制'), '降低'),
    (('优于', '超过', '高于', '强于', '领先'), '优于'),
    (('合作', '联合', '共同', '协作', '参与', '整合'), '合作'),
    (('定义', '指的是', '称为', '是一种', '即'), '定义'),
)

# Post-NFKC bracket/quote characters stripped from the dedup key. NFKC already
# folds full-width ASCII forms into half-width, so both spellings are listed.
_KEY_STRIP_CHARS = str.maketrans('', '', '()[]{}<>"\'`《》〈〉「」『』【】〔〕（）［］｛｝＜＞“”‘’')

# Generic nouns that pass the substring gate but are noise rather than entities.
# Deliberately short - over-filtering loses real entities, and the substring gate
# already removes most hallucination.
_ENTITY_STOPWORDS = frozenset(
    {
        '它', '他', '她', '它们', '我们', '你们', '他们', '这', '那', '该', '此', '其',
        '本文', '本节', '本表', '上述', '如下', '其中', '因此', '所以',
        '系统', '方法', '问题', '方面', '情况', '内容', '部分', '过程', '结果',
        '信息', '时候', '方式', '项目', '用户', '阶段', '时间', '分析', '研究',
        '工作', '模型', '数据', '功能', '需求', '步骤', '示例', '案例', '场景',
    }
)

MAX_ENTITIES_PER_CHUNK = 12
MAX_RELATIONS_PER_CHUNK = 15
MAX_ALIASES_PER_ENTITY = 5
MIN_ENTITY_CHARS = 2
MAX_ENTITY_CHARS = 20
MAX_CHUNK_CHARS = 4500  # ~1500 tokens; longer chunks are truncated and logged as skipped
MAX_ENTITY_CHUNK_HASHES = 50  # caps the render-only cache; chunk_entity stays authoritative


####################
# Pure helpers
####################


def normalize_name_key(name: str) -> str:
    """NFKC + drop whitespace/brackets/quotes + lowercase.

    This is the graph's dedup key, so "AlphaFold 2" / "alphafold2" / "AlphaFold2"
    collapse to one node without any LLM call.
    """
    text = unicodedata.normalize('NFKC', str(name or ''))
    text = text.translate(_KEY_STRIP_CHARS)
    text = re.sub(r'\s+', '', text)
    return text.strip().lower()


def _looks_like_junk(name: str) -> bool:
    """Reject names that are pure digits/punctuation or sentence fragments."""
    if re.fullmatch(r'[\d\s\.\-_/\\%,，。、；;:：!！?？+=*#@&|~^]+', name):
        return True
    # Sentence punctuation inside a name means the model emitted a clause.
    return bool(re.search(r'[。！？；，、]', name))


def normalize_relation(relation: str) -> str:
    """Map a free-text relation onto the closed set (never fails; falls back)."""
    text = unicodedata.normalize('NFKC', str(relation or '')).strip()
    if text in RELATION_TYPES:
        return text
    for keywords, canonical in _RELATION_RULES:
        if any(kw in text for kw in keywords):
            return canonical
    return RELATION_FALLBACK


def normalize_entity_type(entity_type: str) -> str:
    text = unicodedata.normalize('NFKC', str(entity_type or '')).strip()
    return text if text in ENTITY_TYPES else ENTITY_TYPE_FALLBACK


def _substring_gate(name: str, chunk_text: str, normalized_chunk: str) -> bool:
    """Hard anti-hallucination gate: the surface form must occur in the chunk.

    Checked twice - once verbatim, once against the identically-normalised chunk
    text - so that legitimate spacing/bracket differences still pass while
    invented entities do not.
    """
    if not name:
        return False
    if name in chunk_text:
        return True
    key = normalize_name_key(name)
    return bool(key) and key in normalized_chunk


def validate_and_normalize(raw: dict, chunk_text: str) -> tuple[list[dict], list[dict]]:
    """Turn a raw LLM extraction into validated entities and relations.

    Pure: no DB, no network. Returns ``(entities, relations)`` where entities are
    ``{name, name_key, entity_type, aliases}`` and relations are
    ``{source_key, target_key, relation, relation_key, confidence}`` with both
    endpoints guaranteed to exist in the returned entity list.

    Rules applied, in order: clean the surface form, enforce length and stopword
    limits, apply the substring gate, dedup, then resolve relation endpoints.
    """
    normalized_chunk = normalize_name_key(chunk_text)

    entities: list[dict] = []
    by_key: dict[str, dict] = {}

    raw_entities = raw.get('entities') if isinstance(raw, dict) else None
    for item in raw_entities or []:
        if not isinstance(item, dict):
            continue
        name = unicodedata.normalize('NFKC', str(item.get('name') or '')).strip()
        name = name.strip(' \t\r\n.,;:!?、。，；：！？"\'`()[]{}「」『』《》〈〉')
        if not (MIN_ENTITY_CHARS <= len(name) <= MAX_ENTITY_CHARS):
            continue
        if name in _ENTITY_STOPWORDS or _looks_like_junk(name):
            continue
        if not _substring_gate(name, chunk_text, normalized_chunk):
            continue

        aliases: list[str] = []
        for alias in item.get('aliases') or []:
            alias = unicodedata.normalize('NFKC', str(alias or '')).strip()
            if not alias or alias == name or len(alias) > MAX_ENTITY_CHARS:
                continue
            if alias in _ENTITY_STOPWORDS or _looks_like_junk(alias):
                continue
            # Aliases get the same gate - they are also claims about the source.
            if not _substring_gate(alias, chunk_text, normalized_chunk):
                continue
            if alias not in aliases:
                aliases.append(alias)
            if len(aliases) >= MAX_ALIASES_PER_ENTITY:
                break

        name_key = normalize_name_key(name)
        if not name_key:
            continue
        if name_key in by_key:
            # Same entity twice in one chunk: keep the first, pool the aliases.
            existing = by_key[name_key]
            for alias in aliases:
                if alias not in existing['aliases'] and len(existing['aliases']) < MAX_ALIASES_PER_ENTITY:
                    existing['aliases'].append(alias)
            continue

        entity = {
            'name': name,
            'name_key': name_key,
            'entity_type': normalize_entity_type(item.get('type')),
            'aliases': aliases,
        }
        by_key[name_key] = entity
        entities.append(entity)
        if len(entities) >= MAX_ENTITIES_PER_CHUNK:
            break

    relations: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    raw_relations = raw.get('relations') if isinstance(raw, dict) else None
    for item in raw_relations or []:
        if not isinstance(item, dict):
            continue
        source_key = normalize_name_key(item.get('source'))
        target_key = normalize_name_key(item.get('target'))
        # Both endpoints must survive validation, otherwise the edge would dangle.
        if source_key not in by_key or target_key not in by_key:
            continue
        if source_key == target_key:
            continue

        # Hard gate, same mechanism that keeps entities clean. Entities are
        # grounded by "the surface form occurs in the chunk"; relations are
        # grounded by "the quoted sentence occurs in the chunk". Without this the
        # model happily links any two co-occurring entities, which is by far the
        # dominant failure mode - far worse than a missing relation.
        evidence = unicodedata.normalize('NFKC', str(item.get('evidence') or '')).strip()
        if not evidence or normalize_name_key(evidence) not in normalized_chunk:
            continue

        relation = unicodedata.normalize('NFKC', str(item.get('relation') or '')).strip()
        relation_key = normalize_relation(relation)

        confidence = item.get('confidence')
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            confidence = None
        else:
            confidence = max(0, min(100, int(confidence)))

        triple = (source_key, relation_key, target_key)
        if triple in seen:
            continue
        seen.add(triple)

        relations.append(
            {
                'source_key': source_key,
                'target_key': target_key,
                'source_name': by_key[source_key]['name'],
                'target_name': by_key[target_key]['name'],
                'relation': relation or relation_key,
                'relation_key': relation_key,
                'confidence': confidence,
            }
        )
        if len(relations) >= MAX_RELATIONS_PER_CHUNK:
            break

    return entities, relations


def compute_incremental_work(
    current_hashes: set[str], done_hashes: set[str], force: bool = False
) -> tuple[list[str], list[str]]:
    """Reconcile Chroma against the extraction ledger.

    Returns ``(todo, orphan)``: hashes needing extraction, and hashes whose chunk
    no longer exists (their graph rows should be pruned). Content edits and
    merge/split both change the hash, so they re-enter ``todo`` with no hook.
    """
    if force:
        return sorted(current_hashes), sorted(done_hashes - current_hashes)
    todo = current_hashes - done_hashes
    orphan = done_hashes - current_hashes
    return sorted(todo), sorted(orphan)


def render_extraction_prompt(chunk_text: str) -> str:
    """Fill the extraction prompt.

    Uses ``replace`` rather than ``str.format`` so the JSON example in the prompt
    needs no brace escaping.
    """
    return EXTRACTION_PROMPT.replace('{{CHUNK}}', chunk_text)


EXTRACTION_SYSTEM = (
    '你是知识图谱抽取器。从给定的中文技术文档片段中抽取实体与关系。'
    '严格只输出一个 JSON 对象，不要 markdown 围栏，不要任何解释文字。'
)

EXTRACTION_PROMPT = """从下面这段文档中抽取知识图谱。

【实体】要求：
1. 必须是原文中逐字出现的名词性短语，禁止改写、翻译、概括、补全
2. 长度 2~20 字；禁止代词（它/该/这）、泛指词（系统/方法/问题）、整句、纯数字
3. 每段最多 12 个，优先抽具体的专有名词（产品名、技术名、机构名、指标名、法规名、人名）
4. type 只能取以下之一：人物|组织|产品|技术|指标|法规|地点|其他
5. aliases 填原文中出现的同义写法，没有就填空数组

【关系】要求：
1. 每条关系都必须给出 evidence：从原文中**逐字复制**的那一句话，这句话要能直接读出该关系。
   复制不出来，就说明原文没这么说——这条关系不要抽。
2. 两个实体只是同时出现、被并列列举、同属一个列表时，不要抽——那只是共现，不是关系。
3. relation 只能取以下之一：
   属于|包含|研发|使用|应用于|治疗|位于|导致|提升|降低|优于|合作|定义|其他
   「其他」只在确实有关系、但都不在列表里时使用；仅共现不要写「其他」。
4. 「包含」只用于原文出现「包含/包括/由…组成/分为」这类明确表述的情况；
   「属于」只用于原文出现「属于/是…的一部分」这类表述。
5. source 是主体、target 是客体，方向别反（"A 使用 B" → source=A, relation=使用, target=B）
6. source 和 target 必须出现在你上面抽出的实体列表里
7. 禁止 source 与 target 相同
8. 每段最多 15 个；**宁可少抽，也不要抽没把握的**
9. confidence 是 0~100 的整数，表示你对该关系的把握

只输出这个 JSON：
{"entities":[{"name":"...","type":"...","aliases":["..."]}],
 "relations":[{"source":"...","relation":"...","target":"...","evidence":"原文原句","confidence":85}]}

文档片段：
{{CHUNK}}"""


####################
# Chunk sourcing
####################


@dataclass
class ChunkRef:
    """One Chroma document, identified by the hash retrieval will also compute."""

    hash: str
    text: str
    file_id: str | None = None
    chunk_index: int | None = None


async def fetch_kb_chunks(knowledge_id: str) -> list[ChunkRef]:
    """Read every chunk of a KB straight from the vector DB.

    Deliberately not ``knowledge_chunk`` - see the module docstring. ``file_id``
    comes from Chroma metadata; ``chunk_index`` is best-effort and frequently
    absent, since the regular ingest path does not store it.
    """
    from open_webui.retrieval.utils import _content_hash
    from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT

    result = await ASYNC_VECTOR_DB_CLIENT.get(collection_name=knowledge_id)
    if result is None or not result.documents or not result.documents[0]:
        return []

    documents = result.documents[0]
    metadatas = result.metadatas[0] if result.metadatas else [{}] * len(documents)

    chunks: list[ChunkRef] = []
    seen: set[str] = set()
    for idx, text in enumerate(documents):
        if not text:
            continue
        chunk_hash = _content_hash(text)
        # Chroma can hold the same content twice (re-index after a re-upload);
        # the graph keys on content, so collapsing here is correct and cheaper.
        if chunk_hash in seen:
            continue
        seen.add(chunk_hash)

        meta = dict(metadatas[idx] or {}) if idx < len(metadatas) else {}
        file_id = meta.get('file_id') or meta.get('hash')
        raw_index = meta.get('chunk_index')
        chunk_index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else None
        chunks.append(ChunkRef(hash=chunk_hash, text=text, file_id=file_id, chunk_index=chunk_index))

    return chunks


async def backfill_chunk_indexes(knowledge_id: str, chunk_hashes: set[str]) -> dict[str, int]:
    """Best-effort ``content_hash -> chunk_index`` map from ``knowledge_chunk``.

    Missing entries are normal (that table may be empty); callers must render a
    placeholder rather than assume the key exists.
    """
    if not chunk_hashes:
        return {}
    from open_webui.models.knowledge import KnowledgeChunk

    async with get_async_db() as db:
        rows = await db.execute(
            sa_select(KnowledgeChunk.content_hash, KnowledgeChunk.chunk_index).where(
                KnowledgeChunk.knowledge_id == knowledge_id,
                KnowledgeChunk.content_hash.in_(list(chunk_hashes)),
            )
        )
        return {content_hash: index for content_hash, index in rows.all() if content_hash and index is not None}


####################
# LLM extraction
####################


@dataclass
class ExtractionOutcome:
    status: str  # 'ok' | 'failed' | 'skipped'
    entities: list[dict] = field(default_factory=list)
    relations: list[dict] = field(default_factory=list)
    error: str | None = None


async def extract_chunk(chunk: ChunkRef, model: str | None = None, max_attempts: int = 2) -> ExtractionOutcome:
    """Extract one chunk, retrying transport and JSON failures.

    The retry shape mirrors ``_judge_faithfulness`` in ``routers/knowledge.py``:
    on a parse failure the raw output is appended and the model is told it was
    not valid JSON, which works far better than resending the same prompt.
    """
    from open_webui.utils.llm_client import chat_completion, extract_json

    text = chunk.text
    if len(text) > MAX_CHUNK_CHARS:
        text = text[:MAX_CHUNK_CHARS]

    messages = [
        {'role': 'system', 'content': EXTRACTION_SYSTEM},
        {'role': 'user', 'content': render_extraction_prompt(text)},
    ]

    last_error: str | None = None
    for attempt in range(max_attempts):
        try:
            raw = await chat_completion(messages, temperature=0.0, model=model)
        except Exception as exc:  # HTTPException from llm_client, or transport error
            detail = getattr(exc, 'detail', None) or str(exc)
            if 'No API key' in str(detail):  # retrying cannot help
                return ExtractionOutcome(status='failed', error=str(detail))
            last_error = f'LLM call failed: {detail}'
            if attempt + 1 < max_attempts:
                await asyncio.sleep(2**attempt)
            continue

        try:
            parsed = extract_json(raw)
        except Exception as exc:
            last_error = f'JSON parse failed: {exc}'
            if attempt + 1 < max_attempts:
                messages = messages + [
                    {'role': 'assistant', 'content': raw},
                    {'role': 'user', 'content': 'That was not valid JSON. Output ONLY the JSON object, no fences, no prose.'},
                ]
            continue

        entities, relations = validate_and_normalize(parsed, text)
        if not entities:
            # Not a failure: plenty of chunks legitimately hold no entities.
            return ExtractionOutcome(status='ok', entities=[], relations=[])
        return ExtractionOutcome(status='ok', entities=entities, relations=relations)

    return ExtractionOutcome(status='failed', error=last_error or 'extraction failed')


####################
# Persistence
####################


async def _upsert_entity(
    db,
    knowledge_id: str,
    entity: dict,
    chunk_hash: str,
    now: int,
    cache: dict[str, str],
) -> str | None:
    """Return the entity id, inserting it if needed.

    The insert runs inside a SAVEPOINT so a concurrent worker winning the race on
    ``uq_knowledge_graph_entity_kb_name_key`` only rolls back this one statement
    instead of the whole chunk transaction. This is the portable answer across
    SQLite and PostgreSQL - ON CONFLICT differs between them.
    """
    name_key = entity['name_key']
    cached = cache.get(name_key)
    if cached:
        return cached

    entity_id = str(uuid.uuid4())
    try:
        async with db.begin_nested():
            db.add(
                KnowledgeGraphEntity(
                    id=entity_id,
                    knowledge_id=knowledge_id,
                    name=entity['name'],
                    name_key=name_key,
                    canonical_id=None,
                    entity_type=entity['entity_type'],
                    description=None,
                    aliases=entity['aliases'] or None,
                    chunk_hashes=None,
                    mention_count=0,
                    degree=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.flush()
        cache[name_key] = entity_id
        return entity_id
    except IntegrityError:
        pass

    # Lost the race, or the entity already existed: adopt the existing row.
    row = (
        await db.execute(
            sa_select(KnowledgeGraphEntity.id, KnowledgeGraphEntity.aliases).where(
                KnowledgeGraphEntity.knowledge_id == knowledge_id,
                KnowledgeGraphEntity.name_key == name_key,
            )
        )
    ).first()
    if row is None:
        log.warning(f'knowledge_graph:entity_conflict_unresolved kb={knowledge_id} key={name_key}')
        return None

    existing_id, existing_aliases = row[0], list(row[1] or [])
    merged = existing_aliases[:MAX_ALIASES_PER_ENTITY]
    for alias in entity['aliases']:
        if alias not in merged and len(merged) < MAX_ALIASES_PER_ENTITY:
            merged.append(alias)
    if merged != existing_aliases:
        await db.execute(
            sa_update(KnowledgeGraphEntity)
            .where(KnowledgeGraphEntity.id == existing_id)
            .values(aliases=merged, updated_at=now)
        )

    cache[name_key] = existing_id
    return existing_id


async def _upsert_edge(
    db,
    knowledge_id: str,
    source_id: str,
    target_id: str,
    relation: dict,
    chunk_hash: str,
    file_id: str | None,
    now: int,
) -> bool:
    """Insert or merge one triple. Returns True when a new row was created.

    Re-extraction is idempotent: weight counts *distinct* evidence chunks, so
    rebuilding the same chunk never inflates it.
    """
    relation_key = relation['relation_key']
    try:
        async with db.begin_nested():
            db.add(
                KnowledgeGraphEdge(
                    id=str(uuid.uuid4()),
                    knowledge_id=knowledge_id,
                    source_entity_id=source_id,
                    target_entity_id=target_id,
                    relation=relation['relation'],
                    relation_key=relation_key,
                    evidence_chunk_hashes=[chunk_hash],
                    evidence_file_ids=[file_id] if file_id else [],
                    weight=1,
                    is_cross_doc=0,
                    confidence=relation['confidence'],
                    created_at=now,
                    updated_at=now,
                )
            )
            await db.flush()
        return True
    except IntegrityError:
        pass

    row = (
        await db.execute(
            sa_select(
                KnowledgeGraphEdge.id,
                KnowledgeGraphEdge.evidence_chunk_hashes,
                KnowledgeGraphEdge.evidence_file_ids,
                KnowledgeGraphEdge.confidence,
            ).where(
                KnowledgeGraphEdge.knowledge_id == knowledge_id,
                KnowledgeGraphEdge.source_entity_id == source_id,
                KnowledgeGraphEdge.target_entity_id == target_id,
                KnowledgeGraphEdge.relation_key == relation_key,
            )
        )
    ).first()
    if row is None:
        log.warning(f'knowledge_graph:edge_conflict_unresolved kb={knowledge_id} rel={relation_key}')
        return False

    edge_id, hashes, file_ids, confidence = row[0], list(row[1] or []), list(row[2] or []), row[3]
    if chunk_hash in hashes:
        return False  # already counted; keep the row untouched

    hashes.append(chunk_hash)
    if file_id and file_id not in file_ids:
        file_ids.append(file_id)

    incoming = relation['confidence']
    if confidence is None:
        merged_confidence = incoming
    elif incoming is None:
        merged_confidence = confidence
    else:
        merged_confidence = max(confidence, incoming)

    await db.execute(
        sa_update(KnowledgeGraphEdge)
        .where(KnowledgeGraphEdge.id == edge_id)
        .values(
            evidence_chunk_hashes=hashes,
            evidence_file_ids=file_ids,
            weight=len(hashes),
            is_cross_doc=1 if len(file_ids) > 1 else 0,
            confidence=merged_confidence,
            updated_at=now,
        )
    )
    return False


async def _write_chunk_result(
    knowledge_id: str,
    chunk: ChunkRef,
    outcome: ExtractionOutcome,
    chunk_index: int | None,
) -> tuple[int, int]:
    """Persist one chunk's extraction in its own short transaction.

    Per-chunk transactions matter: one big transaction exhausts the WAL on
    PostgreSQL and holds a write lock on SQLite for the whole build.
    """
    now = int(time.time())
    async with get_async_db() as db:
        cache: dict[str, str] = {}
        id_by_key: dict[str, str] = {}

        for entity in outcome.entities:
            entity_id = await _upsert_entity(db, knowledge_id, entity, chunk.hash, now, cache)
            if entity_id:
                id_by_key[entity['name_key']] = entity_id

        # chunk <-> entity links, independent of whether any edge came out
        for name_key, entity_id in id_by_key.items():
            try:
                async with db.begin_nested():
                    db.add(
                        KnowledgeGraphChunkEntity(
                            id=str(uuid.uuid4()),
                            knowledge_id=knowledge_id,
                            chunk_hash=chunk.hash,
                            entity_id=entity_id,
                            file_id=chunk.file_id,
                            chunk_index=chunk_index,
                            created_at=now,
                        )
                    )
                    await db.flush()
            except IntegrityError:
                pass  # link already present from an earlier build of this chunk

        edge_count = 0
        for relation in outcome.relations:
            source_id = id_by_key.get(relation['source_key'])
            target_id = id_by_key.get(relation['target_key'])
            if not source_id or not target_id:
                continue
            if await _upsert_edge(
                db, knowledge_id, source_id, target_id, relation, chunk.hash, chunk.file_id, now
            ):
                edge_count += 1

        # Ledger row: the only thing that makes the next build incremental.
        try:
            async with db.begin_nested():
                db.add(
                    KnowledgeGraphExtractionLog(
                        id=str(uuid.uuid4()),
                        knowledge_id=knowledge_id,
                        chunk_hash=chunk.hash,
                        status=outcome.status,
                        error_message=(outcome.error or None) if outcome.status != 'ok' else None,
                        entity_count=len(outcome.entities),
                        edge_count=edge_count,
                        extracted_at=now,
                    )
                )
                await db.flush()
        except IntegrityError:
            await db.execute(
                sa_update(KnowledgeGraphExtractionLog)
                .where(
                    KnowledgeGraphExtractionLog.knowledge_id == knowledge_id,
                    KnowledgeGraphExtractionLog.chunk_hash == chunk.hash,
                )
                .values(
                    status=outcome.status,
                    error_message=(outcome.error or None) if outcome.status != 'ok' else None,
                    entity_count=len(outcome.entities),
                    edge_count=edge_count,
                    extracted_at=now,
                )
            )

        await db.commit()

    return len(id_by_key), edge_count


async def prune_orphan_chunks(knowledge_id: str, orphan_hashes: list[str]) -> int:
    """Drop graph rows for chunks that no longer exist in the vector DB."""
    if not orphan_hashes:
        return 0
    deleted = 0
    # Chunked so the IN clause stays well inside SQLite's variable limit.
    for start in range(0, len(orphan_hashes), 500):
        batch = orphan_hashes[start : start + 500]
        async with get_async_db() as db:
            await db.execute(
                sa_delete(KnowledgeGraphChunkEntity).where(
                    KnowledgeGraphChunkEntity.knowledge_id == knowledge_id,
                    KnowledgeGraphChunkEntity.chunk_hash.in_(batch),
                )
            )
            await db.execute(
                sa_delete(KnowledgeGraphExtractionLog).where(
                    KnowledgeGraphExtractionLog.knowledge_id == knowledge_id,
                    KnowledgeGraphExtractionLog.chunk_hash.in_(batch),
                )
            )
            await db.commit()
            deleted += len(batch)
    log.info(f'knowledge_graph:pruned kb={knowledge_id} chunks={deleted}')
    return deleted


async def backfill_degrees(knowledge_id: str) -> tuple[int, int]:
    """Recompute ``mention_count``, ``chunk_hashes`` and ``degree`` for every node.

    Done once at the end of a build rather than incrementally: degree is a global
    property, and recomputing it per chunk would be both slower and wrong when an
    edge merges. Alias shadows (``canonical_id`` set) are excluded from traversal
    and therefore keep ``degree=0``.
    """
    now = int(time.time())
    entity_count = 0
    edge_count = 0

    async with get_async_db() as db:
        mention_rows = (
            await db.execute(
                sa_select(
                    KnowledgeGraphChunkEntity.entity_id,
                    sa_func.count(sa_func.distinct(KnowledgeGraphChunkEntity.chunk_hash)),
                )
                .where(KnowledgeGraphChunkEntity.knowledge_id == knowledge_id)
                .group_by(KnowledgeGraphChunkEntity.entity_id)
            )
        ).all()
        mentions = {entity_id: int(count) for entity_id, count in mention_rows}

        hash_rows = (
            await db.execute(
                sa_select(KnowledgeGraphChunkEntity.entity_id, KnowledgeGraphChunkEntity.chunk_hash).where(
                    KnowledgeGraphChunkEntity.knowledge_id == knowledge_id
                )
            )
        ).all()
        hashes_by_entity: dict[str, list[str]] = {}
        for entity_id, chunk_hash in hash_rows:
            bucket = hashes_by_entity.setdefault(entity_id, [])
            if len(bucket) < MAX_ENTITY_CHUNK_HASHES and chunk_hash not in bucket:
                bucket.append(chunk_hash)

        edge_rows = (
            await db.execute(
                sa_select(
                    KnowledgeGraphEdge.source_entity_id,
                    KnowledgeGraphEdge.target_entity_id,
                ).where(KnowledgeGraphEdge.knowledge_id == knowledge_id)
            )
        ).all()
        edge_count = len(edge_rows)

        degrees: dict[str, int] = {}
        for source_id, target_id in edge_rows:
            degrees[source_id] = degrees.get(source_id, 0) + 1
            degrees[target_id] = degrees.get(target_id, 0) + 1

        entity_rows = (
            await db.execute(
                sa_select(
                    KnowledgeGraphEntity.id,
                    KnowledgeGraphEntity.canonical_id,
                    KnowledgeGraphEntity.mention_count,
                    KnowledgeGraphEntity.degree,
                    KnowledgeGraphEntity.chunk_hashes,
                ).where(KnowledgeGraphEntity.knowledge_id == knowledge_id)
            )
        ).all()

        pending = []
        for entity_id, canonical_id, old_mentions, old_degree, old_hashes in entity_rows:
            entity_count += 1
            if canonical_id is not None:
                # Alias shadow: never traversed, so it must not carry a degree.
                new_mentions, new_degree, new_hashes = old_mentions, 0, old_hashes
            else:
                new_mentions = mentions.get(entity_id, 0)
                new_degree = degrees.get(entity_id, 0)
                new_hashes = hashes_by_entity.get(entity_id) or None
            if (new_mentions, new_degree, new_hashes) != (old_mentions, old_degree, old_hashes):
                pending.append(
                    {
                        'b_id': entity_id,
                        'mention_count': new_mentions,
                        'degree': new_degree,
                        'chunk_hashes': new_hashes,
                        'updated_at': now,
                    }
                )

        if pending:
            # Core table update, not the ORM one: an ORM bulk UPDATE whose WHERE
            # targets the primary key is routed through "bulk update by primary
            # key", which insists the parameter dict be keyed by the column name
            # and tries to synchronise the identity map we just populated. Going
            # through ``__table__`` keeps this a plain executemany.
            entity_table = KnowledgeGraphEntity.__table__
            await db.execute(
                entity_table.update().where(entity_table.c.id == bindparam('b_id')),
                pending,
            )
        await db.commit()

    log.info(f'knowledge_graph:degrees kb={knowledge_id} entities={entity_count} edges={edge_count}')
    return entity_count, edge_count


####################
# Orchestration
####################


@dataclass
class BuildStats:
    knowledge_id: str
    task_id: str
    total_chunks: int = 0
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    entities: int = 0
    edges: int = 0
    orphans_pruned: int = 0
    empty_extractions: int = 0  # chunks that yielded no entity - normal, but a useful signal if ~everything is empty
    todo_chunks: int = 0
    status: str = 'pending'


async def _set_task(db, task_id: str, **values) -> None:
    values['updated_at'] = int(time.time())
    await db.execute(sa_update(KnowledgeGraphExtractionTask).where(KnowledgeGraphExtractionTask.id == task_id).values(**values))
    await db.commit()


async def build_graph_for_knowledge(
    knowledge_id: str,
    force: bool = False,
    concurrency: int = 4,
    limit: int | None = None,
    model: str | None = None,
    task_id: str | None = None,
    progress_cb=None,
) -> BuildStats:
    """Extract the whole graph for one knowledge base.

    Long-running by design (tens of minutes for a few dozen files), so callers
    must not await this from a request handler - the API layer schedules it as a
    background task and reports progress through the task row.
    """
    from open_webui.config import GRAPH_EXTRACT_MODEL

    model = model or GRAPH_EXTRACT_MODEL or None

    chunks = await fetch_kb_chunks(knowledge_id)
    chunk_by_hash = {chunk.hash: chunk for chunk in chunks}

    async with get_async_db() as db:
        done_rows = (
            await db.execute(
                sa_select(KnowledgeGraphExtractionLog.chunk_hash).where(
                    KnowledgeGraphExtractionLog.knowledge_id == knowledge_id,
                    KnowledgeGraphExtractionLog.status == 'ok',
                )
            )
        ).all()
    done_hashes = {row[0] for row in done_rows}

    todo, orphan = compute_incremental_work(set(chunk_by_hash), done_hashes, force=force)
    if limit is not None:
        todo = todo[:limit]

    stats = BuildStats(
        knowledge_id=knowledge_id,
        task_id=task_id or '',
        total_chunks=len(chunks),
        todo_chunks=len(todo),
    )

    if task_id:
        async with get_async_db() as db:
            await _set_task(
                db,
                task_id,
                status='running',
                total_chunks=len(chunks),
                processed_chunks=0,
                failed_chunks=0,
                skipped_chunks=0,
                entity_count=0,
                edge_count=0,
                model=model,
                concurrency=concurrency,
                error_message=None,
            )

    try:
        if orphan:
            stats.orphans_pruned = await prune_orphan_chunks(knowledge_id, orphan)

        index_map: dict[str, int] = {}
        if todo:
            index_map = await backfill_chunk_indexes(knowledge_id, set(todo))

        semaphore = asyncio.Semaphore(max(1, concurrency))
        lock = asyncio.Lock()

        async def run_one(chunk_hash: str) -> None:
            chunk = chunk_by_hash[chunk_hash]
            async with semaphore:
                outcome = await extract_chunk(chunk, model=model)

            entity_n, edge_n = await _write_chunk_result(knowledge_id, chunk, outcome, index_map.get(chunk_hash))

            async with lock:
                if outcome.status == 'failed':
                    # Recorded in the ledger but never aborts the build.
                    stats.failed += 1
                    log.warning(f'knowledge_graph:chunk_failed kb={knowledge_id} hash={chunk_hash[:8]} err={outcome.error}')
                elif outcome.status == 'skipped':
                    stats.skipped += 1
                else:
                    stats.entities += entity_n
                    stats.edges += edge_n
                    if not outcome.entities:
                        stats.empty_extractions += 1

            async with lock:
                stats.processed += 1
                snapshot = (stats.processed, stats.failed, stats.skipped, stats.entities, stats.edges)
            if progress_cb:
                await progress_cb(*snapshot)
            elif task_id and stats.processed % 5 == 0:
                async with get_async_db() as db:
                    await _set_task(
                        db,
                        task_id,
                        processed_chunks=snapshot[0],
                        failed_chunks=snapshot[1],
                        skipped_chunks=snapshot[2],
                        entity_count=snapshot[3],
                        edge_count=snapshot[4],
                    )

        await asyncio.gather(*(run_one(chunk_hash) for chunk_hash in todo), return_exceptions=True)

        entity_count, edge_count = await backfill_degrees(knowledge_id)
        stats.entities, stats.edges = entity_count, edge_count
        stats.status = 'partial' if stats.failed else 'completed'
    except asyncio.CancelledError:
        stats.status = 'cancelled'
        if task_id:
            async with get_async_db() as db:
                await _set_task(db, task_id, status='cancelled', processed_chunks=stats.processed)
        raise
    except Exception as exc:
        log.exception(f'knowledge_graph:build_failed kb={knowledge_id}')
        stats.status = 'failed'
        if task_id:
            async with get_async_db() as db:
                await _set_task(
                    db,
                    task_id,
                    status='failed',
                    error_message=str(exc)[:500],
                    processed_chunks=stats.processed,
                    failed_chunks=stats.failed,
                )
        return stats

    if task_id:
        async with get_async_db() as db:
            await _set_task(
                db,
                task_id,
                status=stats.status,
                processed_chunks=stats.processed,
                failed_chunks=stats.failed,
                skipped_chunks=stats.skipped,
                entity_count=stats.entities,
                edge_count=stats.edges,
            )

    log.info(
        f'knowledge_graph:build_done kb={knowledge_id} status={stats.status} '
        f'total={stats.total_chunks} todo={stats.todo_chunks} failed={stats.failed} '
        f'entities={stats.entities} edges={stats.edges}'
    )
    return stats


async def create_build_task(knowledge_id: str, concurrency: int, model: str | None) -> str:
    """Insert a 'pending' task row and return its id, so the API can return early."""
    task_id = str(uuid.uuid4())
    now = int(time.time())
    async with get_async_db() as db:
        db.add(
            KnowledgeGraphExtractionTask(
                id=task_id,
                knowledge_id=knowledge_id,
                status='pending',
                total_chunks=0,
                processed_chunks=0,
                failed_chunks=0,
                skipped_chunks=0,
                entity_count=0,
                edge_count=0,
                model=model,
                concurrency=concurrency,
                error_message=None,
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()
    return task_id


####################
# CLI
####################


async def _cli(args) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

    if args.dry_run:
        all_chunks = await fetch_kb_chunks(args.knowledge_id)
        chunks = all_chunks[: args.limit] if args.limit else all_chunks
        print(f'chunks in vector db: {len(all_chunks)}; extracting: {len(chunks)}')
        total_e = total_r = 0
        samples: list[str] = []
        for chunk in chunks:
            outcome = await extract_chunk(chunk, model=args.model)
            total_e += len(outcome.entities)
            total_r += len(outcome.relations)
            for relation in outcome.relations:
                samples.append(
                    f'  {relation["source_name"]} --{relation["relation_key"]}--> {relation["target_name"]}'
                    f'  (conf={relation["confidence"]})'
                )
            if outcome.status != 'ok':
                print(f'  [{outcome.status}] {outcome.error}')
        print(f'extracted entities={total_e} relations={total_r} (dry run, nothing written)')
        if samples:
            print(f'\nsample triples (first {min(len(samples), 30)}):')
            print('\n'.join(samples[:30]))
        return 0

    task_id = await create_build_task(args.knowledge_id, args.concurrency, args.model)
    print(f'task_id={task_id}')
    stats = await build_graph_for_knowledge(
        args.knowledge_id,
        force=args.force,
        concurrency=args.concurrency,
        limit=args.limit,
        model=args.model,
        task_id=task_id,
    )
    print(json.dumps(stats.__dict__, ensure_ascii=False, indent=2))

    if args.dump:
        await _dump_sample_triples(args.knowledge_id, args.dump)
    return 0 if stats.status in ('completed', 'partial') else 1


async def _dump_sample_triples(knowledge_id: str, count: int) -> None:
    """Print merged triples for the human spot-check S2's acceptance depends on."""
    async with get_async_db() as db:
        names = {
            entity_id: name
            for entity_id, name in (
                await db.execute(
                    sa_select(KnowledgeGraphEntity.id, KnowledgeGraphEntity.name).where(
                        KnowledgeGraphEntity.knowledge_id == knowledge_id
                    )
                )
            ).all()
        }
        edge_rows = (
            await db.execute(
                sa_select(
                    KnowledgeGraphEdge.source_entity_id,
                    KnowledgeGraphEdge.target_entity_id,
                    KnowledgeGraphEdge.relation_key,
                    KnowledgeGraphEdge.weight,
                    KnowledgeGraphEdge.is_cross_doc,
                )
                .where(KnowledgeGraphEdge.knowledge_id == knowledge_id)
                .order_by(KnowledgeGraphEdge.weight.desc())
                .limit(count)
            )
        ).all()

    print(f'\ntop {len(edge_rows)} triples by weight (for manual spot-check):')
    for source_id, target_id, relation_key, weight, is_cross_doc in edge_rows:
        cross = ' [跨文档]' if is_cross_doc else ''
        print(f'  {names.get(source_id, "?")} --{relation_key}--> {names.get(target_id, "?")}  w={weight}{cross}')


def main() -> int:
    parser = argparse.ArgumentParser(description='Build the Phase 12 knowledge graph for one knowledge base.')
    parser.add_argument('--knowledge-id', required=True, help='knowledge base id (also the Chroma collection name)')
    parser.add_argument('--force', action='store_true', help='re-extract every chunk, ignoring the ledger')
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--limit', type=int, default=None, help='cap the number of chunks processed (prompt tuning)')
    parser.add_argument('--model', default=None, help='override the extraction model (default GRAPH_EXTRACT_MODEL/EVAL_MODEL)')
    parser.add_argument('--dry-run', action='store_true', help='extract and print, write nothing to the database')
    parser.add_argument('--dump', type=int, default=0, help='after the build, print N triples by weight for spot-checking')
    args = parser.parse_args()
    return asyncio.run(_cli(args))


if __name__ == '__main__':
    raise SystemExit(main())
