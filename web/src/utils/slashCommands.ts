/**
 * Slash commands of the chat composer: the same commands as the Telegram
 * bot, typed as `/name [args]` at the start of a message.
 *
 * A message is a command only when its first word is exactly the name of a
 * known command (`/compact`, `/compact keep PR numbers`). Anything else goes
 * to the model unchanged: a path (`/root/x is broken`), an unknown word
 * (`/compactfoo`), a name in other letter case (`/Compact`). A message that
 * starts with `//` is text with one leading slash removed, so `//compact`
 * sends `/compact` to the model.
 */

export interface SlashCommand {
  /** Without the slash: `compact`. */
  name: string;
  /** `""` takes none, `"<x>"` a required argument, `"[x]"` an optional one. */
  args: string;
  help: string;
  /** The values the argument may take, when the server lists them. */
  choices?: string[];
}

/**
 * The commands of the web chat, for when `GET /api/commands` does not answer.
 * Keep in step with the server's list (`nerve/commands.py`, surface `web`).
 */
export const BUILTIN_COMMANDS: SlashCommand[] = [
  { name: 'new', args: '[title]', help: 'Start a new session' },
  { name: 'stop', args: '', help: 'Stop the running turn' },
  { name: 'status', args: '', help: 'Show the session: model, effort, agents, context, cost' },
  { name: 'btw', args: '<question>', help: 'Ask a side question now, outside the session' },
  { name: 'compact', args: '[instructions]', help: 'Summarize the conversation to free context' },
  { name: 'context', args: '', help: 'Show what fills the context window' },
  { name: 'cost', args: '[period]', help: "This session's cost, or all of today, week or month" },
  { name: 'sessions', args: '', help: 'Switch to another session' },
  { name: 'rename', args: '<title>', help: 'Rename this session' },
  { name: 'fork', args: '[title]', help: 'Fork this chat into a new session; as a reply, from there' },
  { name: 'retry', args: '', help: 'Send your last message again' },
  { name: 'model', args: '[name]', help: "Show or set this session's model" },
  { name: 'effort', args: '[level]', help: "Show or set this session's reasoning effort" },
  { name: 'ps', args: '', help: "List and stop this session's agents and processes" },
  { name: 'star', args: '', help: 'Keep this session from closing when idle' },
  { name: 'unstar', args: '', help: 'Let this session close when idle again' },
  { name: 'plans', args: '', help: 'Review pending plans' },
  { name: 'diag', args: '', help: 'Show how Nerve is running right now' },
  { name: 'doctor', args: '', help: 'Check the configuration' },
  { name: 'help', args: '', help: 'List the commands and shortcuts' },
];

/**
 * The reasoning effort levels, weakest first: the composer's effort picker
 * and `/effort` when the server's list carries no choices.
 */
export const DEFAULT_EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'];

export type ArgKind = 'none' | 'required' | 'optional';

/** What kind of argument a command takes, read from its `args` spec. */
export function argKind(args: string): ArgKind {
  const spec = args.trim();
  if (!spec) return 'none';
  return spec.startsWith('<') ? 'required' : 'optional';
}

export type ParsedInput =
  | { kind: 'command'; command: SlashCommand; args: string }
  | { kind: 'text'; text: string };

/**
 * Whether a message is a command, and if not, the text to send. Leading and
 * trailing whitespace is ignored; the arguments are everything after the
 * first word, trimmed. A command without arguments followed by more text
 * is a message.
 */
export function parseSlashInput(text: string, commands: SlashCommand[]): ParsedInput {
  const trimmed = text.trim();
  if (trimmed.startsWith('//')) return { kind: 'text', text: trimmed.slice(1) };
  if (!trimmed.startsWith('/')) return { kind: 'text', text };
  const match = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(trimmed);
  const command = match ? commands.find(c => c.name === match[1]) : undefined;
  if (!match || !command) return { kind: 'text', text };
  const args = (match[2] ?? '').trim();
  // `/plans for tomorrow: …` is a message, not the `/plans` command: a
  // command that takes no argument runs only on its own.
  if (args && argKind(command.args) === 'none') return { kind: 'text', text };
  return { kind: 'command', command, args };
}

/**
 * The prefix typed after the slash while the composer holds only `/word`
 * (no space yet, no leading whitespace): the command menu shows for it.
 * Null when the menu has nothing to do with the text.
 */
export function commandMenuQuery(text: string): string | null {
  const match = /^\/([^\s/]*)$/.exec(text);
  return match ? match[1] : null;
}

/** The commands whose name starts with the typed prefix, in list order. */
export function filterCommands(commands: SlashCommand[], query: string): SlashCommand[] {
  return commands.filter(c => c.name.startsWith(query));
}

/** `/compact [instructions]`: how a command is written out for people. */
export function commandUsage(command: SlashCommand): string {
  return command.args ? `/${command.name} ${command.args}` : `/${command.name}`;
}

/**
 * The commands from `GET /api/commands`, or null when the answer holds no
 * usable list (an older server, a malformed body).
 */
export function commandsFromResponse(body: unknown): SlashCommand[] | null {
  const list = (body as { commands?: unknown } | null)?.commands;
  if (!Array.isArray(list)) return null;
  const out: SlashCommand[] = [];
  for (const raw of list) {
    if (!raw || typeof raw !== 'object') continue;
    const c = raw as Record<string, unknown>;
    if (typeof c.name !== 'string' || !/^[a-z0-9_-]+$/i.test(c.name)) continue;
    const choices = Array.isArray(c.choices) ? c.choices.filter((x): x is string => typeof x === 'string') : undefined;
    out.push({
      name: c.name,
      args: typeof c.args === 'string' ? c.args : '',
      help: typeof c.help === 'string' ? c.help : '',
      ...(choices && choices.length ? { choices } : {}),
    });
  }
  return out.length ? out : null;
}
