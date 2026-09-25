/* Unit tests for fsrs.js and clock.js. tests/test_parity.py holds the scheduler
 * to fsrs 6.3.2 on scripted reviews; these pin the Python datetime and rounding
 * rules the port had to reproduce by hand.
 *
 *   node --test tests/js/
 */

import assert from "node:assert/strict";
import test from "node:test";

import { DAY, MINUTE, days, floorDiv, fromDb, fromisoformat, isoformat, toDb } from "../../pl/static/course/clock.js";
import { Card, Rating, Scheduler, State, roundHalfEven } from "../../pl/static/course/fsrs.js";

const T0 = fromisoformat("2026-03-01T22:00:00+00:00");

test("round() is Python's: halves go to the even neighbour", () => {
  const cases = [
    [0.5, 0],
    [1.5, 2],
    [2.5, 2],
    [3.5, 4],
    [2.4999999999999996, 2],
    [2.5000000000000004, 3],
    [-1.5, -2],
  ];
  for (const [x, expected] of cases) assert.equal(roundHalfEven(x), expected, String(x));
});

test("instants print exactly as Python prints them", () => {
  const t = fromisoformat("2026-03-01T22:00:00.000123+00:00");
  assert.equal(isoformat(t), "2026-03-01T22:00:00.000123+00:00");
  assert.equal(isoformat(T0), "2026-03-01T22:00:00+00:00");
  assert.equal(toDb(t), "2026-03-01 22:00:00.000123");
  assert.equal(toDb(T0), "2026-03-01 22:00:00.000000");
  assert.equal(fromDb("2026-03-01 22:00:00.000123"), t);
  assert.equal(fromisoformat("2026-03-02T00:00:00+02:00"), T0);
  assert.throws(() => fromisoformat("1 March 2026"));
});

test("timedelta.days rounds down, as Python's does", () => {
  assert.equal(days(DAY - 1), 0);
  assert.equal(days(DAY), 1);
  assert.equal(days(-1), -1);
  assert.equal(floorDiv(100_000 * DAY - 1, DAY), 99_999);
});

test("a new card rated Good takes the second learning step", () => {
  const scheduler = new Scheduler({ enableFuzzing: false });
  const card = scheduler.reviewCard(new Card({ due: T0 }), Rating.Good, T0);
  assert.equal(card.state, State.Learning);
  assert.equal(card.step, 1);
  assert.equal(card.due - T0, 10 * MINUTE);
  assert.equal(card.lastReview, T0);
  assert.deepEqual(Card.fromDict(card.toDict()), card);
});

test("reviewing leaves the card it was given untouched", () => {
  const scheduler = new Scheduler({ enableFuzzing: false });
  const card = new Card({ due: T0 });
  scheduler.reviewCard(card, Rating.Easy, T0);
  assert.equal(card.state, State.Learning);
  assert.equal(card.stability, null);
});

test("retrievability is zero before the first review, and one on its day", () => {
  const scheduler = new Scheduler({ enableFuzzing: false });
  const fresh = new Card({ due: T0 });
  assert.equal(scheduler.getCardRetrievability(fresh, T0), 0);
  const reviewed = scheduler.reviewCard(fresh, Rating.Easy, T0);
  assert.equal(scheduler.getCardRetrievability(reviewed, T0 + DAY - 1), 1);
  assert.ok(scheduler.getCardRetrievability(reviewed, T0 + DAY) < 1);
});
