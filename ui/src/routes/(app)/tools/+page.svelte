<script lang="ts">
  import { onMount } from 'svelte';
  import {
    BrainCircuit,
    ChevronDown,
    ChevronRight,
    GitBranch,
    ListChecks,
    Plus,
    Search,
    ShieldCheck,
    Trash2,
    Pencil,
    FileText,
    Terminal,
    Globe,
    Wrench,
    Server,
    BookOpen
  } from 'lucide-svelte';

  import { api } from '$lib/api/client';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { addToast } from '$lib/stores/toasts';
  import type { ExecutorConfig, ToolDefinitionSummary, Skill } from '$lib/types/api';

  type ToolsTab = 'registry' | 'skills';

  let activeTab: ToolsTab = 'registry';
  let tools: ToolDefinitionSummary[] = [];
  let skills: Skill[] = [];
  let executors: ExecutorConfig[] = [];
  let loading = true;
  let searchQuery = '';
  let sourceFilter = 'all';
  let categoryFilter = 'all';
  let expandedTools: Set<string> = new Set();

  // Skill form state
  let showSkillForm = false;
  let editingSkill: Skill | null = null;
  let skillForm = { name: '', description: '', instructions: '', tags: '' };

  onMount(async () => {
    await loadData();
  });

  async function loadData() {
    loading = true;
    try {
      const [toolsResult, skillsResult, executorsResult] = await Promise.all([
        api.tools.list(),
        api.skills.list(),
        api.executor.list().catch(() => [])
      ]);
      tools = toolsResult;
      skills = skillsResult;
      executors = executorsResult;
    } catch (err) {
      addToast('Failed to load tools data', 'error');
    } finally {
      loading = false;
    }
  }

  function getSourceType(tool: ToolDefinitionSummary): string {
    return (tool.source as Record<string, string>)?.type || 'unknown';
  }

  function getSourceLabel(sourceType: string): string {
    switch (sourceType) {
      case 'executor': return 'Executor';
      case 'builtin': return 'Built-in';
      case 'local_mcp': return 'Local MCP';
      case 'intaris_mcp': return 'Intaris MCP';
      case 'skill': return 'Skill';
      default: return sourceType;
    }
  }

  function getCategoryIcon(category: string) {
    switch (category) {
      case 'memory': return BrainCircuit;
      case 'filesystem': return FileText;
      case 'shell': return Terminal;
      case 'web': return Globe;
      case 'workflow': return ListChecks;
      case 'orchestration': return GitBranch;
      case 'system': return Server;
      default: return Wrench;
    }
  }

  function getCategoryLabel(category: string): string {
    switch (category) {
      case 'memory': return 'Memory';
      case 'filesystem': return 'Filesystem';
      case 'shell': return 'Shell';
      case 'web': return 'Web';
      case 'workflow': return 'Workflow';
      case 'orchestration': return 'Orchestration';
      case 'system': return 'System';
      default: return category.charAt(0).toUpperCase() + category.slice(1);
    }
  }

  // Fixed display order for categories
  const CATEGORY_ORDER = ['memory', 'filesystem', 'shell', 'web', 'workflow', 'orchestration', 'system'];

  $: filteredTools = tools.filter(tool => {
    const matchesSearch = !searchQuery ||
      tool.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
      tool.description.toLowerCase().includes(searchQuery.toLowerCase());
    const matchesSource = sourceFilter === 'all' || getSourceType(tool) === sourceFilter;
    const matchesCategory = categoryFilter === 'all' || tool.category === categoryFilter;
    return matchesSearch && matchesSource && matchesCategory;
  });

  $: groupedTools = (() => {
    const groups: Record<string, ToolDefinitionSummary[]> = {};
    for (const tool of filteredTools) {
      const cat = tool.category;
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(tool);
    }
    // Sort by fixed order, then any unknown categories at the end
    const sorted: [string, ToolDefinitionSummary[]][] = [];
    for (const cat of CATEGORY_ORDER) {
      if (groups[cat]) sorted.push([cat, groups[cat]]);
    }
    for (const [cat, tools] of Object.entries(groups)) {
      if (!CATEGORY_ORDER.includes(cat)) sorted.push([cat, tools]);
    }
    return sorted;
  })();

  $: sourceTypes = [...new Set(tools.map(getSourceType))];
  $: categoryTypes = [...new Set(tools.map(t => t.category))];

  function toggleTool(name: string) {
    if (expandedTools.has(name)) {
      expandedTools.delete(name);
    } else {
      expandedTools.add(name);
    }
    expandedTools = expandedTools;
  }

  function openSkillForm(skill?: Skill) {
    if (skill) {
      editingSkill = skill;
      skillForm = {
        name: skill.name,
        description: skill.description || '',
        instructions: skill.instructions,
        tags: (skill.tags || []).join(', ')
      };
    } else {
      editingSkill = null;
      skillForm = { name: '', description: '', instructions: '', tags: '' };
    }
    showSkillForm = true;
  }

  async function saveSkill() {
    const tags = skillForm.tags.split(',').map(t => t.trim()).filter(Boolean);
    try {
      if (editingSkill) {
        await api.skills.update(editingSkill.skill_id, {
          name: skillForm.name,
          description: skillForm.description || undefined,
          instructions: skillForm.instructions,
          tags: tags.length ? tags : undefined
        });
        addToast('Skill updated', 'success');
      } else {
        await api.skills.create({
          name: skillForm.name,
          description: skillForm.description || undefined,
          instructions: skillForm.instructions,
          tags: tags.length ? tags : undefined
        });
        addToast('Skill created', 'success');
      }
      showSkillForm = false;
      await loadData();
    } catch (err) {
      addToast('Failed to save skill', 'error');
    }
  }

  async function deleteSkill(skill: Skill) {
    if (skill.source !== 'db') {
      addToast('Cannot delete file-sourced skills', 'error');
      return;
    }
    try {
      await api.skills.delete(skill.skill_id);
      addToast('Skill deleted', 'success');
      await loadData();
    } catch (err) {
      addToast('Failed to delete skill', 'error');
    }
  }
</script>

<svelte:head>
  <title>Tools & Skills - Cognis</title>
</svelte:head>

<div class="max-w-6xl mx-auto px-4 py-6">
  <div class="flex items-center justify-between mb-6">
    <h1 class="text-2xl font-bold text-zinc-100">Tools & Skills</h1>
  </div>

  <!-- Tabs -->
  <div class="flex gap-1 mb-6 border-b border-zinc-700">
    <button
      class="px-4 py-2 text-sm font-medium transition-colors {activeTab === 'registry' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-zinc-400 hover:text-zinc-200'}"
      onclick={() => activeTab = 'registry'}
    >
      Tool Registry
    </button>
    <button
      class="px-4 py-2 text-sm font-medium transition-colors {activeTab === 'skills' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-zinc-400 hover:text-zinc-200'}"
      onclick={() => activeTab = 'skills'}
    >
      Skills ({skills.length})
    </button>
  </div>

  {#if loading}
    <div class="text-zinc-400 text-center py-12">Loading...</div>
  {:else if activeTab === 'registry'}
    <!-- Tool Registry -->
    <div class="space-y-4">
      <!-- Filters -->
      <div class="flex gap-3 items-center flex-wrap">
        <div class="relative flex-1 max-w-md">
          <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
          <input
            type="text"
            placeholder="Search tools..."
            bind:value={searchQuery}
            class="w-full pl-10 pr-4 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-blue-500"
          />
        </div>
        <select
          bind:value={categoryFilter}
          class="px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500"
        >
          <option value="all">All categories</option>
          {#each CATEGORY_ORDER.filter(c => categoryTypes.includes(c)) as cat}
            <option value={cat}>{getCategoryLabel(cat)}</option>
          {/each}
        </select>
        <select
          bind:value={sourceFilter}
          class="px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500"
        >
          <option value="all">All sources</option>
          {#each sourceTypes as source}
            <option value={source}>{getSourceLabel(source)}</option>
          {/each}
        </select>
        <span class="text-sm text-zinc-500">{filteredTools.length} tools</span>
      </div>

      <!-- Grouped tool list by category -->
      {#each groupedTools as [category, categoryTools]}
        {@const categorySource = getSourceType(categoryTools[0])}
        <div class="bg-zinc-800/50 border border-zinc-700 rounded-lg overflow-hidden">
          <div class="px-4 py-3 bg-zinc-800 border-b border-zinc-700 flex items-center gap-2">
            <svelte:component this={getCategoryIcon(category)} class="w-4 h-4 text-zinc-400" />
            <span class="text-sm font-medium text-zinc-200">{getCategoryLabel(category)}</span>
            <Badge>{categoryTools.length}</Badge>
            <span class="text-xs text-zinc-500 ml-auto">{getSourceLabel(categorySource)}</span>
          </div>
          <div class="divide-y divide-zinc-700/50">
            {#each categoryTools as tool}
              <div class="px-4 py-3">
                <button
                  class="w-full flex items-center gap-3 text-left"
                  onclick={() => toggleTool(tool.name)}
                >
                  {#if expandedTools.has(tool.name)}
                    <ChevronDown class="w-4 h-4 text-zinc-500 shrink-0" />
                  {:else}
                    <ChevronRight class="w-4 h-4 text-zinc-500 shrink-0" />
                  {/if}
                  <span class="font-mono text-sm text-zinc-100">{tool.name}</span>
                  <span class="text-sm text-zinc-400 truncate flex-1">{tool.description}</span>
                  <div class="flex items-center gap-2 shrink-0">
                    {#if tool.read_only}
                      <Badge>read-only</Badge>
                    {/if}
                    {#if tool.non_bypassable}
                      <span title="Non-bypassable (always evaluated by guardrails)"><ShieldCheck class="w-4 h-4 text-amber-400" /></span>
                    {/if}
                  </div>
                </button>
                {#if expandedTools.has(tool.name)}
                  <div class="mt-2 ml-11 p-3 bg-zinc-900/50 rounded-lg text-sm space-y-3">
                    <p class="text-zinc-300">{tool.description}</p>

                    <!-- Parameters -->
                    {#if tool.parameters?.properties && Object.keys(tool.parameters.properties).length > 0}
                      <div>
                        <span class="text-zinc-500 text-xs uppercase tracking-wider">Parameters</span>
                        <div class="mt-1.5 border border-zinc-700/50 rounded-lg overflow-hidden">
                          <table class="w-full text-sm">
                            <thead>
                              <tr class="bg-zinc-800/50">
                                <th class="text-left px-3 py-1.5 text-zinc-400 font-medium">Name</th>
                                <th class="text-left px-3 py-1.5 text-zinc-400 font-medium">Type</th>
                                <th class="text-left px-3 py-1.5 text-zinc-400 font-medium">Description</th>
                              </tr>
                            </thead>
                            <tbody class="divide-y divide-zinc-700/30">
                              {#each Object.entries(tool.parameters.properties) as [paramName, param]}
                                {@const isRequired = (tool.parameters.required || []).includes(paramName)}
                                <tr>
                                  <td class="px-3 py-1.5">
                                    <span class="font-mono text-zinc-100">{paramName}</span>
                                    {#if isRequired}
                                      <span class="text-red-400 ml-0.5">*</span>
                                    {/if}
                                  </td>
                                  <td class="px-3 py-1.5 text-zinc-400 font-mono">
                                    {param.type || 'any'}
                                    {#if param.enum}
                                      <span class="text-zinc-500"> ({param.enum.join(' | ')})</span>
                                    {/if}
                                  </td>
                                  <td class="px-3 py-1.5 text-zinc-400">{param.description || ''}</td>
                                </tr>
                              {/each}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    {/if}

                    <!-- Executor assignment -->
                    {#if getSourceType(tool) === 'executor' && executors.length > 0}
                      <div class="flex flex-wrap gap-1.5 items-center">
                        <span class="text-xs text-zinc-500">Enabled on:</span>
                        {#each executors as exec}
                          {@const enabledByGroup = (exec.enabled_tool_groups || []).includes(tool.category)}
                          {@const enabledByName = (exec.enabled_tools || []).includes(tool.name) || (exec.enabled_tools || []).includes('*')}
                          {#if enabledByGroup || enabledByName}
                            <span class="px-2 py-0.5 bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs rounded">{exec.name}</span>
                          {:else}
                            <span class="px-2 py-0.5 bg-zinc-800 border border-zinc-700 text-zinc-500 text-xs rounded line-through">{exec.name}</span>
                          {/if}
                        {/each}
                      </div>
                    {/if}

                    <!-- Metadata -->
                    <div class="flex flex-wrap gap-x-6 gap-y-1 text-xs text-zinc-500">
                      <span>Source: <span class="text-zinc-400">{getSourceLabel(getSourceType(tool))}</span></span>
                      <span>Timeout: <span class="text-zinc-400">{tool.timeout_seconds}s</span></span>
                      {#if !tool.read_only}
                        <span>Non-bypassable: <span class="text-zinc-400">{tool.non_bypassable ? 'Yes' : 'No'}</span></span>
                      {/if}
                    </div>
                  </div>
                {/if}
              </div>
            {/each}
          </div>
        </div>
      {/each}

      {#if filteredTools.length === 0}
        <div class="text-center py-12 text-zinc-500">
          No tools match your search.
        </div>
      {/if}
    </div>

  {:else}
    <!-- Skills -->
    <div class="space-y-4">
      <div class="flex justify-between items-center">
        <p class="text-sm text-zinc-400">
          Skills are instruction + tool bundles that agents can load on demand.
        </p>
        <Button variant="primary" size="sm" onclick={() => openSkillForm()}>
          <Plus class="w-4 h-4 mr-1" /> New Skill
        </Button>
      </div>

      {#if showSkillForm}
        <div class="bg-zinc-800 border border-zinc-700 rounded-lg p-4 space-y-4">
          <h3 class="text-lg font-medium text-zinc-100">
            {editingSkill ? 'Edit Skill' : 'New Skill'}
          </h3>
          <div class="grid grid-cols-2 gap-4">
            <label class="block text-sm text-zinc-400 space-y-1">
              <span>Name</span>
              <input
                type="text"
                bind:value={skillForm.name}
                class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500"
                placeholder="e.g. git-release"
              />
            </label>
            <label class="block text-sm text-zinc-400 space-y-1">
              <span>Tags (comma-separated)</span>
              <input
                type="text"
                bind:value={skillForm.tags}
                class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500"
                placeholder="e.g. git, release, automation"
              />
            </label>
          </div>
          <label class="block text-sm text-zinc-400 space-y-1">
            <span>Description</span>
            <input
              type="text"
              bind:value={skillForm.description}
              class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500"
              placeholder="Brief description of what this skill does"
            />
          </label>
          <label class="block text-sm text-zinc-400 space-y-1">
            <span>Instructions (Markdown)</span>
            <textarea
              bind:value={skillForm.instructions}
              rows="10"
              class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 font-mono focus:outline-none focus:border-blue-500"
              placeholder="# Skill Instructions&#10;&#10;Detailed instructions for the agent..."
            ></textarea>
          </label>
          <div class="flex gap-2 justify-end">
            <Button variant="ghost" size="sm" onclick={() => showSkillForm = false}>Cancel</Button>
            <Button variant="primary" size="sm" onclick={saveSkill} disabled={!skillForm.name || !skillForm.instructions}>
              {editingSkill ? 'Update' : 'Create'}
            </Button>
          </div>
        </div>
      {/if}

      {#if skills.length === 0 && !showSkillForm}
        <div class="text-center py-12 text-zinc-500">
          <BookOpen class="w-8 h-8 mx-auto mb-2 opacity-50" />
          <p>No skills defined yet.</p>
          <p class="text-sm mt-1">Create a skill to bundle instructions and tools for your agents.</p>
        </div>
      {:else}
        <div class="space-y-2">
          {#each skills as skill}
            <div class="bg-zinc-800/50 border border-zinc-700 rounded-lg p-4">
              <div class="flex items-start justify-between">
                <div class="flex-1">
                  <div class="flex items-center gap-2">
                    <BookOpen class="w-4 h-4 text-zinc-400" />
                    <span class="font-medium text-zinc-100">{skill.name}</span>
                    <Badge>{skill.source}</Badge>
                    {#if skill.auto_load}
                      <Badge class="border-blue-500/30 bg-blue-500/10 text-blue-300">auto-load</Badge>
                    {/if}
                  </div>
                  {#if skill.description}
                    <p class="text-sm text-zinc-400 mt-1">{skill.description}</p>
                  {/if}
                  {#if skill.tags && skill.tags.length > 0}
                    <div class="flex gap-1 mt-2">
                      {#each skill.tags as tag}
                        <Badge>{tag}</Badge>
                      {/each}
                    </div>
                  {/if}
                </div>
                <div class="flex gap-1">
                  {#if skill.source === 'db'}
                    <button
                      class="p-1.5 text-zinc-400 hover:text-zinc-200 rounded"
                      onclick={() => openSkillForm(skill)}
                      title="Edit"
                    >
                      <Pencil class="w-4 h-4" />
                    </button>
                    <button
                      class="p-1.5 text-zinc-400 hover:text-red-400 rounded"
                      onclick={() => deleteSkill(skill)}
                      title="Delete"
                    >
                      <Trash2 class="w-4 h-4" />
                    </button>
                  {:else}
                    <Badge>read-only</Badge>
                  {/if}
                </div>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>
