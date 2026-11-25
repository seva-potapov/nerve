import { describe, it, expect } from 'vitest';
import { formatMessageTime } from './messageTime';

// The expected string is built from an absolute instant with the same options,
// so the check is zone-independent. Run with TZ=Australia/Sydney to see the
// naive case fail on the old code.
const label = (ms: number) =>
  new Date(ms).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });

describe('formatMessageTime', () => {
  it('shows a naive UTC timestamp at the instant it names', () => {
    expect(formatMessageTime('2026-11-25 02:59:09')).toBe(label(Date.UTC(2026, 10, 25, 2, 59, 9)));
  });

  it('shows a naive and a zoned copy of the same instant identically', () => {
    expect(formatMessageTime('2026-11-25 02:59:09')).toBe(formatMessageTime('2026-11-25T02:59:09Z'));
  });

  it('returns an empty string for a missing or invalid value', () => {
    expect(formatMessageTime(undefined)).toBe('');
    expect(formatMessageTime('not a time')).toBe('');
  });
});
