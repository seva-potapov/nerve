import { describe, expect, it } from 'vitest';
import {
  BUILTIN_COMMANDS, argKind, commandMenuQuery, commandUsage, commandsFromResponse, filterCommands, parseSlashInput,
} from './slashCommands';

const parse = (text: string) => parseSlashInput(text, BUILTIN_COMMANDS);

describe('parseSlashInput', () => {
  it('reads a command whose first word is exactly a command name', () => {
    const parsed = parse('/compact');
    expect(parsed).toMatchObject({ kind: 'command', args: '' });
    expect(parsed.kind === 'command' && parsed.command.name).toBe('compact');
  });

  it('takes everything after the name as the arguments, trimmed', () => {
    expect(parse('/compact keep PR numbers ')).toMatchObject({ kind: 'command', args: 'keep PR numbers' });
    expect(parse('/new  Weekly\nreview')).toMatchObject({ kind: 'command', args: 'Weekly\nreview' });
  });

  it('sends a no-argument command followed by more text as a message', () => {
    const text = '/plans for tomorrow: ship the fix';
    expect(parseSlashInput(text, BUILTIN_COMMANDS)).toEqual({ kind: 'text', text });
    expect(parseSlashInput('/stop  ', BUILTIN_COMMANDS)).toMatchObject({ kind: 'command', args: '' });
    expect(parseSlashInput('/new release notes', BUILTIN_COMMANDS)).toMatchObject({ kind: 'command', args: 'release notes' });
  });

  it('keeps a path as text', () => {
    expect(parse('/root/nerve-workspace/x is broken')).toEqual({ kind: 'text', text: '/root/nerve-workspace/x is broken' });
    expect(parse('/compact/foo')).toEqual({ kind: 'text', text: '/compact/foo' });
  });

  it('keeps an unknown /word as text', () => {
    expect(parse('/compactfoo')).toEqual({ kind: 'text', text: '/compactfoo' });
    expect(parse('/whatever do this')).toEqual({ kind: 'text', text: '/whatever do this' });
    expect(parse('/')).toEqual({ kind: 'text', text: '/' });
  });

  it('sends // as text with one slash removed', () => {
    expect(parse('//compact')).toEqual({ kind: 'text', text: '/compact' });
    expect(parse('//compact keep it')).toEqual({ kind: 'text', text: '/compact keep it' });
    expect(parse('///x')).toEqual({ kind: 'text', text: '//x' });
  });

  it('ignores leading whitespace', () => {
    expect(parse('  /stop')).toMatchObject({ kind: 'command', args: '' });
    expect(parse('\n//compact')).toEqual({ kind: 'text', text: '/compact' });
  });

  it('matches the name in its own letter case only', () => {
    expect(parse('/Compact')).toEqual({ kind: 'text', text: '/Compact' });
    expect(parse('/STOP')).toEqual({ kind: 'text', text: '/STOP' });
  });

  it('leaves text that does not start with a slash alone', () => {
    expect(parse('please /compact')).toEqual({ kind: 'text', text: 'please /compact' });
  });

  it('knows only the commands it is given', () => {
    expect(parseSlashInput('/compact', [])).toEqual({ kind: 'text', text: '/compact' });
  });
});

describe('the command menu', () => {
  it('opens for /word at the very start, before any space', () => {
    expect(commandMenuQuery('/')).toBe('');
    expect(commandMenuQuery('/co')).toBe('co');
    expect(commandMenuQuery('/compact ')).toBeNull();
    expect(commandMenuQuery(' /co')).toBeNull();
    expect(commandMenuQuery('//co')).toBeNull();
    expect(commandMenuQuery('/root/x')).toBeNull();
    expect(commandMenuQuery('hello')).toBeNull();
  });

  it('filters by the typed prefix, in list order', () => {
    expect(filterCommands(BUILTIN_COMMANDS, 'co').map(c => c.name)).toEqual(['compact', 'context']);
    expect(filterCommands(BUILTIN_COMMANDS, 'st').map(c => c.name)).toEqual(['stop', 'star']);
    expect(filterCommands(BUILTIN_COMMANDS, 'x')).toEqual([]);
    expect(filterCommands(BUILTIN_COMMANDS, '')).toHaveLength(BUILTIN_COMMANDS.length);
  });
});

describe('argument specs', () => {
  it('tells none, required and optional apart', () => {
    expect(argKind('')).toBe('none');
    expect(argKind('<level>')).toBe('required');
    expect(argKind('[instructions]')).toBe('optional');
  });

  it('writes the usage out', () => {
    expect(commandUsage({ name: 'compact', args: '[instructions]', help: '' })).toBe('/compact [instructions]');
    expect(commandUsage({ name: 'stop', args: '', help: '' })).toBe('/stop');
  });
});

describe('commandsFromResponse', () => {
  it('reads the server list', () => {
    expect(commandsFromResponse({
      commands: [
        { name: 'compact', args: '[instructions]', help: 'Summarize' },
        { name: 'effort', args: '<level>', help: 'Effort', choices: ['low', 'high'] },
      ],
    })).toEqual([
      { name: 'compact', args: '[instructions]', help: 'Summarize' },
      { name: 'effort', args: '<level>', help: 'Effort', choices: ['low', 'high'] },
    ]);
  });

  it('drops malformed entries and answers null without a list', () => {
    expect(commandsFromResponse({ commands: [{ name: 'a b' }, null, { help: 'x' }, { name: 'ok' }] }))
      .toEqual([{ name: 'ok', args: '', help: '' }]);
    expect(commandsFromResponse({})).toBeNull();
    expect(commandsFromResponse(null)).toBeNull();
    expect(commandsFromResponse({ commands: [] })).toBeNull();
  });
});
