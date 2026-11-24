import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { api } from '../../../api/client';
import type { ProcessRole, SessionProcess, SessionProcesses } from '../../../types/agents';
import { ChevronDown, ChevronRight, Cpu } from '../../ui/icons';
import { formatElapsed } from './agentVisuals';
import { TwoStepStopButton } from './TwoStepStopButton';

/** How often the list refreshes while it is on screen. */
export const PROCESS_POLL_MS = 3000;
/** A Stop stays armed this long. */
const ARM_MS = 3000;

const GROUPS: { key: string; title: string; roles: ProcessRole[]; collapsed?: boolean }[] = [
  { key: 'call', title: 'Current call', roles: ['call'] },
  { key: 'background', title: 'Background', roles: ['background'] },
  { key: 'orphan', title: 'Left running', roles: ['orphan'] },
  { key: 'internal', title: 'CLI internals', roles: ['cli', 'internal'], collapsed: true },
];
const SIGNALABLE: ReadonlySet<ProcessRole> = new Set(['call', 'background', 'orphan']);

/** "512 KB", "45 MB", "1.2 GB". */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '';
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  if (n < 1024 ** 3) return `${Math.round(n / 1024 / 1024)} MB`;
  return `${(n / 1024 ** 3).toFixed(1)} GB`;
}

function useVisible(): boolean {
  const [visible, setVisible] = useState(() => typeof document === 'undefined' || document.visibilityState !== 'hidden');
  useEffect(() => {
    const on = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', on);
    return () => document.removeEventListener('visibilitychange', on);
  }, []);
  return visible;
}

/**
 * The OS processes the session started, read from the server's /proc:
 * Current call / Background / Left running / CLI internals. Polls every
 * 3 s while mounted (the panel is open) and the page is visible.
 */
export function ProcessesSection({ sessionId }: { sessionId: string | null }) {
  const visible = useVisible();
  const [view, setView] = useState<SessionProcesses | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [armed, setArmed] = useState<number | null>(null);
  const [internalOpen, setInternalOpen] = useState(false);
  const [notice, setNotice] = useState('');
  const armTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const live = useRef(true);

  const load = useCallback(async () => {
    if (!sessionId) return;
    try {
      const v = await api.getSessionProcesses(sessionId);
      if (!live.current) return;
      setView(v && Array.isArray(v.processes) ? v : null);
      setError(null);
    } catch (e) {
      if (live.current) setError(e instanceof Error ? e.message : String(e));
    }
  }, [sessionId]);

  useEffect(() => {
    live.current = true;
    setView(null);
    return () => { live.current = false; };
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId || !visible) return;
    void load();
    const t = setInterval(() => { void load(); }, PROCESS_POLL_MS);
    return () => clearInterval(t);
  }, [sessionId, visible, load]);

  useEffect(() => () => {
    if (armTimer.current !== null) clearTimeout(armTimer.current);
  }, []);

  const disarm = useCallback(() => {
    if (armTimer.current !== null) clearTimeout(armTimer.current);
    armTimer.current = null;
    setArmed(null);
  }, []);

  const press = async (p: SessionProcess) => {
    if (armed !== p.pid) {
      if (armTimer.current !== null) clearTimeout(armTimer.current);
      setArmed(p.pid);
      armTimer.current = setTimeout(() => { armTimer.current = null; setArmed(null); }, ARM_MS);
      return;
    }
    disarm();
    if (!sessionId) return;
    try {
      const r = await api.signalProcess(sessionId, p.pid, { signal: 'TERM', start_ticks: p.start_ticks, tree: true });
      setNotice(r?.via === 'agent' ? `Stopping background shell ${p.pid}` : `Sent TERM to ${p.pid}`);
    } catch (e) {
      setNotice(`Could not stop ${p.pid}: ${e instanceof Error ? e.message : String(e)}`);
    }
    void load();
  };

  if (!sessionId || (!view && !error)) return null;
  if (view && !view.supported) {
    return (
      <section aria-label="Processes" className="px-3 pt-3 pb-3 border-t border-border-subtle">
        <h3 className="text-2xs uppercase tracking-wider text-text-faint">Processes</h3>
        <p className="text-xs text-text-faint mt-1">Process listing needs a Linux server (/proc).</p>
      </section>
    );
  }

  const procs = view?.processes ?? [];
  const now = Date.now();
  const userCount = procs.filter(p => p.role !== 'cli' && p.role !== 'internal').length;

  return (
    <section aria-label="Processes" className="pb-3 border-t border-border-subtle">
      <div className="flex items-center gap-2 px-3 pt-3 pb-1">
        <Cpu size={12} className="text-text-faint" aria-hidden="true" />
        <h3 className="text-2xs uppercase tracking-wider text-text-faint">Processes ({userCount})</h3>
      </div>
      {error && <p role="alert" className="px-3 text-xs text-error">Could not list processes: {error}</p>}
      {procs.length === 0 && !error && (
        <p className="px-3 text-xs text-text-faint">No processes running.</p>
      )}
      {GROUPS.map(g => {
        const rows = procs.filter(p => g.roles.includes(p.role));
        if (rows.length === 0) return null;
        const collapsed = g.collapsed ? !internalOpen : false;
        return (
          <ProcGroup
            key={g.key}
            title={g.title}
            count={rows.length}
            collapsed={collapsed}
            onToggle={g.collapsed ? () => setInternalOpen(v => !v) : undefined}
          >
            {rows.map(p => (
              <ProcRow
                key={`${p.pid}:${p.start_ticks}`}
                proc={p}
                nowMs={now}
                armed={armed === p.pid}
                onPress={SIGNALABLE.has(p.role) && p.state !== 'Z' ? () => { void press(p); } : undefined}
                onDisarm={disarm}
              />
            ))}
          </ProcGroup>
        );
      })}
      <div aria-live="polite" className="sr-only">{notice}</div>
      {notice && <p className="px-3 pt-1 text-2xs text-text-faint">{notice}</p>}
    </section>
  );
}

function ProcGroup({ title, count, collapsed, onToggle, children }: {
  title: string; count: number; collapsed: boolean; onToggle?: () => void; children: ReactNode;
}) {
  const label = `${title} (${count})`;
  const cls = 'text-2xs tracking-wide text-text-faint';
  return (
    <div className="mt-1">
      <div className="px-3 pt-1.5 pb-0.5">
        {onToggle ? (
          <button
            type="button"
            aria-expanded={!collapsed}
            onClick={onToggle}
            className={`${cls} inline-flex items-center gap-1 cursor-pointer hover:text-text-muted`}
          >
            {collapsed ? <ChevronRight size={11} aria-hidden="true" /> : <ChevronDown size={11} aria-hidden="true" />}
            {label}
          </button>
        ) : (
          <h4 className={cls}>{label}</h4>
        )}
      </div>
      {!collapsed && <ul aria-label={title}>{children}</ul>}
    </div>
  );
}

function ProcRow({ proc, nowMs, armed, onPress, onDisarm }: {
  proc: SessionProcess; nowMs: number; armed: boolean; onPress?: () => void; onDisarm: () => void;
}) {
  const age = proc.started_at ? formatElapsed(nowMs - Date.parse(proc.started_at)) : '';
  const meta = [
    `PID ${proc.pid}`,
    age,
    proc.cpu_percent !== null && proc.cpu_percent !== undefined ? `${proc.cpu_percent}% CPU` : null,
    formatBytes(proc.rss_bytes),
    proc.state === 'Z' ? 'zombie' : proc.state === 'R' ? 'running' : null,
  ].filter(Boolean).join(' · ');
  return (
    <li className="flex items-start gap-2 px-3 py-1 hover:bg-surface-hover" data-process-pid={proc.pid}>
      <div className="min-w-0 flex-1">
        <div className="font-mono text-xs text-text truncate" title={proc.command}>{proc.command}</div>
        <div className="text-2xs text-text-faint tabular-nums truncate" title={proc.cwd ?? undefined}>{meta}</div>
      </div>
      {onPress && (
        <TwoStepStopButton label={`process ${proc.pid}`} armed={armed} onPress={onPress} onDisarm={onDisarm} />
      )}
    </li>
  );
}
