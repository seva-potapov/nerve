import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/**
 * Slash commands in the composer: the menu that `/` opens, and what a sent
 * message is — a command only when its first word is a command's name, text
 * (to the model, unchanged) otherwise, and `//` for a leading slash.
 */

vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  api: new Proxy({} as Record<string, unknown>, {
    get: (target, key: string) => (target[key] ??= vi.fn(async () => ({}))),
  }),
}));
vi.mock('../../api/websocket', () => ({
  ws: {
    sendMessage: vi.fn(() => 'sent'), switchSession: vi.fn(), send: vi.fn(), connect: vi.fn(),
    takePendingMessages: vi.fn(() => []),
  },
}));

const { api } = await import('../../api/client');
const { useChatStore } = await import('../../stores/chatStore');
const { useUIStore } = await import('../../stores/uiStore');
const { resetCommandStore } = await import('../../stores/commandStore');
const { ChatInput } = await import('./ChatInput');

const onSend = vi.fn();
const onNavigate = vi.fn();

async function renderComposer() {
  render(<ChatInput onSend={onSend} onStop={vi.fn(async () => {})} isStreaming={false} onNavigate={onNavigate} />);
  await act(async () => {});
}

const textbox = () => screen.getByRole('textbox') as HTMLTextAreaElement;
const menu = () => screen.queryByRole('listbox', { name: 'Commands' });
const status = () => screen.getByRole('status');

beforeEach(() => {
  onSend.mockClear();
  onNavigate.mockClear();
  vi.mocked(api.compactSession).mockReset();
  vi.mocked(api.updateSession).mockReset();
  resetCommandStore();
  useUIStore.setState({ shortcutsModalOpen: false, contextSnapshot: null });
  useChatStore.setState({
    activeSession: 's1', drafts: {}, messages: [], virtualSession: null, quotes: [], isStreaming: false,
    contextUsage: null,
    sessions: [{ id: 's1', title: 'Chat', source: 'web', updated_at: '2026-11-20T10:00:00Z', backend: 'claude', model: 'claude-opus-5', sdk_session_id: 'n1' }],
    availableModels: [
      { id: 'claude-opus-5', provider: 'anthropic', backend: 'claude' },
      { id: 'claude-opus-4-5', provider: 'anthropic', backend: 'claude' },
      { id: 'claude-haiku-4-5', provider: 'anthropic', backend: 'claude' },
    ],
    modelDefaults: { claude: 'claude-opus-5' },
    loadModels: vi.fn(async () => {}),
  });
  // Desktop: Enter sends.
  vi.spyOn(window, 'matchMedia').mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null, addListener: () => {}, removeListener: () => {},
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
  }));
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('the command menu', () => {
  it('opens on / at the start and filters by the typed prefix', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/co');
    const options = within(menu()!).getAllByRole('option');
    expect(options.map(o => o.textContent)).toEqual([
      expect.stringContaining('/compact [instructions]'),
      expect.stringContaining('/context'),
    ]);
    expect(options[0]).toHaveAttribute('aria-selected', 'true');
    expect(textbox()).toHaveAttribute('aria-activedescendant', options[0].id);
  });

  it('stays closed for a path and for text that does not start with /', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/root');
    expect(menu()).toBeNull();
    await user.clear(textbox());
    await user.type(textbox(), 'a /co');
    expect(menu()).toBeNull();
  });

  it('moves the selection with the arrows and completes with Tab', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/co');
    await user.keyboard('{ArrowDown}');
    expect(within(menu()!).getAllByRole('option')[1]).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{ArrowDown}');
    expect(within(menu()!).getAllByRole('option')[0]).toHaveAttribute('aria-selected', 'true');
    await user.keyboard('{ArrowUp}{Tab}');
    expect(textbox()).toHaveValue('/context ');
    expect(textbox()).toHaveFocus();
    expect(menu()).toBeNull();
  });

  it('closes on Escape, keeps the text, and the Escape goes no further', async () => {
    const user = userEvent.setup();
    const onDocKey = vi.fn();
    document.addEventListener('keydown', onDocKey);
    await renderComposer();
    await user.type(textbox(), '/co');
    onDocKey.mockClear();
    await user.keyboard('{Escape}');
    expect(menu()).toBeNull();
    expect(textbox()).toHaveValue('/co');
    expect(onDocKey).not.toHaveBeenCalled();
    // Typing on opens it again.
    await user.type(textbox(), 'm');
    expect(menu()).not.toBeNull();
    document.removeEventListener('keydown', onDocKey);
  });

  it('runs the selected command on Enter', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/pl{Enter}');
    expect(onNavigate).toHaveBeenCalledWith('/plans');
    expect(onSend).not.toHaveBeenCalled();
    expect(textbox()).toHaveValue('');
  });

  it('completes on Enter when the command needs an argument', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/mod{Enter}');
    expect(textbox()).toHaveValue('/model ');
    expect(api.updateSession).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
  });

  it('leaves ↑ history recall alone while it is closed', async () => {
    useChatStore.setState({ messages: [{ role: 'user', blocks: [{ type: 'text', content: '/compact' }] }] });
    const user = userEvent.setup();
    await renderComposer();
    await user.keyboard('{ArrowUp}');
    expect(textbox()).toHaveValue('/compact');
    // A recalled prompt does not open the menu over it.
    expect(menu()).toBeNull();
    await user.keyboard('{ArrowDown}');
    expect(textbox()).toHaveValue('');
  });
});

describe('sending', () => {
  it('sends a path to the model unchanged', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/root/nerve-workspace/x is broken{Enter}');
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend.mock.calls[0][0]).toBe('/root/nerve-workspace/x is broken');
  });

  it('sends an unknown /word as text', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/compactfoo bar{Enter}');
    expect(onSend.mock.calls[0][0]).toBe('/compactfoo bar');
    expect(api.compactSession).not.toHaveBeenCalled();
  });

  it('sends //compact as the text /compact', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '//compact{Enter}');
    expect(onSend.mock.calls[0][0]).toBe('/compact');
    expect(api.compactSession).not.toHaveBeenCalled();
  });

  it('compacts with the instructions and shows the outcome', async () => {
    vi.mocked(api.compactSession).mockResolvedValueOnce({
      session_id: 's1', live: true, can_compact: true, total_tokens: 2800, max_tokens: 200000,
      auto_compact_threshold: null, categories: [], compacted: { pre_tokens: 14500, post_tokens: 2800 },
    });
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/compact keep PR numbers{Enter}');
    expect(api.compactSession).toHaveBeenCalledWith('s1', 'keep PR numbers');
    expect(onSend).not.toHaveBeenCalled();
    expect(await within(status()).findByText('Compacted: 14.5k → 2.8k tokens')).toBeInTheDocument();
    // The context meter of this session gets the new figures.
    expect(useUIStore.getState().contextSnapshot).toMatchObject({ sessionId: 's1', context: { total_tokens: 2800 } });
  });

  it('says Compacted. when the runtime gave no figures', async () => {
    vi.mocked(api.compactSession).mockResolvedValueOnce({
      session_id: 's1', live: false, can_compact: true, total_tokens: 0, max_tokens: 200000,
      auto_compact_threshold: null, categories: [], compacted: {},
    });
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/compact{Enter}');
    expect(api.compactSession).toHaveBeenCalledWith('s1', undefined);
    expect(await within(status()).findByText('Compacted.')).toBeInTheDocument();
  });

  it('shows the message of a refused compact', async () => {
    vi.mocked(api.compactSession).mockRejectedValueOnce(
      new Error('409: {"detail":{"code":"busy","message":"The agent is busy. Try again when the turn ends."}}'),
    );
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/compact{Enter}');
    expect(await within(status()).findByText('The agent is busy. Try again when the turn ends.')).toBeInTheDocument();
    expect(onSend).not.toHaveBeenCalled();
  });

  it('names the valid levels for a bad /effort', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/effort huge{Enter}');
    expect(within(status()).getByText('Unknown effort "huge". Use one of: low, medium, high, xhigh, max.')).toBeInTheDocument();
    expect(api.updateSession).not.toHaveBeenCalled();
  });

  it('sets the effort of this chat through the store, so the picker follows', async () => {
    const setSessionEffort = vi.spyOn(useChatStore.getState(), 'setSessionEffort');
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/effort HIGH{Enter}');
    expect(setSessionEffort).toHaveBeenCalledWith('s1', 'high');
    expect(api.updateSession).toHaveBeenCalledWith('s1', { effort: 'high' });
    expect(await within(status()).findByText('Effort set to high for this chat.')).toBeInTheDocument();
    expect(useChatStore.getState().sessions[0].effort).toBe('high');
    expect(screen.getByRole('combobox', { name: 'Effort' })).toHaveValue('high');
  });

  it('switches the model by its menu name', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/model haiku{Enter}');
    expect(api.updateSession).toHaveBeenCalledWith('s1', { model: 'claude-haiku-4-5' });
    expect(within(status()).getByText('Model set to Haiku 4.5 for this chat.')).toBeInTheDocument();
  });

  it('lists the candidates of an ambiguous model name', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/model opus{Enter}');
    expect(within(status()).getByText('"opus" matches more than one model: claude-opus-5, claude-opus-4-5.')).toBeInTheDocument();
    expect(api.updateSession).not.toHaveBeenCalled();
  });

  it('stars this chat and keeps the list in step', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/star{Enter}');
    expect(api.updateSession).toHaveBeenCalledWith('s1', { starred: true });
    expect(await within(status()).findByText('Starred.')).toBeInTheDocument();
    expect(useChatStore.getState().sessions[0].starred).toBe(true);
  });

  it('opens the shortcuts modal on /help, and says when /stop has nothing to stop', async () => {
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/help{Enter}');
    expect(useUIStore.getState().shortcutsModalOpen).toBe(true);
    await user.type(textbox(), '/stop{Enter}');
    expect(within(status()).getByText('Nothing is running.')).toBeInTheDocument();
    expect(onSend).not.toHaveBeenCalled();
  });

  it('runs /new with a title as a new named chat', async () => {
    const real = { id: 'srv1', title: '', source: 'web', status: 'created' };
    vi.mocked(api.createSession).mockResolvedValueOnce(real);
    const user = userEvent.setup();
    await renderComposer();
    await user.type(textbox(), '/new Weekly review{Enter}');
    await act(async () => {});
    expect(onNavigate).toHaveBeenCalledWith('/chat');
    expect(api.updateSession).toHaveBeenCalledWith('srv1', { title: 'Weekly review' });
    expect(onNavigate).toHaveBeenLastCalledWith('/chat/srv1', { replace: true });
    expect(useChatStore.getState().activeSession).toBe('srv1');
  });
});
