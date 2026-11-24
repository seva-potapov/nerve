import { act, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SessionProcess, SessionProcesses } from '../../../types/agents';

const getSessionProcesses = vi.fn();
const signalProcess = vi.fn();
vi.mock('../../../api/client', async (orig) => ({
  ...(await orig<typeof import('../../../api/client')>()),
  api: {
    getSessionProcesses: (...a: unknown[]) => getSessionProcesses(...a),
    signalProcess: (...a: unknown[]) => signalProcess(...a),
  },
}));

const { ProcessesSection, PROCESS_POLL_MS, formatBytes } = await import('./ProcessesSection');

function proc(pid: number, role: SessionProcess['role'], extra: Partial<SessionProcess> = {}): SessionProcess {
  return {
    pid, ppid: 1, role, task_id: null, agent_id: null, name: 'x', state: 'S', start_ticks: pid * 10,
    started_at: new Date(Date.now() - 65_000).toISOString(), cpu_seconds: 1, cpu_percent: 2.5,
    rss_bytes: 50 * 1024 * 1024, threads: 1, cwd: '/w', command: `cmd-${pid}`, ...extra,
  };
}

function view(processes: SessionProcess[], supported = true): SessionProcesses {
  return { session_id: 's1', supported, root_pid: 100, processes };
}

async function flush() {
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: false });
  getSessionProcesses.mockReset();
  signalProcess.mockReset();
});
afterEach(() => {
  vi.useRealTimers();
});

describe('ProcessesSection', () => {
  it('groups processes by role and folds the CLI internals', async () => {
    getSessionProcesses.mockResolvedValue(view([
      proc(100, 'cli'), proc(101, 'internal'), proc(102, 'call', { task_id: 'b1234567' }),
      proc(103, 'background', { agent_id: 'ag1' }), proc(200, 'orphan'),
    ]));
    render(<ProcessesSection sessionId="s1" />);
    await flush();
    expect(screen.getByText('Processes (3)')).toBeTruthy();
    expect(within(screen.getByRole('list', { name: 'Current call' })).getByText('cmd-102')).toBeTruthy();
    expect(within(screen.getByRole('list', { name: 'Background' })).getByText('cmd-103')).toBeTruthy();
    expect(within(screen.getByRole('list', { name: 'Left running' })).getByText('cmd-200')).toBeTruthy();
    expect(screen.queryByText('cmd-100')).toBeNull();
    act(() => { screen.getByRole('button', { name: /CLI internals/ }).click(); });
    expect(screen.getByText('cmd-100')).toBeTruthy();
    expect(screen.getByText('cmd-101')).toBeTruthy();
    // CLI and its helpers have no Stop.
    expect(screen.queryByRole('button', { name: 'Stop process 100' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Stop process 101' })).toBeNull();
    expect(screen.getByText(/PID 102 · 1m · 2.5% CPU · 50 MB/)).toBeTruthy();
  });

  it('polls while mounted and stops when unmounted', async () => {
    getSessionProcesses.mockResolvedValue(view([]));
    const { unmount } = render(<ProcessesSection sessionId="s1" />);
    await flush();
    expect(getSessionProcesses).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(PROCESS_POLL_MS); });
    expect(getSessionProcesses).toHaveBeenCalledTimes(2);
    unmount();
    await act(async () => { vi.advanceTimersByTime(PROCESS_POLL_MS * 3); });
    expect(getSessionProcesses).toHaveBeenCalledTimes(2);
  });

  it('does not poll while the page is hidden', async () => {
    getSessionProcesses.mockResolvedValue(view([]));
    const spy = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    render(<ProcessesSection sessionId="s1" />);
    await act(async () => { vi.advanceTimersByTime(PROCESS_POLL_MS * 3); });
    expect(getSessionProcesses).not.toHaveBeenCalled();
    spy.mockReturnValue('visible');
    await act(async () => { document.dispatchEvent(new Event('visibilitychange')); });
    await flush();
    expect(getSessionProcesses).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });

  it('stops a process on the second press, with its start ticks and the tree', async () => {
    getSessionProcesses.mockResolvedValue(view([proc(200, 'orphan')]));
    signalProcess.mockResolvedValue({ pid: 200, via: 'signal', signalled: [200] });
    render(<ProcessesSection sessionId="s1" />);
    await flush();
    act(() => { screen.getByRole('button', { name: 'Stop process 200' }).click(); });
    expect(signalProcess).not.toHaveBeenCalled();
    await act(async () => { screen.getByRole('button', { name: 'Confirm: stop process 200' }).click(); });
    expect(signalProcess).toHaveBeenCalledWith('s1', 200, { signal: 'TERM', start_ticks: 2000, tree: true });
    await flush();
    expect(screen.getAllByText('Sent TERM to 200').length).toBeGreaterThan(0);
  });

  it('says when the server cannot list processes', async () => {
    getSessionProcesses.mockResolvedValue(view([], false));
    render(<ProcessesSection sessionId="s1" />);
    await flush();
    expect(screen.getByText(/needs a Linux server/)).toBeTruthy();
  });

  it('renders nothing for a malformed answer', async () => {
    getSessionProcesses.mockResolvedValue({});
    const { container } = render(<ProcessesSection sessionId="s1" />);
    await flush();
    expect(container.textContent).toBe('');
  });

  it('formats bytes', () => {
    expect(formatBytes(512 * 1024)).toBe('512 KB');
    expect(formatBytes(45 * 1024 * 1024)).toBe('45 MB');
    expect(formatBytes(1.25 * 1024 ** 3)).toBe('1.3 GB');
  });
});
