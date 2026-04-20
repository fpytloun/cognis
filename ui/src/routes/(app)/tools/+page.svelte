<script lang="ts">
  import { onMount } from 'svelte';
  import BrainCircuit from 'lucide-svelte/icons/brain-circuit';
import ChevronDown from 'lucide-svelte/icons/chevron-down';
import ChevronRight from 'lucide-svelte/icons/chevron-right';
import Download from 'lucide-svelte/icons/download';
import ExternalLink from 'lucide-svelte/icons/external-link';
import GitBranch from 'lucide-svelte/icons/git-branch';
import Import from 'lucide-svelte/icons/import';
import ListChecks from 'lucide-svelte/icons/list-checks';
import Plug from 'lucide-svelte/icons/plug';
import Plus from 'lucide-svelte/icons/plus';
import Search from 'lucide-svelte/icons/search';
import Settings from 'lucide-svelte/icons/settings';
import ShieldCheck from 'lucide-svelte/icons/shield-check';
import Trash2 from 'lucide-svelte/icons/trash-2';
import Pencil from 'lucide-svelte/icons/pencil';
import FileText from 'lucide-svelte/icons/file-text';
import Terminal from 'lucide-svelte/icons/terminal';
import Globe from 'lucide-svelte/icons/globe';
import Wrench from 'lucide-svelte/icons/wrench';
import Server from 'lucide-svelte/icons/server';
  import BookOpen from 'lucide-svelte/icons/book-open';
  import Eye from 'lucide-svelte/icons/eye';
  import Upload from 'lucide-svelte/icons/upload';
  import X from 'lucide-svelte/icons/x';

  import { api } from '$lib/api/client';
  import SkillDetailSheet from '$lib/components/skills/SkillDetailSheet.svelte';
  import Badge from '$lib/components/ui/Badge.svelte';
  import Button from '$lib/components/ui/Button.svelte';
  import { confirmAction } from '$lib/stores/confirm';
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
  import type { ExecutorConfig, MCPServerConfigResponse, IntarisMCPServer, Skill, SkillAssetInput, ToolDefinitionSummary, SystemDiagnostics } from '$lib/types/api';

  type ToolsTab = 'builtin' | 'intaris_mcp' | 'executor_mcp' | 'skills';
  type SkillImportMode = 'url' | 'content' | 'package';
  type SkillFormAsset = SkillAssetInput & { size_bytes?: number };

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
  let expandedGroups: Set<string> = new Set();

  function toggleGroup(key: string) {
    if (expandedGroups.has(key)) {
      expandedGroups.delete(key);
    } else {
      expandedGroups.add(key);
    }
    expandedGroups = expandedGroups;
  }

  let showSkillForm = false;
  let showImportForm = false;
  let importMode: SkillImportMode = 'url';
  let selectedSkillId: string | null = null;
  let selectedSkill: Skill | null = null;
  let editingSkill: Skill | null = null;
  let skillForm = {
    name: '',
    description: '',
    instructions: '',
    tags: '',
    autoLoad: false,
    toolsJson: '[]',
    promptTemplatesJson: '{}',
    secretPlaceholders: '',
    assets: [] as SkillFormAsset[]
  };
  let importForm = {
    url: '',
    content: '',
    contentFormat: '',
    packageName: '',
    packageB64: '',
    name: '',
    tags: '',
    autoLoad: false
  };

  $: selectedSkill = skills.find((skill) => skill.skill_id === selectedSkillId) || null;

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
        autoLoad: Boolean(skill.attach_to_all_agents ?? skill.auto_load),
        toolsJson: JSON.stringify(ver?.tools || [], null, 2),
        promptTemplatesJson: JSON.stringify(ver?.prompt_templates || {}, null, 2),
        secretPlaceholders: (ver?.secret_placeholders || []).join(', '),
        assets: (ver?.asset_manifest || []).map((asset) => ({
          filename: asset.filename,
          existing_asset_id: asset.asset_id,
          content_type: asset.content_type,
          size_bytes: asset.size_bytes
        }))
      };
    } else {
      editingSkill = null;
      skillForm = {
        name: '',
        description: '',
        instructions: '',
        tags: '',
        autoLoad: false,
        toolsJson: '[]',
        promptTemplatesJson: '{}',
        secretPlaceholders: '',
        assets: []
      };
    }
    showSkillForm = true;
    showImportForm = false;
  }

  function openImportForm() {
    importMode = 'url';
    importForm = {
      url: '',
      content: '',
      contentFormat: '',
      packageName: '',
      packageB64: '',
      name: '',
      tags: '',
      autoLoad: false
    };
    showImportForm = true;
    showSkillForm = false;
  }

  function parseJsonField<T>(label: string, raw: string, fallback: T): T {
    if (!raw.trim()) return fallback;
    const parsed = JSON.parse(raw);
    void label;
    return parsed as T;
  }

  async function uploadSkillAssets(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    const files = Array.from(target.files || []);
    if (files.length === 0) return;
    try {
      for (const file of files) {
        const uploaded = await api.artifacts.upload(file, 'skill_asset');
        skillForm.assets = [
          ...skillForm.assets.filter((asset) => asset.filename !== uploaded.filename),
          {
            filename: uploaded.filename,
            source_artifact_id: uploaded.artifact_id,
            content_type: uploaded.mime_type,
            size_bytes: uploaded.size_bytes
          }
        ];
      }
      addToast('Skill asset uploaded.', 'success');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to upload skill asset', 'error');
    } finally {
      target.value = '';
    }
  }

  function removeSkillAsset(filename: string) {
    skillForm.assets = skillForm.assets.filter((asset) => asset.filename !== filename);
  }

  async function loadPackageFile(event: Event) {
    const target = event.currentTarget as HTMLInputElement;
    const file = target.files?.[0];
    if (!file) return;
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = '';
    for (const byte of bytes) {
      binary += String.fromCharCode(byte);
    }
    importForm.packageB64 = btoa(binary);
    importForm.packageName = file.name;
  }

  async function saveSkill() {
    const tags = skillForm.tags.split(',').map(t => t.trim()).filter(Boolean);
    try {
      const tools = parseJsonField<Record<string, unknown>[]>('tools', skillForm.toolsJson, []);
      const promptTemplates = parseJsonField<Record<string, unknown>>('prompt templates', skillForm.promptTemplatesJson, {});
      const secretPlaceholders = skillForm.secretPlaceholders.split(',').map((item) => item.trim()).filter(Boolean);
      const payload = {
        name: skillForm.name,
        description: skillForm.description || undefined,
        instructions: skillForm.instructions,
        tags: tags.length ? tags : undefined,
        attach_to_all_agents: skillForm.autoLoad,
        tools,
        prompt_templates: promptTemplates,
        secret_placeholders: secretPlaceholders,
        assets: skillForm.assets.map((asset) => ({
          filename: asset.filename,
          existing_asset_id: asset.existing_asset_id,
          source_artifact_id: asset.source_artifact_id,
          content_type: asset.content_type
        }))
      };
      if (editingSkill) {
        await api.skills.update(editingSkill.skill_id, payload);
        addToast('Skill updated', 'success');
      } else {
        await api.skills.create(payload);
        addToast('Skill created', 'success');
      }
      showSkillForm = false;
      await loadData();
    } catch (err) {
      addToast(err instanceof Error ? err.message : 'Failed to save skill', 'error');
    }
  }

  async function importSkill() {
    const tags = importForm.tags.split(',').map(t => t.trim()).filter(Boolean);
    try {
      if (importMode === 'url' && !importForm.url.trim()) {
        addToast('URL is required', 'error');
        return;
      }
      if (importMode === 'content' && !importForm.content.trim()) {
        addToast('Content is required', 'error');
        return;
      }
      if (importMode === 'package' && !importForm.packageB64) {
        addToast('Package file is required', 'error');
        return;
      }
      await api.skills.import({
        url: importMode === 'url' ? importForm.url.trim() : undefined,
        content: importMode === 'content' ? importForm.content : undefined,
        content_b64: importMode === 'package' ? importForm.packageB64 : undefined,
        filename: importMode === 'package' ? importForm.packageName : undefined,
        format: importMode === 'content' ? (importForm.contentFormat || undefined) : (importMode === 'package' ? 'cognis_package' : undefined),
        name: importForm.name.trim() || undefined,
        tags: tags.length ? tags : undefined,
        attach_to_all_agents: importForm.autoLoad
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
      if (result.warnings.length > 0) {
        addToast(result.warnings.join(' '), 'warning');
      }
      let blob: Blob;
      if (result.content_b64) {
        const bytes = Uint8Array.from(atob(result.content_b64), (char) => char.charCodeAt(0));
        blob = new Blob([bytes], { type: result.content_type || 'application/octet-stream' });
      } else {
        blob = new Blob([result.content || ''], { type: result.content_type || 'text/plain' });
      }
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
    if (skill.is_system) {
      addToast('System skills cannot be deleted', 'error');
      return;
    }
    if (skill.source !== 'db' && skill.source !== 'imported') {
      addToast('Cannot delete read-only skills', 'error');
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

  async function resetSkill(skill: Skill) {
    const confirmed = await confirmAction({
      title: 'Reset system skill?',
      message: `Reset ${skill.name} to the shipped default content? This creates a new skill version and discards local edits to the current content.`,
      confirmLabel: 'Reset skill',
      cancelLabel: 'Keep current version',
      variant: 'danger'
    });
    if (!confirmed) return;
    try {
      await api.skills.reset(skill.skill_id);
      addToast('Skill reset to default', 'success');
      await loadData();
    } catch (err) {
      addToast('Failed to reset skill', 'error');
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
          <button class="w-full px-4 py-3 bg-zinc-800 border-b border-zinc-700 flex items-center gap-2 text-left hover:bg-zinc-750 transition-colors" onclick={() => toggleGroup(group.category)}>
            {#if expandedGroups.has(group.category)}
              <ChevronDown class="w-4 h-4 text-zinc-500 shrink-0" />
            {:else}
              <ChevronRight class="w-4 h-4 text-zinc-500 shrink-0" />
            {/if}
            <svelte:component this={getCategoryIcon(group.category)} class="w-4 h-4 text-zinc-400" />
            <span class="text-sm font-medium text-zinc-200">{getCategoryLabel(group.category)}</span>
            <Badge>{group.tools.length}</Badge>
            <span class="text-xs text-zinc-500 ml-auto">{formatSourceSummary(group.sourceTypes)}</span>
          </button>
          {#if expandedGroups.has(group.category)}
            <div class="divide-y divide-zinc-700/50">
              {#each group.tools as tool}
                {@const toolKey = getToolKey(tool)}
                {@render toolRow(tool, toolKey)}
              {/each}
            </div>
          {/if}
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
          {@const groupKey = `intaris:${serverGroup.serverName}`}
          <div class="bg-zinc-800/50 border border-zinc-700 rounded-lg overflow-hidden">
            <button class="w-full px-4 py-3 bg-zinc-800 border-b border-zinc-700 flex items-center gap-2 text-left hover:bg-zinc-750 transition-colors" onclick={() => toggleGroup(groupKey)}>
              {#if expandedGroups.has(groupKey)}
                <ChevronDown class="w-4 h-4 text-zinc-500 shrink-0" />
              {:else}
                <ChevronRight class="w-4 h-4 text-zinc-500 shrink-0" />
              {/if}
              <Plug class="w-4 h-4 text-zinc-400" />
              <span class="text-sm font-medium text-zinc-200">{serverGroup.serverName}</span>
              <Badge>{serverGroup.tools.length}</Badge>
              <span class="text-xs text-zinc-500 ml-auto">Intaris MCP</span>
            </button>
            {#if expandedGroups.has(groupKey)}
              <div class="divide-y divide-zinc-700/50">
                {#each serverGroup.tools as tool}
                  {@const toolKey = getToolKey(tool)}
                  {@render mcpToolRow(tool, toolKey)}
                {/each}
              </div>
            {/if}
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
          {@const groupKey = `executor:${serverGroup.serverName}`}
          <div class="bg-zinc-800/50 border border-zinc-700 rounded-lg overflow-hidden">
            <button class="w-full px-4 py-3 bg-zinc-800 border-b border-zinc-700 flex items-center gap-2 text-left hover:bg-zinc-750 transition-colors" onclick={() => toggleGroup(groupKey)}>
              {#if expandedGroups.has(groupKey)}
                <ChevronDown class="w-4 h-4 text-zinc-500 shrink-0" />
              {:else}
                <ChevronRight class="w-4 h-4 text-zinc-500 shrink-0" />
              {/if}
              <Plug class="w-4 h-4 text-zinc-400" />
              <span class="text-sm font-medium text-zinc-200">{serverGroup.serverName}</span>
              <Badge>{serverGroup.tools.length}</Badge>
              <Badge class="border-sky-500/30 bg-sky-500/10 text-sky-300">cached</Badge>
              <span class="text-xs text-zinc-500 ml-auto">Executor MCP</span>
            </button>
            {#if expandedGroups.has(groupKey)}
              <div class="divide-y divide-zinc-700/50">
                {#each serverGroup.tools as tool}
                  {@const toolKey = getToolKey(tool)}
                  {@render mcpToolRow(tool, toolKey)}
                {/each}
              </div>
            {/if}
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
    <div class="space-y-4">
      <div class="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
        <p class="text-sm text-zinc-400">Skills are versioned instruction, tool, and asset bundles stored in Cognis and staged to executors only when needed.</p>
        <div class="flex flex-wrap gap-2">
          <Button variant="ghost" size="sm" onclick={openImportForm}><Import class="w-4 h-4 mr-1" /> Import</Button>
          <Button variant="primary" size="sm" onclick={() => openSkillForm()}><Plus class="w-4 h-4 mr-1" /> New Skill</Button>
        </div>
      </div>

      {#if showImportForm}
        <div class="bg-zinc-800 border border-zinc-700 rounded-lg p-4 space-y-4">
          <h3 class="text-lg font-medium text-zinc-100">Import Skill</h3>
          <div class="flex flex-wrap gap-2">
            <Button size="sm" variant={importMode === 'url' ? 'primary' : 'secondary'} onclick={() => importMode = 'url'}>URL</Button>
            <Button size="sm" variant={importMode === 'content' ? 'primary' : 'secondary'} onclick={() => importMode = 'content'}>Paste content</Button>
            <Button size="sm" variant={importMode === 'package' ? 'primary' : 'secondary'} onclick={() => importMode = 'package'}>Package</Button>
          </div>
          {#if importMode === 'url'}
            <p class="text-sm text-zinc-400">Import a SKILL.md file or GitHub skill URL. Cognis will best-effort parse upstream/community formats.</p>
            <label class="block text-sm text-zinc-400 space-y-1"><span>URL</span><input type="url" bind:value={importForm.url} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="https://github.com/user/repo/blob/main/skills/my-skill/SKILL.md" /></label>
          {:else if importMode === 'content'}
            <p class="text-sm text-zinc-400">Paste SKILL.md or Cognis YAML content directly.</p>
            <label class="block text-sm text-zinc-400 space-y-1">
              <span>Format (optional)</span>
              <select bind:value={importForm.contentFormat} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500">
                <option value="">Auto-detect</option>
                <option value="skill_md">SKILL.md</option>
                <option value="cognis_yaml">Cognis YAML</option>
              </select>
            </label>
            <label class="block text-sm text-zinc-400 space-y-1"><span>Content</span><textarea bind:value={importForm.content} rows="12" class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 font-mono focus:outline-none focus:border-blue-500" placeholder="---&#10;name: my-skill&#10;---&#10;&#10;Instructions..."></textarea></label>
          {:else}
            <p class="text-sm text-zinc-400">Upload a full Cognis package to import instructions together with assets.</p>
            <label class="block text-sm text-zinc-400 space-y-1"><span>Package file</span><input type="file" accept=".zip,application/zip" onchange={loadPackageFile} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" /></label>
            {#if importForm.packageName}
              <p class="text-xs text-zinc-500">Loaded {importForm.packageName}</p>
            {/if}
          {/if}
          <div class="grid gap-4 md:grid-cols-2">
            <label class="block text-sm text-zinc-400 space-y-1"><span>Name override (optional)</span><input type="text" bind:value={importForm.name} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="Leave empty to use imported name" /></label>
            <label class="block text-sm text-zinc-400 space-y-1"><span>Tags (comma-separated)</span><input type="text" bind:value={importForm.tags} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="e.g. imported, claude" /></label>
          </div>
          <label class="flex items-center gap-2 text-sm text-zinc-400"><input type="checkbox" bind:checked={importForm.autoLoad} class="rounded border-zinc-600" /> Attach to all agents</label>
          <div class="flex gap-2 justify-end">
            <Button variant="ghost" size="sm" onclick={() => showImportForm = false}>Cancel</Button>
            <Button variant="primary" size="sm" onclick={importSkill}>Import</Button>
          </div>
        </div>
      {/if}

      {#if showSkillForm}
        <div class="bg-zinc-800 border border-zinc-700 rounded-lg p-4 space-y-4">
          <h3 class="text-lg font-medium text-zinc-100">{editingSkill ? 'Edit Skill' : 'New Skill'}</h3>
          <div class="grid gap-4 md:grid-cols-2">
            <label class="block text-sm text-zinc-400 space-y-1"><span>Name</span><input type="text" bind:value={skillForm.name} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="e.g. git-release" /></label>
            <label class="block text-sm text-zinc-400 space-y-1"><span>Tags (comma-separated)</span><input type="text" bind:value={skillForm.tags} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="e.g. git, release, automation" /></label>
          </div>
          <label class="block text-sm text-zinc-400 space-y-1"><span>Description</span><input type="text" bind:value={skillForm.description} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="Brief description of what this skill does" /></label>
          <label class="block text-sm text-zinc-400 space-y-1"><span>Instructions (Markdown)</span><textarea bind:value={skillForm.instructions} rows="10" class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 font-mono focus:outline-none focus:border-blue-500" placeholder="# Skill Instructions&#10;&#10;Detailed instructions for the agent..."></textarea></label>
          <div class="grid gap-4 xl:grid-cols-2">
            <label class="block text-sm text-zinc-400 space-y-1"><span>Tools JSON</span><textarea bind:value={skillForm.toolsJson} rows="10" class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 font-mono focus:outline-none focus:border-blue-500" placeholder="[]"></textarea></label>
            <label class="block text-sm text-zinc-400 space-y-1"><span>Prompt templates JSON</span><textarea bind:value={skillForm.promptTemplatesJson} rows="10" class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 font-mono focus:outline-none focus:border-blue-500" placeholder="object"></textarea></label>
          </div>
          <label class="block text-sm text-zinc-400 space-y-1"><span>Secret placeholders (comma-separated)</span><input type="text" bind:value={skillForm.secretPlaceholders} class="w-full px-3 py-2 bg-zinc-900 border border-zinc-700 rounded-lg text-sm text-zinc-200 focus:outline-none focus:border-blue-500" placeholder="API_KEY, ACCESS_TOKEN" /></label>
          <div class="space-y-3 rounded-lg border border-zinc-700 bg-zinc-900/60 p-4">
            <div class="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
              <div>
                <p class="text-sm font-medium text-zinc-100">Assets</p>
                <p class="text-xs text-zinc-500">Upload files once, then Cognis versions and stages them on executors.</p>
              </div>
              <label class="inline-flex cursor-pointer items-center gap-2 rounded-lg border border-zinc-700 px-3 py-2 text-sm text-zinc-200 hover:border-zinc-600">
                <Upload class="h-4 w-4" /> Add files
                <input type="file" multiple class="hidden" onchange={uploadSkillAssets} />
              </label>
            </div>
            {#if skillForm.assets.length > 0}
              <div class="space-y-2">
                {#each skillForm.assets as asset}
                  <div class="flex flex-col gap-2 rounded-lg border border-zinc-700/70 bg-zinc-950/70 p-3 md:flex-row md:items-center md:justify-between">
                    <div>
                      <p class="font-mono text-xs text-zinc-100">{asset.filename}</p>
                      <p class="mt-1 text-xs text-zinc-500">{asset.content_type || 'application/octet-stream'}{#if asset.size_bytes} · {asset.size_bytes} bytes{/if}</p>
                    </div>
                    <button class="inline-flex items-center gap-1 text-xs text-zinc-400 hover:text-red-300" type="button" onclick={() => removeSkillAsset(asset.filename)}>
                      <X class="h-3.5 w-3.5" /> Remove
                    </button>
                  </div>
                {/each}
              </div>
            {:else}
              <p class="text-xs text-zinc-500">No assets attached.</p>
            {/if}
          </div>
          <label class="flex items-center gap-2 text-sm text-zinc-400"><input type="checkbox" bind:checked={skillForm.autoLoad} class="rounded border-zinc-600" /> Attach to all agents</label>
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
              <div class="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                <div class="flex-1">
                  <div class="flex items-center gap-2">
                    <BookOpen class="w-4 h-4 text-zinc-400" />
                    <span class="font-medium text-zinc-100">{skill.name}</span>
                    <Badge>{skill.source}</Badge>
                    {#if skill.is_system}<Badge class="border-amber-500/30 bg-amber-500/10 text-amber-300">system</Badge>{/if}
                    {#if skill.attach_to_all_agents ?? skill.auto_load}<Badge class="border-blue-500/30 bg-blue-500/10 text-blue-300">attached to all agents</Badge>{/if}
                  </div>
                  {#if skill.description}<p class="text-sm text-zinc-400 mt-1">{skill.description}</p>{/if}
                  {#if skill.tags && skill.tags.length > 0}<div class="flex gap-1 mt-2">{#each skill.tags as tag}<Badge>{tag}</Badge>{/each}</div>{/if}
                </div>
                <div class="flex flex-wrap gap-1">
                  <button class="p-1.5 text-zinc-400 hover:text-zinc-200 rounded" onclick={() => { selectedSkillId = skill.skill_id; }} title="View skill"><Eye class="w-4 h-4" /></button>
                  <button class="p-1.5 text-zinc-400 hover:text-zinc-200 rounded" onclick={() => exportSkill(skill)} title="Export as SKILL.md"><Download class="w-4 h-4" /></button>
                  <button class="p-1.5 text-zinc-400 hover:text-zinc-200 rounded" onclick={() => exportSkill(skill, 'cognis_package')} title="Export package"><FileText class="w-4 h-4" /></button>
                  {#if skill.source === 'db' || skill.source === 'imported'}
                    {#if skill.is_system}
                      <button class="p-1.5 text-zinc-400 hover:text-amber-300 rounded" onclick={() => resetSkill(skill)} title="Reset to default"><ShieldCheck class="w-4 h-4" /></button>
                      <Badge>read-only</Badge>
                    {:else}
                      <button class="p-1.5 text-zinc-400 hover:text-zinc-200 rounded" onclick={() => openSkillForm(skill)} title="Edit"><Pencil class="w-4 h-4" /></button>
                      <button class="p-1.5 text-zinc-400 hover:text-red-400 rounded" onclick={() => deleteSkill(skill)} title="Delete"><Trash2 class="w-4 h-4" /></button>
                    {/if}
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
                  {#if skill.current_version.secret_placeholders && skill.current_version.secret_placeholders.length > 0}<span>Secrets: <span class="text-zinc-400">{skill.current_version.secret_placeholders.length}</span></span>{/if}
                </div>
              {/if}
            </div>
          {/each}
        </div>
      {/if}

      <SkillDetailSheet
        open={selectedSkillId !== null}
        skill={selectedSkill}
        onClose={() => { selectedSkillId = null; }}
        allowRestore={Boolean(selectedSkill && !selectedSkill.is_system && (selectedSkill.source === 'db' || selectedSkill.source === 'imported'))}
        onRestored={async () => {
          await loadData();
        }}
      />
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
        <!-- Mobile: stacked card per parameter. Desktop: 3-column table. -->
        <div class="mt-1.5 md:hidden space-y-2">
          {#each Object.entries(tool.parameters.properties) as [paramName, param]}
            {@const isRequired = (tool.parameters.required || []).includes(paramName)}
            <div class="rounded-lg border border-zinc-700/50 bg-zinc-900/60 p-3">
              <div class="flex items-start justify-between gap-3">
                <span class="font-mono text-sm text-zinc-100 break-all">{paramName}{#if isRequired}<span class="text-red-400 ml-0.5">*</span>{/if}</span>
                <span class="shrink-0 font-mono text-xs text-zinc-400">{param.type || 'any'}</span>
              </div>
              {#if param.enum}
                <p class="mt-1 text-xs text-zinc-500">({param.enum.join(' | ')})</p>
              {/if}
              {#if param.description}
                <p class="mt-2 text-xs text-zinc-400">{param.description}</p>
              {/if}
            </div>
          {/each}
        </div>
        <div class="mt-1.5 hidden rounded-lg border border-zinc-700/50 md:block overflow-x-auto">
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
