/* Datetimes as the Python writes them, and the clock.
 *
 * An instant is an integer count of microseconds since the Unix epoch, in UTC.
 * Python's datetimes carry microseconds, and every stored timestamp and FSRS
 * state has them, but a JavaScript Date holds milliseconds. An integer holds
 * microseconds exactly until the year 2255.
 *
 * Durations are microseconds too, so `timedelta` arithmetic is exact integer
 * arithmetic.
 */

export const SECOND = 1_000_000;
export const MINUTE = 60 * SECOND;
export const HOUR = 60 * MINUTE;
export const DAY = 24 * HOUR;

/* Floor division of integers. The float quotient can round across an integer
 * when the divisor is large, so it is corrected exactly. */
export function floorDiv(a, b) {
  let q = Math.floor(a / b);
  if (q * b > a) q -= 1;
  else if ((q + 1) * b <= a) q += 1;
  return q;
}

/* Python's `timedelta.days` of a duration: whole days, rounded down. */
export function days(duration) {
  return floorDiv(duration, DAY);
}

const pad = (n, width = 2) => String(n).padStart(width, "0");

function civil(instant, separator, fraction) {
  const ms = floorDiv(instant, 1000);
  const date = new Date(ms);
  const micros = date.getUTCMilliseconds() * 1000 + (instant - ms * 1000);
  const text =
    `${pad(date.getUTCFullYear(), 4)}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    `${separator}${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
  return fraction === "always" || micros ? `${text}.${pad(micros, 6)}` : text;
}

/* `datetime.isoformat()` of an aware UTC datetime, as `fsrs` stores `due` and
 * `last_review`: microseconds only when there are some. */
export function isoformat(instant) {
  return `${civil(instant, "T", "when-nonzero")}+00:00`;
}

const ISO = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:([+-])(\d{2}):(\d{2})|Z)?$/;

/* `datetime.fromisoformat` for the forms the Python and this port write: with
 * or without a fraction, and with a UTC offset or none, which reads as UTC. */
export function fromisoformat(text) {
  const m = ISO.exec(text);
  if (m === null) throw new Error(`Invalid isoformat string: '${text}'`);
  const [, year, month, day, hour, minute, second, fraction = "", sign, offsetHours, offsetMinutes] = m;
  let instant =
    Date.UTC(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second)) * 1000 +
    Number(fraction.padEnd(6, "0"));
  if (sign !== undefined) {
    const offset = (Number(offsetHours) * 60 + Number(offsetMinutes)) * MINUTE;
    instant -= sign === "+" ? offset : -offset;
  }
  return instant;
}

/* A DateTime column's text, as SQLAlchemy writes it to SQLite: UTC with no
 * zone, and always six fraction digits. SQL compares these as text, so the
 * format is exact. */
export function toDb(instant) {
  return civil(instant, " ", "always");
}

/* A DateTime column's text back to an instant. */
export function fromDb(text) {
  return fromisoformat(text);
}

/* The clock every module reads, defaulting to the real one. The parity tests
 * replace it, as `journey_sim` replaces the Python's `datetime`. */
let reading = null;

export function now() {
  return reading === null ? Date.now() * 1000 : reading();
}

export function useClock(read) {
  reading = read;
}

/* ------------------------------------------------------------ time zones
 *
 * The learner's day is their own, from local midnight to local midnight, as
 * `zoneinfo` computes it. Intl knows each zone's wall clock but not its
 * transitions, so a midnight near a clock change is found by search. */

const formatters = new Map();

function formatter(zone) {
  let f = formatters.get(zone);
  if (f === undefined) {
    f = new Intl.DateTimeFormat("en-US", {
      timeZone: zone,
      hourCycle: "h23",
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "numeric",
      second: "numeric",
    });
    formatters.set(zone, f);
  }
  return f;
}

/* Whether Intl knows `zone`, as `ZoneInfo(zone)` would. */
export function knownZone(zone) {
  try {
    formatter(zone);
    return true;
  } catch {
    return false;
  }
}

/* The wall-clock reading of `instant` in `zone`, as microseconds of a clock
 * that runs in UTC. */
function wallClock(instant, zone) {
  const seconds = floorDiv(instant, SECOND);
  const part = {};
  for (const { type, value } of formatter(zone).formatToParts(new Date(seconds * 1000))) {
    part[type] = Number(value);
  }
  const hour = part.hour === 24 ? 0 : part.hour;
  const wall = Date.UTC(part.year, part.month - 1, part.day, hour, part.minute, part.second);
  return wall * 1000 + (instant - seconds * SECOND);
}

function offsetAt(instant, zone) {
  return wallClock(instant, zone) - instant;
}

const dateText = (wall) => new Date(floorDiv(wall, 1000)).toISOString().slice(0, 10);

/* `instant.astimezone(zone).date()`, as "YYYY-MM-DD". */
export function localDate(instant, zone) {
  return dateText(wallClock(instant, zone));
}

/* The instant a zone's clocks change between `from` and `to`, to the second,
 * given the offset in force at `from`. */
function transition(from, to, zone, before) {
  let lo = floorDiv(from, SECOND);
  let hi = floorDiv(to, SECOND);
  while (hi - lo > 1) {
    const mid = Math.floor((lo + hi) / 2);
    if (offsetAt(mid * SECOND, zone) === before) lo = mid;
    else hi = mid;
  }
  return hi * SECOND;
}

/* `datetime.combine(date, time.min).replace(tzinfo=zone).astimezone(UTC)`.
 *
 * `zoneinfo` places a transition on the wall clock at its instant plus the
 * larger of the two offsets, and a local time before that takes the earlier
 * offset. So a midnight the clocks skip (America/Havana in March) maps to the
 * instant the day starts, and one they repeat (Havana in November) to its first
 * occurrence. */
export function localMidnight(date, zone) {
  const [year, month, day] = date.split("-").map(Number);
  const wall = Date.UTC(year, month - 1, day) * 1000;
  const before = offsetAt(wall - 2 * DAY, zone);
  const after = offsetAt(wall + 2 * DAY, zone);
  if (before === after) return wall - before;
  const change = transition(wall - 2 * DAY, wall + 2 * DAY, zone, before);
  return wall - (wall < change + Math.max(before, after) ? before : after);
}

/* `date + timedelta(days=n)`, on "YYYY-MM-DD". */
export function addDays(date, n) {
  const [year, month, day] = date.split("-").map(Number);
  return dateText(Date.UTC(year, month - 1, day + n) * 1000);
}

/* `(later - earlier).days`, on "YYYY-MM-DD". */
export function daysBetween(later, earlier) {
  const at = (date) => {
    const [year, month, day] = date.split("-").map(Number);
    return Date.UTC(year, month - 1, day);
  };
  return Math.round((at(later) - at(earlier)) / 86_400_000);
}
