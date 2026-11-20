import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { Session } from '../../types/chat';

/** The session search from the keyboard: ↓/↑ through the results, Enter opens one. */

vi.mock('../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../api/client')>()),
  api: new Proxy({} as Record<string, unknown>, {
    get: (target, key: string) => (target[key] ??= key === 'listActors'
      ? vi.fn(async () => ({ actors: [] }))
      : vi.fn(async () => ({}))),
  }),
}));
vi.mock('../../api/websocket', () => ({
  ws: {
    switchSession: vi.fn(), send: vi.fn(), connect: vi.fn(), disconnect: vi.fn(), onMessage: vi.fn(() => () => {}),
    subscribeAgent: vi.fn(() => 'sent'), unsubscribeAgent: vi.fn(() => 'sent'), onOpen: vi.fn(() => () => {}),
  },
}));

const { api } = await import('../../api/client');
const { useChatStore } = await import('../../stores/chatStore');
const { SessionSidebar } = await import('./SessionSidebar');

const session = (id: string): Session => ({ id, title: `Title ${id}`, source: 'web', updated_at: new Date().toISOString() });

function Where() {
  return <div data-testid="where">{useLocation().pathname}</div>;
}

async function openSearch(query: string) {
  render(
    <MemoryRouter initialEntries={['/chat']}>
      <SessionSidebar
        sessions={[]} activeSession="" agentStatus={{ state: 'idle' }}
        onCreate={() => {}} onDelete={() => {}}
      />
      <Routes><Route path="*" element={<Where />} /></Routes>
    </MemoryRouter>,
  );
  act(() => { useChatStore.getState().requestSearchFocus(); });
  const user = userEvent.setup();
  const field = await screen.findByPlaceholderText('Search sessions...');
  expect(field).toHaveFocus();
  await user.type(field, query);
  await screen.findByText('2 results');
  return { user, field };
}

beforeEach(() => {
  localStorage.clear();
  useChatStore.setState({
    sessions: [], searchResults: null, searchQuery: '', archivedSessions: null, systemSessions: null,
    archivedCount: 0, systemCount: 0, drafts: {}, reads: {}, readsBaseline: 0,
    virtualSession: null, activeSession: '',
  });
  vi.mocked(api.searchSessions).mockResolvedValue({ sessions: [session('a1'), session('b2')] });
});

describe('the session search from the keyboard', () => {
  it('moves through the results with ↓ and ↑, and back to the field', async () => {
    const { user, field } = await openSearch('title');
    const links = () => screen.getAllByRole('link').filter(a => a.getAttribute('href')?.startsWith('/chat/'));
    await user.keyboard('{ArrowDown}');
    expect(links()[0]).toHaveFocus();
    await user.keyboard('{ArrowDown}');
    expect(links()[1]).toHaveFocus();
    await user.keyboard('{ArrowDown}');
    expect(links()[1]).toHaveFocus();
    await user.keyboard('{ArrowUp}{ArrowUp}');
    expect(field).toHaveFocus();
  });

  it('opens the focused result on Enter', async () => {
    const { user } = await openSearch('title');
    await user.keyboard('{ArrowDown}{ArrowDown}{Enter}');
    expect(screen.getByTestId('where')).toHaveTextContent('/chat/b2');
  });

  it('opens the first result on Enter in the field', async () => {
    const { user } = await openSearch('title');
    await user.keyboard('{Enter}');
    expect(screen.getByTestId('where')).toHaveTextContent('/chat/a1');
  });
});
