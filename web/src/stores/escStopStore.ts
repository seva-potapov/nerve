import { create } from 'zustand';

/** How Escape stops a running turn. Kept per browser, not on the server. */
export interface EscStopPrefs {
  /** A second Escape within `windowMs` stops the turn; the first only warns. */
  double: boolean;
  windowMs: number;
}

export const ESC_STOP_KEY = 'nerve-esc-stop';
export const DEFAULT_ESC_STOP: EscStopPrefs = { double: true, windowMs: 1000 };
export const ESC_WINDOW_MIN_MS = 200;
export const ESC_WINDOW_MAX_MS = 5000;

export function clampWindow(ms: number): number {
  if (!Number.isFinite(ms)) return DEFAULT_ESC_STOP.windowMs;
  return Math.min(ESC_WINDOW_MAX_MS, Math.max(ESC_WINDOW_MIN_MS, Math.round(ms)));
}

export function readEscStopPrefs(): EscStopPrefs {
  let saved: Partial<Record<keyof EscStopPrefs, unknown>> = {};
  try {
    saved = JSON.parse(localStorage.getItem(ESC_STOP_KEY) ?? '{}') ?? {};
  } catch { /* a damaged value reads as the defaults */ }
  return {
    double: saved.double !== false,
    windowMs: typeof saved.windowMs === 'number' ? clampWindow(saved.windowMs) : DEFAULT_ESC_STOP.windowMs,
  };
}

export interface EscStopState extends EscStopPrefs {
  /** The first Escape was pressed: until this time (ms epoch) a second one stops. 0 = not armed. */
  armedUntil: number;
  setDouble: (double: boolean) => void;
  setWindowMs: (ms: number) => void;
  /**
   * One Escape aimed at the running turn. Returns `stop` when the turn
   * should stop now, `armed` when this press only armed the second one.
   */
  press: (now?: number) => 'stop' | 'armed';
  disarm: () => void;
}

let disarmTimer: ReturnType<typeof setTimeout> | null = null;
function clearTimer() {
  if (disarmTimer !== null) clearTimeout(disarmTimer);
  disarmTimer = null;
}

/**
 * Escape-to-stop. By default it takes two presses within one second, so a
 * stray Escape (closing a popup that already closed, leaving a field) does
 * not stop the turn. Every tab of the browser shares the choice through
 * localStorage and its `storage` event.
 */
export const useEscStopStore = create<EscStopState>((set, get) => {
  const save = (patch: Partial<EscStopPrefs>) => {
    const { double, windowMs } = { ...get(), ...patch };
    localStorage.setItem(ESC_STOP_KEY, JSON.stringify({ double, windowMs }));
    set(patch);
  };
  window.addEventListener('storage', (e) => {
    if (e.key === ESC_STOP_KEY) set(readEscStopPrefs());
  });

  return {
    ...readEscStopPrefs(),
    armedUntil: 0,
    setDouble: (double) => {
      clearTimer();
      set({ armedUntil: 0 });
      save({ double });
    },
    setWindowMs: (ms) => save({ windowMs: clampWindow(ms) }),
    press: (now = Date.now()) => {
      const { double, windowMs, armedUntil } = get();
      if (!double || (armedUntil > 0 && now <= armedUntil)) {
        get().disarm();
        return 'stop';
      }
      clearTimer();
      set({ armedUntil: now + windowMs });
      disarmTimer = setTimeout(() => {
        disarmTimer = null;
        set({ armedUntil: 0 });
      }, windowMs);
      return 'armed';
    },
    disarm: () => {
      clearTimer();
      if (get().armedUntil !== 0) set({ armedUntil: 0 });
    },
  };
});
