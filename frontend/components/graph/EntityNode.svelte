<script lang="ts">
	import { Handle, Position, type NodeProps } from '@xyflow/svelte';

	// NodeProps['data'] is `unknown`, so the fields this component reads are
	// declared here rather than indexed off it. Not exported: a bare `export type`
	// in an instance script is parsed as an export modifier and fails to compile.
	type EntityNodeData = {
		id: string;
		name?: string;
		entity_type?: string;
		degree?: number;
		mention_count?: number;
		selected?: boolean;
	};

	type $$Props = NodeProps & { data: EntityNodeData };
	export let data: EntityNodeData;

	const OTHER_TYPE = '其他';

	const TYPE_STYLES: Record<string, string> = {
		人物: 'bg-rose-100 text-rose-700 dark:bg-rose-900/40 dark:text-rose-300',
		组织: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300',
		产品: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
		技术: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300',
		指标: 'bg-violet-100 text-violet-700 dark:bg-violet-900/40 dark:text-violet-300',
		法规: 'bg-slate-200 text-slate-700 dark:bg-slate-700/50 dark:text-slate-300',
		地点: 'bg-teal-100 text-teal-700 dark:bg-teal-900/40 dark:text-teal-300',
		其他: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400'
	};

	$: typeClass = TYPE_STYLES[data?.entity_type ?? OTHER_TYPE] ?? TYPE_STYLES[OTHER_TYPE];
	$: degree = data?.degree ?? 0;
	// Degree drives the border: a hub node should read as a hub at a glance, since
	// it is exactly the node the traversal deliberately refuses to expand from.
	$: weightClass =
		degree >= 20
			? 'border-red-400 dark:border-red-500'
			: degree >= 10
				? 'border-blue-400 dark:border-blue-500'
				: 'border-gray-200 dark:border-gray-700';
</script>

<div
	class="px-3 py-2 shadow-sm rounded-xl dark:bg-gray-900 bg-white border-2 {weightClass} w-44 {data?.selected
		? 'ring-2 ring-blue-500'
		: ''}"
>
	<div class="text-xs font-medium text-black dark:text-white line-clamp-2 leading-snug">
		{data?.name ?? ''}
	</div>
	<div class="mt-1 flex items-center gap-1">
		<span class="text-[10px] px-1.5 py-0.5 rounded-full {typeClass}">{data?.entity_type ?? '其他'}</span>
		<span class="text-[10px] text-gray-400">度 {degree}</span>
		{#if (data?.mention_count ?? 0) > 1}
			<span class="text-[10px] text-gray-400">· 提及 {data.mention_count}</span>
		{/if}
	</div>
	<Handle type="target" position={Position.Top} class="w-2 rounded-full dark:bg-gray-900" />
	<Handle type="source" position={Position.Bottom} class="w-2 rounded-full dark:bg-gray-900" />
</div>
