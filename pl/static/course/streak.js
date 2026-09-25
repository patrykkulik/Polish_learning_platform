/* pl.streak, ported: the streak, with both conditions and a real timezone.
 *
 * It advances only on a day that both clears the review debt and meets the
 * daily goal, and the day is the learner's own, from local midnight. Freezes
 * cover missed days; `progress` returns a retention figure beside it that
 * survives the streak breaking.
 */

import { daysBetween, toDb } from "./clock.js";
import {
  DAILY_NEW_CAP,
  MASTERY_STABILITY_DAYS,
  endOfUserDay,
  settledToday,
  startOfUserDay,
  userToday,
} from "./composer.js";

/* pl.streak.MAX_FREEZES and DAYS_PER_FREEZE */
export const MAX_FREEZES = 2;
export const DAYS_PER_FREEZE = 10;

/* pl.streak._streak: the learner's row, created on first use. A GET that
 * creates it rolls it back with the rest of the request. */
function streakRow(db, userId) {
  let row = db.get("streak", userId);
  if (row === null) {
    row = {
      user_id: userId,
      current: 0,
      longest: 0,
      freezes: MAX_FREEZES,
      last_completed_on: null,
      absence_settled_on: null,
    };
    db.insert("streak", row);
  }
  return row;
}

/* pl.streak.debt_remaining: cards still due before the learner's next midnight
 * that have not had their turn today. */
export function debtRemaining(db, userId, settings) {
  const settled = settledToday(db, userId, settings);
  const due = db.scalars("SELECT card.id FROM card WHERE card.user_id = ? AND card.due_at <= ?", [
    userId,
    toDb(endOfUserDay(settings)),
  ]);
  return due.filter((id) => !settled.has(id)).length;
}

/* pl.streak.items_completed_today: distinct items answered today. */
export function itemsCompletedToday(db, userId, settings) {
  return db.scalar(
    "SELECT count(DISTINCT attempt.item_id) FROM attempt " +
      "WHERE attempt.user_id = ? AND attempt.created_at >= ? AND attempt.created_at < ?",
    [userId, toDb(startOfUserDay(settings)), toDb(endOfUserDay(settings))]
  );
}

/* pl.streak._apply_absence: spend freezes across missed days, then break the
 * streak — settled at most once a day. */
function applyAbsence(db, row, today) {
  if (row.last_completed_on === null) return;
  if (row.absence_settled_on === today) return;
  const settledThrough = row.absence_settled_on;
  row.absence_settled_on = today;
  db.update("streak", row.user_id, { absence_settled_on: today });

  const missed = daysBetween(today, row.last_completed_on) - 1;
  if (missed <= 0) return;

  // Days an earlier visit during this same absence already paid for.
  let covered = 0;
  if (settledThrough !== null && settledThrough > row.last_completed_on) {
    covered = daysBetween(settledThrough, row.last_completed_on) - 1;
  }
  const outstanding = missed - Math.max(0, covered);
  if (outstanding <= 0) return;

  const spend = Math.min(outstanding, row.freezes);
  row.freezes -= spend;
  if (outstanding > spend) row.current = 0;
  db.update("streak", row.user_id, { freezes: row.freezes, current: row.current });
}

/* pl.streak.record_activity: advance the streak if, and only if, both
 * conditions hold today. Commits, unless today already counted. */
export function recordActivity(db, userId, settings) {
  const row = streakRow(db, userId);
  const today = userToday(settings);

  if (row.last_completed_on === today) return row;

  applyAbsence(db, row, today);

  const goal = "daily_goal_items" in settings ? settings.daily_goal_items : DAILY_NEW_CAP;
  const goalMet = itemsCompletedToday(db, userId, settings) >= goal;
  const debtClear = debtRemaining(db, userId, settings) === 0;

  if (goalMet && debtClear) {
    row.current += 1;
    row.longest = Math.max(row.longest, row.current);
    row.last_completed_on = today;
    if (row.current % DAYS_PER_FREEZE === 0) row.freezes = Math.min(MAX_FREEZES, row.freezes + 1);
    db.update("streak", userId, {
      current: row.current,
      longest: row.longest,
      last_completed_on: row.last_completed_on,
      freezes: row.freezes,
    });
  }

  db.commit();
  return row;
}

/* pl.streak.progress: the streak, beside a metric that survives it breaking. */
export function progress(db, userId, settings) {
  const row = streakRow(db, userId);
  const retained = db.scalar("SELECT count(*) FROM card WHERE card.user_id = ? AND card.stability_max >= ?", [
    userId,
    MASTERY_STABILITY_DAYS,
  ]);
  const total = db.scalar("SELECT count(*) FROM card WHERE card.user_id = ?", [userId]);
  return {
    streak: row.current,
    longest: row.longest,
    freezes: row.freezes,
    debt: debtRemaining(db, userId, settings),
    retained,
    tracked: total,
  };
}
