"""Phase 12 retrieval-side graph expansion (multi-hop recall).

The mechanism in one paragraph: take the chunks the normal hybrid retrieval
already found as *seeds*, look up the entities those seed chunks mention, walk
the graph one or two hops to reach further entities, then pull back every chunk
that mentions any reached entity. Those chunks are frequently in a different
file - which is the whole point, since two documents that share an entity are
strongly related even when their wording is far apart in embedding space.

Two invariants, both load-bearing:

1. **Scores must stay in one space.** Chroma's ``distances`` are cosine
   distances (lower is better) while post-rerank ``metadata['score']`` is the
   CrossEncoder's sigmoid output (higher is better), and the shared merge helper
   sorts everything descending. Mixing the two would silently invert the ranking.
   So graph candidates are *always* pushed through the same reranker and land in
   that same score space, and the caller refuses to expand unless a reranker is
   configured.

2. **The join key is ``_content_hash(chunk text)``**, the same digest retrieval
   surfaces as ``metadata['_chunk_hash']``. See ``knowledge_graph.py``.

The traversal is deliberately plain Python BFS rather than a recursive CTE: two
hops is four indexed ``IN`` queries, it cannot cycle, and provenance (which
entity, how many hops) comes out for free. A CTE would introduce
``GROUP_CONCAT``/``string_agg`` dialect splits for no measurable gain at this
scale.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass

from langchain_core.documents import Document
from sqlalchemy import bindparam
from sqlalchemy import select as sa_select

from open_webui.internal.db import get_async_db
from open_webui.models.knowledge import (
    KnowledgeGraphChunkEntity,
    KnowledgeGraphEdge,
    KnowledgeGraphEntity,
)

log = logging.getLogger(__name__)

# Kept local rather than imported from retrieval.utils: that module imports this
# one, so importing back would be circular. tests/test_graph_retrieval.py asserts
# these stay equal to the real constants.
CHUNK_HASH_KEY = '_chunk_hash'
GRAPH_META_KEY = 'graph'

# Emitted in metadata[GRAPH_META_KEY]['score_space'] so downstream consumers
# (evaluate/query scores, the eval harness, the UI) can tell whether a score is
# comparable with the vector path's cosine distance. It never is.
SCORE_SPACE = 'rerank'

DEFAULT_GRAPH_BONUS = 0.05
# 'append' rather than 'rerank' by default. Kept here as a constant, next to the
# bonus, so the two places that default it cannot drift apart; config.py defaults
# rag.graph_merge_mode to the same value.
DEFAULT_MERGE_MODE = 'append'
DEFAULT_ENTITY_HITS_CAP = 3
# A seed set larger than this contributes little (the graph frontier saturates
# long before) and would balloon the IN clauses.
MAX_SEED_ENTITIES = 200


####################
# Pure helpers
####################


def graph_prior(hops: int, entity_hits: int, bonus: float = DEFAULT_GRAPH_BONUS) -> float:
    """Additive nudge favouring close, multiply-supported graph chunks.

    Calibration note, because the natural assumption is wrong. The shipped
    reranker (``bge-reranker-base``) returns **sigmoid** scores in [0, 1], not raw
    logits -- ``CrossEncoder.predict`` applies ``Sigmoid()`` unless told
    otherwise. Measured in this repo: raw logits ``[-7.23, -10.19]`` come back as
    ``[7.3e-04, 3.7e-05]``.

    So 0.05 is *not* a negligible tiebreak on a [0, 1] scale -- it can overturn a
    gap of several relevance points, and top-ranked rerank scores routinely sit
    within 0.01 of each other. Measured on the 4-doc demo KB (16 questions, K=3,
    file-level recall): with ``mode='rerank'`` that made the graph arm score
    *below* plain hybrid retrieval (overall Δ -0.073, cross-doc Δ -0.146), because
    graph chunks outranked base chunks and pushed them past position K.

    The original reasoning -- "a bonus too small to reorder the top-K makes the
    feature a no-op" -- is true but incomplete: a bonus large enough to reorder
    the top-K buys a promotion at the cost of the base hit it displaces. Covering
    the downside is why ``merge_mode`` now defaults to 'append' (graph chunks
    extend the set instead of competing for its head), which makes the bonus a
    non-issue for the default path. It still matters under 'rerank', and this
    function stays tunable via ``rag.graph_bonus``; set 0 to disable.
    """
    if bonus <= 0:
        return 0.0
    closeness = 1.0 / (1.0 + max(0, hops))
    support = min(max(0, entity_hits), DEFAULT_ENTITY_HITS_CAP) / DEFAULT_ENTITY_HITS_CAP
    return bonus * closeness * support


def item_hash(item: dict) -> str:
    """Dedup key for a retrieval item: the chunk hash when present, else a digest."""
    metadata = item.get('metadata') or {}
    chunk_hash = metadata.get(CHUNK_HASH_KEY)
    if chunk_hash:
        return chunk_hash
    return hashlib.sha256((item.get('text') or '').encode()).hexdigest()


def item_score(item: dict) -> float:
    score = item.get('score')
    return float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else float('-inf')


def result_to_items(result: dict | None) -> list[dict]:
    """Flatten query_collection's ``{'documents': [[...]], ...}`` into a flat list."""
    if not result:
        return []
    documents = (result.get('documents') or [[]])[0] or []
    metadatas = (result.get('metadatas') or [[]])[0] or []
    distances = (result.get('distances') or [[]])[0] or []

    items = []
    for idx, text in enumerate(documents):
        items.append(
            {
                'text': text,
                'metadata': dict(metadatas[idx] or {}) if idx < len(metadatas) else {},
                'score': distances[idx] if idx < len(distances) else None,
            }
        )
    return items


def items_to_result(items: list[dict]) -> dict:
    """Inverse of ``result_to_items``, restoring query_collection's shape."""
    return {
        'documents': [[item.get('text') or '' for item in items]],
        'metadatas': [[item.get('metadata') or {} for item in items]],
        'distances': [[item.get('score') for item in items]],
    }


def merge_graph_candidates(
    base_items: list[dict],
    graph_items: list[dict],
    k: int,
    mode: str = DEFAULT_MERGE_MODE,
) -> list[dict]:
    """Merge graph-recalled chunks into the base result. Pure - no DB, no network.

    Dedup is by chunk hash; on a collision the higher score wins while the base
    item's metadata is kept (it carries the fields the UI already renders) and
    the graph provenance is attached alongside.

    ``mode='rerank'`` re-sorts everything by score, which is only meaningful
    because both sides are in the reranker's logit space. ``mode='append'``
    keeps the base ordering untouched and appends graph chunks after it - the
    conservative option when a caller does not trust the scores.
    """
    if k <= 0:
        return []

    by_hash: dict[str, dict] = {}
    order: list[str] = []

    for item in base_items:
        key = item_hash(item)
        existing = by_hash.get(key)
        if existing is None:
            by_hash[key] = item
            order.append(key)
        elif item_score(item) > item_score(existing):
            by_hash[key] = item

    for item in graph_items:
        key = item_hash(item)
        provenance = (item.get('metadata') or {}).get(GRAPH_META_KEY)
        existing = by_hash.get(key)
        if existing is None:
            by_hash[key] = item
            order.append(key)
            continue

        merged = dict(existing)
        merged['score'] = max(item_score(existing), item_score(item))
        if provenance:
            # A base hit that is also graph-reachable: keep its metadata and
            # record the provenance, so it stays inspectable either way.
            merged['metadata'] = {**(existing.get('metadata') or {}), GRAPH_META_KEY: provenance}
        by_hash[key] = merged

    items = [by_hash[key] for key in order]
    if mode != 'append':
        items.sort(key=item_score, reverse=True)
    return items[:k]


####################
# Graph traversal
####################


@dataclass
class GraphCandidate:
    """A chunk reached through the graph, with the provenance that found it."""

    chunk_hash: str
    hops: int
    entity_hits: int
    via_entities: list[str]
    file_id: str | None = None


async def collect_graph_candidates(
    knowledge_id: str,
    seed_hashes: set[str],
    max_hops: int = 1,
    hub_cap: int = 20,
    budget: int = 4,
    max_candidates: int | None = None,
) -> list[GraphCandidate]:
    """Walk the graph from the seed chunks' entities and return related chunks.

    Hub entities (``degree > hub_cap``) are excluded from *both* the seed set and
    every expansion step. Without that, a node like "人工智能" reaches most of the
    knowledge base within two hops and the expansion degenerates into "return
    everything", which is worse than not expanding at all.
    """
    if not seed_hashes or max_hops < 1:
        return []

    limit = max_candidates if max_candidates is not None else max(budget * 4, budget)

    async with get_async_db() as db:
        # --- seeds: entities mentioned by the chunks we already retrieved ---
        seed_rows = (
            await db.execute(
                sa_select(KnowledgeGraphChunkEntity.entity_id)
                .join(KnowledgeGraphEntity, KnowledgeGraphEntity.id == KnowledgeGraphChunkEntity.entity_id)
                .where(
                    KnowledgeGraphChunkEntity.knowledge_id == knowledge_id,
                    KnowledgeGraphChunkEntity.chunk_hash.in_(list(seed_hashes)),
                    KnowledgeGraphEntity.canonical_id.is_(None),
                    KnowledgeGraphEntity.degree <= hub_cap,
                )
                .limit(MAX_SEED_ENTITIES)
            )
        ).all()
        if not seed_rows:
            return []

        entity_hops: dict[str, int] = {row[0]: 0 for row in seed_rows}
        frontier = list(entity_hops)

        # --- expand, tracking the shortest hop that reached each entity ---
        edge_table = KnowledgeGraphEdge.__table__
        entity_table = KnowledgeGraphEntity.__table__
        for hop in range(1, max_hops + 1):
            if not frontier:
                break
            reached: set[str] = set()
            for source_col, target_col in (
                (edge_table.c.source_entity_id, edge_table.c.target_entity_id),
                (edge_table.c.target_entity_id, edge_table.c.source_entity_id),
            ):
                rows = (
                    await db.execute(
                        sa_select(target_col)
                        .select_from(edge_table.join(entity_table, entity_table.c.id == target_col))
                        .where(
                            edge_table.c.knowledge_id == knowledge_id,
                            # Must be `.in_(bindparam(..., expanding=True))`. Writing
                            # `col == bindparam(..., expanding=True)` renders a row-value
                            # tuple `= (?, ?, ...)`, which SQLite rejects with
                            # "row value misused" and PostgreSQL with a type error.
                            source_col.in_(bindparam('frontier', expanding=True)),
                            entity_table.c.canonical_id.is_(None),
                            entity_table.c.degree <= hub_cap,
                        ),
                        {'frontier': frontier},
                    )
                ).all()
                reached.update(row[0] for row in rows)

            frontier = [entity_id for entity_id in reached if entity_id not in entity_hops]
            for entity_id in frontier:
                entity_hops[entity_id] = hop

        reached_ids = list(entity_hops)

        # --- pull back every chunk mentioning a reached entity ---
        # Separate query rather than folding this into the BFS: provenance
        # aggregation is far clearer in Python, and it avoids the SQL dialect
        # differences that string aggregation would drag in.
        chunk_rows = (
            await db.execute(
                sa_select(
                    KnowledgeGraphChunkEntity.chunk_hash,
                    KnowledgeGraphChunkEntity.entity_id,
                    KnowledgeGraphChunkEntity.file_id,
                ).where(
                    KnowledgeGraphChunkEntity.knowledge_id == knowledge_id,
                    KnowledgeGraphChunkEntity.entity_id.in_(reached_ids),
                )
            )
        ).all()

    by_chunk: dict[str, GraphCandidate] = {}
    for chunk_hash, entity_id, file_id in chunk_rows:
        if chunk_hash in seed_hashes:
            continue  # already in the base result
        candidate = by_chunk.get(chunk_hash)
        if candidate is None:
            candidate = GraphCandidate(
                chunk_hash=chunk_hash,
                hops=entity_hops.get(entity_id, max_hops),
                entity_hits=0,
                via_entities=[],
                file_id=file_id,
            )
            by_chunk[chunk_hash] = candidate
        candidate.entity_hits += 1
        if entity_id not in candidate.via_entities:
            candidate.via_entities.append(entity_id)
        candidate.hops = min(candidate.hops, entity_hops.get(entity_id, max_hops))
        if candidate.file_id is None:
            candidate.file_id = file_id

    # Closest first, then best-supported. Ties are broken by hash for a stable
    # order, which keeps the eval harness reproducible run to run.
    candidates = sorted(by_chunk.values(), key=lambda c: (c.hops, -c.entity_hits, c.chunk_hash))
    return candidates[:limit]


async def fetch_chunk_texts(knowledge_id: str, chunk_hashes: list[str]) -> tuple[dict[str, str], int]:
    """Resolve chunk hashes to text.

    Primary source is ``knowledge_chunk``; anything missing falls back to reading
    the vector DB. That fallback is mandatory rather than defensive:
    ``knowledge_chunk`` is only populated once someone opens the Chunks tab, so a
    KB whose chunks were never inspected has an empty table while Chroma is full.

    Returns ``(hash -> text, missing_count)``. The miss count is reported rather
    than swallowed, because a silent join failure would look exactly like "the
    graph found nothing useful".
    """
    if not chunk_hashes:
        return {}, 0

    found: dict[str, str] = {}
    for start in range(0, len(chunk_hashes), 500):
        batch = chunk_hashes[start : start + 500]
        async with get_async_db() as db:
            from open_webui.models.knowledge import KnowledgeChunk

            rows = (
                await db.execute(
                    sa_select(KnowledgeChunk.content_hash, KnowledgeChunk.content).where(
                        KnowledgeChunk.knowledge_id == knowledge_id,
                        KnowledgeChunk.content_hash.in_(batch),
                    )
                )
            ).all()
        for content_hash, content in rows:
            if content_hash and content:
                found[content_hash] = content

    missing = [chunk_hash for chunk_hash in chunk_hashes if chunk_hash not in found]
    if missing:
        from open_webui.retrieval.utils import _content_hash
        from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT

        result = await ASYNC_VECTOR_DB_CLIENT.get(collection_name=knowledge_id)
        documents = (result.documents[0] if result and result.documents else []) or []
        wanted = set(missing)
        for text in documents:
            if not text:
                continue
            digest = _content_hash(text)
            if digest in wanted:
                found[digest] = text
                wanted.discard(digest)
                if not wanted:
                    break
        missing = list(wanted)

    return found, len(missing)


async def resolve_file_names(file_ids: list[str]) -> dict[str, str]:
    """``file.id -> filename`` for rendering graph-recalled sources."""
    file_ids = [file_id for file_id in dict.fromkeys(file_ids) if file_id]
    if not file_ids:
        return {}
    from open_webui.models.files import File

    names: dict[str, str] = {}
    for start in range(0, len(file_ids), 500):
        batch = file_ids[start : start + 500]
        async with get_async_db() as db:
            rows = (await db.execute(sa_select(File.id, File.filename).where(File.id.in_(batch)))).all()
        names.update({row[0]: row[1] for row in rows})
    return names


####################
# Readiness gate
####################


def graph_retrieval_readiness(
    reranking_function,
    hybrid_enabled: bool,
    graph_enabled: bool,
) -> tuple[bool, str]:
    """Whether graph expansion can run, and a human reason when it cannot.

    The guard is not defensive padding -- it is load-bearing, and every clause
    here has a concrete failure behind it:

    * no reranker -> graph chunks would be scored in the reranker's space while
      the base result sits in cosine-distance space, and the shared merge helper
      sorts both descending. The ranking silently inverts.
    * no hybrid search -> same score-space problem, plus `query_collection` never
      builds a reranker in that configuration anyway.

    Returns ``(ready, reason)``; ``reason`` is empty when ready. The reason
    string is surfaced verbatim by ``GET /{id}/graph/stats`` and rendered as a
    banner in the UI, because the failure mode is *silent* otherwise: the feature
    is switched on, nothing happens, and no error is raised.
    """
    if not graph_enabled:
        return False, '图谱检索未启用（rag.enable_graph_retrieval = false）'
    if not hybrid_enabled:
        return False, '需要开启混合检索（rag.enable_hybrid_search），否则两者分数空间不一致'
    if reranking_function is None:
        return False, '未配置重排模型（rag.reranking_model），图谱召回的 chunk 无法与基础结果同空间比较'
    return True, ''


_WARNED_NOT_READY = False


async def maybe_expand_query_result_with_graph(
    result: dict | None,
    *,
    knowledge_id: str | None,
    query: str,
    reranking_function,
    k: int,
    enabled: bool,
    hybrid_enabled: bool = True,
    hops: int = 1,
    budget: int = 0,
    merge_mode: str = DEFAULT_MERGE_MODE,
    hub_cap: int = 20,
    graph_bonus: float = DEFAULT_GRAPH_BONUS,
) -> dict | None:
    """Wrap the expansion so a graph failure can never discard a good result.

    Called on the *already merged* result of whichever retrieval branch ran, so
    graph problems degrade to plain hybrid retrieval instead of a 500.
    """
    global _WARNED_NOT_READY

    if not enabled or not knowledge_id or not query:
        return result
    if not result:
        return result

    # Switched on but not usable is the dangerous state: nothing happens on any
    # request and nothing complains, so a demo would just show no effect. Say it
    # once per process rather than on every retrieval.
    ready, reason = graph_retrieval_readiness(
        reranking_function, hybrid_enabled=hybrid_enabled, graph_enabled=enabled
    )
    if not ready:
        if not _WARNED_NOT_READY:
            _WARNED_NOT_READY = True
            log.warning(f'graph_retrieval:not_ready kb={knowledge_id} reason={reason}')
        return result

    try:
        return await expand_query_result_with_graph(
            result=result,
            knowledge_id=knowledge_id,
            query=query,
            reranking_function=reranking_function,
            k=k,
            hops=hops,
            budget=budget or None,
            merge_mode=merge_mode,
            hub_cap=hub_cap,
            graph_bonus=graph_bonus,
        )
    except Exception as exc:
        log.warning(f'graph_retrieval:expand_failed kb={knowledge_id} err={exc}')
        return result


####################
# Entry point
####################


async def expand_query_result_with_graph(
    result: dict | None,
    knowledge_id: str,
    query: str,
    reranking_function,
    k: int,
    hops: int = 1,
    budget: int | None = None,
    merge_mode: str = DEFAULT_MERGE_MODE,
    hub_cap: int = 20,
    graph_bonus: float = DEFAULT_GRAPH_BONUS,
) -> dict:
    """Expand a hybrid retrieval result with graph-reachable chunks.

    Returns the result unchanged when there is nothing to add, so the caller can
    always assign the return value unconditionally.
    """
    base_items = result_to_items(result)
    if not base_items:
        return result or {'documents': [[]], 'metadatas': [[]], 'distances': [[]]}

    if budget is None or budget <= 0:
        budget = max(2, k // 3)

    seed_hashes = {item_hash(item) for item in base_items}
    candidates = await collect_graph_candidates(
        knowledge_id=knowledge_id,
        seed_hashes=seed_hashes,
        max_hops=max(1, hops),
        hub_cap=hub_cap,
        budget=budget,
    )
    if not candidates:
        return result

    texts, missing = await fetch_chunk_texts(knowledge_id, [c.chunk_hash for c in candidates])
    if missing:
        # Reported, never swallowed: a join failure and "nothing relevant found"
        # are indistinguishable from the outside otherwise.
        log.warning(
            f'graph_retrieval:join_miss kb={knowledge_id} missing={missing} of {len(candidates)} candidates'
        )
    candidates = [c for c in candidates if c.chunk_hash in texts]
    if not candidates:
        return result

    documents = [Document(page_content=texts[c.chunk_hash]) for c in candidates]
    try:
        scores = await asyncio.to_thread(reranking_function, query, documents)
    except Exception as exc:
        log.warning(f'graph_retrieval:rerank_failed kb={knowledge_id} err={exc}')
        return result
    score_list = scores.tolist() if hasattr(scores, 'tolist') else list(scores)
    if len(score_list) != len(candidates):
        log.warning(
            f'graph_retrieval:rerank_score_mismatch kb={knowledge_id} '
            f'scores={len(score_list)} documents={len(candidates)}'
        )
        return result

    names = await resolve_file_names([c.file_id for c in candidates])

    graph_items = []
    for candidate, rerank_score in zip(candidates, score_list):
        base_score = float(rerank_score)
        prior = graph_prior(candidate.hops, candidate.entity_hits, graph_bonus)
        graph_items.append(
            {
                'text': texts[candidate.chunk_hash],
                'metadata': {
                    'file_id': candidate.file_id,
                    'name': names.get(candidate.file_id, '') if candidate.file_id else '',
                    CHUNK_HASH_KEY: candidate.chunk_hash,
                    GRAPH_META_KEY: {
                        'hops': candidate.hops,
                        'entity_hits': candidate.entity_hits,
                        'via_entities': candidate.via_entities,
                        'rerank_score': base_score,
                        'prior': prior,
                        'score_space': SCORE_SPACE,
                    },
                },
                'score': base_score + prior,
            }
        )

    # Cap the graph contribution at ``budget``, and size the merged output to
    # k + (however many graph chunks actually made it).
    #
    # Merging at plain ``k`` is the tempting version and it is wrong: graph chunks
    # that outscore base chunks then *evict* them, so the result becomes "replace
    # recall" rather than "expand recall". Measured on the 4-file test KB, the
    # evicting version dropped distinct source files from 3 to 2 while adding four
    # graph hits -- a net loss disguised as a feature.
    #
    # Sizing at ``k + len(graph_items)`` keeps every base hit *in the merged list*,
    # but that is not the same as keeping it in the caller's top-k, and the
    # difference is not academic. With ``mode='rerank'`` the graph items are sorted
    # *into* the base order, so they can still push base hits past position k for
    # any caller that slices to k -- which is the original eviction, one layer up.
    # Re-measured on the demo KB (16 questions, K=3, file-level recall, ground
    # truth machine-verified): rerank mode scored -0.073 overall / -0.146 on the
    # cross-document subset, i.e. worse than not expanding at all; append mode
    # scored exactly 0.000 with the same graph_added=2 per query. Hence 'append'
    # as the default -- expansion is additive there *for the consumer too*.
    graph_items = sorted(graph_items, key=item_score, reverse=True)[: max(1, budget)]
    merged = merge_graph_candidates(base_items, graph_items, k=k + len(graph_items), mode=merge_mode)
    log.info(
        f'graph_retrieval:expanded kb={knowledge_id} base={len(base_items)} '
        f'added={len(graph_items)} merged={len(merged)} hops={hops} mode={merge_mode}'
    )
    return items_to_result(merged)
