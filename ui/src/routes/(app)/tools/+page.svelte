<script lang="ts">
  import { onMount } from 'svelte';
  import {
    BrainCircuit,
    ChevronDown,
    ChevronRight,
    Download,
    ExternalLink,
    GitBranch,
    Import,
    ListChecks,
    Plug,
    Plus,
    Search,
    Settings,
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
  import {
    buildRegistryWarnings,
    CATEGORY_ORDER,
    filterTools,
    filterMcpTools,
    formatMcpCommand,
    formatSourceSummary,
    getSourceLabel,
    getSourceType,
    getToolKey,
    groupToolsByCategory,
    groupToolsByServer,
    isCachedObservedTool,
    mergeToolInventories
  } from '$lib/tools-registry';
  import type { ExecutorConfig, MCPServerConfigResponse, IntarisMCPServer, ToolDefinitionSummary, Skill, SystemDiagnostics } from '$lib/types/api';

  type ToolsTab = 'builtin' | 'intaris_mcp' | 'executor_mcp' | 'skills';

  let activeTab: ToolsTab = 'builtin';
  let staticTools: ToolDefinitionSummary[] = [];
  let intarisMcpTools: ToolDefinitionSummary[] = [];
  let observedLocalMcpTools: ToolDefinitionSummary[] = [];
  let intarisMcpServers: IntarisMCPServer[] = [];
  let skills: Skill[] = [];
  let executors: ExecutorConfig[] = [];
  let mcpServerConfigs: MCPServerConfigResponse[] = [];
  let loading = true;
  let registryWarnings: string[] = [];
  let intarisUrl: string | null = null;

  let builtinSearch = '';
  let builtinCategoryFilter = 'all';
  let intarisSearch = '';
  let executorMcpSearch = '';

  let expandedTools: Set<string> = new Set();

  let showSkillForm = false;
  let showImportForm = false;
  let editingSkill: Skill | null = null;
  let skillForm = { name: '', description: '', instructions: '', tags: '', autoLoad: false };
  let importForm = { url: '', name: '', tags: '', autoLoad: false };

  onMount(async () => {
    await loadData();
  });

  async function loadData() {
    loading = true;
    registryWarnings = [];
    try {
      const [toolsResult, intarisToolsResult, observedToolsResult, intarisServersResult, skillsResult, executorsResult, mcpServersResult, diagResult] = await Promise.allSettled([
        api.tools.list(),
        api.tools.intarisMcpTools(),
        api.tools.observedLocalMcpTools(),
        api.tools.intarisMcpServers(),
        api.skills.list(),
        api.executor.list(),
        api.tools.listMcpServerConfigs(),
        api.system.diagnostics()
      ]);

      staticTools = toolsResult.status === 'fulfilled' ? toolsResult.value : [];
      intarisMcpTools = intarisToolsResult.status === 'fulfilled' ? intarisToolsResult.value : [];
      observedLocalMcpTools = observedToolsResult.status === 'fulfilled' ? observedToolsResult.value : [];
      intarisMcpServers = intarisServersResult.status === 'fulfilled' ? intarisServersResult.value : [];
      skills = skillsResult.status === 'fulfilled' ? skillsResult.value : [];
      executors = executorsResult.status === 'fulfilled' ? executorsResult.value : [];
      mcpServerConfigs = mcpServersResult.status === 'fulfilled' ? mcpServersResult.value : [];

      if (diagResult.status === 'fulfilled') {
        const config = (diagResult.value as SystemDiagnostics).config as Record<string, unknown>;
        const url = config?.intaris_url;
        intarisUrl = typeof url === 'string' && url ? url : null;
      }

      registryWarnings = buildRegistryWarnings({
        staticTools: toolsResult.status === 'fulfilled',
        intarisMcpTools: intarisToolsResult.status === 'fulfilled',
        observedLocalMcpTools: observedToolsResult.status === 'fulfilled',
        skills: skillsResult.status === 'fulfilled',
        executors: executorsResult.status === 'fulfilled',
        mcpServers: mcpServersResult.status === 'fulfilled'
      });
    } catch (err) {
      addToast('Failed to load tools data', 'error');
    } finally {
      loading = false;
    }
  }

  function getCategoryIcon(category: string) {
    switch (category) {
      case 'memory': return BrainCircuit;
      case 'filesystem': return FileText;
      case 'shell': return Terminal;
      case 'web': return Globe;
      case 'mcp': return Plug;
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
      case 'mcp': return 'MCP';
      case 'workflow': return 'Workflow';
      case 'orchestration': return 'Orchestration';
      case 'system': return 'System';
      default: return category.charAt(0).toUpperCase() + category.slice(1);
    }
  }

  $: filteredBuiltinTools = filterTools(staticTools, { searchQuery: builtinSearch, categoryFilter: builtinCategoryFilter });
  $: groupedBuiltinTools = groupToolsByCategory(filteredBuiltinTools);
  $: builtinCategoryTypes = [...new Set(staticTools.map(t => t.category))];
  $: orderedBuiltinCategories = [
    ...CATEGORY_ORDER.filter((c) => builtinCategoryTypes.includes(c)),
    ...builtinCategoryTypes.filter((c) => !CATEGORY_ORDER.includes(c)).sort()
  ];

  $: filteredIntarisTools = filterMcpTools(intarisMcpTools, intarisSearch);
  $: intarisServerGroups = groupToolsByServer(filteredIntarisTools);

  $: filteredExecutorMcpTools = filterMcpTools(observedLocalMcpTools, executorMcpSearch);
  $: executorMcpServerGroups = groupToolsByServer(filteredExecutorMcpTools);

  function toggleTool(key: string) {
    if (expandedTools.has(key)) {
      expandedTools.delete(key);
    } else {
      expandedTools.add(key);
    }
    expandedTools = expandedTools;
  }

  function openSkillForm(skill?: Skill) {
    if (skill) {
      editingSkill = skill;
      const ver = skill.current_version;
      skillForm = {
        name: skill.name,
        description: skill.description || '',
        instructions: ver?.instructions || skill.instructions,
        tags: (skill.tags || []).join(', '),
        autoLoad: skill.auto_load
      };
    } else {
      editingSkill = null;
      skillForm = { name: '', description: '', instructions: '', tags: '', autoLoad: false };
    }
    showSkillForm = true;
    showImportForm = false;
  }

  function openImportForm() {
    importForm = { url: '', name: '', tags: '', autoLoad: false };
    showImportForm = true;
    showSkillForm = false;
  }

  async function saveSkill() {
    const tags = skillForm.tags.split(',').map(t => t.trim()).filter(Boolean);
    try {
      if (editingSkill) {
        await api.skills.update(editingSkill.skill_id, {
          name: skillForm.name,
          description: skillForm.description || undefined,
          instructions: skillForm.instructions,
          tags: tags.length ? tags : undefined,
          auto_load: skillForm.autoLoad
        });
        addToast('Skill updated', 'success');
      } else {
        await api.skills.create({
          name: skillForm.name,
          description: skillForm.description || undefined,
          instructions: skillForm.instructions,
          tags: tags.length ? tags : undefined,
          auto_load: skillForm.autoLoad
        });
        addToast('Skill created', 'success');
      }
      showSkillForm = false;
      await loadData();
    } catch (err) {
      addToast('Failed to save skill', 'error');
    }
  }

  async function importSkill() {
    if (!importForm.url.trim()) {
      addToast('URL is required', 'error');
      return;
    }
    const tags = importForm.tags.split(',').map(t => t.trim()).filter(Boolean);
    try {
      await api.skills.import({
        url: importForm.url.trim(),
        name: importForm.name.trim() || undefined,
        tags: tags.length ? tags : undefined,
        auto_load: importForm.autoLoad
      });
      addToast('Skill imported successfully', 'success');
      showImportForm = false;
      await loadData();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Failed to import skill';
      addToast(msg, 'error');
    }
  }

  async function exportSkill(skill: Skill, format: string = 'skill_md') {
    try {
      const result = await api.skills.export(skill.skill_id, format);
      const blob = new Blob([result.content], { type: 'text/plain' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = result.filename;
      a.click();
      URL.revokeObjectURL(url);
      addToast('Skill exported', 'success');
    } catch (err) {
      addToast('Failed to export skill', 'error');
    }
  }

  async function deleteSkill(skill: Skill) {
    if (skill.source !== 'db' && skill.source !== 'imported') {
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

  <div class="flex gap-1 mb-6 border-b border-zinc-700 overflow-x-auto">
    <button class="px-4 py-2 text-sm font-medium transition-colors whitespace-nowrap {activeTab === 'builtin' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-zinc-400 hover:text-zinc-200'}" onclick={() => activeTab = 'builtin'}>
      Built-in Tools ({staticTools.length})
    </button>
    <button class="px-4 py-2 text-sm font-medium transition-colors whitespace-nowrap {activeTab === 'intaris_mcp' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-zinc-400 hover:text-zinc-200'}" onclick={() => activeTab = 'intaris_mcp'}>
      Intaris MCP ({intarisMcpTools.length})
    </button>
    <button class="px-4 py-2 text-sm font-medium transition-colors whitespace-nowrap {activeTab === 'executor_mcp' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-zinc-400 hover:text-zinc-200'}" onclick={() => activeTab = 'executor_mcp'}>
      Executor MCP ({observedLocalMcpTools.length})
    </button>
    <button class="px-4 py-2 text-sm font-medium transition-colors whitespace-nowrap {activeTab === 'skills' ? 'text-blue-400 border-b-2 border-blue-400' : 'text-zinc-400 hover:text-zinc-200'}" onclick={() => activeTab = 'skills'}>
      Skills ({skills.length})
    </button>
  </div>

  {#if loading}
    <div class="text-zinc-400 text-center py-12">Loading...</div>

  {:else if activeTab === 'builtin'}
    <div class="space-y-4">
      {#if registryWarnings.some(w => w.includes('Static'))}
        <div class="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm text-amber-200">Static tool registry failed to load.</div>
      {/if}

      <div class="flex gap-3 items-center flex-wrap">
        <div class="relative flex-1 max-w-md">
          <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
          <input type="text" placeholder="Search tools..." bind:value={builtinSearch} class="w-full pl-10 pr-4 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-blue-500" />
        </div>
        <select bind:value={builtinCategoryFilter} class="px-3 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500">
          <option value="all">All categories</option>
          {#each orderedBuiltinCategories as cat}
            <option value={cat}>{getCategoryLabel(cat)}</option>
          {/each}
        </select>
        <span class="text-sm text-zinc-500">{filteredBuiltinTools.length} tools</span>
      </div>

      {#each groupedBuiltinTools as group}
        <div class="bg-zinc-800/50 border border-zinc-700 rounded-lg overflow-hidden">
          <div class="px-4 py-3 bg-zinc-800 border-b border-zinc-700 flex items-center gap-2">
            <svelte:component this={getCategoryIcon(group.category)} class="w-4 h-4 text-zinc-400" />
            <span class="text-sm font-medium text-zinc-200">{getCategoryLabel(group.category)}</span>
            <Badge>{group.tools.length}</Badge>
            <span class="text-xs text-zinc-500 ml-auto">{formatSourceSummary(group.sourceTypes)}</span>
          </div>
          <div class="divide-y divide-zinc-700/50">
            {#each group.tools as tool}
              {@const toolKey = getToolKey(tool)}
              {@render toolRow(tool, toolKey)}
            {/each}
          </div>
        </div>
      {/each}

      {#if filteredBuiltinTools.length === 0}
        <div class="text-center py-12 text-zinc-500">No tools match your search.</div>
      {/if}
    </div>

  {:else if activeTab === 'intaris_mcp'}
    <div class="space-y-4">
      {#if registryWarnings.some(w => w.includes('Intaris'))}
        <div class="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm text-amber-200">Intaris MCP tools are unavailable right now.</div>
      {/if}

      <div class="flex gap-3 items-center flex-wrap">
        <div class="relative flex-1 max-w-md">
          <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
          <input type="text" placeholder="Search Intaris MCP tools..." bind:value={intarisSearch} class="w-full pl-10 pr-4 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-blue-500" />
        </div>
        <span class="text-sm text-zinc-500">{filteredIntarisTools.length} tools</span>
        <div class="ml-auto">
          {#if intarisUrl}
            <a href={intarisUrl} target="_blank" rel="noopener noreferrer" class="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-blue-400 hover:text-blue-300 border border-zinc-700 rounded-lg hover:border-zinc-600 transition-colors">
              <ExternalLink class="w-3.5 h-3.5" />
              Configure in Intaris
            </a>
          {/if}
        </div>
      </div>

      {#if intarisServerGroups.length === 0 && !intarisSearch}
        <div class="text-center py-12 text-zinc-500">
          <Plug class="w-8 h-8 mx-auto mb-2 opacity-50" />
          <p>No Intaris MCP servers configured.</p>
          {#if intarisUrl}
            <p class="text-sm mt-1">Configure MCP servers in the <a href={intarisUrl} target="_blank" rel="noopener noreferrer" class="text-blue-400 hover:text-blue-300">Intaris web UI</a>.</p>
          {/if}
        </div>
      {:else}
        {#each intarisServerGroups as serverGroup}
          <div class="bg-zinc-800/50 border border-zinc-700 rounded-lg overflow-hidden">
            <div class="px-4 py-3 bg-zinc-800 border-b border-zinc-700 flex items-center gap-2">
              <Plug class="w-4 h-4 text-zinc-400" />
              <span class="text-sm font-medium text-zinc-200">{serverGroup.serverName}</span>
              <Badge>{serverGroup.tools.length}</Badge>
              <span class="text-xs text-zinc-500 ml-auto">Intaris MCP</span>
            </div>
            <div class="divide-y divide-zinc-700/50">
              {#each serverGroup.tools as tool}
                {@const toolKey = getToolKey(tool)}
                {@render mcpToolRow(tool, toolKey)}
              {/each}
            </div>
          </div>
        {/each}
        {#if filteredIntarisTools.length === 0 && intarisSearch}
          <div class="text-center py-12 text-zinc-500">No Intaris MCP tools match your search.</div>
        {/if}
      {/if}
    </div>

  {:else if activeTab === 'executor_mcp'}
    <div class="space-y-4">
      {#if registryWarnings.some(w => w.includes('local MCP') || w.includes('MCP servers'))}
        {#each registryWarnings.filter(w => w.includes('local MCP') || w.includes('MCP servers')) as warning}
          <div class="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-sm text-amber-200">{warning}</div>
        {/each}
      {/if}

      <div class="flex gap-3 items-center flex-wrap">
        <div class="relative flex-1 max-w-md">
          <Search class="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500" />
          <input type="text" placeholder="Search executor MCP tools..." bind:value={executorMcpSearch} class="w-full pl-10 pr-4 py-2 bg-zinc-800 border border-zinc-700 rounded-lg text-sm text-zinc-200 placeholder-zinc-500 focus:outline-none focus:border-blue-500" />
        </div>
        <span class="text-sm text-zinc-500">{filteredExecutorMcpTools.length} tools</span>
        <div class="ml-auto">
          <a href="/settings#tools" class="inline-flex items-center gap-1.5 px-3 py-1.5 text-sm text-blue-400 hover:text-blue-300 border border-zinc-700 rounded-lg hover:border-zinc-600 transition-colors">
            <Settings class="w-3.5 h-3.5" />
            Configure MCP Servers
          </a>
        </div>
      </div>

      {#if executorMcpServerGroups.length > 0}
        {#each executorMcpServerGroups as serverGroup}
          <div class="bg-zinc-800/50 border border-zinc-700 rounded-lg overflow-hidden">
            <div class="px-4 py-3 bg-zinc-800 border-b border-zinc-700 flex items-center gap-2">
              <Plug class="w-4 h-4 text-zinc-400" />
              <span class="text-sm font-medium text-zinc-200">{serverGroup.serverName}</span>
              <Badge>{serverGroup.tools.length}</Badge>
              <Badge class="border-sky-500/30 bg-sky-500/10 text-sky-300">cached</Badge>
              <span class="text-xs text-zinc-500 ml-auto">Executor MCP</span>
            </div>
            <div class="divide-y divide-zinc-700/50">
              {#each serverGroup.tools as tool}
                {@const toolKey = getToolKey(tool)}
                {@render mcpToolRow(tool, toolKey)}
              {/each}
            </div>
          </div>
        {/each}
      {/if}

      {#if filteredExecutorMcpTools.length === 0 && executorMcpSearch}
        <div class="text-center py-12 text-zinc-500">No executor MCP tools match your search.</div>
      {/if}

      {#if mcpServerConfigs.length > 0}
        <div class="space-y-3 {executorMcpServerGroups.length > 0 ? 'mt-6 pt-6 border-t border-zinc-700' : ''}">
          <div>
            <h2 class="text-lg font-medium text-zinc-100">Configured MCP Servers</h2>
            <p class="text-sm text-zinc-400">Server definitions managed in Settings. Tools above are cached from executor inventory.</p>
          </div>
          <div class="grid gap-3 md:grid-cols-2">
            {#each mcpServerConfigs as server}
              <div class="rounded-lg border border-zinc-700 bg-zinc-800/50 p-4 space-y-2">
                <div class="flex items-center gap-2">
                  <span class="font-medium text-zinc-100">{server.name}</span>
                  <Badge>{server.transport}</Badge>
                  <Badge class={server.status === 'active' ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300' : 'border-zinc-700 bg-zinc-800 text-zinc-400'}>{server.status}</Badge>
                </div>
                {#if server.description}
                  <p class="text-sm text-zinc-400">{server.description}</p>
                {/if}
                <div class="flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
                  {#if server.command}
                    <span>Command: <span class="font-mono text-zinc-400">{formatMcpCommand(server.command, server.args)}</span></span>
                  {/if}
                  {#if server.url}
                    <span>URL: <span class="text-zinc-400">{server.url}</span></span>
                  {/if}
                  <span>Timeout: <span class="text-zinc-400">{server.timeout_seconds}s</span></span>
                </div>
              </div>
            {/each}
          </div>
        </div>
      {:else if executorMcpServerGroups.length === 0 && !executorMcpSearch}
        <div class="text-center py-12 text-zinc-500">
          <Plug class="w-8 h-8 mx-auto mb-2 opacity-50" />
          <p>No MCP servers configured.</p>
          <p class="text-sm mt-1">Add MCP servers in <a href="/settings#tools" class="text-blue-400 hover:text-blue-300">Settings</a> and assign them to executors.</p>
        </div>
      {/if}
    </div>

  {:else}
    <!-- Skills tab - unchanged -->
    <div class="space-y-4">
      <div class="flex justify-between items-center">
        <p class="text-sm text-zinc-400">Skills are versioned instruction + tool bundles that agents can load on demand.</p>
        <div class="flex gap-2">
          <Button variant="ghost" size="sm" onclick={openImportForm}><Import class="w-4 h-4 mr-1" /> Import from URL</Button>
          <Button variant="primary" size="sm" onclick={() => openSkillForm()}><Plus class="w-4 h-4 mr-1" /> New Skill</Button>
        </div>
      </div>

      {#if showImportForm}
        <div class="bg-zinc-800 border border-zinc-700 rounded-lg p-4 space-y-4">
          <h3 class="text-lg font-medium text-zinc-100">Import Skill from URL</h3>
          <p class="text-sm text-zinc-400">Import a SKILL.md file from GitHub or any URL. Supports Claude Code / Agent Skills format and Cognis YAML.</p>
          <label class="block text-sm text-zinc-400 space-y-1"><span>URL</span><input type="url" bind:value={importForm.url} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="https://github.com/user/repo/blob/main/skills/my-skill/SKILL.md" /></label>
          <div class="grid grid-cols-2 gap-4">
            <label class="block text-sm text-zinc-400 space-y-1"><span>Name override (optional)</span><input type="text" bind:value={importForm.name} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="Leave empty to use imported name" /></label>
            <label class="block text-sm text-zinc-400 space-y-1"><span>Tags (comma-separated)</span><input type="text" bind:value={importForm.tags} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="e.g. imported, claude" /></label>
          </div>
          <label class="flex items-center gap-2 text-sm text-zinc-400"><input type="checkbox" bind:checked={importForm.autoLoad} class="rounded border-zinc-600" /> Auto-load for all agents</label>
          <div class="flex gap-2 justify-end">
            <Button variant="ghost" size="sm" onclick={() => showImportForm = false}>Cancel</Button>
            <Button variant="primary" size="sm" onclick={importSkill} disabled={!importForm.url.trim()}>Import</Button>
          </div>
        </div>
      {/if}

      {#if showSkillForm}
        <div class="bg-zinc-800 border border-zinc-700 rounded-lg p-4 space-y-4">
          <h3 class="text-lg font-medium text-zinc-100">{editingSkill ? 'Edit Skill' : 'New Skill'}</h3>
          <div class="grid grid-cols-2 gap-4">
            <label class="block text-sm text-zinc-400 space-y-1"><span>Name</span><input type="text" bind:value={skillForm.name} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="e.g. git-release" /></label>
            <label class="block text-sm text-zinc-400 space-y-1"><span>Tags (comma-separated)</span><input type="text" bind:value={skillForm.tags} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="e.g. git, release, automation" /></label>
          </div>
          <label class="block text-sm text-zinc-400 space-y-1"><span>Description</span><input type="text" bind:value={skillForm.description} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="Brief description of what this skill does" /></label>
          <label class="block text-sm text-zinc-400 space-y-1"><span>Instructions (Markdown)</span><textarea bind:value={skillForm.instructions} rows="10" class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 font-mono focus:outline-none focus:border-blue-500" placeholder="# Skill Instructions&#10;&#10;Detailed instructions for the agent..."></textarea></label>
          <label class="flex items-center gap-2 text-sm text-zinc-400"><input type="checkbox" bind:checked={skillForm.autoLoad} class="rounded border-zinc-600" /> Auto-load for all agents</label>
          <div class="flex gap-2 justify-end">
            <Button variant="ghost" size="sm" onclick={() => showSkillForm = false}>Cancel</Button>
            <Button variant="primary" size="sm" onclick={saveSkill} disabled={!skillForm.name || !skillForm.instructions}>{editingSkill ? 'Update' : 'Create'}</Button>
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
                    {#if skill.auto_load}<Badge class="border-blue-500/30 bg-blue-500/10 text-blue-300">auto-load</Badge>{/if}
                  </div>
                  {#if skill.description}<p class="text-sm text-zinc-400 mt-1">{skill.description}</p>{/if}
                  {#if skill.tags && skill.tags.length > 0}<div class="flex gap-1 mt-2">{#each skill.tags as tag}<Badge>{tag}</Badge>{/each}</div>{/if}
                </div>
                <div class="flex gap-1">
                  <button class="p-1.5 text-zinc-400 hover:text-zinc-200 rounded" onclick={() => exportSkill(skill)} title="Export as SKILL.md"><Download class="w-4 h-4" /></button>
                  {#if skill.source === 'db' || skill.source === 'imported'}
                    <button class="p-1.5 text-zinc-400 hover:text-zinc-200 rounded" onclick={() => openSkillForm(skill)} title="Edit"><Pencil class="w-4 h-4" /></button>
                    <button class="p-1.5 text-zinc-400 hover:text-red-400 rounded" onclick={() => deleteSkill(skill)} title="Delete"><Trash2 class="w-4 h-4" /></button>
                  {:else}<Badge>read-only</Badge>{/if}
                </div>
              </div>
              {#if skill.current_version}
                <div class="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
                  <span>Version: <span class="text-zinc-400">v{skill.current_version.version_number}</span></span>
                  <span>Hash: <span class="font-mono text-zinc-400">{skill.current_version.content_hash.slice(0, 8)}</span></span>
                  {#if skill.current_version.tools && skill.current_version.tools.length > 0}<span>Tools: <span class="text-zinc-400">{skill.current_version.tools.length}</span></span>{/if}
                  {#if skill.current_version.source_url}<span>Imported from: <span class="text-zinc-400 truncate max-w-[200px] inline-block align-bottom" title={skill.current_version.source_url}>{skill.current_version.source_url}</span></span>{/if}
                  {#if skill.current_version.asset_manifest && skill.current_version.asset_manifest.length > 0}<span>Assets: <span class="text-zinc-400">{skill.current_version.asset_manifest.length}</span></span>{/if}
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>

{#snippet toolRow(tool: ToolDefinitionSummary, toolKey: string)}
  <div class="px-4 py-3">
    <button class="w-full flex items-center gap-3 text-left" onclick={() => toggleTool(toolKey)}>
      {#if expandedTools.has(toolKey)}<ChevronDown class="w-4 h-4 text-zinc-500 shrink-0" />{:else}<ChevronRight class="w-4 h-4 text-zinc-500 shrink-0" />{/if}
      <span class="font-mono text-sm text-zinc-100">{tool.name}</span>
      <span class="text-sm text-zinc-400 truncate flex-1">{tool.description}</span>
      <div class="flex items-center gap-2 shrink-0">
        {#if tool.read_only}<Badge>read-only</Badge>{/if}
        {#if tool.non_bypassable}<span title="Non-bypassable (always evaluated by guardrails)"><ShieldCheck class="w-4 h-4 text-amber-400" /></span>{/if}
      </div>
    </button>
    {#if expandedTools.has(toolKey)}
      {@render toolDetail(tool)}
    {/if}
  </div>
{/snippet}

{#snippet mcpToolRow(tool: ToolDefinitionSummary, toolKey: string)}
  <div class="px-4 py-3">
    <button class="w-full flex items-center gap-3 text-left" onclick={() => toggleTool(toolKey)}>
      {#if expandedTools.has(toolKey)}<ChevronDown class="w-4 h-4 text-zinc-500 shrink-0" />{:else}<ChevronRight class="w-4 h-4 text-zinc-500 shrink-0" />{/if}
      <span class="font-mono text-sm text-zinc-100">{tool.source.raw_tool_name || tool.name}</span>
      <span class="text-sm text-zinc-400 truncate flex-1">{tool.description}</span>
      <div class="flex items-center gap-2 shrink-0">
        {#if tool.read_only}<Badge>read-only</Badge>{/if}
        {#if tool.non_bypassable}<span title="Non-bypassable"><ShieldCheck class="w-4 h-4 text-amber-400" /></span>{/if}
      </div>
    </button>
    {#if expandedTools.has(toolKey)}
      {@render toolDetail(tool)}
    {/if}
  </div>
{/snippet}

{#snippet toolDetail(tool: ToolDefinitionSummary)}
  <div class="mt-2 ml-11 p-3 bg-zinc-900/50 rounded-lg text-sm space-y-3">
    <p class="text-zinc-300">{tool.description}</p>
    {#if tool.parameters?.properties && Object.keys(tool.parameters.properties).length > 0}
      <div>
        <span class="text-zinc-500 text-xs uppercase tracking-wider">Parameters</span>
        <div class="mt-1.5 border border-zinc-700/50 rounded-lg overflow-hidden">
          <table class="w-full text-sm">
            <thead><tr class="bg-zinc-800/50"><th class="text-left px-3 py-1.5 text-zinc-400 font-medium">Name</th><th class="text-left px-3 py-1.5 text-zinc-400 font-medium">Type</th><th class="text-left px-3 py-1.5 text-zinc-400 font-medium">Description</th></tr></thead>
            <tbody class="divide-y divide-zinc-700/30">
              {#each Object.entries(tool.parameters.properties) as [paramName, param]}
                {@const isRequired = (tool.parameters.required || []).includes(paramName)}
                <tr>
                  <td class="px-3 py-1.5"><span class="font-mono text-zinc-100">{paramName}</span>{#if isRequired}<span class="text-red-400 ml-0.5">*</span>{/if}</td>
                  <td class="px-3 py-1.5 text-zinc-400 font-mono">{param.type || 'any'}{#if param.enum}<span class="text-zinc-500"> ({param.enum.join(' | ')})</span>{/if}</td>
                  <td class="px-3 py-1.5 text-zinc-400">{param.description || ''}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}
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
    <div class="flex flex-wrap gap-x-6 gap-y-1 text-xs text-zinc-500">
      <span>Source: <span class="text-zinc-400">{getSourceLabel(getSourceType(tool))}</span></span>
      <span>Timeout: <span class="text-zinc-400">{tool.timeout_seconds}s</span></span>
      {#if tool.source.server_name}<span>Server: <span class="text-zinc-400">{tool.source.server_name}</span></span>{/if}
      {#if !tool.read_only}<span>Non-bypassable: <span class="text-zinc-400">{tool.non_bypassable ? 'Yes' : 'No'}</span></span>{/if}
    </div>
  </div>
{/snippet}
