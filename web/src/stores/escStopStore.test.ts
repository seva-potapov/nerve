import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DEFAULT_ESC_STOP, ESC_STOP_KEY, readEscStopPrefs, useEscStopStore } from './escStopStore';

const reset = () => useEscStopStore.setState({ ...DEFAULT_ESC_STOP, armedUntil: 0 });

describe('escStopStore', () => {
  beforeEach(() => { vi.useFakeTimers(); localStorage.clear(); reset(); });
  afterEach(() => { useEscStopStore.getState().disarm(); vi.useRealTimers(); });

  it('defaults to double press within 1000 ms', () => {
    expect(readEscStopPrefs()).toEqual({ double: true, windowMs: 1000 });
  });

  it('one press only arms', () => {
    expect(useEscStopStore.getState().press(10_000)).toBe('armed');
    expect(useEscStopStore.getState().armedUntil).toBe(11_000);
  });

  it('a second press inside the window stops and disarms', () => {
    const { press } = useEscStopStore.getState();
    press(10_000);
    expect(press(10_999)).toBe('stop');
    expect(useEscStopStore.getState().armedUntil).toBe(0);
    // The stop used both presses: the next one arms again.
    expect(press(11_100)).toBe('armed');
  });

  it('a second press after the window arms again', () => {
    const { press } = useEscStopStore.getState();
    press(10_000);
    expect(press(11_001)).toBe('armed');
  });

  it('the timer clears the hint when the window ends', () => {
    useEscStopStore.getState().press();
    expect(useEscStopStore.getState().armedUntil).toBeGreaterThan(0);
    vi.advanceTimersByTime(999);
    expect(useEscStopStore.getState().armedUntil).toBeGreaterThan(0);
    vi.advanceTimersByTime(1);
    expect(useEscStopStore.getState().armedUntil).toBe(0);
  });

  it('single mode stops on the first press and is saved', () => {
    useEscStopStore.getState().setDouble(false);
    expect(useEscStopStore.getState().press(10_000)).toBe('stop');
    expect(JSON.parse(localStorage.getItem(ESC_STOP_KEY)!)).toEqual({ double: false, windowMs: 1000 });
  });

  it('uses and clamps a custom window', () => {
    useEscStopStore.getState().setWindowMs(2500);
    const { press } = useEscStopStore.getState();
    press(10_000);
    expect(press(12_400)).toBe('stop');
    useEscStopStore.getState().setWindowMs(10);
    expect(useEscStopStore.getState().windowMs).toBe(200);
    useEscStopStore.getState().setWindowMs(99_999);
    expect(useEscStopStore.getState().windowMs).toBe(5000);
  });

  it('reads damaged or partial storage as defaults', () => {
    localStorage.setItem(ESC_STOP_KEY, '{bad');
    expect(readEscStopPrefs()).toEqual(DEFAULT_ESC_STOP);
    localStorage.setItem(ESC_STOP_KEY, JSON.stringify({ windowMs: 1500 }));
    expect(readEscStopPrefs()).toEqual({ double: true, windowMs: 1500 });
  });

  it('follows a change made in another tab', () => {
    localStorage.setItem(ESC_STOP_KEY, JSON.stringify({ double: false, windowMs: 700 }));
    window.dispatchEvent(new StorageEvent('storage', { key: ESC_STOP_KEY }));
    expect(useEscStopStore.getState()).toMatchObject({ double: false, windowMs: 700 });
  });
});
