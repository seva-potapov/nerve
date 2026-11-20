import { useEffect } from 'react';
import { useChatStore } from '../../stores/chatStore';
import { useEscStopStore } from '../../stores/escStopStore';

/**
 * "Press Esc again to stop", while the first of the two Escape presses that
 * stop a turn is armed. The turn ending disarms it, so an Escape in the next
 * turn starts over.
 */
export function EscStopHint() {
  const armed = useEscStopStore((s) => s.armedUntil > 0);
  const isStreaming = useChatStore((s) => s.isStreaming);

  useEffect(() => {
    if (!isStreaming) useEscStopStore.getState().disarm();
  }, [isStreaming]);

  if (!armed || !isStreaming) return null;
  return (
    <div role="status" className="pointer-events-none fixed inset-x-0 bottom-28 z-50 flex justify-center">
      <span className="rounded-lg border border-border bg-surface-raised px-3 py-1.5 text-sm text-text-secondary shadow-lg">
        Press <kbd className="rounded border border-border-subtle bg-surface px-1.5 py-0.5 font-mono text-xs">Esc</kbd> again to stop
      </span>
    </div>
  );
}
