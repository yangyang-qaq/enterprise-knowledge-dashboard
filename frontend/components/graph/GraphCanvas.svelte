<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import { theme } from '$lib/stores';
	import { Background, Controls, SvelteFlow, BackgroundVariant } from '@xyflow/svelte';
	import '@xyflow/svelte/dist/style.css';

	import EntityNode from './EntityNode.svelte';

	const dispatch = createEventDispatcher();

	// Must be a stable script-level reference. Rebuilding this object inside a
	// reactive statement remounts every node component on each redraw and
	// silently kills all interaction.
	const nodeTypes = { entity: EntityNode };

	export let nodes;
	export let edges;
</script>

<SvelteFlow
	{nodes}
	{nodeTypes}
	{edges}
	fitView
	minZoom={0.05}
	maxZoom={4}
	colorMode={$theme.includes('dark')
		? 'dark'
		: $theme === 'system'
			? window.matchMedia('(prefers-color-scheme: dark)').matches
				? 'dark'
				: 'light'
			: 'light'}
	nodesConnectable={false}
	on:nodeclick={(e) => dispatch('nodeclick', e.detail)}
>
	<Controls showLock={false} />
	<Background variant={BackgroundVariant.Dots} />
</SvelteFlow>
