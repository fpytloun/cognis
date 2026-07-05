import { describe, expect, it } from 'vitest';

import {
  applySlashSuggestion,
  isSystemSlashCommand,
  localSlashCommandSuggestions,
  normalizeSlashCommandInput,
  parseChatModeDirectiveInput,
  slashParameterSuggestionCommand
} from '$lib/slash-commands';
import type { SlashCommandSuggestion } from '$lib/types/api';

describe('slash command helpers', () => {
  it('normalizes and recognizes backend-owned slash commands', () => {
    expect(normalizeSlashCommandInput('/   skill cog')).toBe('/skill cog');
    expect(isSystemSlashCommand('/skill cognis-coding')).toBe(true);
    expect(isSystemSlashCommand('/task write tests')).toBe(true);
    expect(isSystemSlashCommand('/profile fast')).toBe(true);
    expect(isSystemSlashCommand('/compact')).toBe(true);
    expect(isSystemSlashCommand('/compact now')).toBe(false);
    expect(isSystemSlashCommand('/help extra')).toBe(false);
    expect(isSystemSlashCommand('/context usage')).toBe(false);
    expect(isSystemSlashCommand('/plan')).toBe(true);
    expect(isSystemSlashCommand('/plan inspect this code')).toBe(false);
    expect(isSystemSlashCommand('/build implement this')).toBe(false);
    expect(isSystemSlashCommand('/default answer normally')).toBe(false);
    expect(isSystemSlashCommand('/not-a-command')).toBe(false);
  });

  it('keeps exact-only and prefix command routing explicit', () => {
    const exactOnlyCommands = [
      '/help',
      '/context',
      '/info',
      '/lsp',
      '/compact',
      '/summarize',
      '/new',
      '/reset',
      '/clear',
      '/undo',
      '/redo',
      '/plan',
      '/build',
      '/default'
    ];
    const prefixCommands = [
      '/fork',
      '/model',
      '/thinking',
      '/profile',
      '/skill',
      '/executor',
      '/task',
      '/research',
      '/implement',
      '/delegate',
      '/approve',
      '/deny',
      '/retry',
      '/continue',
      '/stop',
      '/cancel'
    ];

    for (const command of exactOnlyCommands) {
      expect(isSystemSlashCommand(command), command).toBe(true);
      expect(isSystemSlashCommand(`${command} extra`), `${command} extra`).toBe(false);
    }
    for (const command of prefixCommands) {
      expect(isSystemSlashCommand(command), command).toBe(true);
      expect(isSystemSlashCommand(`${command} extra`), `${command} extra`).toBe(true);
      expect(isSystemSlashCommand(`${command}extra`), `${command}extra`).toBe(false);
    }
  });

  it('parses one-shot chat mode directives with stripped display content', () => {
    expect(parseChatModeDirectiveInput('/plan')).toEqual({
      mode: 'plan',
      oneShot: false,
      content: null,
    });
    expect(parseChatModeDirectiveInput('/plan inspect this code')).toEqual({
      mode: 'plan',
      oneShot: true,
      content: 'inspect this code',
    });
    expect(parseChatModeDirectiveInput('/build implement this')).toEqual({
      mode: 'build',
      oneShot: true,
      content: 'implement this',
    });
    expect(parseChatModeDirectiveInput('/model gpt-5')).toBeNull();
  });

  it('filters command suggestions and inserts a trailing space for argument commands', () => {
    const suggestions = localSlashCommandSuggestions('/sk');

    expect(suggestions).toHaveLength(1);
    expect(suggestions[0]).toMatchObject({
      kind: 'command',
      command: '/skill',
      insert_text: '/skill ',
      suffix: 'space'
    });
  });

  it('requests dynamic parameter suggestions only for structured parameter commands', () => {
    expect(slashParameterSuggestionCommand('/skill cog')).toBe('/skill');
    expect(slashParameterSuggestionCommand('/thinking ')).toBe('/thinking');
    expect(slashParameterSuggestionCommand('/profile ')).toBe('/profile');
    expect(localSlashCommandSuggestions('/profile ')).toEqual([]);
    expect(slashParameterSuggestionCommand('/task write tests')).toBeNull();
    expect(slashParameterSuggestionCommand('/fork new topic')).toBeNull();
  });

  it('applies backend-provided canonical insert text', () => {
    const suggestion: SlashCommandSuggestion = {
      kind: 'parameter',
      command: '/skill',
      value: 'cognis-coding',
      label: 'Cognis Coding',
      description: 'Coding guidance',
      insert_text: '/skill cognis-coding',
      suffix: 'none',
      badges: []
    };

    expect(applySlashSuggestion(suggestion)).toBe('/skill cognis-coding');
  });
});
