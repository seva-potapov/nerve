import { describe, it, expect } from 'vitest';
import { localWallClock } from './serverTime';

// The expected text is built from the local getters, so the first check holds
// in any zone. Run with TZ=Australia/Sydney to see a UTC-as-local mistake here
// (it would read 02:59 where the local clock says 13:59).
const pad = (n: number) => String(n).padStart(2, '0');

describe('localWallClock', () => {
  it('gives the local wall clock of a naive UTC timestamp', () => {
    const local = new Date(Date.UTC(2026, 10, 25, 2, 59, 9));
    const expected = `${local.getFullYear()}-${pad(local.getMonth() + 1)}-${pad(local.getDate())}`
      + `T${pad(local.getHours())}:${pad(local.getMinutes())}:${pad(local.getSeconds())}`;
    expect(localWallClock('2026-11-25 02:59:09')).toBe(expected);
  });

  it('reads back as the same instant, to the second', () => {
    const wall = localWallClock('2026-11-25 02:59:09.123456');
    expect(new Date(wall).getTime()).toBe(Date.UTC(2026, 10, 25, 2, 59, 9));
  });

  it('is empty for a missing value and keeps one that does not parse', () => {
    expect(localWallClock(undefined)).toBe('');
    expect(localWallClock('')).toBe('');
    expect(localWallClock('not a time')).toBe('not a time');
  });
});
