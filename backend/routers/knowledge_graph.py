"""Phase 12 knowledge-graph API.

Mounted at ``/{id}/graph/*`` rather than ``/graph/{id}`` on purpose. Starlette
matches routes in registration order, and ``routers/knowledge.py`` already owns
``GET /{id}`` -- so a ``/graph/{id}`` shape would be captured by that route first
and hand the literal string ``"graph"`` to ``id``. Even mounted first, keeping
the KB id in the same position as every other knowledge route avoids the whole
class of bug.

Two things here are easy to get wrong and are therefore explicit rather than
inferred:

* **Permission.** ``/{id}/graph/*`` is checked exactly like ``GET /{id}``.
  The graph leaks file names, extracted relations and entity names, so it is a
  fresh information-disclosure surface -- checking existence alone is not enough.
* **Build is asynchronous.** A build over a few dozen files is tens of minutes;
  a blocking request would always time out. ``POST /build`` returns a task id
  immediately and progress is read from the task row.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func
from sqlalchemy import select as sa_select

from open_webui.config import ENABLE_RAG_HYBRID_SEARCH, GRAPH_EXTRACT_CONCURRENCY, GRAPH_EXTRACT_MODEL
from open_webui.constants import ERROR_MESSAGES
from open_webui.internal.db import get_async_session
from open_webui.models.access_grants import AccessGrants
from open_webui.models.config import Config
from open_webui.models.knowledge import (
    KnowledgeGraphBuildForm,
    KnowledgeGraphChunkEntity,
    KnowledgeGraphEdge,
    KnowledgeGraphEntity,
    KnowledgeGraphExtractionLog,
    KnowledgeGraphExtractionTask,
    KnowledgeGraphExtractionTaskModel,
    KnowledgeGraphSearchForm,
    Knowledges,
)
from open_webui.retrieval.graph_retrieval import (
    GRAPH_META_KEY,
    fetch_chunk_texts,
    graph_retrieval_readiness,
    resolve_file_names,
)
from open_webui.retrieval.knowledge_graph import (
    build_graph_for_knowledge,
    create_build_task,
    fetch_kb_chunks,
)
from open_webui.retrieval.utils import query_collection
from open_webui.utils.auth import get_verified_user

log = logging.getLogger(__name__)

router = APIRouter()

# One in-flight build per knowledge base. Keyed by knowledge_id, and the value is
# the asyncio.Task so /cancel has something to cancel.
_RUNNING_BUILDS: dict[str, asyncio.Task] = {}

# Cap on nodes returned by GET /graph. The canvas cannot usefully render more,
# and an uncapped response for a large KB is a multi-megabyte payload.
MAX_GRAPH_NODES = 400
MAX_GRAPH_EDGES = 1200


async def _require_knowledge(
    knowledge_id: str,
    user,
    db,
    permission: str = 'read',
):
    """404 unless the user can *use* this knowledge base.

    Mirrors the check in ``routers/knowledge.py`` deliberately, including
    answering 404 rather than 403 -- not leaking the existence of a KB the
    caller may not see.
    """
    knowledge = await Knowledges.get_knowledge_by_id(id=knowledge_id, db=db)
    if not knowledge:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    allowed = (
        user.role == 'admin'
        or knowledge.user_id == user.id
        or await AccessGrants.has_access(
            user_id=user.id,
            resource_type='knowledge',
            resource_id=knowledge_id,
            permission=permission,
            db=db,
        )
    )
    if not allowed:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)
    return knowledge


async def _readiness(request: Request) -> tuple[bool, str]:
    """Resolve the score-space guard against live config rather than env constants.

    ``ENABLE_RAG_HYBRID_SEARCH`` is imported only as the fallback: the value that
    actually governs retrieval is the runtime config row, and reporting a stale
    env default here would produce a readiness banner that contradicts behaviour.
    """
    values = await Config.get_many('rag.enable_hybrid_search', 'rag.enable_graph_retrieval')
    hybrid_enabled = values.get('rag.enable_hybrid_search')
    graph_enabled = values.get('rag.enable_graph_retrieval')
    if hybrid_enabled is None:
        hybrid_enabled = ENABLE_RAG_HYBRID_SEARCH
    if graph_enabled is None:
        graph_enabled = False

    reranking_function = getattr(request.app.state, 'RERANKING_FUNCTION', None)
    return graph_retrieval_readiness(reranking_function, bool(hybrid_enabled), bool(graph_enabled))


####################
# Build
####################


@router.post('/{id}/graph/build')
async def build_graph(
    request: Request,
    id: str,
    form_data: KnowledgeGraphBuildForm,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    """Start an extraction build and return immediately.

    Deliberately does *not* report ``total_chunks``/``todo_chunks`` up front the
    way a synchronous design would: computing them means pulling the entire
    collection out of Chroma, which the build has to do anyway moments later.
    Poll ``/build/status`` and the numbers appear as the build fills them in.
    """
    # Write permission, not read: a build spends LLM budget and rewrites graph state.
    await _require_knowledge(id, user, db, 'write')

    running = _RUNNING_BUILDS.get(id)
    if running is not None and not running.done():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='该知识库已有建图任务在运行中')

    concurrency = form_data.concurrency or GRAPH_EXTRACT_CONCURRENCY
    model = GRAPH_EXTRACT_MODEL or None
    task_id = await create_build_task(id, concurrency, model)

    task = asyncio.create_task(
        build_graph_for_knowledge(
            knowledge_id=id,
            force=bool(form_data.force),
            concurrency=concurrency,
            limit=form_data.limit,
            model=model,
            task_id=task_id,
        )
    )
    _RUNNING_BUILDS[id] = task
    task.add_done_callback(lambda _t: _RUNNING_BUILDS.pop(id, None))

    return {'task_id': task_id, 'status': 'pending'}


@router.get('/{id}/graph/build/status', response_model=KnowledgeGraphExtractionTaskModel | None)
async def get_build_status(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    """Latest build job for this KB. Cheap: reads one row, touches Chroma never.

    ``stale`` is *not* reported here even though it is the more interesting flag
    -- deciding it requires re-reading the whole collection, which must not
    happen on a fast poll loop. ``/graph/stats`` reports it instead.
    """
    await _require_knowledge(id, user, db)

    row = (
        await db.execute(
            sa_select(KnowledgeGraphExtractionTask)
            .where(KnowledgeGraphExtractionTask.knowledge_id == id)
            .order_by(KnowledgeGraphExtractionTask.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if row is None:
        return None
    return KnowledgeGraphExtractionTaskModel.model_validate(row)


@router.post('/{id}/graph/build/cancel')
async def cancel_build(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    await _require_knowledge(id, user, db, 'write')

    task = _RUNNING_BUILDS.get(id)
    if task is None or task.done():
        return {'cancelled': False}

    # The build catches CancelledError, marks the task row 'cancelled' and
    # re-raises, so the row is consistent by the time this returns.
    task.cancel()
    return {'cancelled': True}


@router.post('/{id}/graph/build/retry-failed')
async def retry_failed(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    """Re-run only the chunks whose ledger row says 'failed'.

    ``force`` is wrong for this: it would redo the whole KB, which costs real
    money. Deleting the failed ledger rows puts exactly those chunks back into
    the incremental diff, and chunks that succeeded stay untouched.
    """
    await _require_knowledge(id, user, db, 'write')

    running = _RUNNING_BUILDS.get(id)
    if running is not None and not running.done():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='该知识库已有建图任务在运行中')

    result = await db.execute(
        KnowledgeGraphExtractionLog.__table__.delete().where(
            KnowledgeGraphExtractionLog.knowledge_id == id,
            KnowledgeGraphExtractionLog.status == 'failed',
        )
    )
    await db.commit()
    retried = result.rowcount or 0

    if not retried:
        return {'task_id': None, 'retried_chunks': 0}

    concurrency = GRAPH_EXTRACT_CONCURRENCY
    model = GRAPH_EXTRACT_MODEL or None
    task_id = await create_build_task(id, concurrency, model)
    task = asyncio.create_task(
        build_graph_for_knowledge(
            knowledge_id=id,
            force=False,
            concurrency=concurrency,
            model=model,
            task_id=task_id,
        )
    )
    _RUNNING_BUILDS[id] = task
    task.add_done_callback(lambda _t: _RUNNING_BUILDS.pop(id, None))

    return {'task_id': task_id, 'retried_chunks': retried}


@router.get('/{id}/graph/build/stream')
async def stream_build_progress(
    request: Request,
    id: str,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    """SSE build progress. Polling ``/build/status`` stays the fallback.

    Mirrors ``/{id}/progress/stream``: reuse the request's session and commit
    between iterations, plus an inactivity timeout. Rows are only emitted when
    they change, so a quiet build doesn't spam the client once a second.
    """
    await _require_knowledge(id, user, db)

    SSE = chr(10) + chr(10)

    async def event_generator():
        last = None
        last_active = time.time()
        while True:
            row = (
                await db.execute(
                    sa_select(KnowledgeGraphExtractionTask)
                    .where(KnowledgeGraphExtractionTask.knowledge_id == id)
                    .order_by(KnowledgeGraphExtractionTask.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            payload = KnowledgeGraphExtractionTaskModel.model_validate(row).model_dump() if row else None

            if payload != last:
                last = payload
                last_active = time.time()
                yield 'data: ' + json.dumps(payload) + SSE

            if payload and payload.get('status') in ('completed', 'failed', 'cancelled', 'partial'):
                break

            if time.time() - last_active > 300:
                break

            await asyncio.sleep(1)
            # Release the read transaction so the polling loop sees fresh rows.
            await db.commit()

    return StreamingResponse(event_generator(), media_type='text/event-stream')


####################
# Read
####################


@router.get('/{id}/graph/stats')
async def get_graph_stats(
    request: Request,
    id: str,
    check_stale: bool = Query(False, description='对比 Chroma 全量计算 stale（较慢，O(collection)）'),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    """Graph summary. ``retrieval_ready`` is the field the UI banner keys off.

    Reports a feature that is switched on but cannot actually run, with the
    reason. The failure mode being defended against is silent: hybrid search off
    or no reranker means the guard blocks expansion and *nothing visible
    happens*.
    """
    await _require_knowledge(id, user, db)

    entity_count = (
        await db.execute(
            sa_select(func.count(KnowledgeGraphEntity.id)).where(
                KnowledgeGraphEntity.knowledge_id == id,
                KnowledgeGraphEntity.canonical_id.is_(None),
            )
        )
    ).scalar_one()

    alias_count = (
        await db.execute(
            sa_select(func.count(KnowledgeGraphEntity.id)).where(
                KnowledgeGraphEntity.knowledge_id == id,
                KnowledgeGraphEntity.canonical_id.is_not(None),
            )
        )
    ).scalar_one()

    edge_count, cross_doc_count = (
        await db.execute(
            sa_select(
                func.count(KnowledgeGraphEdge.id),
                func.sum(KnowledgeGraphEdge.is_cross_doc),
            ).where(KnowledgeGraphEdge.knowledge_id == id)
        )
    ).one()

    relation_rows = (
        await db.execute(
            sa_select(KnowledgeGraphEdge.relation_key, func.count(KnowledgeGraphEdge.id))
            .where(KnowledgeGraphEdge.knowledge_id == id)
            .group_by(KnowledgeGraphEdge.relation_key)
        )
    ).all()

    covered_chunks = (
        await db.execute(
            sa_select(func.count(func.distinct(KnowledgeGraphChunkEntity.chunk_hash))).where(
                KnowledgeGraphChunkEntity.knowledge_id == id
            )
        )
    ).scalar_one()

    built_rows = (
        await db.execute(
            sa_select(func.count(KnowledgeGraphExtractionLog.id)).where(
                KnowledgeGraphExtractionLog.knowledge_id == id,
                KnowledgeGraphExtractionLog.status == 'ok',
            )
        )
    ).scalar_one()

    last_task = (
        await db.execute(
            sa_select(KnowledgeGraphExtractionTask)
            .where(KnowledgeGraphExtractionTask.knowledge_id == id)
            .order_by(KnowledgeGraphExtractionTask.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    total_chunks = last_task.total_chunks if last_task else None
    coverage_pct = round(covered_chunks / total_chunks * 100, 1) if total_chunks else None

    pending_chunks = None
    orphan_chunks = None
    stale = None
    if check_stale:
        # O(collection) on purpose: this is the only way to answer "is the graph
        # still in sync with the vectors" without write-path hooks, so it is
        # opt-in rather than paid on every stats read.
        current = {chunk.hash for chunk in await fetch_kb_chunks(id)}
        done_rows = (
            await db.execute(
                sa_select(KnowledgeGraphExtractionLog.chunk_hash).where(
                    KnowledgeGraphExtractionLog.knowledge_id == id,
                    KnowledgeGraphExtractionLog.status == 'ok',
                )
            )
        ).all()
        done = {row[0] for row in done_rows}
        pending_chunks = len(current - done)
        orphan_chunks = len(done - current)
        stale = bool(pending_chunks or orphan_chunks)

    ready, reason = await _readiness(request)

    return {
        'knowledge_id': id,
        'entity_count': entity_count,
        'alias_count': alias_count,
        'edge_count': edge_count or 0,
        'cross_doc_edge_count': int(cross_doc_count or 0),
        'relation_types': {row[0]: row[1] for row in relation_rows},
        'covered_chunks': covered_chunks,
        'built_chunks': built_rows,
        'total_chunks': total_chunks,
        'coverage_pct': coverage_pct,
        'pending_chunks': pending_chunks,
        'orphan_chunks': orphan_chunks,
        'stale': stale,
        'last_built_at': last_task.updated_at if last_task else None,
        'last_status': last_task.status if last_task else None,
        'building': bool(_RUNNING_BUILDS.get(id) and not _RUNNING_BUILDS[id].done()),
        'retrieval_ready': ready,
        'retrieval_ready_reason': reason,
    }


@router.get('/{id}/graph')
async def get_graph(
    request: Request,
    id: str,
    min_weight: int = Query(1, ge=1, description='只返回 weight >= min_weight 的边'),
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    """Nodes + edges for the canvas.

    Only canonical nodes are returned -- alias shadows would double every concept
    on screen. Nodes are capped at the highest-degree ``MAX_GRAPH_NODES`` and
    edges are restricted to the surviving node set, so the payload always
    describes a self-consistent subgraph rather than dangling references.
    """
    await _require_knowledge(id, user, db)

    entity_rows = (
        await db.execute(
            sa_select(KnowledgeGraphEntity)
            .where(
                KnowledgeGraphEntity.knowledge_id == id,
                KnowledgeGraphEntity.canonical_id.is_(None),
            )
            .order_by(KnowledgeGraphEntity.degree.desc(), KnowledgeGraphEntity.name)
            .limit(MAX_GRAPH_NODES)
        )
    ).scalars().all()

    nodes = [
        {
            'id': entity.id,
            'name': entity.name,
            'entity_type': entity.entity_type,
            'degree': entity.degree,
            'mention_count': entity.mention_count,
            'aliases': entity.aliases or [],
        }
        for entity in entity_rows
    ]
    node_ids = {node['id'] for node in nodes}

    edge_rows = (
        await db.execute(
            sa_select(KnowledgeGraphEdge)
            .where(
                KnowledgeGraphEdge.knowledge_id == id,
                KnowledgeGraphEdge.weight >= min_weight,
                KnowledgeGraphEdge.source_entity_id.in_(node_ids),
                KnowledgeGraphEdge.target_entity_id.in_(node_ids),
            )
            .order_by(KnowledgeGraphEdge.weight.desc())
            .limit(MAX_GRAPH_EDGES)
        )
    ).scalars().all()

    edges = [
        {
            'id': edge.id,
            'source': edge.source_entity_id,
            'target': edge.target_entity_id,
            'relation': edge.relation,
            'relation_key': edge.relation_key,
            'weight': edge.weight,
            'confidence': edge.confidence,
            'is_cross_doc': edge.is_cross_doc,
        }
        for edge in edge_rows
    ]

    total_entities = (
        await db.execute(
            sa_select(func.count(KnowledgeGraphEntity.id)).where(
                KnowledgeGraphEntity.knowledge_id == id,
                KnowledgeGraphEntity.canonical_id.is_(None),
            )
        )
    ).scalar_one()

    return {
        'nodes': nodes,
        'edges': edges,
        'truncated': total_entities > len(nodes),
        'stats': {'total_entities': total_entities, 'returned_nodes': len(nodes), 'returned_edges': len(edges)},
    }


@router.get('/{id}/graph/entities/{entity_id}')
async def get_entity_detail(
    request: Request,
    id: str,
    entity_id: str,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    """Entity detail: where it came from and what it connects to.

    The chunk list is the payoff of clicking a node -- it answers "which text
    actually mentions this", which is what makes the graph inspectable instead of
    decorative.
    """
    await _require_knowledge(id, user, db)

    entity = (
        await db.execute(
            sa_select(KnowledgeGraphEntity).where(
                KnowledgeGraphEntity.knowledge_id == id,
                KnowledgeGraphEntity.id == entity_id,
            )
        )
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=ERROR_MESSAGES.NOT_FOUND)

    outgoing = (
        await db.execute(
            sa_select(KnowledgeGraphEdge).where(
                KnowledgeGraphEdge.knowledge_id == id,
                KnowledgeGraphEdge.source_entity_id == entity_id,
            )
        )
    ).scalars().all()
    incoming = (
        await db.execute(
            sa_select(KnowledgeGraphEdge).where(
                KnowledgeGraphEdge.knowledge_id == id,
                KnowledgeGraphEdge.target_entity_id == entity_id,
            )
        )
    ).scalars().all()

    neighbour_ids = {edge.target_entity_id for edge in outgoing} | {edge.source_entity_id for edge in incoming}
    neighbours = (
        (
            await db.execute(
                sa_select(KnowledgeGraphEntity).where(KnowledgeGraphEntity.id.in_(neighbour_ids))
            )
        ).scalars().all()
        if neighbour_ids
        else []
    )
    neighbour_by_id = {n.id: n for n in neighbours}

    chunk_rows = (
        await db.execute(
            sa_select(
                KnowledgeGraphChunkEntity.chunk_hash,
                KnowledgeGraphChunkEntity.file_id,
                KnowledgeGraphChunkEntity.chunk_index,
            )
            .where(
                KnowledgeGraphChunkEntity.knowledge_id == id,
                KnowledgeGraphChunkEntity.entity_id == entity_id,
            )
            .limit(200)
        )
    ).all()

    names = await resolve_file_names([row[1] for row in chunk_rows])

    # Chunk text is what makes the drawer worth opening -- "which text actually
    # mentions this entity" is the question an entity node can't answer on its
    # own. Resolved through the same helper retrieval uses, which falls back to
    # Chroma because `knowledge_chunk` is empty for any KB whose Chunks tab was
    # never opened.
    texts, _missing = await fetch_chunk_texts(id, [row[0] for row in chunk_rows])

    return {
        'entity': {
            'id': entity.id,
            'name': entity.name,
            'entity_type': entity.entity_type,
            'description': entity.description,
            'aliases': entity.aliases or [],
            'mention_count': entity.mention_count,
            'degree': entity.degree,
        },
        'relations': [
            {
                'id': edge.id,
                'direction': 'out',
                'relation': edge.relation,
                'relation_key': edge.relation_key,
                'other_id': edge.target_entity_id,
                'other_name': neighbour_by_id[edge.target_entity_id].name
                if edge.target_entity_id in neighbour_by_id
                else '',
                'weight': edge.weight,
                'confidence': edge.confidence,
                'is_cross_doc': edge.is_cross_doc,
            }
            for edge in outgoing
        ]
        + [
            {
                'id': edge.id,
                'direction': 'in',
                'relation': edge.relation,
                'relation_key': edge.relation_key,
                'other_id': edge.source_entity_id,
                'other_name': neighbour_by_id[edge.source_entity_id].name
                if edge.source_entity_id in neighbour_by_id
                else '',
                'weight': edge.weight,
                'confidence': edge.confidence,
                'is_cross_doc': edge.is_cross_doc,
            }
            for edge in incoming
        ],
        'chunks': [
            {
                'chunk_hash': row[0],
                'file_id': row[1],
                'file_name': names.get(row[1], ''),
                'chunk_index': row[2],
                'text': (texts.get(row[0]) or '')[:500],
            }
            for row in chunk_rows
        ],
    }


####################
# Search (demo + eval)
####################


def _summarise_items(result: dict | None, names: dict[str, str] | None = None) -> dict:
    """Count hits and distinct source files for one retrieval arm.

    File names are resolved from ``file_id`` via the ``file`` table, *not* read
    from ``metadata['name']``. That is not defensive coding -- a single KB here
    genuinely carries two metadata schemas, one per ingestion path, and only the
    upstream one writes ``name``/``source``. Chunks written by the Phase 1/7
    chunking pipeline have neither, so trusting metadata yields a blank source
    for those and the blank is indistinguishable from "no source".
    """
    if not result:
        return {'hits': 0, 'distinct_files': 0, 'from_graph': 0, 'items': []}
    documents = (result.get('documents') or [[]])[0] or []
    metadatas = (result.get('metadatas') or [[]])[0] or []
    distances = (result.get('distances') or [[]])[0] or []
    names = names or {}

    items = []
    files: set[str] = set()
    from_graph = 0
    for idx, text in enumerate(documents):
        meta = dict(metadatas[idx] or {}) if idx < len(metadatas) else {}
        file_id = meta.get('file_id')
        if file_id:
            files.add(file_id)
        provenance = meta.get(GRAPH_META_KEY)
        if provenance:
            from_graph += 1
        items.append(
            {
                'text': (text or '')[:500],
                'name': names.get(file_id) or meta.get('name') or meta.get('source') or '',
                'file_id': file_id,
                'score': distances[idx] if idx < len(distances) else None,
                'from': 'graph' if provenance else 'base',
                'graph': provenance,
            }
        )
    return {'hits': len(items), 'distinct_files': len(files), 'from_graph': from_graph, 'items': items}


@router.post('/{id}/graph/search')
async def graph_search(
    request: Request,
    id: str,
    form_data: KnowledgeGraphSearchForm,
    user=Depends(get_verified_user),
    db=Depends(get_async_session),
):
    """Run one query with and without graph expansion, side by side.

    Both arms go through the same ``query_collection`` call with the same ``k``
    and the same reranker; the only difference is the ``use_graph`` flag. That is
    what makes ``distinct_files_base -> distinct_files_graph`` a meaningful
    number rather than an artefact of two different code paths.

    Cost is two full retrievals per call, so this is a demo/eval endpoint -- the
    chat path is untouched and expands only once.
    """
    await _require_knowledge(id, user, db)

    query = (form_data.query or '').strip()
    if not query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail='query 不能为空')

    k = form_data.k if form_data.k else 10
    embedding_function = request.app.state.EMBEDDING_FUNCTION

    common = {
        'request': request,
        'collection_names': [id],
        'queries': [query],
        'embedding_function': embedding_function,
        'k': k,
        'k_reranker': k,  # same reasoning as evaluate/query: don't truncate to top_k_reranker
        'knowledge_id': id,
    }
    try:
        base_result = await query_collection(**common, use_graph=False)
        graph_result = await query_collection(**common, use_graph=True)
    except Exception as e:
        log.exception(e)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

    def _file_ids(result: dict | None) -> list[str]:
        metas = ((result or {}).get('metadatas') or [[]])[0] or []
        return [m.get('file_id') for m in metas if m and m.get('file_id')]

    names = await resolve_file_names(_file_ids(base_result) + _file_ids(graph_result))

    base = _summarise_items(base_result, names)
    graph = _summarise_items(graph_result, names)
    ready, reason = await _readiness(request)

    return {
        'query': query,
        'k': k,
        'base': base,
        'graph': graph,
        'stats': {
            'base_hits': base['hits'],
            'graph_hits': graph['hits'],
            'graph_added': graph['from_graph'],
            'distinct_files_base': base['distinct_files'],
            'distinct_files_graph': graph['distinct_files'],
            'new_files': sorted(
                {item['file_id'] for item in graph['items'] if item['file_id']}
                - {item['file_id'] for item in base['items'] if item['file_id']}
            ),
            'retrieval_ready': ready,
            'retrieval_ready_reason': reason,
        },
    }
