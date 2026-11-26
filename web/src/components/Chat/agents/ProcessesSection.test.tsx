import { act, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { SessionProcess, SessionProcesses } from '../../../types/agents';
import { processTree, procKey } from './processTree';

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

/** [pid, aria-level] of each row in a group's list, in screen order. */
function rowsOf(group: string): [number, number][] {
  return within(screen.getByRole('list', { name: group })).getAllByRole('listitem')
    .map(li => [Number(li.getAttribute('data-process-pid')), Number(li.getAttribute('aria-level'))]);
}

/** A sweep left running: python3 -> 2 x timeout -> clickhouse, in the
 * server's order (start time, then PID), so each clickhouse row comes after
 * both timeouts, as when they start in the same clock tick. */
function sweep(): SessionProcess[] {
  return [
    proc(200, 'orphan', { start_ticks: 1000, command: 'python3 -I detect_new.py' }),
    proc(201, 'orphan', { ppid: 200, start_ticks: 2000, command: 'timeout 280 clickhouse client A' }),
    proc(202, 'orphan', { ppid: 200, start_ticks: 2000, command: 'timeout 280 clickhouse client B' }),
    proc(203, 'orphan', { ppid: 201, start_ticks: 2000, command: 'clickhouse client A' }),
    proc(204, 'orphan', { ppid: 202, start_ticks: 2000, command: 'clickhouse client B' }),
  ];
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

  it('shows each process under its parent, all open', async () => {
    getSessionProcesses.mockResolvedValue(view(sweep()));
    render(<ProcessesSection sessionId="s1" />);
    await flush();
    expect(rowsOf('Left running')).toEqual([[200, 1], [201, 2], [203, 3], [202, 2], [204, 3]]);
    expect(screen.getByText('Left running (5)')).toBeTruthy();
    // Parents fold, leaves don't; a parent's Stop still takes its whole tree.
    expect(screen.getByRole('button', { name: 'Collapse PID 200' }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByRole('button', { name: 'Collapse PID 201' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Collapse PID 203' })).toBeNull();
    signalProcess.mockResolvedValue({ pid: 200, via: 'signal', signalled: [203, 204, 201, 202, 200] });
    act(() => { screen.getByRole('button', { name: 'Stop process 200' }).click(); });
    await act(async () => { screen.getByRole('button', { name: 'Confirm: stop process 200' }).click(); });
    expect(signalProcess).toHaveBeenCalledWith('s1', 200, { signal: 'TERM', start_ticks: 1000, tree: true });
  });

  it('starts a group at a process whose parent is in another group', async () => {
    getSessionProcesses.mockResolvedValue(view([
      proc(100, 'cli'), proc(101, 'internal', { ppid: 100 }),
      proc(102, 'call', { ppid: 100 }), proc(103, 'call', { ppid: 102 }),
    ]));
    render(<ProcessesSection sessionId="s1" />);
    await flush();
    expect(rowsOf('Current call')).toEqual([[102, 1], [103, 2]]);
    act(() => { screen.getByRole('button', { name: /CLI internals/ }).click(); });
    expect(rowsOf('CLI internals')).toEqual([[100, 1], [101, 2]]);
  });

  it('folds a subtree on its arrow and keeps it folded across polls', async () => {
    // Each poll answers with new objects, as the server does.
    getSessionProcesses.mockImplementation(async () => view(sweep()));
    render(<ProcessesSection sessionId="s1" />);
    await flush();
    act(() => { screen.getByRole('button', { name: 'Collapse PID 200' }).click(); });
    expect(rowsOf('Left running')).toEqual([[200, 1]]);
    expect(screen.getByText(/PID 200 · .* · 4 hidden/)).toBeTruthy();
    // The group still counts every process.
    expect(screen.getByText('Left running (5)')).toBeTruthy();
    await act(async () => { vi.advanceTimersByTime(PROCESS_POLL_MS); });
    await flush();
    expect(getSessionProcesses).toHaveBeenCalledTimes(2);
    expect(rowsOf('Left running')).toEqual([[200, 1]]);
    act(() => { screen.getByRole('button', { name: 'Expand PID 200' }).click(); });
    expect(rowsOf('Left running')).toEqual([[200, 1], [201, 2], [203, 3], [202, 2], [204, 3]]);
    expect(screen.queryByText(/hidden/)).toBeNull();
  });

  it('adds no fold arrows to a group without nesting', async () => {
    getSessionProcesses.mockResolvedValue(view([proc(200, 'orphan'), proc(300, 'orphan')]));
    render(<ProcessesSection sessionId="s1" />);
    await flush();
    expect(rowsOf('Left running')).toEqual([[200, 1], [300, 1]]);
    expect(screen.queryByRole('button', { name: /Collapse PID/ })).toBeNull();
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

describe('processTree', () => {
  const shape = (rows: ReturnType<typeof processTree>) => rows.map(r => [r.proc.pid, r.depth, r.childCount, r.hidden]);

  it('nests by ppid and keeps the list order among siblings', () => {
    expect(shape(processTree(sweep()))).toEqual([
      [200, 0, 2, 0], [201, 1, 1, 0], [203, 2, 0, 0], [202, 1, 1, 0], [204, 2, 0, 0],
    ]);
  });

  it('counts the rows a fold hides, nested folds included', () => {
    const procs = sweep();
    const folded = new Set([procKey(procs[1]), procKey(procs[0])]);
    expect(shape(processTree(procs, folded))).toEqual([[200, 0, 2, 4]]);
    expect(shape(processTree(procs, new Set([procKey(procs[1])])))).toEqual([
      [200, 0, 2, 0], [201, 1, 1, 1], [202, 1, 1, 0], [204, 2, 0, 0],
    ]);
  });

  it('does not fold a new process that reuses a folded PID', () => {
    const procs = sweep();
    const stale = new Set([procKey({ ...procs[0], start_ticks: 1 })]);
    expect(processTree(procs, stale)).toHaveLength(5);
  });

  it('shows every process once when parent links form a cycle', () => {
    const a = proc(10, 'orphan', { ppid: 11 });
    const b = proc(11, 'orphan', { ppid: 10 });
    const self = proc(12, 'orphan', { ppid: 12 });
    expect(shape(processTree([a, b, self]))).toEqual([[12, 0, 0, 0], [10, 0, 1, 0], [11, 1, 1, 0]]);
  });
});
