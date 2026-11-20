import { create } from 'zustand';
import { api } from '../api/client';
import { BUILTIN_COMMANDS, commandsFromResponse, type SlashCommand } from '../utils/slashCommands';

/**
 * The composer's slash commands. They start as the built-in list, so the
 * commands work before (and without) the server's answer; `load` replaces
 * them with `GET /api/commands?surface=web` once, and keeps the built-in
 * list when that fails.
 */
interface CommandState {
  commands: SlashCommand[];
  /** The server's list arrived. */
  fromServer: boolean;
  load: () => Promise<void>;
}

let loading: Promise<void> | null = null;

export const useCommandStore = create<CommandState>((set) => ({
  commands: BUILTIN_COMMANDS,
  fromServer: false,
  load: () => {
    // Through a resolved promise, so that a throw of the call itself lands
    // in the catch too.
    loading ??= Promise.resolve()
      .then(() => api.getCommands())
      .then((body) => {
        const commands = commandsFromResponse(body);
        if (commands) set({ commands, fromServer: true });
      })
      .catch(() => { /* keep the built-in list */ });
    return loading;
  },
}));

/** For tests: forget the loaded list. */
export function resetCommandStore(): void {
  loading = null;
  useCommandStore.setState({ commands: BUILTIN_COMMANDS, fromServer: false });
}
