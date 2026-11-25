import { describe, it, expect } from 'vitest';
import { parseServerTime } from './serverTime';

// Expected values are absolute instants, so these hold in any local zone.
// The bug only shows where the local zone is not UTC: run with
// TZ=Australia/Sydney to see the naive cases fail on the old code.
const utc = (y: number, mo: number, d: number, h: number, mi: number, s = 0, ms = 0) =>
  Date.UTC(y, mo - 1, d, h, mi, s, ms);

describe('parseServerTime', () => {
  it('reads a naive SQLite timestamp as UTC', () => {
    expect(parseServerTime('2026-11-25 02:59:09').getTime()).toBe(utc(2026, 11, 25, 2, 59, 9));
  });

  it('reads a naive ISO timestamp as UTC', () => {
    expect(parseServerTime('2026-11-25T02:59:09').getTime()).toBe(utc(2026, 11, 25, 2, 59, 9));
  });

  it('reads fractional seconds without a zone as UTC', () => {
    expect(parseServerTime('2026-11-25 02:59:09.123456').getTime()).toBe(utc(2026, 11, 25, 2, 59, 9, 123));
  });

  it('keeps a timestamp that already has a zone', () => {
    expect(parseServerTime('2026-11-25T02:59:09Z').getTime()).toBe(utc(2026, 11, 25, 2, 59, 9));
    expect(parseServerTime('2026-11-25T13:59:09+11:00').getTime()).toBe(utc(2026, 11, 25, 2, 59, 9));
    expect(parseServerTime('2026-11-25T02:59:09.123456-0000').getTime()).toBe(utc(2026, 11, 25, 2, 59, 9, 123));
  });

  it('passes a date alone through as new Date reads it', () => {
    expect(parseServerTime('2026-11-25').getTime()).toBe(new Date('2026-11-25').getTime());
  });

  it('gives an invalid date for a missing or empty value', () => {
    expect(Number.isNaN(parseServerTime(undefined).getTime())).toBe(true);
    expect(Number.isNaN(parseServerTime(null).getTime())).toBe(true);
    expect(Number.isNaN(parseServerTime('').getTime())).toBe(true);
  });
});
