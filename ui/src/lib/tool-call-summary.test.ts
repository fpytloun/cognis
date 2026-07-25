import { describe, expect, it } from 'vitest';
import {
  delegationToolCallDisplayTitle,
  managedConversationToolPresentation,
  memoryToolPresentation,
  nativeInspectionToolPresentation,
  skillLoadDisplayName,
  stepTodoWriteStatusSummary,
  toolOutputHelperPresentation,
  webToolPresentation,
  workflowToolPresentation
} from './tool-call-summary';

describe('tool call summaries', () => {
  it('builds web search cards with result and lazy image references', () => {
    expect(webToolPresentation({
      toolName: 'web_search',
      status: 'completed',
      arguments: { query: 'example charts', include_images: true },
      result: [
        '[[result:1]]',
        '[1] Example chart',
        '    URL: https://example.com/chart',
        '    Domain: example.com',
        '    Snippet: Quarterly results.',
        '',
        '[[media:1]]',
        'URL: https://cdn.example.com/chart.png',
        'Source: duckduckgo_image_search',
        'Lazy artifact: tool_artifact:call_123:media:1',
      ].join('\n'),
    })).toMatchObject({
      kind: 'web_tool',
      webKind: 'search',
      requestText: 'example charts',
      results: [{ title: 'Example chart', url: 'https://example.com/chart' }],
      media: [{ url: 'https://cdn.example.com/chart.png', artifactRef: 'tool_artifact:call_123:media:1' }],
    });
  });

  it('builds web fetch cards with extracted content and image references', () => {
    expect(webToolPresentation({
      toolName: 'web_fetch',
      status: 'completed',
      arguments: { url: 'https://example.com/article', format: 'markdown' },
      result: [
        '[[page:1]]',
        'URL: https://example.com/article',
        'Domain: example.com',
        '',
        '# Example article',
        'Body content.',
        '',
        '[[media:1]]',
        'URL: https://cdn.example.com/hero.jpg',
        'Role: hero',
        'Lazy artifact: tool_artifact:call_123:media:1',
      ].join('\n'),
    })).toMatchObject({
      kind: 'web_tool',
      webKind: 'fetch',
      requestText: 'https://example.com/article',
      content: '# Example article\nBody content.',
      media: [{ url: 'https://cdn.example.com/hero.jpg' }],
    });
  });

  it('keeps skill_load focused on the resolved skill name', () => {
    expect(skillLoadDisplayName({
      toolName: 'skill_load',
      status: 'completed',
      result: JSON.stringify({
        name: 'Cognis Coding',
        loaded: true,
        tool_count: 2
      })
    })).toBe('Cognis Coding');
  });

  it('summarizes step_todo_write active and pending todos from the result', () => {
    expect(stepTodoWriteStatusSummary({
      toolName: 'step_todo_write',
      status: 'completed',
      result: JSON.stringify({
        status: 'updated',
        todos: [
          { content: 'Implement summary', status: 'in_progress' },
          { content: 'Run tests', status: 'pending' },
          { content: 'Commit', status: 'pending' }
        ]
      })
    })).toBe('1 active, 2 pending');
  });

  it('uses explicit step_todo_write status summaries from the backend when present', () => {
    expect(stepTodoWriteStatusSummary({
      toolName: 'step_todo_write',
      status: 'completed',
      result: JSON.stringify({
        status_summary: '1 active, 2 pending'
      })
    })).toBe('1 active, 2 pending');
  });

  it('falls back to step_todo_write arguments while running', () => {
    expect(stepTodoWriteStatusSummary({
      toolName: 'step_todo_write',
      status: 'started',
      arguments: {
        todos: [
          { content: 'Implement summary', status: 'in_progress' },
          { content: 'Run tests', status: 'pending' }
        ]
      }
    })).toBe('1 active, 1 pending');
  });

  it('includes terminal todo states when present', () => {
    expect(stepTodoWriteStatusSummary({
      toolName: 'step_todo_write',
      status: 'completed',
      result: JSON.stringify({
        todos: [
          { content: 'Implement summary', status: 'completed' },
          { content: 'Skip unrelated cleanup', status: 'cancelled' }
        ]
      })
    })).toBe('1 completed, 1 cancelled');
  });

  it('prefers explicit delegation titles over detailed task prompts', () => {
    expect(delegationToolCallDisplayTitle({
      title: 'Explore chat rendering',
      task: 'Read the relevant chat rendering files and trace the whole delegate flow.'
    })).toBe('Explore chat rendering');
  });

  it('falls back to the delegation task when no title is present', () => {
    expect(delegationToolCallDisplayTitle({
      task: 'Trace delegate rendering.'
    })).toBe('Trace delegate rendering.');
  });

  it('uses a follow-up instruction when no delegation title or task is available', () => {
    expect(delegationToolCallDisplayTitle({
      instruction: 'Re-review the implementation after the fix.'
    })).toBe('Re-review the implementation after the fix.');
  });

  it('builds managed conversation presentations for running create calls', () => {
    expect(managedConversationToolPresentation({
      toolName: 'agent_conversation_create',
      status: 'started',
      arguments: {
        title: 'Stage 4 B2',
        agent_id: 'laforge',
        chat_mode: 'build',
        initial_message: 'Implement the runtime frame construction.'
      }
    })).toMatchObject({
      kind: 'managed_conversation_tool',
      title: 'Start managed conversation',
      requestLabel: 'Conversation',
      requestText: 'Stage 4 B2',
      resultSummary: 'Managed conversation operation is running.',
      primaryConversation: {
        title: 'Stage 4 B2',
        agentId: 'laforge',
        status: 'running'
      }
    });
  });

  it('builds managed conversation presentations from wait results', () => {
    expect(managedConversationToolPresentation({
      toolName: 'agent_conversation_wait',
      status: 'completed',
      arguments: {
        conversation_id: 'conv_123'
      },
      result: JSON.stringify({
        status: 'completed',
        waited: true,
        conversation: {
          conversation_id: 'conv_123',
          session_id: 'sess_456',
          agent_id: 'laforge',
          title: 'Stage 4 B2',
          conversation_state: 'open',
          turn_state: 'completed',
          last_result_summary: 'Committed the implementation.',
          controller_conversation_id: 'conv_parent'
        }
      })
    })).toMatchObject({
      title: 'Wait for managed conversation',
      requestText: 'conv_123',
      resultSummary: 'Committed the implementation.',
      primaryConversation: {
        conversationId: 'conv_123',
        sessionId: 'sess_456',
        agentId: 'laforge',
        title: 'Stage 4 B2',
        status: 'completed',
        controllerConversationId: 'conv_parent'
      }
    });
  });

  it('uses the live managed-conversation payload while wait is active', () => {
    expect(managedConversationToolPresentation({
      toolName: 'agent_conversation_wait',
      status: 'started',
      arguments: {
        conversation_id: 'conv_123'
      },
      managedConversation: {
        status: 'running',
        conversation: {
          conversation_id: 'conv_123',
          session_id: 'sess_456',
          agent_id: 'laforge',
          title: 'Stage 4 B2',
          conversation_state: 'open',
          turn_state: 'running',
          active_turn_id: 'turn_789',
          last_result_summary: 'Running focused tests.'
        }
      }
    })).toMatchObject({
      title: 'Wait for managed conversation',
      resultSummary: 'Running focused tests.',
      primaryConversation: {
        conversationId: 'conv_123',
        sessionId: 'sess_456',
        agentId: 'laforge',
        status: 'running',
        turnState: 'running',
        summary: 'Running focused tests.'
      }
    });
  });

  it('exposes live managed todo progress for a joined conversation', () => {
    const presentation = managedConversationToolPresentation({
      toolName: 'agent_conversation_wait',
      status: 'started',
      arguments: { conversation_id: 'conv_123' },
      managedConversation: {
        status: 'running',
        conversation: { conversation_id: 'conv_123', turn_state: 'running' },
        tool_call_count: 4,
        last_tool: 'step_todo_write',
        todos: [
          { content: 'Implement streaming', status: 'completed' },
          { content: 'Run regression tests', status: 'in_progress' },
        ],
      },
    });

    expect(presentation?.toolCallCount).toBe(4);
    expect(presentation?.lastTool).toBe('step_todo_write');
    expect(presentation?.todos).toEqual([
      { content: 'Implement streaming', status: 'completed', priority: 'medium' },
      { content: 'Run regression tests', status: 'in_progress', priority: 'medium' },
    ]);
    expect(presentation?.todoSummary).toBe('1 active, 1 completed');
  });

  it('keeps completed async managed conversation results as static accepted snapshots', () => {
    expect(managedConversationToolPresentation({
      toolName: 'agent_conversation_create',
      status: 'completed',
      arguments: {
        title: 'Stage 4 C4',
        agent_id: 'laforge'
      },
      result: JSON.stringify({
        status: 'accepted',
        message: 'Agent work conversation created and running.',
        conversation: {
          conversation_id: 'conv_async',
          agent_id: 'laforge',
          title: 'Stage 4 C4',
          conversation_state: 'open',
          turn_state: 'running'
        }
      })
    })).toMatchObject({
      resultSummary: 'Agent work conversation created and running.',
      primaryConversation: {
        conversationId: 'conv_async',
        status: 'running'
      },
      displayStatus: 'running'
    });
  });

  it('does not fabricate Running after a settled create without conversation data', () => {
    const presentation = managedConversationToolPresentation({
      toolName: 'agent_conversation_create',
      status: 'completed',
      arguments: { title: 'Verify routing', agent_id: 'laforge' },
      result: null
    });
    expect(presentation?.primaryConversation?.status).toBe('unknown');
    expect(presentation?.displayStatus).toBe('unknown');
  });

  it('overlays live state onto an accepted create result', () => {
    const presentation = managedConversationToolPresentation({
      toolName: 'agent_conversation_create',
      status: 'completed',
      arguments: { title: 'Verify routing', agent_id: 'laforge' },
      result: JSON.stringify({
        status: 'accepted',
        conversation: {
          conversation_id: 'conv_live',
          turn_state: 'running',
          controller_conversation_id: 'conv_controller',
          follow_up_conversation_id: 'conv_follow_up'
        }
      }),
      managedConversation: {
        status: 'completed',
        conversation: {
          conversation_id: 'conv_live',
          turn_state: 'completed',
          last_result_summary: 'Finished'
        },
        tool_call_count: 4,
        last_tool: 'todo_write',
        todos: [{ content: 'Verify result', status: 'completed' }]
      }
    });
    expect(presentation?.conversations).toHaveLength(1);
    expect(presentation?.primaryConversation?.conversationId).toBe('conv_live');
    expect(presentation?.displayStatus).toBe('completed');
    expect(presentation?.primaryConversation?.summary).toBe('Finished');
    expect(presentation?.primaryConversation?.controllerConversationId).toBe('conv_controller');
    expect(presentation?.primaryConversation?.followUpConversationId).toBe('conv_follow_up');
    expect(presentation?.resultSummary).toBe('Finished');
    expect(presentation?.toolCallCount).toBe(4);
    expect(presentation?.lastTool).toBe('todo_write');
    expect(presentation?.todoSummary).toBe('1 completed');
  });

  it('keeps a settled async tool card live while its managed turn runs', () => {
    const presentation = managedConversationToolPresentation({
      toolName: 'agent_conversation_send',
      status: 'completed',
      arguments: { conversation_id: 'conv_live', message: 'Continue' },
      result: JSON.stringify({ status: 'accepted' }),
      managedConversation: {
        status: 'running',
        conversation: { conversation_id: 'conv_live', turn_state: 'running' }
      }
    });

    expect(presentation?.displayStatus).toBe('running');
  });

  it('builds managed conversation list presentations', () => {
    expect(managedConversationToolPresentation({
      toolName: 'agent_conversation_list',
      status: 'completed',
      arguments: {
        status: 'all'
      },
      result: JSON.stringify({
        count: 2,
        conversations: [
          { conversation_id: 'conv_a', agent_id: 'laforge', title: 'A', turn_state: 'running' },
          { conversation_id: 'conv_b', agent_id: 'lumi', title: 'B', turn_state: 'completed' }
        ]
      })
    })).toMatchObject({
      title: 'List managed conversations',
      resultSummary: '2 managed conversations found.',
      conversations: [
        { conversationId: 'conv_a', status: 'running' },
        { conversationId: 'conv_b', status: 'completed' }
      ]
    });
  });

  it('builds native read presentations with line-numbered code and continuation footer', () => {
    expect(nativeInspectionToolPresentation({
      toolName: 'read',
      status: 'completed',
      arguments: {
        file_path: '/repo/src/main.ts',
        offset: 1,
        limit: 2
      },
      result: [
        '1: const answer = 42;',
        '2: export { answer };',
        '',
        '(Showing lines 1-2 of 4. Use offset=3 and limit≤2 to continue.)'
      ].join('\n')
    })).toMatchObject({
      kind: 'native_inspection_tool',
      nativeKind: 'read',
      title: 'Read file',
      requestText: '/repo/src/main.ts',
      summary: 'Read 2 lines (1-2).',
      footer: '(Showing lines 1-2 of 4. Use offset=3 and limit≤2 to continue.)',
      readLines: [
        { lineNumber: 1, content: 'const answer = 42;' },
        { lineNumber: 2, content: 'export { answer };' }
      ]
    });
  });

  it('builds native grep presentations grouped by file', () => {
    expect(nativeInspectionToolPresentation({
      toolName: 'grep',
      status: 'completed',
      arguments: {
        pattern: 'answer',
        path: '/repo/src'
      },
      result: [
        '/repo/src/main.ts:10: const answer = 42;',
        '/repo/src/main.ts-11- export { answer };',
        '/repo/src/other.ts:3: answer();'
      ].join('\n')
    })).toMatchObject({
      nativeKind: 'grep',
      summary: '2 matches in 2 files.',
      grepGroups: [
        {
          path: '/repo/src/main.ts',
          matches: [
            { lineNumber: 10, text: 'const answer = 42;', isMatch: true },
            { lineNumber: 11, text: 'export { answer };', isMatch: false }
          ]
        },
        {
          path: '/repo/src/other.ts',
          matches: [
            { lineNumber: 3, text: 'answer();', isMatch: true }
          ]
        }
      ]
    });
  });

  it('builds native path-list presentations for glob and list_directory', () => {
    expect(nativeInspectionToolPresentation({
      toolName: 'glob',
      status: 'completed',
      arguments: {
        pattern: '**/*.ts',
        path: '/repo'
      },
      result: '/repo/src/main.ts\n/repo/src/other.ts'
    })).toMatchObject({
      nativeKind: 'glob',
      summary: '2 paths returned.',
      pathEntries: [
        { path: '/repo/src/main.ts', name: 'main.ts' },
        { path: '/repo/src/other.ts', name: 'other.ts' }
      ]
    });

    expect(nativeInspectionToolPresentation({
      toolName: 'list_directory',
      status: 'completed',
      arguments: {
        path: '/repo/src'
      },
      result: '/repo/src/lib/\n/repo/src/main.ts'
    })).toMatchObject({
      nativeKind: 'list_directory',
      requestText: '/repo/src',
      pathEntries: [
        { path: '/repo/src/lib/', kind: 'directory' },
        { path: '/repo/src/main.ts', kind: 'unknown' }
      ]
    });
  });

  it('treats list_directory status markers as footer instead of paths', () => {
    expect(nativeInspectionToolPresentation({
      toolName: 'list_directory',
      status: 'completed',
      arguments: {
        path: '/repo'
      },
      result: 'src/\nREADME.md\n... (truncated, 250 total entries)'
    })).toMatchObject({
      nativeKind: 'list_directory',
      summary: '2 paths returned.',
      footer: '... (truncated, 250 total entries)',
      pathEntries: [
        { path: 'src/', kind: 'directory' },
        { path: 'README.md', kind: 'unknown' }
      ]
    });

    expect(nativeInspectionToolPresentation({
      toolName: 'list_directory',
      status: 'completed',
      arguments: {
        path: '/empty'
      },
      result: '(empty directory)'
    })).toMatchObject({
      nativeKind: 'list_directory',
      summary: 'Directory is empty.',
      footer: '(empty directory)',
      pathEntries: []
    });
  });

  it('does not claim non-native read tools as filesystem read output', () => {
    expect(nativeInspectionToolPresentation({
      toolName: 'office_read',
      status: 'completed',
      arguments: {
        path: '/tmp/report.xlsx'
      },
      result: 'Workbook summary'
    })).toBeNull();

    expect(nativeInspectionToolPresentation({
      toolName: 'artifact_read',
      status: 'completed',
      arguments: {
        artifact_id: 'art_123'
      },
      result: 'Artifact summary'
    })).toBeNull();
  });

  it('keeps native lsp output as a structured fallback card', () => {
    expect(nativeInspectionToolPresentation({
      toolName: 'lsp',
      status: 'completed',
      arguments: {
        operation: 'hover',
        file_path: '/repo/src/main.ts',
        line: 10,
        character: 7
      },
      result: 'const answer: number'
    })).toMatchObject({
      nativeKind: 'lsp',
      title: 'Language server query',
      requestText: 'hover',
      summary: '1 output line returned.',
      outputText: 'const answer: number'
    });
  });

  it('builds compact write_deliverable presentations only for successful calls', () => {
    const richPresentation = workflowToolPresentation({
      toolName: 'write_deliverable',
      status: 'completed',
      arguments: {
        title: 'Rich report',
        content: 'Fallback summary.',
        format: 'rich',
        rich: { blocks: [{ type: 'markdown', content: 'raw model payload' }] }
      },
      result: JSON.stringify({
        status: 'buffered',
        deliverable_id: 'dlv_rich',
        version: 1,
        rich: { blocks: [{ type: 'markdown', content: 'server-normalized payload' }] },
        validation_warnings: ['normalized']
      })
    });
    expect(richPresentation).toMatchObject({
      kind: 'write_deliverable',
      title: 'Rich report',
      format: 'rich',
      status: 'buffered',
      deliverableId: 'dlv_rich',
      version: 1,
      note: 'Final deliverable renders at turn end.'
    });
    expect(JSON.stringify(richPresentation)).not.toContain('Fallback summary');
    expect(JSON.stringify(richPresentation)).not.toContain('server-normalized payload');
    expect(JSON.stringify(richPresentation)).not.toContain('raw model payload');

    expect(workflowToolPresentation({
      toolName: 'write_deliverable',
      status: 'completed',
      arguments: {
        title: 'Rich report',
        content: 'Fallback summary.',
        format: 'rich',
        rich: { blocks: [{ type: 'markdown', content: 'raw model payload' }] }
      },
      result: 'Output shortened before JSON could be parsed'
    })).toMatchObject({
      kind: 'write_deliverable',
      title: 'Rich report',
      format: 'rich',
      status: 'completed'
    });

    expect(workflowToolPresentation({
      toolName: 'write_deliverable',
      status: 'completed',
      arguments: {
        title: 'Implementation report',
        content: '# Done\n\nValidation passed.',
        format: 'markdown',
        outputs: { files: ['ui/src/lib/tool-call-summary.ts'] }
      },
      result: JSON.stringify({
        status: 'buffered',
        deliverable_id: 'dlv_123',
        version: 2,
        length: 27
      })
    })).toMatchObject({
      kind: 'write_deliverable',
      title: 'Implementation report',
      deliverableId: 'dlv_123',
      version: 2,
      length: 27,
      outputKeys: ['files']
    });

    expect(workflowToolPresentation({
      toolName: 'write_deliverable',
      status: 'failed',
      isError: true,
      arguments: { content: '# Not rendered' },
      result: JSON.stringify({ status: 'rejected' })
    })).toBeNull();
  });

  it('builds rich step_complete presentation only for successful calls', () => {
    expect(workflowToolPresentation({
      toolName: 'step_complete',
      status: 'completed',
      arguments: {
        summary: 'Implemented rich workflow tool cards.',
        outcome: { status: 'success' },
        claims: ['Successful calls are rendered richly'],
        outputs: { changed_files: ['ToolCallBlock.svelte'] },
        metadata: { reviewed: true },
        notification: { mode: 'direct' }
      },
      result: JSON.stringify({ status: 'completed' })
    })).toMatchObject({
      kind: 'step_complete',
      summary: 'Implemented rich workflow tool cards.',
      outcomeStatus: 'success',
      claims: ['Successful calls are rendered richly'],
      outputs: [
        { key: 'changed_files', value: ['ToolCallBlock.svelte'] }
      ],
      metadata: [
        { key: 'reviewed', value: true }
      ],
      outputKeys: ['changed_files'],
      metadataKeys: ['reviewed'],
      notificationMode: 'direct'
    });

    expect(workflowToolPresentation({
      toolName: 'step_complete',
      status: 'completed',
      isError: true,
      arguments: { summary: 'Rejected completion' },
      result: JSON.stringify({ status: 'rejected' })
    })).toBeNull();
  });

  it('builds rich step_todo_write presentation only for successful calls', () => {
    expect(workflowToolPresentation({
      toolName: 'step_todo_write',
      status: 'completed',
      arguments: {
        todos: [
          { content: 'Inspect payloads', status: 'completed' },
          { content: 'Run validation', status: 'in_progress' }
        ]
      },
      result: JSON.stringify({
        status: 'updated',
        count: 2,
        todos: [
          { content: 'Inspect payloads', status: 'completed' },
          { content: 'Run validation', status: 'in_progress' }
        ],
        unchanged: false,
        non_terminal_count: 1,
        guidance: '1 todo remains open.'
      })
    })).toMatchObject({
      kind: 'step_todo_write',
      status: 'updated',
      count: 2,
      statusSummary: '1 active, 1 completed',
      guidance: '1 todo remains open.',
      unchanged: false,
      nonTerminalCount: 1,
      todos: [
        { content: 'Inspect payloads', status: 'completed' },
        { content: 'Run validation', status: 'in_progress' }
      ]
    });

    expect(workflowToolPresentation({
      toolName: 'step_todo_write',
      status: 'failed',
      isError: true,
      arguments: {
        todos: [
          { content: 'Not rendered richly', status: 'pending' }
        ]
      },
      result: JSON.stringify({ status: 'rejected' })
    })).toBeNull();
  });

  it('builds rich read_tool_output helper presentations', () => {
    expect(toolOutputHelperPresentation({
      toolName: 'read_tool_output',
      status: 'completed',
      arguments: {
        call_id: 'call_123',
        offset: 104,
        limit: 40
      },
      result: [
        '1: 104: function clearPendingChunks() {',
        '2: 105:   return;',
        '',
        '(Showing lines 104-105 of 3948. Use offset=106 to continue.)',
        '',
        '(Total: 2 lines)'
      ].join('\n')
    })).toMatchObject({
      kind: 'tool_output_helper',
      helperKind: 'read_tool_output',
      title: 'Read stored tool output',
      summary: 'Read call_123 from line 104, 40 lines',
      sourceCallId: 'call_123',
      queryEntries: [
        { key: 'source call', value: 'call_123' },
        { key: 'offset', value: 104 },
        { key: 'limit', value: 40 }
      ],
      receivedSummary: 'Received lines 104–105 of 3948.',
      receivedDetails: [
        { key: 'page', value: 'lines 104–105 of 3948' },
        { key: 'returned lines', value: '2' },
        { key: 'next offset', value: '106' }
      ],
      continuationHint: 'Use offset=106 to continue.'
    });
  });

  it('builds rich search_tool_output helper presentations', () => {
    expect(toolOutputHelperPresentation({
      toolName: 'search_tool_output',
      status: 'completed',
      arguments: {
        call_id: 'call_abc',
        pattern: 'timeout',
        context_lines: 2
      },
      result: '1: line with timeout'
    })).toMatchObject({
      helperKind: 'search_tool_output',
      summary: 'Search call_abc for “timeout”',
      queryEntries: [
        { key: 'source call', value: 'call_abc' },
        { key: 'pattern', value: 'timeout' },
        { key: 'context lines', value: 2 }
      ],
      receivedSummary: 'Received 1 output line.'
    });
  });

  it('keeps failed and running tool output helper states visible', () => {
    expect(toolOutputHelperPresentation({
      toolName: 'read_tool_output',
      status: 'failed',
      isError: true,
      arguments: { call_id: 'call_missing' },
      result: 'No stored output found for call_missing'
    })).toMatchObject({
      summary: 'Read call_missing',
      receivedSummary: 'Tool output query failed: No stored output found for call_missing'
    });

    expect(toolOutputHelperPresentation({
      toolName: 'read_tool_output',
      status: 'started',
      arguments: { call_id: 'call_running' }
    })).toMatchObject({
      summary: 'Read call_running',
      receivedSummary: 'Waiting for stored tool output.'
    });
  });

  it('builds user-friendly memory_add presentations', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_add',
      status: 'completed',
      arguments: {
        role: 'assistant',
        memory_type: 'episodic',
        importance: 'normal',
        ttl_days: 90,
        content: 'Implemented and committed Cognis Matrix thread fork fix 1d9e3034: bounded Matrix...'
      },
      result: JSON.stringify({
        results: [
          {
            id: '214582e2-052e-48da-8eb5-e713c2',
            memory: 'Assistant implemented and committed Cognis Matrix thread fork fix 1d9e3034.',
            event: 'ADD'
          }
        ],
        artifact: null,
        error: false,
        message: null
      })
    })).toMatchObject({
      kind: 'memory_tool',
      variant: 'saved',
      title: 'Save memory',
      summary: '1 memory saved.',
      requestLabel: 'Memory to save',
      badges: ['type: episodic', 'role: assistant', 'importance: normal', 'ttl: 90 days'],
      resultSummary: '1 memory saved.',
      resultItems: [
        {
          title: 'ADD · 214582e2-052e-48da-8eb5-e713c2',
          body: 'Assistant implemented and committed Cognis Matrix thread fork fix 1d9e3034.',
          accent: 'memory'
        }
      ]
    });
  });

  it('does not count attached artifacts as saved memories', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_add',
      status: 'completed',
      arguments: {
        content: 'Saved with a detailed artifact.',
        role: 'assistant'
      },
      result: JSON.stringify({
        results: [
          {
            id: 'mem_with_artifact',
            memory: 'Saved with a detailed artifact.',
            event: 'ADD'
          }
        ],
        artifact: {
          id: 'art_1',
          filename: 'details.md',
          content_type: 'text/markdown'
        },
        error: false,
        message: null
      })
    })).toMatchObject({
      variant: 'saved',
      summary: '1 memory saved.',
      resultSummary: '1 memory saved.',
      resultItems: [
        {
          title: 'ADD · mem_with_artifact',
          accent: 'memory'
        },
        {
          title: 'details.md · art_1',
          accent: 'artifact'
        }
      ]
    });
  });

  it('summarizes top-level array memory_add_batch results with partial failures', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_add_batch',
      status: 'completed',
      arguments: {
        memories: [
          { content: 'First memory.' },
          { content: 'Second memory.' }
        ]
      },
      result: JSON.stringify([
        {
          results: [
            {
              id: 'batch_mem_1',
              memory: 'First memory.',
              event: 'ADD'
            }
          ],
          artifact: {
            id: 'batch_art_1',
            filename: 'first-details.md',
            content_type: 'text/markdown'
          },
          error: false,
          message: null
        },
        {
          results: [],
          error: true,
          message: 'Second memory failed validation'
        }
      ])
    })).toMatchObject({
      variant: 'saved',
      title: 'Save memories',
      summary: '1 memory saved, 1 failed.',
      resultSummary: '1 memory saved, 1 failed.',
      resultItems: [
        {
          title: 'ADD · batch_mem_1',
          body: 'First memory.',
          accent: 'memory'
        },
        {
          title: 'first-details.md · batch_art_1',
          accent: 'artifact'
        },
        {
          title: 'Second memory failed validation',
          accent: 'generic'
        }
      ]
    });
  });

  it('builds user-friendly memory search presentations', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_search',
      status: 'completed',
      arguments: {
        query: 'tool rendering',
        limit: 2,
        role: 'assistant'
      },
      result: JSON.stringify({
        results: [
          {
            id: 'mem_1',
            memory: 'Assistant implemented rich rendering for helper tools.',
            score: 0.86,
            metadata: {
              memory_type: 'episodic',
              role: 'assistant',
              categories: ['work'],
              importance: 'normal'
            },
            has_artifacts: true
          }
        ]
      })
    })).toMatchObject({
      title: 'Search memories',
      requestLabel: 'Search query',
      requestText: 'tool rendering',
      badges: ['role: assistant', 'limit: 2'],
      resultSummary: '1 memory found.',
      resultItems: [
        {
          title: 'mem_1',
          body: 'Assistant implemented rich rendering for helper tools.',
          accent: 'memory',
          meta: expect.arrayContaining([
            { key: 'score', value: '0.86' },
            { key: 'type', value: 'episodic' },
            { key: 'artifacts', value: 'attached' }
          ])
        }
      ]
    });
  });

  it('supports content-based memory result records', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_list',
      status: 'completed',
      arguments: {
        limit: 1
      },
      result: JSON.stringify({
        results: [
          {
            id: 'mem_content',
            content: 'Existing backend result shape that stores the memory text in content.'
          }
        ]
      })
    })).toMatchObject({
      title: 'List memories',
      resultSummary: '1 memory found.',
      resultItems: [
        {
          title: 'mem_content',
          body: 'Existing backend result shape that stores the memory text in content.',
          accent: 'memory'
        }
      ]
    });
  });

  it('builds user-friendly memory_ask presentations', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_ask',
      status: 'completed',
      arguments: {
        question: 'What did we decide about raw payloads?',
        include_memories: true
      },
      result: JSON.stringify({
        answer: 'Use a rich default card and keep raw payloads collapsed.',
        results: [
          { id: 'mem_1', memory: 'User wants raw payloads collapsed.' }
        ],
        count: 1,
        queries: ['raw payloads'],
        stats: { latency_ms: 123 }
      })
    })).toMatchObject({
      title: 'Ask memory',
      resultSummary: 'Answered from stored memories.',
      answer: 'Use a rich default card and keep raw payloads collapsed.',
      resultDetails: expect.arrayContaining([
        { key: 'count', value: 1 },
        { key: 'queries', value: 'raw payloads' },
        { key: 'stats.latency_ms', value: 123 }
      ])
    });
  });

  it('summarizes recent memory result records', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_recent',
      status: 'completed',
      arguments: {
        days: 7,
        limit: 1
      },
      result: JSON.stringify({
        results: [
          {
            id: 'mem_recent',
            memory: 'Recent implementation context.'
          }
        ]
      })
    })).toMatchObject({
      title: 'Recent memories',
      requestLabel: 'Recent memories',
      resultSummary: '1 memory found.',
      resultItems: [
        {
          title: 'mem_recent',
          body: 'Recent implementation context.',
          accent: 'memory'
        }
      ]
    });
  });

  it('renders memory_get_artifact content as primary text', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_get_artifact',
      status: 'completed',
      arguments: {
        memory_id: 'mem_1',
        artifact_id: 'art_1'
      },
      result: JSON.stringify({
        content: '# Artifact\n\nFull artifact body.',
        total_size: 30,
        has_more: false
      })
    })).toMatchObject({
      title: 'Read memory artifact',
      text: '# Artifact\n\nFull artifact body.',
      resultSummary: 'Artifact operation completed.',
      resultDetails: expect.arrayContaining([
        { key: 'has more', value: false },
        { key: 'total size', value: 30 }
      ])
    });
  });

  it('keeps memory failures readable without requiring raw payloads', () => {
    expect(memoryToolPresentation({
      toolName: 'memory_get_artifact',
      status: 'failed',
      isError: true,
      arguments: {
        memory_id: 'mem_1',
        artifact_id: 'art_1'
      },
      result: 'Memory operation failed: artifact not found'
    })).toMatchObject({
      title: 'Read memory artifact',
      resultLabel: 'Problem',
      error: 'Memory operation failed: artifact not found',
      resultSummary: 'Memory operation failed: artifact not found'
    });
  });
});
