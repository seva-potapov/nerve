import { useEffect, useRef, useState } from 'react';
import { api, apiErrorDetail, type SessionContext } from '../../api/client';
import { useUIStore } from '../../stores/uiStore';
import { ContextPopover, type CompactState } from './ContextPopover';
import { contextTokens, formatTokens, meterColor, type ContextUsage } from './contextUsage';

const POPOVER_ID = 'context-popover';

/**
 * The chat header's context meter. A click opens the context popover, which
 * loads the breakdown of the session's context and offers Compact. Render it
 * with `key={sessionId}`, so a session switch starts it fresh.
 */
export function ContextBar({ usage, sessionId, backend, busy, sessionCostUsd }: {
  usage: ContextUsage;
  sessionId: string;
  backend: string;
  /** A turn runs: Compact is disabled until the agent is idle. */
  busy: boolean;
  sessionCostUsd?: number;
}) {
  const [open, setOpen] = useState(false);
  // The last breakdown, with the usage it was loaded after: the next turn's
  // usage makes it old.
  const [loaded, setLoaded] = useState<{ usage: ContextUsage; context: SessionContext } | null>(null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [compact, setCompact] = useState<CompactState>({ step: 'idle' });
  const wrapRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  // Opened by the composer's `/context`: the popover takes the focus, and
  // Escape hands it back to the composer rather than to this button.
  const fromCommandRef = useRef(false);

  // `/context` in the composer, and the context a `/compact` of this session
  // measured. Only what happens while this meter is mounted counts: a
  // session switch remounts it, and must not reopen it.
  const usageRef = useRef(usage);
  useEffect(() => { usageRef.current = usage; }, [usage]);
  useEffect(() => useUIStore.subscribe((state, prev) => {
    if (state.contextPopoverNonce !== prev.contextPopoverNonce) {
      fromCommandRef.current = true;
      setCompact(c => (c.step === 'running' ? c : { step: 'idle' }));
      setOpen(true);
      requestAnimationFrame(() => document.getElementById(POPOVER_ID)?.focus());
    }
    const snapshot = state.contextSnapshot;
    if (snapshot && snapshot !== prev.contextSnapshot && snapshot.sessionId === sessionId) {
      setLoaded({ usage: usageRef.current, context: snapshot.context });
    }
  }), [sessionId]);

  // Load the breakdown when the popover opens, and again after each turn.
  useEffect(() => {
    if (!open) return;
    let stale = false;
    api.getSessionContext(sessionId)
      .then(context => {
        if (stale) return;
        setLoaded({ usage, context });
        setLoadFailed(false);
      })
      .catch(() => { if (!stale) setLoadFailed(true); });
    return () => { stale = true; };
  }, [open, sessionId, usage]);

  // Close on an outside press, and on Escape, which must not also reach the
  // global Escape that stops the turn.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      e.preventDefault();
      e.stopPropagation();
      setOpen(false);
      if (fromCommandRef.current) document.getElementById('nerve-chat-input')?.focus();
      else triggerRef.current?.focus();
      fromCommandRef.current = false;
    };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey, true);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey, true);
    };
  }, [open]);

  // The meter shows the measured use while no turn ended after it.
  const measured = loaded?.usage === usage && loaded.context.live ? loaded.context : null;
  const estimate = contextTokens(usage);
  const used = measured?.total_tokens ?? estimate.tokens;
  const exact = measured != null || estimate.exact;
  const max = measured?.max_tokens || usage.max_context_tokens;
  const pct = max > 0 ? Math.min((used / max) * 100, 100) : 0;

  const toggle = () => {
    fromCommandRef.current = false;
    if (!open && compact.step !== 'running') setCompact({ step: 'idle' });
    setOpen(!open);
  };

  const runCompact = async () => {
    setCompact({ step: 'running' });
    try {
      const after = await api.compactSession(sessionId);
      setLoaded({ usage, context: after });
      setCompact({ step: 'done', freed: used - after.total_tokens });
    } catch (e) {
      const message = apiErrorDetail(e)?.message;
      setCompact({ step: 'error', message: typeof message === 'string' ? message : 'Compact failed.' });
    }
  };

  return (
    <div ref={wrapRef} className="relative flex">
      <button
        ref={triggerRef}
        type="button"
        aria-expanded={open}
        aria-controls={open ? POPOVER_ID : undefined}
        aria-label={`Context window: ${exact ? '' : 'about '}${formatTokens(used)} of ${formatTokens(max)} tokens`}
        onClick={toggle}
        // On a phone, the pseudo-element makes the tap target 44 px high
        // (28 px + 2 x 8 px) and keeps the header and the meter as they are.
        className="relative flex items-center gap-2 h-7 px-1.5 rounded hover:bg-surface-raised cursor-pointer transition-colors before:absolute before:inset-x-0 before:-inset-y-2 md:before:hidden"
      >
        <span className="hidden md:inline text-xs text-text-dim whitespace-nowrap tabular-nums">
          {exact ? '' : '~'}{formatTokens(used)} / {formatTokens(max)}
        </span>
        <span className="md:hidden text-xs text-text-dim tabular-nums">{Math.round(pct)}%</span>
        <span className="w-10 md:w-20 h-1.5 bg-border-subtle rounded-full overflow-hidden">
          <span
            className="block h-full rounded-full transition-all duration-300"
            style={{ width: `${pct}%`, backgroundColor: meterColor(pct) }}
          />
        </span>
      </button>
      {open && (
        <ContextPopover
          id={POPOVER_ID}
          used={used}
          exact={exact}
          max={max}
          context={loaded?.context ?? null}
          loadFailed={loadFailed}
          usage={usage}
          backend={backend}
          busy={busy}
          sessionCostUsd={sessionCostUsd}
          compact={compact}
          onCompact={() => setCompact({ step: 'confirm' })}
          onConfirm={() => void runCompact()}
          onCancel={() => setCompact({ step: 'idle' })}
        />
      )}
    </div>
  );
}
