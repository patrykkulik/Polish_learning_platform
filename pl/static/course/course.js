/* pl.course, ported: what the course does for its learner, one function per
 * route the pages call, taking plain arguments and returning plain data.
 * phone.js answers the page's requests with these, as pl.device answers them
 * with pl.course.
 *
 * Each opens a database session, and commits only where the Python commits;
 * closing it rolls back the rest.
 */

import { classify, classifySentence } from "./classify.js";
import { DAY, fromDb, localDate, now, toDb } from "./clock.js";
import * as composer from "./composer.js";
import * as teaching from "./concepts.js";
import * as database from "./db.js";
import { AUDIBLE, AssertionError, ExpectedSlot, Form, MULTI_SLOT } from "./domain.js";
import { explain } from "./explain.js";
import * as schedule from "./schedule.js";
import * as streaks from "./streak.js";
import { parse } from "./tags.js";

/* pl.course.Refused: a request the course declines, with its HTTP status. */
export class Refused extends Error {
  constructor(status, detail) {
    super(detail);
    this.name = "Refused";
    this.status = status;
    this.detail = detail;
  }
}

/* Python's truthiness, for a JSON column. */
const truthy = (value) =>
  Array.isArray(value) ? value.length > 0 : value !== null && typeof value === "object" ? Object.keys(value).length > 0 : Boolean(value);

/* pl.content.ingest.ensure_user: the one learner. */
export function ensureUser(db, email = null) {
  let user = db.first("app_user", "LIMIT 1");
  if (user === null) {
    user = {
      email,
      created_at: now(),
      settings_json: { tz: "Europe/London", daily_goal_items: composer.DAILY_NEW_CAP },
    };
    user.id = db.insert("app_user", user);
    db.insert("streak", {
      user_id: user.id,
      current: 0,
      longest: 0,
      freezes: 2,
      last_completed_on: null,
      absence_settled_on: null,
    });
  }
  return user;
}

/* pl.course._user */
function learner(db) {
  let user = db.first("app_user", "LIMIT 1");
  if (user === null) {
    user = ensureUser(db);
    db.commit();
  }
  return user;
}

/* pl.course.expected_slot: the grader's input, from stored rows. */
export function expectedSlot(db, item) {
  const target = item.target_form_id === null ? null : db.get("form", item.target_form_id);
  if (target === null) throw new Refused(404, "item has no target form");
  const lexeme = db.get("lexeme", target.lexeme_id);
  const paradigm = db
    .select("form", "WHERE form.lexeme_id = ? ORDER BY form.surface, form.id", [lexeme.id])
    .map((row) => new Form(row.surface, lexeme.lemma, parse(row.morph_tag)));
  const expected = paradigm.find((f) => f.surface === target.surface && f.tag.raw === target.morph_tag);
  const partner = lexeme.aspect_partner_id !== null ? db.get("lexeme", lexeme.aspect_partner_id) : null;
  return new ExpectedSlot(expected, paradigm, partner ? partner.lemma : null);
}

/* pl.course.grade_item: one submission, whichever kind of item it is. */
export function gradeItem(db, item, submitted) {
  if (!MULTI_SLOT.has(item.exercise_type)) return classify(expectedSlot(db, item), submitted);
  const slots = db.select("item_slot", "WHERE item_slot.item_id = ? ORDER BY item_slot.slot_index", [item.id]);
  const targetIndex = slots.findIndex((s) => s.target_form_id !== null);
  return classifySentence(
    slots.map((s) => s.expected_surface),
    targetIndex,
    expectedSlot(db, item),
    submitted
  );
}

/* pl.course._serialise: what the page may see before it answers — never the
 * answer, nor which option is right. */
export function serialise(db, item) {
  const node = db.get("node", item.node_id);
  return {
    id: item.id,
    exercise_type: item.exercise_type,
    prompt: item.prompt,
    gloss: item.gloss,
    options: item.options_json,
    has_audio: AUDIBLE.has(item.exercise_type) && composer.audioAvailable(),
    options_audio: truthy(item.options_json) && composer.audioAvailable(),
    node: { key: node.key, title: node.title, type: node.type },
  };
}

/* pl.course.speech_text: what a play button says — a listening item's
 * sentence, or one option by its position — for the phone's voice to speak. */
export function speechText(itemId, index = null) {
  if (!composer.audioAvailable()) throw new Refused(503, "no speech synthesiser on this machine");
  const db = database.session();
  try {
    const item = db.get("item", itemId);
    if (index === null) {
      if (item === null || !AUDIBLE.has(item.exercise_type)) throw new Refused(404, "no audio for this item");
      return { text: item.expected_answer };
    }
    const options = item !== null ? item.options_json || [] : [];
    if (!(0 <= index && index < options.length)) throw new Refused(404, "no such option");
    return { text: options[index] };
  } finally {
    db.close();
  }
}

/* pl.course.get_session: the day's session, or an extra round. `order`
 * shuffles what is served, in place. */
export function getSession(limit = 20, extra = false, { order }) {
  const db = database.session();
  try {
    const user = learner(db);
    const [items, stats] = composer.buildSession(db, user.id, user.settings_json, limit, extra);
    const served = [...items];
    order(served);
    return {
      items: served.map((item) => serialise(db, item)),
      stats,
      lesson: teaching.lessonFor(db, user.id),
      progress: streaks.progress(db, user.id, user.settings_json),
    };
  } finally {
    db.close();
  }
}

/* pl.course.submit: grade one answer, schedule what it scores, and commit. */
export function submit(itemId, answer, latencyMs = null) {
  const db = database.session();
  try {
    const user = learner(db);
    const item = db.get("item", itemId);
    if (item === null) throw new Refused(404, "no such item");

    const attempt = { user_id: user.id, item_id: item.id, submitted: answer, latency_ms: latencyMs, created_at: now() };
    attempt.id = db.insert("attempt", attempt);

    const diagnosis = gradeItem(db, item, answer);
    const applied = schedule.applyDiagnosis(
      db,
      user.id,
      item,
      diagnosis,
      attempt.id,
      composer.startOfUserDay(user.settings_json)
    );
    db.commit();

    // Populations the routing table would have scored but this answer did not
    // move, split by why.
    const dayStart = composer.startOfUserDay(user.settings_json);
    const rated = schedule.ratingsFor(item, diagnosis.errorClass);
    const countedEarlier = [];
    const cooledDown = [];
    const cards = [...schedule.cardsForItem(db, user.id, item)].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    for (const [population, card] of cards) {
      if (!rated.has(population) || applied.has(population)) continue;
      if (schedule.cardAdvancedSince(db, card, dayStart)) countedEarlier.push(population);
      else cooledDown.push(population);
    }

    return {
      correct: diagnosis.isCorrect,
      error_class: diagnosis.errorClass,
      message: explain(diagnosis),
      expected: item.expected_answer,
      scored: Object.fromEntries(applied),
      counted_earlier: countedEarlier,
      cooled_down: cooledDown,
      progress: streaks.progress(db, user.id, user.settings_json),
    };
  } finally {
    db.close();
  }
}

/* pl.course's milestones. */
export const RETAINED_MILESTONES = [10, 25, 50, 100, 250, 500];
export const VOCABULARY_MILESTONES = [10, 25, 50, 100, 250];
export const STREAK_MILESTONES = [3, 7, 14, 30, 60, 100];

/* pl.course._standing */
function standing(value, thresholds) {
  return {
    value,
    reached: Math.max(0, ...thresholds.filter((t) => value >= t)),
    next: thresholds.find((t) => value < t) ?? null,
  };
}

/* pl.course._milestones_reached: where the learner stands against the next
 * round number, true every time it is rendered. */
function milestonesReached(db, userId, streakCurrent) {
  const retained = db.scalar("SELECT count(*) FROM card WHERE card.user_id = ? AND card.stability_max >= ?", [
    userId,
    composer.MASTERY_STABILITY_DAYS,
  ]);
  return {
    retained: standing(retained, RETAINED_MILESTONES),
    vocabulary: standing(composer.knownLexemes(db, userId).size, VOCABULARY_MILESTONES),
    streak: standing(streakCurrent, STREAK_MILESTONES),
  };
}

/* pl.course.complete: end of session — unlocks, then the streak. */
export function complete() {
  const db = database.session();
  try {
    const user = learner(db);
    const newly = composer.evaluateUnlocks(db, user.id);
    const row = streaks.recordActivity(db, user.id, user.settings_json);
    return {
      unlocked: newly
        .map((key) => db.first("node", 'WHERE node."key" = ?', [key]))
        .filter((node) => node !== null)
        .map((node) => ({ key: node.key, title: node.title, type: node.type, explanation: node.explanation_md })),
      streak: row.current,
      milestones: milestonesReached(db, user.id, row.current),
      progress: streaks.progress(db, user.id, user.settings_json),
    };
  } finally {
    db.close();
  }
}

/* pl.course._retention_curve: the share of reviews recalled, by the learner's
 * local day. `Again` is the only failure. */
function retentionCurve(db, userId, settings, days = 30) {
  const zone = settings.tz ?? "UTC";
  const since = composer.startOfUserDay(settings) - (days - 1) * DAY;
  const rows = db.values(
    "SELECT attempt.created_at, review.rating FROM attempt JOIN review ON review.attempt_id = attempt.id " +
      "WHERE attempt.user_id = ? AND attempt.created_at >= ?",
    [userId, toDb(since)]
  );
  const buckets = new Map();
  for (const [createdAt, rating] of rows) {
    const day = localDate(fromDb(createdAt), zone);
    if (!buckets.has(day)) buckets.set(day, [0, 0]);
    const tally = buckets.get(day);
    tally[0] += 1;
    if (rating > 1) tally[1] += 1;
  }
  return [...buckets.keys()].sort().map((day) => ({ day, reviews: buckets.get(day)[0], recalled: buckets.get(day)[1] }));
}

/* pl.course.list_concepts */
export function listConcepts() {
  const db = database.session();
  try {
    return { concepts: teaching.index(db, learner(db).id) };
  } finally {
    db.close();
  }
}

/* pl.course.get_concept: a concept reached, in full; one not reached, by name. */
export function getConcept(key) {
  const db = database.session();
  try {
    const user = learner(db);
    const concept = teaching.byKey(key);
    if (concept === null) throw new Refused(404, "no such concept");
    const state = { key: concept.key, title: concept.title, read: teaching.readKeys(db, user.id).has(key) };
    if (!teaching.isOpen(db, user.id, concept)) return { ...state, open: false };
    return { ...state, open: true, ...teaching.render(db, concept) };
  } finally {
    db.close();
  }
}

/* pl.course.read_concept: acknowledge a lesson, and commit. */
export function readConcept(key) {
  const db = database.session();
  try {
    const user = learner(db);
    const concept = teaching.byKey(key);
    if (concept === null) throw new Refused(404, "no such concept");
    if (!teaching.isOpen(db, user.id, concept)) throw new Refused(403, "this concept has not opened yet");
    teaching.markRead(db, user.id, key);
    return { key, read: true };
  } finally {
    db.close();
  }
}

/* pl.course._mastered_for_display: `is_mastered`, but a gate nobody can
 * evaluate reads "not yet" instead of failing the page. */
function masteredForDisplay(db, userId, node, detail) {
  if (detail.strata <= 0 && node.type !== "function") return false;
  try {
    return composer.isMastered(db, userId, node);
  } catch (error) {
    if (error instanceof AssertionError) return false;
    throw error;
  }
}

/* Python's `round(x, 4)`. `toFixed` rounds an exact tie up, and Python to the
 * even neighbour; at four places the ties are exactly the odd multiples of
 * 1/32, which a node of 32 strata reaches. */
export function round4(x) {
  const scaled = x * 32;
  if (Number.isInteger(scaled) && scaled % 2 !== 0) {
    const lower = Math.floor(x * 10_000);
    return (lower % 2 === 0 ? lower : lower + 1) / 10_000;
  }
  return Number(x.toFixed(4));
}

/* pl.course.graph: the skill graph with per-node mastery. `mastered` is the
 * gate's own answer, which never falls; `mastery` decays between sessions. */
export function graph() {
  const db = database.session();
  try {
    const user = learner(db);
    const nodes = [];
    for (const node of db.select("node", "ORDER BY node.id")) {
      const detail = composer.nodeMastery(db, user.id, node);
      nodes.push({
        key: node.key,
        title: node.title,
        type: node.type,
        unlocked: composer.isUnlocked(db, user.id, node),
        mastered: masteredForDisplay(db, user.id, node, detail),
        mastery: round4(detail.retention),
        strata: detail.strata,
        started: detail.started,
        mastered_strata: detail.mastered,
        explanation: node.explanation_md,
      });
    }

    const met = composer.knownLexemes(db, user.id);
    const figures = streaks.progress(db, user.id, user.settings_json);
    return {
      nodes,
      vocabulary: { met: met.size, total: db.scalar("SELECT count(*) FROM lexeme") },
      retention_curve: retentionCurve(db, user.id, user.settings_json),
      milestones: milestonesReached(db, user.id, figures.streak),
      progress: figures,
    };
  } finally {
    db.close();
  }
}
