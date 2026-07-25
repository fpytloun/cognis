import { fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { auth } from '$lib/stores/auth';
import type { LocalModelCatalogItem } from '$lib/types/api';
import Page from './+page.svelte';

function catalogModel(): LocalModelCatalogItem {
  return {
    catalog_id: 'ollama:qwen3:8b',
    source: 'ollama',
    requested_ref: 'qwen3:8b',
    title: 'Qwen 3 8B',
    publisher: 'Qwen',
    repository_url: null,
    model_card_url: null,
    revision_sha: null,
    license: 'Apache-2.0',
    description: 'Test model',
    downloads: null,
    likes: null,
    last_modified: null,
    pipeline_tag: null,
    tags: [],
    base_models: [],
    capabilities: ['chat'],
    parameter_count: 8_000_000_000,
    quantizations: [
      {
        name: 'Q4_K_M',
        requested_ref: 'qwen3:8b',
        file_name: null,
        size_bytes: 5_000_000_000,
        bits_per_weight: 4.5
      }
    ],
    file_size_bytes: 5_000_000_000,
    advertised_max_context: 32768,
    architecture: {},
    architecture_name: null,
    metadata_status: 'complete',
    metadata_confidence: 'high',
    metadata_diagnostics: [],
    reference_integrity: 'floating',
    warnings: []
  };
}

const mocks = vi.hoisted(() => ({
  catalog: vi.fn(),
  detail: vi.fn(),
  deployments: vi.fn(),
  executors: vi.fn(),
  providers: vi.fn(),
  recommendProvider: vi.fn(),
  plan: vi.fn(),
  createDeployment: vi.fn(),
  createManagedDeployment: vi.fn(),
  attachManagedProvider: vi.fn(),
  targets: vi.fn(),
  operations: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
  ApiError: class ApiError extends Error {
    status = 500;
  },
  asApiError: (error: unknown) =>
    error instanceof Error ? error : new Error('Unexpected API error'),
  api: {
    localModels: {
      catalog: mocks.catalog,
      detail: mocks.detail,
      deployments: mocks.deployments,
      targets: mocks.targets,
      operations: mocks.operations,
      resolve: vi.fn(),
      plan: mocks.plan,
      recommendProvider: mocks.recommendProvider,
      createDeployment: mocks.createDeployment,
      createManagedDeployment: mocks.createManagedDeployment,
      attachManagedProvider: mocks.attachManagedProvider,
      findOrCreateProvider: vi.fn(),
      updateDeployment: vi.fn(),
      reconcile: vi.fn()
    },
    executor: { list: mocks.executors },
    llmProviders: { list: mocks.providers }
  }
}));

describe('Local Models page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    auth.updateUser({ email: 'owner@example.com', name: 'Owner', role: 'user' });
    mocks.catalog.mockResolvedValue({
      items: [],
      next_cursor: null,
      cached: false,
      pagination_note: null,
      sources: [
        {
          source: 'installed',
          available: false,
          detail: 'Live inventory is not available yet.',
          retry_after_seconds: null
        },
        {
          source: 'ollama',
          available: false,
          detail: 'Curated catalog is temporarily unavailable.',
          retry_after_seconds: null
        }
      ]
    });
    mocks.deployments.mockResolvedValue([]);
    mocks.executors.mockResolvedValue([]);
    mocks.providers.mockResolvedValue({
      items: [
        {
          provider_id: 'owner-ollama',
          display_name: 'Owner Ollama',
          location: 'executor',
          backend: 'litellm',
          owner_email: 'owner@example.com',
          config: { preset: 'ollama', executor_id: 'owned', models: [] },
          is_default: false,
          status: 'active',
          created_at: null,
          updated_at: null,
          models: [],
          last_test: null
        }
      ],
      next_cursor: null
    });
    mocks.recommendProvider.mockResolvedValue({
      requested_ref: 'qwen3:8b',
      runtime_name: 'qwen3:8b',
      recommended_provider_id: 'owner-ollama',
      candidates: [
        {
          provider_id: 'owner-ollama',
          display_name: 'Owner Ollama',
          owner_email: 'owner@example.com',
          executor_ids: ['owned'],
          contains_model: false,
          managed_local: false,
          healthy_host_count: 1,
          reason_codes: ['compatible_ollama_provider', 'healthy_hosts', 'user_owned']
        }
      ]
    });
    mocks.targets.mockResolvedValue([]);
    mocks.operations.mockResolvedValue([]);
  });

  afterEach(() => {
    auth.clear();
  });

  it('renders catalog failures as non-blocking guidance with accessible navigation', async () => {
    render(Page);

    expect(await screen.findByRole('heading', { name: 'Local Models' })).toBeInTheDocument();
    expect(
      await screen.findByText('Curated catalog is temporarily unavailable.')
    ).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Local model sections' })).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'Catalog source' })).toBeInTheDocument();
    expect(screen.getByRole('textbox', { name: 'Search local model catalog' })).toBeInTheDocument();
    expect(screen.queryByText(/Desired state created/i)).not.toBeInTheDocument();
  });

  it('defaults to the backend recommendation, scopes hosts, and never auto-creates accidentally', async () => {
    mocks.catalog.mockResolvedValue({
      items: [catalogModel()],
      next_cursor: null,
      cached: false,
      pagination_note: null,
      sources: []
    });
    mocks.executors.mockResolvedValue([
      {
        executor_id: 'owned',
        name: 'Owned executor',
        status: 'active',
        runtime_state: 'online',
        local_inference_enabled: true,
        ollama_management_enabled: true,
        owner_email: 'owner@example.com',
        shared: false,
        labels: {}
      },
      {
        executor_id: 'outside',
        name: 'Outside provider',
        status: 'active',
        runtime_state: 'online',
        local_inference_enabled: true,
        ollama_management_enabled: true,
        owner_email: 'owner@example.com',
        shared: false,
        labels: {}
      }
    ]);
    mocks.plan.mockResolvedValue({
      assessment_generation: 1,
      advisory_only: true,
      requested_context_tokens: 32768,
      advertised_max_context: 32768,
      advertised_max_exceeded: false,
      recommended_context_tokens: 32768,
      context_options: [],
      executors: []
    });
    mocks.createDeployment.mockResolvedValue({
      deployment_id: 'created',
      owner_email: 'owner@example.com',
      shared: false,
      runtime_type: 'ollama',
      requested_ref: 'qwen3:8b',
      canonical_name: 'qwen3:8b',
      runtime_name: 'qwen3:8b',
      source: 'ollama',
      digest: null,
      revision: null,
      selector: { executor_ids: ['owned'] },
      desired_state: 'present',
      update_policy: 'if_changed',
      prune_policy: 'retain',
      max_parallel: 1,
      generation: 1,
      provider_id: 'owner-ollama',
      lifecycle_state: 'managed',
      capacity_override_acknowledged: false,
      capacity_assessment_generation: 1,
      reconcile_requested_at: null,
      created_at: '2026-07-14T00:00:00Z',
      updated_at: '2026-07-14T00:00:00Z'
    });

    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'Plan deployment' }));
    expect(await screen.findByRole('combobox', { name: 'Ollama provider' })).toHaveValue(
      'owner-ollama'
    );
    expect(screen.getAllByText('Owned executor')).toHaveLength(2);
    expect(screen.queryByText('Outside provider')).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Estimate fit' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Create deployment' }));

    expect(mocks.createDeployment).toHaveBeenCalledWith(
      expect.objectContaining({ provider_id: 'owner-ollama' })
    );
    expect(mocks.createManagedDeployment).not.toHaveBeenCalled();
  });

  it('ignores a stale provider recommendation after selecting a newer model', async () => {
    const first = catalogModel();
    const second = {
      ...catalogModel(),
      catalog_id: 'ollama:gemma3:4b',
      requested_ref: 'gemma3:4b',
      title: 'Gemma 3 4B',
      quantizations: [
        {
          ...catalogModel().quantizations[0],
          requested_ref: 'gemma3:4b'
        }
      ]
    };
    mocks.catalog.mockResolvedValue({
      items: [first, second],
      next_cursor: null,
      cached: false,
      pagination_note: null,
      sources: []
    });
    mocks.providers.mockResolvedValue({
      items: [
        {
          provider_id: 'provider-a',
          display_name: 'Provider A',
          config: { preset: 'ollama', executor_id: 'owned' },
          models: []
        },
        {
          provider_id: 'provider-b',
          display_name: 'Provider B',
          config: { preset: 'ollama', executor_id: 'owned' },
          models: []
        }
      ],
      next_cursor: null
    });
    let resolveFirst!: (value: unknown) => void;
    let resolveSecond!: (value: unknown) => void;
    mocks.recommendProvider
      .mockReturnValueOnce(new Promise((resolve) => (resolveFirst = resolve)))
      .mockReturnValueOnce(new Promise((resolve) => (resolveSecond = resolve)));

    render(Page);
    const planButtons = await screen.findAllByRole('button', { name: 'Plan deployment' });
    await fireEvent.click(planButtons[0]);
    await fireEvent.click(planButtons[1]);
    expect(screen.getByRole('button', { name: 'Estimating…' })).toBeDisabled();
    resolveSecond({
      requested_ref: 'gemma3:4b',
      runtime_name: 'gemma3:4b',
      recommended_provider_id: 'provider-b',
      candidates: [
        {
          provider_id: 'provider-b',
          display_name: 'Provider B',
          owner_email: 'owner@example.com',
          executor_ids: ['owned'],
          contains_model: false,
          managed_local: false,
          healthy_host_count: 1,
          reason_codes: ['compatible_ollama_provider']
        }
      ]
    });
    resolveFirst({
      requested_ref: 'qwen3:8b',
      runtime_name: 'qwen3:8b',
      recommended_provider_id: 'provider-a',
      candidates: [
        {
          provider_id: 'provider-a',
          display_name: 'Provider A',
          owner_email: 'owner@example.com',
          executor_ids: ['owned'],
          contains_model: false,
          managed_local: false,
          healthy_host_count: 1,
          reason_codes: ['compatible_ollama_provider']
        }
      ]
    });

    await vi.waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Ollama provider' })).toHaveValue('provider-b')
    );
  });

  it('shows provider-resolved disabled hosts with actionable settings guidance', async () => {
    mocks.catalog.mockResolvedValue({
      items: [catalogModel()],
      next_cursor: null,
      cached: false,
      pagination_note: null,
      sources: []
    });
    mocks.executors.mockResolvedValue([
      {
        executor_id: 'owned',
        name: 'Owned executor',
        status: 'active',
        runtime_state: 'online',
        local_inference_enabled: true,
        ollama_management_enabled: false,
        owner_email: 'owner@example.com',
        shared: false,
        labels: {}
      }
    ]);

    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'Plan deployment' }));

    expect(await screen.findByText(/Model management is disabled/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open executor settings' })).toHaveAttribute(
      'href',
      '/settings?tab=executors'
    );
    expect(screen.getAllByRole('checkbox').some((checkbox) => checkbox.hasAttribute('disabled'))).toBe(
      true
    );
  });

  it('uses the accepted query rather than unsubmitted edits for cursor pagination', async () => {
    const model: LocalModelCatalogItem = {
      catalog_id: 'ollama:qwen3:8b',
      source: 'ollama',
      requested_ref: 'qwen3:8b',
      title: 'Qwen 3 8B',
      publisher: 'Qwen',
      repository_url: null,
      model_card_url: null,
      revision_sha: null,
      license: 'Apache-2.0',
      description: 'Test model',
      downloads: null,
      likes: null,
      last_modified: null,
      pipeline_tag: null,
      tags: [],
      base_models: [],
      capabilities: ['chat'],
      parameter_count: 8_000_000_000,
      quantizations: [
        {
          name: 'Q4_K_M',
          requested_ref: 'qwen3:8b',
          file_name: null,
          size_bytes: 5_000_000_000,
          bits_per_weight: 4.5
        }
      ],
      file_size_bytes: 5_000_000_000,
      advertised_max_context: 32768,
      architecture: {},
      architecture_name: null,
      metadata_status: 'complete',
      metadata_confidence: 'high',
      metadata_diagnostics: [],
      reference_integrity: 'floating',
      warnings: []
    };
    mocks.catalog
      .mockResolvedValueOnce({
        items: [model],
        next_cursor: 'next_page',
        cached: false,
        pagination_note: null,
        sources: []
      })
      .mockResolvedValueOnce({
        items: [],
        next_cursor: null,
        cached: false,
        pagination_note: null,
        sources: []
      });

    render(Page);
    expect(await screen.findByText('Qwen 3 8B')).toBeInTheDocument();
    await fireEvent.input(screen.getByRole('textbox', { name: 'Search local model catalog' }), {
      target: { value: 'unsubmitted query' }
    });
    await fireEvent.click(screen.getByRole('button', { name: 'Load more' }));
    await vi.waitFor(() => expect(mocks.catalog).toHaveBeenCalledTimes(2));

    expect(mocks.catalog.mock.calls[1][0]).toMatchObject({
      query: '',
      cursor: 'next_page'
    });
  });

  it('ignores repository details that resolve after a newer catalog search', async () => {
    const basic: LocalModelCatalogItem = {
      catalog_id: 'huggingface:acme/old-GGUF',
      source: 'huggingface',
      requested_ref: 'hf.co/acme/old-GGUF:Q4_K_M',
      title: 'old-GGUF',
      publisher: 'acme',
      repository_url: 'https://huggingface.co/acme/old-GGUF',
      model_card_url: 'https://huggingface.co/acme/old-GGUF#model-card',
      revision_sha: 'a'.repeat(40),
      license: null,
      description: null,
      downloads: null,
      likes: null,
      last_modified: null,
      pipeline_tag: null,
      tags: ['gguf'],
      base_models: [],
      capabilities: ['chat'],
      parameter_count: null,
      quantizations: [
        {
          name: 'Q4_K_M',
          requested_ref: 'hf.co/acme/old-GGUF:Q4_K_M',
          file_name: 'old-Q4_K_M.gguf',
          size_bytes: null,
          bits_per_weight: 4.5
        }
      ],
      file_size_bytes: null,
      advertised_max_context: null,
      architecture: {},
      architecture_name: null,
      metadata_status: 'basic',
      metadata_confidence: 'medium',
      metadata_diagnostics: [],
      reference_integrity: 'floating',
      warnings: []
    };
    let resolveDetail!: (value: LocalModelCatalogItem) => void;
    mocks.detail.mockReturnValue(
      new Promise<LocalModelCatalogItem>((resolve) => {
        resolveDetail = resolve;
      })
    );
    mocks.catalog
      .mockResolvedValueOnce({
        items: [basic],
        next_cursor: null,
        cached: false,
        pagination_note: null,
        sources: []
      })
      .mockResolvedValueOnce({
        items: [{ ...basic, catalog_id: 'huggingface:acme/new-GGUF', title: 'new-GGUF' }],
        next_cursor: null,
        cached: false,
        pagination_note: null,
        sources: []
      });

    render(Page);
    expect(await screen.findByText('old-GGUF')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('new-GGUF')).toBeInTheDocument();
    resolveDetail({ ...basic, description: 'Stale detail must not render.' });
    await Promise.resolve();
    await Promise.resolve();

    expect(screen.queryByText('Stale detail must not render.')).not.toBeInTheDocument();
    expect(screen.queryByText('old-GGUF')).not.toBeInTheDocument();
  });

  it('attaches a newer page to an existing same-revision detail request', async () => {
    const basic: LocalModelCatalogItem = {
      catalog_id: 'huggingface:acme/model-GGUF',
      source: 'huggingface',
      requested_ref: 'hf.co/acme/model-GGUF:Q4_K_M',
      title: 'model-GGUF',
      publisher: 'acme',
      repository_url: 'https://huggingface.co/acme/model-GGUF',
      model_card_url: 'https://huggingface.co/acme/model-GGUF#model-card',
      revision_sha: 'b'.repeat(40),
      license: null,
      description: null,
      downloads: null,
      likes: null,
      last_modified: null,
      pipeline_tag: null,
      tags: ['gguf'],
      base_models: [],
      capabilities: ['chat'],
      parameter_count: null,
      quantizations: [
        {
          name: 'Q4_K_M',
          requested_ref: 'hf.co/acme/model-GGUF:Q4_K_M',
          file_name: 'model-Q4_K_M.gguf',
          size_bytes: null,
          bits_per_weight: 4.5
        }
      ],
      file_size_bytes: null,
      advertised_max_context: null,
      architecture: {},
      architecture_name: null,
      metadata_status: 'basic',
      metadata_confidence: 'medium',
      metadata_diagnostics: [],
      reference_integrity: 'floating',
      warnings: []
    };
    let resolveDetail!: (value: LocalModelCatalogItem) => void;
    mocks.detail.mockReturnValue(
      new Promise<LocalModelCatalogItem>((resolve) => {
        resolveDetail = resolve;
      })
    );
    mocks.catalog.mockResolvedValue({
      items: [basic],
      next_cursor: null,
      cached: false,
      pagination_note: null,
      sources: []
    });

    render(Page);
    expect(await screen.findByText('model-GGUF')).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await vi.waitFor(() => expect(mocks.catalog).toHaveBeenCalledTimes(2));
    await fireEvent.change(screen.getByLabelText('Selected quant size'), {
      target: { value: 'le4gib' }
    });
    resolveDetail({
      ...basic,
      description: 'Hydrated on the newer page.',
      quantizations: [{ ...basic.quantizations[0], size_bytes: 5_000_000_000 }],
      file_size_bytes: 5_000_000_000,
      metadata_status: 'complete',
      metadata_confidence: 'high'
    });

    expect(await screen.findByText('Hydrated on the newer page.')).toBeInTheDocument();
    expect(mocks.detail).toHaveBeenCalledTimes(1);
  });

  it('invalidates a completed capacity plan when selected metadata hydrates', async () => {
    const basic: LocalModelCatalogItem = {
      catalog_id: 'huggingface:acme/plan-GGUF',
      source: 'huggingface',
      requested_ref: 'hf.co/acme/plan-GGUF:Q4_K_M',
      title: 'plan-GGUF',
      publisher: 'acme',
      repository_url: 'https://huggingface.co/acme/plan-GGUF',
      model_card_url: 'https://huggingface.co/acme/plan-GGUF#model-card',
      revision_sha: 'c'.repeat(40),
      license: null,
      description: null,
      downloads: null,
      likes: null,
      last_modified: null,
      pipeline_tag: null,
      tags: ['gguf'],
      base_models: [],
      capabilities: ['chat'],
      parameter_count: null,
      quantizations: [
        {
          name: 'Q4_K_M',
          requested_ref: 'hf.co/acme/plan-GGUF:Q4_K_M',
          file_name: 'plan-Q4_K_M.gguf',
          size_bytes: null,
          bits_per_weight: 4.5
        }
      ],
      file_size_bytes: null,
      advertised_max_context: null,
      architecture: {},
      architecture_name: null,
      metadata_status: 'basic',
      metadata_confidence: 'medium',
      metadata_diagnostics: [],
      reference_integrity: 'floating',
      warnings: []
    };
    let resolveDetail!: (value: LocalModelCatalogItem) => void;
    mocks.detail.mockReturnValue(
      new Promise<LocalModelCatalogItem>((resolve) => {
        resolveDetail = resolve;
      })
    );
    mocks.catalog.mockResolvedValue({
      items: [basic],
      next_cursor: null,
      cached: false,
      pagination_note: null,
      sources: []
    });
    mocks.executors.mockResolvedValue([
      {
        executor_id: 'owned',
        name: 'Owned executor',
        status: 'active',
        owner_email: 'owner@example.com',
        shared: false,
        labels: {}
      }
    ]);
    mocks.plan.mockResolvedValue({
      assessment_generation: 7,
      advisory_only: true,
      requested_context_tokens: 32768,
      advertised_max_context: null,
      advertised_max_exceeded: false,
      recommended_context_tokens: 32768,
      context_options: [],
      executors: [
        {
          executor_id: 'owned',
          executor_name: 'Owned executor',
          context_tokens: 32768,
          static: {
            status: 'FIT',
            confidence: 'high',
            available_bytes: 64,
            accelerator_available_bytes: null,
            host_available_bytes: 64,
            reason_codes: ['host_memory_sufficient']
          },
          admission: {
            status: 'FIT',
            confidence: 'high',
            available_bytes: 64,
            accelerator_available_bytes: null,
            host_available_bytes: 64,
            reason_codes: ['host_memory_sufficient']
          },
          breakdown: {
            weights_bytes: 4,
            kv_cache_min_bytes: 1,
            kv_cache_max_bytes: 2,
            runtime_buffer_bytes: 1,
            reserved_headroom_bytes: 2,
            required_min_bytes: 8,
            required_max_bytes: 9
          },
          unified_memory: false,
          snapshot_age_seconds: 1,
          advertised_max_exceeded: false,
          assumptions: []
        }
      ]
    });

    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'Plan deployment' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Estimate fit' }));
    expect(await screen.findByRole('button', { name: 'Create deployment' })).toBeInTheDocument();

    resolveDetail({
      ...basic,
      parameter_count: 8_000_000_000,
      quantizations: [{ ...basic.quantizations[0], size_bytes: 5_000_000_000 }],
      file_size_bytes: 5_000_000_000,
      metadata_status: 'complete',
      metadata_confidence: 'high'
    });

    await vi.waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Create deployment' })).not.toBeInTheDocument()
    );
    expect(screen.getByRole('status')).toHaveTextContent('Run estimate');
  });

  it('invalidates stale plans, hides non-mutable executors, and preserves successful writes', async () => {
    const model = {
      catalog_id: 'ollama:qwen3:8b',
      source: 'ollama',
      requested_ref: 'qwen3:8b',
      title: 'Qwen 3 8B',
      publisher: 'Qwen',
      repository_url: null,
      model_card_url: null,
      revision_sha: null,
      license: 'Apache-2.0',
      description: 'Test model',
      downloads: null,
      likes: null,
      last_modified: null,
      pipeline_tag: null,
      tags: [],
      base_models: [],
      capabilities: ['chat'],
      parameter_count: 8_000_000_000,
      quantizations: [
        {
          name: 'Q4_K_M',
          requested_ref: 'qwen3:8b',
          file_name: null,
          size_bytes: 5_000_000_000,
          bits_per_weight: 4.5
        }
      ],
      file_size_bytes: 5_000_000_000,
      advertised_max_context: 131072,
      architecture: {},
      architecture_name: null,
      metadata_status: 'complete',
      metadata_confidence: 'high',
      metadata_diagnostics: [],
      reference_integrity: 'floating',
      warnings: []
    };
    const assessment = {
      assessment_generation: 99,
      advisory_only: true,
      requested_context_tokens: 32768,
      advertised_max_context: 131072,
      advertised_max_exceeded: false,
      recommended_context_tokens: 32768,
      context_options: [],
      executors: [
        {
          executor_id: 'owned',
          executor_name: 'Owned executor',
          context_tokens: 32768,
          static: {
            status: 'FIT',
            confidence: 'high',
            available_bytes: 64,
            accelerator_available_bytes: null,
            host_available_bytes: 64,
            reason_codes: ['host_memory_sufficient']
          },
          admission: {
            status: 'FIT',
            confidence: 'high',
            available_bytes: 64,
            accelerator_available_bytes: null,
            host_available_bytes: 64,
            reason_codes: ['host_memory_sufficient']
          },
          breakdown: {
            weights_bytes: 5,
            kv_cache_min_bytes: 1,
            kv_cache_max_bytes: 2,
            runtime_buffer_bytes: 1,
            reserved_headroom_bytes: 2,
            required_min_bytes: 9,
            required_max_bytes: 10
          },
          unified_memory: false,
          snapshot_age_seconds: 1,
          advertised_max_exceeded: false,
          assumptions: []
        }
      ]
    };
    mocks.catalog.mockResolvedValue({
      items: [model],
      next_cursor: null,
      cached: false,
      pagination_note: null,
      sources: []
    });
    mocks.executors.mockResolvedValue([
      {
        executor_id: 'owned',
        name: 'Owned executor',
        status: 'active',
        owner_email: 'owner@example.com',
        shared: false,
        labels: {}
      },
      {
        executor_id: 'shared',
        name: 'Shared executor',
        status: 'active',
        owner_email: 'system@cognis.local',
        shared: true,
        labels: {}
      }
    ]);
    let resolvePendingPlan!: (value: typeof assessment) => void;
    const pendingPlan = new Promise<typeof assessment>((resolve) => {
      resolvePendingPlan = resolve;
    });
    let rejectStalePlan!: (reason: Error) => void;
    const staleRejectedPlan = new Promise<typeof assessment>((_resolve, reject) => {
      rejectStalePlan = reject;
    });
    mocks.plan
      .mockReturnValueOnce(pendingPlan)
      .mockResolvedValueOnce(assessment)
      .mockReturnValueOnce(staleRejectedPlan)
      .mockResolvedValue(assessment);
    mocks.createDeployment.mockResolvedValue({
      deployment_id: 'lmd_created',
      owner_email: 'owner@example.com',
      shared: false,
      runtime_type: 'ollama',
      requested_ref: 'qwen3:8b',
      canonical_name: 'qwen3:8b',
      runtime_name: 'qwen3:8b',
      source: 'ollama',
      digest: null,
      revision: '8b',
      selector: { executor_ids: ['owned'] },
      desired_state: 'present',
      update_policy: 'if_changed',
      prune_policy: 'retain',
      max_parallel: 1,
      generation: 1,
      provider_id: 'owner-ollama',
      lifecycle_state: 'managed',
      capacity_override_acknowledged: false,
      capacity_assessment_generation: 99,
      reconcile_requested_at: null,
      created_at: '2026-07-13T00:00:00Z',
      updated_at: '2026-07-13T00:00:00Z'
    });

    render(Page);
    await fireEvent.click(await screen.findByRole('button', { name: 'Plan deployment' }));
    expect(screen.queryByText('Shared executor')).not.toBeInTheDocument();
    expect(screen.getByText(/1 shared or non-mutable executor is hidden/i)).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'Estimate fit' }));
    await fireEvent.input(screen.getByLabelText('Custom context tokens'), {
      target: { value: '200000' }
    });
    resolvePendingPlan(assessment);
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByRole('button', { name: 'Create deployment' })).not.toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Run estimate');

    await fireEvent.click(screen.getByRole('button', { name: 'Estimate fit' }));
    expect(await screen.findByRole('button', { name: 'Create deployment' })).toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Estimate fit' }));
    await fireEvent.input(screen.getByLabelText('Custom context tokens'), {
      target: { value: '210000' }
    });
    expect(screen.queryByRole('button', { name: 'Create deployment' })).not.toBeInTheDocument();
    await fireEvent.click(screen.getByRole('button', { name: 'Estimate fit' }));
    expect(await screen.findByRole('button', { name: 'Create deployment' })).toBeInTheDocument();
    rejectStalePlan(new Error('obsolete request failed'));
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create deployment' })).toBeInTheDocument();
    mocks.targets.mockRejectedValueOnce(new Error('runtime unavailable'));
    await fireEvent.click(await screen.findByRole('button', { name: 'Create deployment' }));

    expect(
      await screen.findByText(/Desired state was created, but runtime target/i)
    ).toBeInTheDocument();
    expect(screen.queryByText('Failed to create deployment')).not.toBeInTheDocument();
  });
});
