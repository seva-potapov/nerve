/**
 * A server timestamp as a Date.
 *
 * SQLite's CURRENT_TIMESTAMP writes UTC as "2026-11-25 02:59:09", with no zone
 * marker. JavaScript reads a date-time with no zone as local time, so such
 * values showed up to 11 hours early in Australia/Sydney. Here they are read as
 * UTC. A value that already has a zone ("Z", "+11:00") and a bare date
 * ("2026-11-25") go to Date unchanged.
 */

/** A clock time, as in "02:59" of "2026-11-25 02:59:09". */
const CLOCK = /\d:\d{2}/;
/** A zone at the end: "Z", "+11:00", "-0500", "+11". */
const ZONE = /(?:Z|[+-]\d{2}(?::?\d{2})?)$/i;

export function parseServerTime(value: string | null | undefined): Date {
  if (!value) return new Date(Number.NaN);
  const text = value.trim();
  if (CLOCK.test(text) && !ZONE.test(text)) {
    return new Date(`${text.replace(' ', 'T')}Z`);
  }
  return new Date(text);
}

/**
 * The same instant as the browser's local wall clock, "YYYY-MM-DDTHH:mm:ss"
 * with no zone, the shape of memU's happened_at. "" for a missing value; a value
 * that does not parse comes back unchanged.
 */
export function localWallClock(value: string | null | undefined): string {
  if (!value) return '';
  const date = parseServerTime(value);
  if (Number.isNaN(date.getTime())) return value;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}
