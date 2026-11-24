import { create } from 'zustand';
import type { SessionContext } from '../api/client';

/**
 * Misc UI state that doesn't belong to a single feature store: the
 * keyboard-shortcuts modal flag, the composer's `/context` and
 * `/compact` reaching the chat header's context meter, and the text answer
 * of a command such as `/status` or `/btw`.
 */
interface UIState {
  shortcutsModalOpen: boolean;
  openShortcutsModal: () => void;
  closeShortcutsModal: () => void;
  toggleShortcutsModal: () => void;
  /** Bumped by `/context`: the context meter opens its popover. */
  contextPopoverNonce: number;
  requestContextPopover: () => void;
  /** The context measured by a `/compact`, for the meter of that session. */
  contextSnapshot: { sessionId: string; context: SessionContext; nonce: number } | null;
  publishContext: (sessionId: string, context: SessionContext) => void;
  /** A command's text answer (`/status`, `/cost`, `/diag`, `/btw`), shown in a dialog. */
  commandOutput: { title: string; text: string } | null;
  showCommandOutput: (title: string, text: string) => void;
  closeCommandOutput: () => void;
}

export const useUIStore = create<UIState>((set, get) => ({
  shortcutsModalOpen: false,
  openShortcutsModal: () => set({ shortcutsModalOpen: true }),
  closeShortcutsModal: () => set({ shortcutsModalOpen: false }),
  toggleShortcutsModal: () => set({ shortcutsModalOpen: !get().shortcutsModalOpen }),
  contextPopoverNonce: 0,
  requestContextPopover: () => set(s => ({ contextPopoverNonce: s.contextPopoverNonce + 1 })),
  contextSnapshot: null,
  publishContext: (sessionId, context) => set(s => ({
    contextSnapshot: { sessionId, context, nonce: (s.contextSnapshot?.nonce ?? 0) + 1 },
  })),
  commandOutput: null,
  showCommandOutput: (title, text) => set({ commandOutput: { title, text } }),
  closeCommandOutput: () => set({ commandOutput: null }),
}));
