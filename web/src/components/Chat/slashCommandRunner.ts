import { api, apiErrorDetail } from '../../api/client';
import { useChatStore } from '../../stores/chatStore';
import { useUIStore } from '../../stores/uiStore';
import { findSessionById } from '../../utils/findSession';
import { forkChat } from '../../utils/forkChat';
import { modelLabel, type ModelEntry } from '../../utils/modelNames';
import { argKind, commandUsage, type SlashCommand } from '../../utils/slashCommands';
import { formatTokens } from './contextUsage';

/**
 * What a slash command did, shown in the composer's status line. Never sent
 * to the model. `busy`: it still runs (a compact), and a later report
 * replaces this one.
 */
export interface CommandFeedback {
  tone: 'success' | 'error' | 'info';
  text: string;
  busy?: boolean;
}

/** What a command needs from the composer that is not in a store. */
export interface CommandContext {
  navigate: (to: string, options?: { replace?: boolean }) => void;
  /**
   * Show feedback (null clears it). `sessionId`: the chat it is about, when
   * that is not the chat on screen once it arrives (a compact that finishes
   * after a switch).
   */
  report: (feedback: CommandFeedback | null, sessionId?: string) => void;
  /** The models the composer's model menu can switch this chat to. */
  models: ModelEntry[];
  currentModel: string | null;
  pickModel: (backend: string, id: string) => void;
  /** Why the model cannot change right now (a turn runs, agents run), or null. */
  modelLocked: string | null;
}

/** The server's message for a failed request, or the fallback. */
export function errorText(e: unknown, fallback: string): string {
  const message = apiErrorDetail(e)?.message;
  if (typeof message === 'string' && message) return message;
  if (e instanceof Error) {
    const colon = e.message.indexOf(':');
    try {
      const detail: unknown = JSON.parse(e.message.slice(colon + 1).trim())?.detail;
      if (typeof detail === 'string' && detail) return detail;
    } catch { /* not a JSON body */ }
  }
  return fallback;
}

const normalize = (s: string) => s.toLowerCase().replace(/[\s._-]+/g, '');

/**
 * The model that `/model <query>` names: an id (any letter case), a menu
 * name ("Opus 4.5", "opus-4.5", "opus45"), or the unique start of either
 * ("haiku" when there is one Haiku). `candidates` lists the models a query
 * that matches more than one could mean.
 */
export function matchModel(query: string, models: ModelEntry[]): { match: ModelEntry | null; candidates: ModelEntry[] } {
  const q = query.trim();
  if (!q) return { match: null, candidates: [] };
  const byId = models.find(m => m.id.toLowerCase() === q.toLowerCase());
  if (byId) return { match: byId, candidates: [] };
  const n = normalize(q);
  const names = (m: ModelEntry) => [normalize(m.id), normalize(modelLabel(m.id)), normalize(m.id.replace(/^claude-/, ''))];
  const exact = models.filter(m => names(m).includes(n));
  if (exact.length === 1) return { match: exact[0], candidates: [] };
  if (exact.length > 1) return { match: null, candidates: exact };
  const prefix = models.filter(m => names(m).some(name => name.startsWith(n)));
  if (prefix.length === 1) return { match: prefix[0], candidates: [] };
  return { match: null, candidates: prefix };
}

/**
 * Run a slash command of the composer. Feedback goes to `ctx.report`; a
 * command whose effect is on screen (a new chat, a page, a dialog) reports
 * nothing on success.
 */
export async function runSlashCommand(command: SlashCommand, args: string, ctx: CommandContext): Promise<void> {
  const store = useChatStore.getState();
  const sessionId = store.activeSession;
  const isNew = !sessionId || store.virtualSession?.id === sessionId;
  const row = sessionId
    ? findSessionById(sessionId, store.sessions, store.archivedSessions, store.systemSessions)
    : undefined;
  const fail = (text: string) => ctx.report({ tone: 'error', text });

  if (argKind(command.args) === 'required' && !args) {
    fail(`Usage: ${commandUsage(command)}`);
    return;
  }

  switch (command.name) {
    case 'compact': {
      if (isNew) return fail('Nothing to compact in a new chat.');
      ctx.report({ tone: 'info', text: 'Compacting… this can take a minute.', busy: true }, sessionId);
      try {
        const result = await api.compactSession(sessionId, args || undefined);
        useUIStore.getState().publishContext(sessionId, result);
        // The runtime's figures, when it gave them (`{}` or null otherwise).
        const pre = result.compacted?.pre_tokens;
        const post = result.compacted?.post_tokens;
        ctx.report({
          tone: 'success',
          text: typeof pre === 'number' && typeof post === 'number'
            ? `Compacted: ${formatTokens(pre)} → ${formatTokens(post)} tokens`
            : 'Compacted.',
        }, sessionId);
      } catch (e) {
        ctx.report({ tone: 'error', text: errorText(e, 'Compact failed.') }, sessionId);
      }
      return;
    }

    case 'context': {
      if (isNew || !store.contextUsage) {
        ctx.report({ tone: 'info', text: 'No context figures yet: they show after the first answer.' });
        return;
      }
      useUIStore.getState().requestContextPopover();
      return;
    }

    case 'new': {
      ctx.navigate('/chat');
      await useChatStore.getState().createSession();
      if (!args) return;
      try {
        const id = await useChatStore.getState().ensureRealSession();
        await useChatStore.getState().renameSession(id, args);
        ctx.navigate(`/chat/${id}`, { replace: true });
      } catch (e) {
        fail(errorText(e, 'Could not create the chat.'));
      }
      return;
    }

    case 'stop': {
      if (!store.isStreaming) {
        ctx.report({ tone: 'info', text: 'Nothing is running.' });
        return;
      }
      await store.stopSession();
      ctx.report({ tone: 'success', text: 'Stop sent.' });
      return;
    }

    case 'star':
    case 'unstar': {
      const starred = command.name === 'star';
      if (isNew) return fail(`Send a message first: a new chat cannot be ${starred ? 'starred' : 'unstarred'}.`);
      if (row && !!row.starred === starred) {
        ctx.report({ tone: 'info', text: starred ? 'Already starred.' : 'Not starred.' });
        return;
      }
      try {
        await store.setSessionStarred(sessionId, starred);
        ctx.report({ tone: 'success', text: starred ? 'Starred.' : 'Unstarred.' });
      } catch (e) {
        fail(errorText(e, starred ? 'Could not star this chat.' : 'Could not unstar this chat.'));
      }
      return;
    }

    case 'fork': {
      if (isNew || (row && !row.sdk_session_id)) return fail('Nothing to fork yet: send a message first.');
      const forkId = await forkChat(sessionId);
      if (forkId) ctx.navigate(`/chat/${forkId}`);
      return;
    }

    case 'sessions': {
      store.revealSessionList();
      store.requestSearchFocus();
      return;
    }

    case 'plans':
      ctx.navigate('/plans');
      return;

    case 'doctor':
      ctx.navigate('/diagnostics');
      return;

    case 'model': {
      if (ctx.modelLocked) return fail(ctx.modelLocked);
      const { match, candidates } = matchModel(args, ctx.models);
      if (!match) {
        const offered = (candidates.length ? candidates : ctx.models).map(m => m.id);
        return fail(
          candidates.length
            ? `"${args}" matches more than one model: ${offered.join(', ')}.`
            : offered.length
              ? `Unknown model "${args}". Models: ${offered.join(', ')}.`
              : 'This chat has no other model to switch to.',
        );
      }
      if (match.id === ctx.currentModel) {
        ctx.report({ tone: 'info', text: `Already on ${modelLabel(match.id)}.` });
        return;
      }
      ctx.pickModel(match.backend, match.id);
      ctx.report({ tone: 'success', text: `Model set to ${modelLabel(match.id)} for this chat.` });
      return;
    }

    case 'help':
      useUIStore.getState().openShortcutsModal();
      return;

    default:
      fail(`/${command.name} is not available in the web chat.`);
  }
}
