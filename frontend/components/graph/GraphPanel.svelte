<script lang="ts">
	import { onMount, onDestroy, getContext } from 'svelte';
	import type { Writable } from 'svelte/store';
	import type { i18n as i18nType } from 'i18next';
	import { page } from '$app/stores';
	import { writable } from 'svelte/store';
	import { toast } from 'svelte-sonner';

	const i18n = getContext<Writable<i18nType>>('i18n');
	import { user } from '$lib/stores';
	import {
		buildKnowledgeGraph,
		cancelKnowledgeGraphBuild,
		getKnowledgeGraph,
		getKnowledgeGraphBuildStatus,
		getKnowledgeGraphEntity,
		getKnowledgeGraphStats,
		searchKnowledgeGraph
	} from '$lib/apis/knowledge';

	import Spinner from '$lib/components/common/Spinner.svelte';
	import GraphCanvas from './GraphCanvas.svelte';

	const ENTITY_TYPES = ['人物', '组织', '产品', '技术', '指标', '法规', '地点', '其他'];
	const TERMINAL_STATUSES = ['completed', 'partial', 'failed', 'cancelled'];

	let knowledgeId = '';
	let loadingGraph = false;
	let loadingStats = false;

	let stats: any = null;
	let rawNodes: any[] = [];
	let rawEdges: any[] = [];
	let truncated = false;

	let activeTypes: Set<string> = new Set(ENTITY_TYPES);
	let minWeight = 1;

	let selectedNodeId: string | null = null;
	let entityDetail: any = null;
	let loadingEntity = false;

	let building = false;
	let buildTask: any = null;
	let pollTimer: any = null;

	let query = '';
	let searchK = 3;
	let searching = false;
	let comparison: any = null;

	// @xyflow/svelte 0.1.x takes nodes/edges as writable stores, not plain arrays.
	const nodes = writable<any[]>([]);
	const edges = writable<any[]>([]);

	$: if ($page.params.id) {
		knowledgeId = $page.params.id;
	}

	$: if (rawNodes.length || rawEdges.length) {
		relayout();
	}

	const token = () => $user?.token ?? '';

	//////////////////////////////////
	// Data loading
	//////////////////////////////////

	const loadStats = async () => {
		if (!knowledgeId) return;
		loadingStats = true;
		try {
			stats = await getKnowledgeGraphStats(token(), knowledgeId);
		} catch (e: any) {
			stats = null;
		} finally {
			loadingStats = false;
		}
	};

	const loadGraph = async () => {
		if (!knowledgeId) return;
		loadingGraph = true;
		try {
			const res = await getKnowledgeGraph(token(), knowledgeId, minWeight);
			rawNodes = res?.nodes ?? [];
			rawEdges = res?.edges ?? [];
			truncated = res?.truncated ?? false;
		} catch (e: any) {
			toast.error(e?.detail ?? '加载图谱失败');
		} finally {
			loadingGraph = false;
		}
	};

	//////////////////////////////////
	// Layout
	//
	// BFS layering rather than a force simulation: no new dependency, and the
	// result is deterministic, which matters when the same KB is demoed twice.
	// Level 0 is the highest-degree node -- the graph's de facto centre -- and
	// each level is spread evenly around x=0.
	//////////////////////////////////

	const relayout = () => {
		const visible = rawNodes.filter((n) => activeTypes.has(n.entity_type ?? '其他'));
		const visibleIds = new Set(visible.map((n) => n.id));

		const adjacency = new Map<string, Set<string>>();
		visible.forEach((n) => adjacency.set(n.id, new Set()));
		rawEdges.forEach((e) => {
			if (!visibleIds.has(e.source) || !visibleIds.has(e.target)) return;
			adjacency.get(e.source)?.add(e.target);
			adjacency.get(e.target)?.add(e.source);
		});

		const degreeOf = (id: string) => adjacency.get(id)?.size ?? 0;
		const levels = new Map<string, number>();

		// BFS from each component's highest-degree node. Isolated clusters are laid
		// out after the previous component rather than on top of it, so a KB with
		// several disconnected entity clusters stays readable.
		const byDegreeDesc = [...visible].sort((a, b) => degreeOf(b.id) - degreeOf(a.id));
		let nextLevelOffset = 0;
		for (const seed of byDegreeDesc) {
			if (levels.has(seed.id)) continue;

			levels.set(seed.id, nextLevelOffset);
			const queue = [seed.id];
			let maxLevel = nextLevelOffset;
			while (queue.length) {
				const current = queue.shift() as string;
				const currentLevel = levels.get(current) ?? 0;
				[...(adjacency.get(current) ?? [])]
					.sort((a, b) => degreeOf(b) - degreeOf(a))
					.forEach((next) => {
						if (levels.has(next)) return;
						levels.set(next, currentLevel + 1);
						maxLevel = Math.max(maxLevel, currentLevel + 1);
						queue.push(next);
					});
			}
			nextLevelOffset = maxLevel + 1;
		}

		const perLevel = new Map<number, string[]>();
		visible.forEach((n) => {
			const level = levels.get(n.id) ?? 0;
			if (!perLevel.has(level)) perLevel.set(level, []);
			perLevel.get(level)!.push(n.id);
		});

		const positions = new Map<string, { x: number; y: number }>();
		[...perLevel.keys()]
			.sort((a, b) => a - b)
			.forEach((level) => {
				const ids = perLevel.get(level)!.sort((a, b) => degreeOf(b) - degreeOf(a));
				const gap = 240;
				ids.forEach((id, index) => {
					positions.set(id, {
						x: (index - (ids.length - 1) / 2) * gap,
						y: level * 150
					});
				});
			});

		nodes.set(
			visible.map((n) => ({
				id: n.id,
				type: 'entity',
				data: { ...n, selected: n.id === selectedNodeId },
				position: positions.get(n.id) ?? { x: 0, y: 0 }
			}))
		);

		edges.set(
			rawEdges
				.filter((e) => visibleIds.has(e.source) && visibleIds.has(e.target))
				.map((e) => ({
					id: e.id,
					source: e.source,
					target: e.target,
					selectable: false,
					animated: e.is_cross_doc,
					label: e.weight > 1 ? String(e.weight) : undefined,
					class: e.is_cross_doc ? 'stroke-emerald-500' : 'stroke-gray-300 dark:stroke-gray-600',
					type: 'smoothstep'
				}))
		);
	};

	const toggleType = (type: string) => {
		const next = new Set(activeTypes);
		if (next.has(type)) next.delete(type);
		else next.add(type);
		activeTypes = next;
	};

	const applyMinWeight = () => {
		loadGraph();
	};

	//////////////////////////////////
	// Entity drawer
	//////////////////////////////////

	const onNodeClick = async (detail: any) => {
		const id = detail?.node?.id;
		if (!id) return;
		selectedNodeId = id;
		relayout();
		loadingEntity = true;
		entityDetail = null;
		try {
			entityDetail = await getKnowledgeGraphEntity(token(), knowledgeId, id);
		} catch (e: any) {
			toast.error(e?.detail ?? '加载实体详情失败');
		} finally {
			loadingEntity = false;
		}
	};

	const focusNeighbour = (id: string) => {
		onNodeClick({ node: { id } });
	};

	//////////////////////////////////
	// Build
	//////////////////////////////////

	const startPolling = () => {
		stopPolling();
		pollTimer = setInterval(async () => {
			const status = await getKnowledgeGraphBuildStatus(token(), knowledgeId).catch(() => null);
			buildTask = status;
			if (status && TERMINAL_STATUSES.includes(status.status)) {
				stopPolling();
				building = false;
				await loadStats();
				await loadGraph();
				toast.success(`建图结束：${status.status}`);
			}
		}, 2000);
	};

	const stopPolling = () => {
		if (pollTimer) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	};

	const rebuild = async (force: boolean) => {
		building = true;
		try {
			const res = await buildKnowledgeGraph(token(), knowledgeId, { force });
			buildTask = { id: res?.task_id, status: 'pending', processed_chunks: 0, total_chunks: 0 };
			startPolling();
		} catch (e: any) {
			building = false;
			toast.error(e?.detail ?? e ?? '启动建图失败');
		}
	};

	const cancelBuild = async () => {
		try {
			await cancelKnowledgeGraphBuild(token(), knowledgeId);
			toast.info('已请求取消');
		} catch (e: any) {
			toast.error(e?.detail ?? '取消失败');
		}
	};

	const buildProgress = () => {
		const total = buildTask?.total_chunks ?? 0;
		const done = buildTask?.processed_chunks ?? 0;
		if (!total) return 0;
		return Math.min(100, Math.round((done / total) * 100));
	};

	//////////////////////////////////
	// A/B comparison
	//////////////////////////////////

	const runComparison = async () => {
		if (!query.trim()) return;
		searching = true;
		comparison = null;
		try {
			comparison = await searchKnowledgeGraph(token(), knowledgeId, { query, k: searchK });
		} catch (e: any) {
			toast.error(e?.detail ?? '检索对比失败');
		} finally {
			searching = false;
		}
	};

	const isGraphOnly = (item: any) => item?.from === 'graph';

	// Read straight off the server's stats rather than recomputing from the two
	// item lists -- `new_files` is file ids, so it counts files, not chunks.
	const newFileCount = () => comparison?.stats?.new_files?.length ?? 0;

	onMount(() => {
		loadStats();
		loadGraph();
	});

	onDestroy(() => {
		stopPolling();
		nodes.set([]);
		edges.set([]);
	});
</script>

<div class="flex flex-col h-full overflow-hidden">
	<!-- Header: stats + readiness -->
	<div class="px-4 py-3 border-b border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-950">
		<div class="flex items-center justify-between gap-4">
			<div class="flex items-center gap-3">
				<h2 class="text-base font-semibold text-gray-800 dark:text-gray-100">🕸️ 知识图谱</h2>
				{#if stats}
					<span class="text-xs text-gray-500">
						覆盖率 {stats.coverage_pct}% · 已建 {stats.built_chunks}/{stats.total_chunks} chunk
					</span>
				{/if}
			</div>
			<div class="flex items-center gap-2">
				{#if building}
					<button
						on:click={cancelBuild}
						class="px-3 py-1.5 text-xs rounded-lg border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800"
					>
						取消
					</button>
				{:else}
					<button
						on:click={() => rebuild(false)}
						class="px-3 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700"
					>
						🔄 重建图谱
					</button>
					<button
						on:click={() => rebuild(true)}
						class="px-3 py-1.5 text-xs rounded-lg border border-gray-300 dark:border-gray-600 hover:bg-gray-100 dark:hover:bg-gray-800"
						title="忽略增量台账，全部 chunk 重新抽取（消耗更多 LLM 额度）"
					>
						全量重抽
					</button>
				{/if}
			</div>
		</div>

		{#if building && buildTask}
			<div class="mt-2">
				<div class="flex justify-between text-[11px] text-gray-500 mb-1">
					<span>建图进行中（{buildTask.status}）</span>
					<span>{buildTask.processed_chunks ?? 0}/{buildTask.total_chunks ?? 0}</span>
				</div>
				<div class="w-full h-1.5 bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
					<div class="h-full bg-blue-600 transition-all" style="width: {buildProgress()}%"></div>
				</div>
			</div>
		{/if}

		<!-- The banner the whole readiness API exists for: switched on but unusable
		     is otherwise completely invisible -- no error, just no effect. -->
		{#if stats && stats.retrieval_ready === false}
			<div
				class="mt-2 px-3 py-2 rounded-lg text-xs bg-amber-50 dark:bg-amber-900/30 text-amber-800 dark:text-amber-200 border border-amber-200 dark:border-amber-800"
			>
				⚠️ 聊天检索不会启用图谱扩展：{stats.retrieval_ready_reason}
				<span class="opacity-75">（下方「对比检索」会强制开启，仅用于对照）</span>
			</div>
		{/if}

		{#if stats && stats.stale}
			<div class="mt-2 text-xs text-gray-500">
				语料有变化（待抽取 {stats.pending_chunks}、已失效 {stats.orphan_chunks}），建议重建。
			</div>
		{/if}

		<!-- Stat cards -->
		{#if stats}
			<div class="mt-3 grid grid-cols-2 md:grid-cols-4 gap-2">
				<div class="px-3 py-2 rounded-lg bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700">
					<div class="text-[11px] text-gray-500">实体</div>
					<div class="text-lg font-semibold text-gray-800 dark:text-gray-100">{stats.entity_count}</div>
					<div class="text-[10px] text-gray-400">别名 {stats.alias_count}</div>
				</div>
				<div class="px-3 py-2 rounded-lg bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700">
					<div class="text-[11px] text-gray-500">关系</div>
					<div class="text-lg font-semibold text-gray-800 dark:text-gray-100">{stats.edge_count}</div>
					<div class="text-[10px] text-gray-400">跨文档 {stats.cross_doc_edge_count}</div>
				</div>
				<div class="px-3 py-2 rounded-lg bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700">
					<div class="text-[11px] text-gray-500">覆盖率</div>
					<div class="text-lg font-semibold text-gray-800 dark:text-gray-100">{stats.coverage_pct}%</div>
					<div class="text-[10px] text-gray-400">待抽取 {stats.pending_chunks}</div>
				</div>
				<div class="px-3 py-2 rounded-lg bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700">
					<div class="text-[11px] text-gray-500">检索就绪</div>
					<div
						class="text-lg font-semibold {stats.retrieval_ready
							? 'text-emerald-600'
							: 'text-amber-600'}"
					>
						{stats.retrieval_ready ? '就绪' : '未就绪'}
					</div>
					<div class="text-[10px] text-gray-400">{stats.last_status ?? '未建图'}</div>
				</div>
			</div>
		{/if}
	</div>

	<!-- Filters -->
	<div class="px-4 py-2 border-b border-gray-200 dark:border-gray-700 flex flex-wrap items-center gap-2">
		{#each ENTITY_TYPES as type}
			<button
				on:click={() => toggleType(type)}
				class="px-2 py-0.5 text-[11px] rounded-full border transition {activeTypes.has(type)
					? 'bg-blue-50 dark:bg-blue-900/40 border-blue-300 dark:border-blue-700 text-blue-700 dark:text-blue-300'
					: 'border-gray-200 dark:border-gray-700 text-gray-400'}"
			>
				{type}
			</button>
		{/each}
		<div class="flex items-center gap-2 ml-auto">
			<span class="text-[11px] text-gray-500">最小权重 {minWeight}</span>
			<input
				type="range"
				min="1"
				max="10"
				bind:value={minWeight}
				on:change={applyMinWeight}
				class="w-24"
			/>
			{#if truncated}
				<span class="text-[11px] text-amber-600">节点过多，已截断显示</span>
			{/if}
		</div>
	</div>

	<!-- Canvas + drawer -->
	<div class="flex-1 flex overflow-hidden">
		<div class="flex-1 relative bg-gray-50 dark:bg-gray-950">
			{#if loadingGraph}
				<div class="absolute inset-0 flex items-center justify-center z-10">
					<Spinner />
				</div>
			{/if}
			{#if rawNodes.length === 0 && !loadingGraph}
				<div class="absolute inset-0 flex items-center justify-center text-sm text-gray-400">
					暂无图谱数据，先点「重建图谱」
				</div>
			{/if}
			<GraphCanvas {nodes} {edges} on:nodeclick={(e) => onNodeClick(e.detail)} />
		</div>

		{#if selectedNodeId}
			<div
				class="w-[380px] flex-shrink-0 border-l border-gray-200 dark:border-gray-700 overflow-y-auto p-4 bg-white dark:bg-gray-900"
			>
				<div class="flex items-start justify-between">
					<h3 class="text-sm font-semibold text-gray-800 dark:text-gray-100">实体详情</h3>
					<button
						on:click={() => {
							selectedNodeId = null;
							entityDetail = null;
							relayout();
						}}
						class="text-gray-400 hover:text-gray-600 text-xs"
					>
						关闭
					</button>
				</div>

				{#if loadingEntity}
					<div class="mt-4 flex justify-center"><Spinner /></div>
				{:else if entityDetail}
					<div class="mt-3">
						<div class="text-base font-medium text-gray-800 dark:text-gray-100">
							{entityDetail.entity?.name}
						</div>
						<div class="mt-1 flex flex-wrap items-center gap-2 text-[11px] text-gray-500">
							<span class="px-1.5 py-0.5 rounded-full bg-gray-100 dark:bg-gray-800">
								{entityDetail.entity?.entity_type}
							</span>
							<span>度 {entityDetail.entity?.degree}</span>
							<span>提及 {entityDetail.entity?.mention_count}</span>
						</div>
						{#if entityDetail.entity?.aliases?.length}
							<div class="mt-2 text-[11px] text-gray-500">
								别名：{entityDetail.entity.aliases.join('、')}
							</div>
						{/if}
						{#if entityDetail.entity?.description}
							<div class="mt-2 text-xs text-gray-600 dark:text-gray-400">
								{entityDetail.entity.description}
							</div>
						{/if}
					</div>

					<div class="mt-4">
						<div class="text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">
							关联 chunk（{entityDetail.chunks?.length ?? 0}）
						</div>
						<div class="space-y-2">
							{#each entityDetail.chunks ?? [] as chunk}
								<div
									class="px-2 py-2 rounded-lg bg-gray-50 dark:bg-gray-800 text-[11px] text-gray-600 dark:text-gray-300"
								>
									<div class="text-[10px] text-gray-400 mb-1">
										{chunk.file_name || '未知文件'}{chunk.chunk_index !== null && chunk.chunk_index !== undefined
											? ` · #${chunk.chunk_index}`
											: ''}
									</div>
									{chunk.text}
								</div>
							{/each}
						</div>
					</div>

					<div class="mt-4">
						<div class="text-xs font-medium text-gray-600 dark:text-gray-300 mb-1">
							邻居关系（{entityDetail.relations?.length ?? 0}）
						</div>
						<div class="space-y-1">
							{#each entityDetail.relations ?? [] as rel}
								<button
									on:click={() => focusNeighbour(rel.other_id)}
									class="w-full text-left px-2 py-1 rounded text-[11px] hover:bg-gray-100 dark:hover:bg-gray-800 text-gray-600 dark:text-gray-300"
								>
									<span class="text-gray-400">{rel.direction === 'out' ? '→' : '←'}</span>
									<span class="text-blue-600 dark:text-blue-400">{rel.relation}</span>
									<span class="text-gray-400">{rel.direction === 'out' ? '→' : '←'}</span>
									{rel.other_name}
									{#if rel.is_cross_doc}
										<span class="ml-1 text-[10px] text-emerald-600">跨文档</span>
									{/if}
								</button>
							{/each}
						</div>
					</div>
				{/if}
			</div>
		{/if}
	</div>

	<!-- Retrieval A/B -->
	<div class="border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-950">
		<div class="px-4 py-2 flex items-center gap-2">
			<input
				class="flex-1 px-3 py-1.5 text-xs rounded-lg border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900"
				placeholder="输入问题，对比「基础混合检索」与「+图谱扩展」的召回差异"
				bind:value={query}
				on:keydown={(e) => e.key === 'Enter' && runComparison()}
			/>
			<label class="text-[11px] text-gray-500 flex items-center gap-1">
				k
				<input
					type="number"
					min="1"
					max="20"
					bind:value={searchK}
					class="w-12 px-1 py-1 text-xs rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-900"
				/>
			</label>
			<button
				on:click={runComparison}
				disabled={searching}
				class="px-3 py-1.5 text-xs rounded-lg bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50"
			>
				{searching ? '检索中…' : '对比检索'}
			</button>
		</div>

		{#if comparison}
			<div class="px-4 pb-3 grid grid-cols-2 gap-3">
				{#each [{ key: 'base', label: '基础混合检索' }, { key: 'graph', label: '+ 图谱扩展' }] as arm}
					{@const data = comparison[arm.key]}
					<div>
						<div class="flex items-center justify-between text-[11px] text-gray-500 mb-1">
							<span class="font-medium">{arm.label}</span>
							<span>
								命中 {data?.hits ?? 0} · 文件 {data?.distinct_files ?? 0}
								{#if arm.key === 'graph' && (data?.from_graph ?? 0) > 0}
									<span class="text-emerald-600">· 图谱贡献 {data.from_graph}</span>
								{/if}
							</span>
						</div>
						<div class="space-y-1">
							{#each data?.items ?? [] as item}
								<div
									class="px-2 py-1.5 rounded text-[11px] {isGraphOnly(item)
										? 'bg-emerald-50 dark:bg-emerald-900/30 border border-emerald-200 dark:border-emerald-800'
										: 'bg-white dark:bg-gray-900 border border-gray-200 dark:border-gray-700'}"
								>
									<div class="flex items-center justify-between text-[10px] text-gray-400">
										<span>{item.name ?? '未知文件'}</span>
										<span>
											{#if isGraphOnly(item)}
												<span class="text-emerald-600 font-medium">图谱召回</span>
											{/if}
											{item.score !== null && item.score !== undefined
												? ` ${Number(item.score).toFixed(3)}`
												: ''}
										</span>
									</div>
									<div class="text-gray-600 dark:text-gray-300 line-clamp-2">{item.text}</div>
								</div>
							{/each}
						</div>
					</div>
				{/each}

				<div class="col-span-2 text-[11px] text-gray-500">
					图谱新增文件：{newFileCount()} 个
					{#if newFileCount() > 0}
						<span class="text-emerald-600">（跨文档召回提升）</span>
					{/if}
				</div>
			</div>
		{/if}
	</div>
</div>
