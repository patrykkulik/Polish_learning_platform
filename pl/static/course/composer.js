/* pl.session, ported, under the name the Python imports it by: session
 * composition and the node unlock gate.
 *
 * The learner is served debt, then remediation, then new, and nothing new is
 * introduced while the backlog is deep. The segments are composed in a
 * different order — debt, new, remediation — because remediation has an
 * appetite for the whole session. The unlock gate reads `stability_max`, a
 * high-water mark, never current stability. The Python module's docstrings
 * carry the measurements behind every constant and rule here; this port
 * changes none of them.
 *
 * A referent — a card's (population, id) — is a string "population:id", so
 * that sets of them compare as the Python's sets of tuples do. Queries the
 * Python leaves unordered are ordered as SQLite returns them: by the index a
 * lookup walks, or else by primary key.
 */

import { DAY, addDays, fromDb, localDate, localMidnight, now, toDb } from "./clock.js";
import * as concepts from "./concepts.js";
import { AUDIBLE, AssertionError } from "./domain.js";
import * as schedule from "./schedule.js";
import { LEXICAL, MORPH, PATTERN, REFERENT, Rating, populationsFor } from "./schedule.js";

/* pl.session's constants. */
export const MASTERY_STABILITY_DAYS = 7.0;
export const MASTERY_FRACTION = 0.8;
export const MASTERY_ALLOWED_SHORTFALL = 1;
export const MASTERY_MIN_REVIEWS = 3;
export const MASTERY_MIN_SPAN_DAYS = 7;
export const DAILY_NEW_CAP = 10;
export const EXTRA_ROUND_NEW = 5;
export const DEBT_TOLERANCE = 5;

/* pl.audio.available, on a phone: whether a Polish voice can speak. Off until
 * the page says the phone has one. */
let voice = false;

export function useVoice(available) {
  voice = available;
}

export function audioAvailable() {
  return voice;
}

/* A referent, as a set member. */
export const referent = (population, id) => `${population}:${id}`;
const populationOf = (ref) => ref.slice(0, ref.indexOf(":"));

const difference = (a, b) => new Set([...a].filter((x) => !b.has(x)));
const isSubset = (a, b) => [...a].every((x) => b.has(x));
const placeholders = (values) => values.map(() => "?").join(", ");

/* Python 3.12's `sum()` of floats, which is Neumaier-compensated. */
function pySum(values) {
  let total = 0;
  let compensation = 0.0;
  let first = true;
  for (const x of values) {
    if (first) {
      total = 0 + x;
      first = false;
      continue;
    }
    const t = total + x;
    if (Math.abs(total) >= Math.abs(x)) compensation += total - t + x;
    else compensation += x - t + total;
    total = t;
  }
  if (compensation && Number.isFinite(compensation)) total += compensation;
  return total;
}

const zoneOf = (settings) => settings.tz ?? "UTC";

/* pl.session.user_today */
export function userToday(settings) {
  return localDate(now(), zoneOf(settings));
}

/* pl.session.end_of_user_day: the learner's next midnight. */
export function endOfUserDay(settings) {
  return localMidnight(addDays(userToday(settings), 1), zoneOf(settings));
}

/* pl.session.start_of_user_day: the learner's last midnight. */
export function startOfUserDay(settings) {
  return localMidnight(userToday(settings), zoneOf(settings));
}

// ------------------------------------------------------------------ unlocking

/* pl.session._prereqs */
function prereqs(db, nodeId) {
  return db
    .scalars(
      "SELECT node_prereq.prereq_node_id FROM node_prereq WHERE node_prereq.node_id = ? " +
        "ORDER BY node_prereq.prereq_node_id",
      [nodeId]
    )
    .map((id) => db.get("node", id));
}

/* pl.session.is_unlocked: open from the start with no prerequisites, else
 * latched by a row written once and never deleted. */
export function isUnlocked(db, userId, node) {
  if (!prereqs(db, node.id).length) return true;
  return db.get("node_unlock", [userId, node.id]) !== null;
}

/* pl.session._gating_refs: the referents mastery is measured over. */
function gatingRefs(db, node) {
  if (node.type === "grammar") {
    return [
      PATTERN,
      db.scalars("SELECT pattern.id FROM pattern WHERE pattern.node_id = ? ORDER BY pattern.id", [node.id]),
    ];
  }
  if (node.type === "vocabulary") {
    const lexemes = db.scalars(
      "SELECT node_lexeme.lexeme_id FROM node_lexeme WHERE node_lexeme.node_id = ? ORDER BY node_lexeme.lexeme_id",
      [node.id]
    );
    const senses = lexemes.length
      ? db.scalars(
          `SELECT sense.id FROM sense WHERE sense.lexeme_id IN (${placeholders(lexemes)}) ORDER BY sense.id`,
          lexemes
        )
      : [];
    return [LEXICAL, senses];
  }
  return ["", []];
}

/* The learner's card for one referent, or null. */
function cardOf(db, userId, population, refId) {
  return db.first(
    "card",
    `WHERE user_id = ? AND population = ? AND "${REFERENT[population]}" = ? ORDER BY due_at, id`,
    [userId, population, refId]
  );
}

/* pl.session._card_is_mastered: retained for a week, recalled three times, and
 * known for a week — all read from this card's own reviews. */
function cardIsMastered(db, card) {
  if (card === null || card.stability_max < MASTERY_STABILITY_DAYS) return false;
  const [successes, firstSuccess] = db.values(
    "SELECT count(review.id), min(attempt.created_at) FROM review JOIN attempt ON attempt.id = review.attempt_id " +
      "WHERE review.card_id = ? AND review.rating >= ?",
    [card.id, Rating.Good]
  )[0];
  if (successes < MASTERY_MIN_REVIEWS || firstSuccess === null) return false;
  return now() - fromDb(firstSuccess) >= MASTERY_MIN_SPAN_DAYS * DAY;
}

/* pl.session.strata_needed */
export function strataNeeded(n) {
  let byFraction = n;
  for (let k = 0; k <= n; k += 1) {
    if (k / n >= MASTERY_FRACTION) {
      byFraction = k;
      break;
    }
  }
  return Math.max(1, Math.min(byFraction, n - MASTERY_ALLOWED_SHORTFALL));
}

/* pl.session.is_mastered: raises for a node whose gate cannot be evaluated. */
export function isMastered(db, userId, node) {
  if (node.type === "function") {
    return prereqs(db, node.id).every((p) => isMastered(db, userId, p));
  }
  const [population, refIds] = gatingRefs(db, node);
  if (!refIds.length) {
    throw new AssertionError(
      `node '${node.key}' of type '${node.type}' has no gating referents; its unlock condition is undefined`
    );
  }
  let mastered = 0;
  for (const refId of refIds) {
    if (cardIsMastered(db, cardOf(db, userId, population, refId))) mastered += 1;
  }
  return mastered >= strataNeeded(refIds.length);
}

/* pl.session.node_mastery: the node's progress as the display sees it —
 * current retrievability, which decays, beside the gate's own count. */
export function nodeMastery(db, userId, node) {
  if (node.type === "function") {
    const parts = prereqs(db, node.id).map((p) => nodeMastery(db, userId, p));
    const strata = parts.reduce((sum, p) => sum + p.strata, 0);
    return {
      strata,
      started: parts.reduce((sum, p) => sum + p.started, 0),
      mastered: parts.reduce((sum, p) => sum + p.mastered, 0),
      retention: strata ? pySum(parts.map((p) => p.retention * p.strata)) / strata : 0.0,
    };
  }
  const [population, refIds] = gatingRefs(db, node);
  if (!refIds.length) return { strata: 0, started: 0, mastered: 0, retention: 0.0 };

  let started = 0;
  let mastered = 0;
  let retained = 0.0;
  for (const refId of refIds) {
    const card = cardOf(db, userId, population, refId);
    if (card === null) continue;
    started += 1;
    retained += schedule.retrievability(card);
    if (cardIsMastered(db, card)) mastered += 1;
  }
  return { strata: refIds.length, started, mastered, retention: retained / refIds.length };
}

/* pl.session.evaluate_unlocks: latch any node whose prerequisites are all
 * mastered. Run at session end, and commits. */
export function evaluateUnlocks(db, userId) {
  const newly = [];
  for (const node of db.select("node", "ORDER BY node.id")) {
    if (isUnlocked(db, userId, node)) continue;
    const required = prereqs(db, node.id);
    if (required.length && required.every((p) => isUnlocked(db, userId, p) && isMastered(db, userId, p))) {
      db.insert("node_unlock", { user_id: userId, node_id: node.id, unlocked_at: now() });
      newly.push(node.key);
    }
  }
  db.commit();
  return newly;
}

// --------------------------------------------------------------- composition

/* pl.session.settled_today: cards that have had their turn today, whether or
 * not the answer scored them. Read by the composer and the streak alike. */
export function settledToday(db, userId, settings) {
  if (!schedule.ONE_REVIEW_PER_DAY) return new Set();
  const since = startOfUserDay(settings);
  const settled = schedule.cardsAdvancedSince(db, userId, since);
  const answered = db.select(
    "item",
    "WHERE item.id IN (SELECT attempt.item_id FROM attempt WHERE attempt.user_id = ? AND attempt.created_at >= ?) " +
      "ORDER BY item.id",
    [userId, toDb(since)]
  );
  if (!answered.length) return settled;
  const senseByForm = sensesByForm(db);
  const turned = new Set();
  for (const item of answered) {
    for (const ref of itemReferents(db.get("node", item.node_id), item, senseByForm)) turned.add(ref);
  }
  for (const card of db.select("card", "WHERE user_id = ? ORDER BY due_at, id", [userId])) {
    const id = card.sense_id || card.form_id || card.pattern_id;
    if (turned.has(referent(card.population, id))) settled.add(card.id);
  }
  return settled;
}

/* pl.session.known_lexemes: lexemes the learner holds a lexical or morph card for. */
export function knownLexemes(db, userId) {
  const known = new Set(
    db.scalars(
      "SELECT form.lexeme_id FROM form JOIN card ON card.form_id = form.id WHERE card.user_id = ? AND card.population = ?",
      [userId, MORPH]
    )
  );
  for (const id of db.scalars(
    "SELECT sense.lexeme_id FROM sense JOIN card ON card.sense_id = sense.id WHERE card.user_id = ? AND card.population = ?",
    [userId, LEXICAL]
  )) {
    known.add(id);
  }
  return known;
}

/* pl.session._least_practised_first: fewest attempts first, and among equals a
 * starting point that advances with the card's review count. */
function leastPractisedFirst(db, userId, items, rotate = 0) {
  if (items.length < 2) return items;
  const ids = items.map((i) => i.id);
  const counts = new Map(
    db.values(
      `SELECT attempt.item_id, count(attempt.id) FROM attempt WHERE attempt.user_id = ? AND attempt.item_id IN (${placeholders(ids)}) ` +
        "GROUP BY attempt.item_id",
      [userId, ...ids]
    )
  );
  const count = (item) => counts.get(item.id) ?? 0;
  const ordered = [...items].sort((a, b) => count(a) - count(b) || a.id - b.id);
  const fewest = count(ordered[0]);
  let equals = ordered.filter((i) => count(i) === fewest);
  const rest = ordered.filter((i) => count(i) !== fewest);
  if (rotate && equals.length > 1) {
    const offset = rotate % equals.length;
    equals = [...equals.slice(offset), ...equals.slice(0, offset)];
  }
  return [...equals, ...rest];
}

/* pl.session._lexeme_of_item */
function lexemeOfItem(db, item) {
  if (item.target_form_id === null) return null;
  const form = db.get("form", item.target_form_id);
  return form ? form.lexeme_id : null;
}

/* pl.session._items_for_card: the items that could serve one due card. `known`
 * restricts a pattern card to lexemes the learner has met; null does not. */
function itemsForCard(db, card, known = null) {
  let items;
  if (card.population === MORPH) {
    items = db.select("item", "WHERE item.target_form_id = ? ORDER BY item.id", [card.form_id]);
  } else if (card.population === PATTERN) {
    items = db.select("item", "WHERE item.pattern_id = ? ORDER BY item.id", [card.pattern_id]);
    if (known !== null) items = items.filter((i) => known.has(lexemeOfItem(db, i)));
  } else {
    const sense = db.get("sense", card.sense_id);
    items = db
      .rows(
        "item",
        "SELECT item.* FROM item JOIN node ON node.id = item.node_id " +
          "WHERE node.type = ? AND item.pattern_id IS NULL ORDER BY item.id",
        ["vocabulary"]
      )
      .filter((i) => lexemeOfItem(db, i) === sense.lexeme_id);
  }
  return leastPractisedFirst(db, card.user_id, items, card.reps);
}

/* pl.session._started_referents: every referent the learner holds a card for. */
function startedReferents(db, userId) {
  const started = new Set();
  for (const card of db.select("card", "WHERE user_id = ? ORDER BY due_at, id", [userId])) {
    const id = card.sense_id || card.form_id || card.pattern_id;
    if (id !== null) started.add(referent(card.population, id));
  }
  return started;
}

/* pl.session._sense_by_form: form id -> the sense of its lexeme. Where a lexeme
 * has several senses the last the join yields wins, as in the Python's dict. */
function sensesByForm(db) {
  const out = new Map();
  for (const [formId, senseId] of db.values(
    "SELECT form.id, sense.id FROM form JOIN sense ON sense.lexeme_id = form.lexeme_id " +
      "ORDER BY sense.id, form.surface, form.id"
  )) {
    out.set(formId, senseId);
  }
  return out;
}

/* pl.session._forms_of_taught_words: every form of a word a vocabulary node teaches. */
function formsOfTaughtWords(db) {
  return new Set(
    db.scalars(
      "SELECT form.id FROM form JOIN node_lexeme ON node_lexeme.lexeme_id = form.lexeme_id " +
        "JOIN node ON node.id = node_lexeme.node_id WHERE node.type = ?",
      ["vocabulary"]
    )
  );
}

/* pl.session._item_referents: the cards this item would score, created or not. */
export function itemReferents(node, item, senseByForm) {
  const allowed = populationsFor(node);
  const refs = new Set();
  if (allowed.has(MORPH) && item.target_form_id !== null) refs.add(referent(MORPH, item.target_form_id));
  if (allowed.has(PATTERN) && item.pattern_id !== null) refs.add(referent(PATTERN, item.pattern_id));
  if (allowed.has(LEXICAL) && item.target_form_id !== null) {
    const senseId = senseByForm.get(item.target_form_id);
    if (senseId !== undefined) refs.add(referent(LEXICAL, senseId));
  }
  return refs;
}

/* pl.session._extra_round_review: what an extra round reviews, best first,
 * with the cards each item scores. Only items whose every card the learner
 * holds; first those still scored, then the weakest node's. */
function extraRoundReview(db, userId, settings, started, senseByForm) {
  const at = now();
  const moved = schedule.cardsAdvancedSince(db, userId, at - schedule.EARLY_REVIEW_COOLDOWN_DAYS * DAY);
  for (const id of settledToday(db, userId, settings)) moved.add(id);
  const cardId = new Map();
  for (const card of db.select("card", "WHERE user_id = ? ORDER BY due_at, id", [userId])) {
    cardId.set(referent(card.population, card[REFERENT[card.population]]), card.id);
  }
  const weak = weakestNode(db, userId);
  const nodes = new Map(db.select("node", "ORDER BY node.id").map((n) => [n.id, n]));

  const ranked = [];
  for (const item of db.select("item", "ORDER BY item.id")) {
    const refs = itemReferents(nodes.get(item.node_id), item, senseByForm);
    if (!refs.size || difference(refs, started).size) continue;
    const counts = [...refs].some((ref) => cardId.has(ref) && !moved.has(cardId.get(ref)));
    const inWeakest = weak !== null && item.node_id === weak.id;
    ranked.push([!counts, !inWeakest, item, refs]);
  }
  ranked.sort((a, b) => Number(a[0]) - Number(b[0]) || Number(a[1]) - Number(b[1]));
  return ranked.map(([, , item, refs]) => [item, refs]);
}

/* pl.session.weakest_node: the highest error rate over the trailing window;
 * the first of equals, in node order. */
export function weakestNode(db, userId, days = 14) {
  const since = toDb(now() - days * DAY);
  const rows = db.values(
    "SELECT error_event.node_id, count(error_event.id) FROM error_event JOIN attempt ON attempt.id = error_event.attempt_id " +
      "WHERE attempt.user_id = ? AND attempt.created_at >= ? GROUP BY error_event.node_id ORDER BY error_event.node_id",
    [userId, since]
  );
  if (!rows.length) return null;

  let best = null;
  let bestRate = 0.0;
  for (const [nodeId, errors] of rows) {
    const total = db.scalar(
      "SELECT count(*) FROM attempt JOIN item ON item.id = attempt.item_id " +
        "WHERE attempt.user_id = ? AND attempt.created_at >= ? AND item.node_id = ?",
      [userId, since, nodeId]
    );
    const rate = total ? errors / total : 0.0;
    if (rate > bestRate) {
      best = nodeId;
      bestRate = rate;
    }
  }
  return best ? db.get("node", best) : null;
}

/* pl.session.offerable: a listening item needs a voice to be answerable. */
export function offerable(item) {
  return !AUDIBLE.has(item.exercise_type) || audioAvailable();
}

/* pl.session._introduction_budget: new cards today still allows, counted from
 * the cards themselves. */
function introductionBudget(db, userId, settings) {
  const introduced = db.scalar("SELECT count(*) FROM card WHERE card.user_id = ? AND card.created_at >= ?", [
    userId,
    toDb(startOfUserDay(settings)),
  ]);
  return Math.max(0, DAILY_NEW_CAP - (introduced || 0));
}

/* pl.session.build_session: compose the day's queue — debt, then new, then
 * remediation, served debt, remediation, new. Returns [items, stats]. */
export function buildSession(db, userId, settings, limit = 20, extra = false) {
  const horizon = endOfUserDay(settings);
  const debtItems = [];
  let debtIntroduced = 0;
  const remedial = [];
  const newItems = [];
  const seen = new Set();

  const total = () => debtItems.length + remedial.length + newItems.length;
  const add = (bucket, item) => {
    if (seen.has(item.id) || total() >= limit) return false;
    seen.add(item.id);
    bucket.push(item);
    return true;
  };

  let budget = extra ? EXTRA_ROUND_NEW : introductionBudget(db, userId, settings);
  const started = startedReferents(db, userId);
  const senseByForm = sensesByForm(db);
  const taughtWords = formsOfTaughtWords(db);
  const gated = concepts.gatedNodeIds(db, userId);
  const referentsOf = (item) => itemReferents(db.get("node", item.node_id), item, senseByForm);

  // 1 — debt: every due card that has not had its turn today, served by one
  // item, preferring one that introduces nothing.
  const settled = settledToday(db, userId, settings);
  const due = db
    .select("card", "WHERE card.user_id = ? AND card.due_at <= ? ORDER BY card.due_at, card.id", [
      userId,
      toDb(horizon),
    ])
    .filter((card) => !settled.has(card.id));
  const known = knownLexemes(db, userId);
  for (const card of due) {
    const candidates = itemsForCard(db, card, known)
      .filter(offerable)
      .map((item) => [difference(referentsOf(item), started).size > 0, item])
      .sort((a, b) => Number(a[0]) - Number(b[0]))
      .map(([, item]) => item);
    for (const item of candidates) {
      if (add(debtItems, item)) {
        // Charged if it introduced after all.
        const introducedByDebt = difference(referentsOf(item), started);
        for (const ref of introducedByDebt) started.add(ref);
        debtIntroduced += introducedByDebt.size;
        break;
      }
    }
  }
  const debtTotal = due.length;
  const debtServed = debtItems.length;

  // 2 — new, while the backlog is small enough to bear it: round-robin across
  // the open nodes from an offset that advances with the learner, openers of
  // unmet strata first.
  let introduced = 0;
  budget = Math.max(0, budget - debtIntroduced);
  if (due.length <= DEBT_TOLERANCE) {
    let nodeItems = db
      .select("node", "ORDER BY node.id")
      .filter((node) => isUnlocked(db, userId, node) && !gated.has(node.id))
      .map((node) => [node, db.select("item", "WHERE item.node_id = ? ORDER BY item.id", [node.id])]);
    if (nodeItems.length) {
      const offset = started.size % nodeItems.length;
      nodeItems = [...nodeItems.slice(offset), ...nodeItems.slice(0, offset)];
    }
    for (const openersOnly of [true, false]) {
      // Each pool is one iterator, shared across rounds, as in the Python.
      let pools = nodeItems.map(([node, items]) => ({ node, items, next: 0 }));
      while (pools.length && introduced < budget && total() < limit) {
        let progressed = false;
        for (const pool of [...pools]) {
          const node = pool.node;
          if (introduced >= budget || total() >= limit) break;
          let exhausted = true;
          while (pool.next < pool.items.length) {
            const item = pool.items[pool.next];
            pool.next += 1;
            if (!offerable(item)) continue;
            const refs = itemReferents(node, item, senseByForm);
            const unmet = difference(refs, started);
            if (openersOnly && ![...unmet].some((ref) => populationOf(ref) === PATTERN)) continue;
            // A word before its endings.
            if (
              node.type !== "vocabulary" &&
              taughtWords.has(item.target_form_id) &&
              !started.has(referent(LEXICAL, senseByForm.get(item.target_form_id) ?? null))
            ) {
              continue;
            }
            // Skip only an item that is entirely old.
            if (refs.size && isSubset(refs, started)) continue;
            const cost = unmet.size || 1;
            if (cost > budget - introduced) continue;
            if (seen.has(item.id)) continue;
            if (add(newItems, item)) {
              for (const ref of refs) started.add(ref);
              introduced += cost;
              progressed = true;
            }
            exhausted = false;
            break;
          }
          if (exhausted) pools = pools.filter((p) => p !== pool);
        }
        if (!progressed) break;
      }
    }
  }

  // 3 — remediation, filling what is left. An extra round reviews instead.
  if (extra && total() < limit) {
    const covered = new Set();
    for (const [item, refs] of extraRoundReview(db, userId, settings, started, senseByForm)) {
      if (total() >= limit) break;
      // One item per word.
      if (isSubset(refs, covered) || !offerable(item)) continue;
      if (add(remedial, item)) for (const ref of refs) covered.add(ref);
    }
  } else if (total() < limit) {
    const weak = weakestNode(db, userId);
    if (weak !== null) {
      for (const item of db.select("item", "WHERE item.node_id = ? ORDER BY item.id", [weak.id])) {
        if (!offerable(item)) continue;
        if (total() >= limit) break;
        // Remediation re-drills; it never introduces.
        if (difference(itemReferents(weak, item, senseByForm), started).size) continue;
        add(remedial, item);
      }
    }
  }

  return [
    [...debtItems, ...remedial, ...newItems],
    {
      debt_total: debtTotal,
      debt_served: debtServed,
      introduced,
      introduced_items: newItems.length,
    },
  ];
}
