/* pl.schedule, ported: FSRS scheduling, and the routing table that decides
 * which cards a submission scores.
 *
 * One submission touches several cards, and the error class decides which of
 * them are scored — not whether the answer was "right". A population absent
 * from a routing entry is not scored at all: no review row, due date untouched.
 * The Python module's docstring has the reasoning in full.
 */

import { tokenise } from "./classify.js";
import { DAY, now, toDb } from "./clock.js";
import { ErrorClass, STRICT_ORTHOGRAPHY } from "./domain.js";
import { Card as FsrsCard, Rating, Scheduler } from "./fsrs.js";
import * as morph from "./morph.js";

export { Rating };

/* pl.schedule.SCHEDULER: stock parameters, fuzz on. The parity tests turn the
 * fuzz off, as they do the Python's. */
export const SCHEDULER = new Scheduler();

export const LEXICAL = "lexical";
export const MORPH = "morph";
export const PATTERN = "pattern";

/* pl.schedule.ROUTING: error class -> population -> rating, in the Python's
 * order, which is the order reviews are written in. */
export const ROUTING = new Map([
  [ErrorClass.CORRECT, new Map([[LEXICAL, Rating.Good], [MORPH, Rating.Good], [PATTERN, Rating.Good]])],
  [ErrorClass.ORTHOGRAPHY, new Map([[LEXICAL, Rating.Hard], [MORPH, Rating.Hard], [PATTERN, Rating.Hard]])],
  [ErrorClass.CASE_WRONG, new Map([[PATTERN, Rating.Again]])],
  [ErrorClass.ANIMACY, new Map([[PATTERN, Rating.Again]])],
  [ErrorClass.NUMBER_WRONG, new Map([[PATTERN, Rating.Again]])],
  [ErrorClass.GENDER_AGREEMENT, new Map([[PATTERN, Rating.Again]])],
  [ErrorClass.ASPECT_WRONG, new Map([[PATTERN, Rating.Again]])],
  [ErrorClass.CASE_RIGHT_FORM_WRONG, new Map([[MORPH, Rating.Again], [PATTERN, Rating.Good]])],
  [ErrorClass.LEXICAL, new Map([[LEXICAL, Rating.Again]])],
  [ErrorClass.UNANALYSABLE, new Map([[MORPH, Rating.Again], [PATTERN, Rating.Again]])],
  [ErrorClass.WORD_ORDER, new Map([[PATTERN, Rating.Again]])],
  [ErrorClass.MISSING_CONSTITUENT, new Map([[PATTERN, Rating.Again]])],
]);

/* pl.schedule.populations_for: which populations an item under `node` may score. */
export function populationsFor(node) {
  if (node.type === "vocabulary") return new Set([LEXICAL]);
  if (node.type === "grammar") return new Set([MORPH, PATTERN]);
  return new Set();
}

/* The column a population's card names its referent in. */
export const REFERENT = { [LEXICAL]: "sense_id", [MORPH]: "form_id", [PATTERN]: "pattern_id" };

/* pl.schedule._new_card: `due` is the course's clock, not the library's. */
function newCard(userId, population, referent) {
  const at = now();
  const fresh = new FsrsCard({ due: at });
  return {
    user_id: userId,
    population,
    sense_id: null,
    form_id: null,
    pattern_id: null,
    [REFERENT[population]]: referent,
    fsrs_state_json: fresh.toDict(),
    due_at: fresh.due,
    created_at: at,
    reps: 0,
    lapses: 0,
    stability_max: 0.0,
  };
}

/* pl.schedule.card_for: fetch, or lazily create, the card for one referent. */
export function cardFor(db, userId, population, refId) {
  const column = REFERENT[population];
  const card = db.first(
    "card",
    `WHERE user_id = ? AND population = ? AND "${column}" = ? ORDER BY due_at, id`,
    [userId, population, refId]
  );
  if (card !== null) return card;
  const created = newCard(userId, population, refId);
  created.id = db.insert("card", created);
  return created;
}

/* pl.schedule.cards_for_item: every card this item may score, created if absent. */
export function cardsForItem(db, userId, item) {
  const node = db.get("node", item.node_id);
  const allowed = populationsFor(node);
  const out = new Map();

  if (allowed.has(LEXICAL) && item.target_form_id !== null) {
    const form = db.get("form", item.target_form_id);
    // The Python's `db.scalar` takes the first sense a scan finds.
    const sense = db.first("sense", "WHERE lexeme_id = ? ORDER BY id", [form.lexeme_id]);
    if (sense !== null) out.set(LEXICAL, cardFor(db, userId, LEXICAL, sense.id));
  }
  if (allowed.has(MORPH) && item.target_form_id !== null) {
    out.set(MORPH, cardFor(db, userId, MORPH, item.target_form_id));
  }
  if (allowed.has(PATTERN) && item.pattern_id !== null) {
    out.set(PATTERN, cardFor(db, userId, PATTERN, item.pattern_id));
  }
  return out;
}

/* pl.schedule.retrievability: the card's predicted recall, decaying with time. */
export function retrievability(card, at = null) {
  const state = FsrsCard.fromDict(card.fsrs_state_json);
  return SCHEDULER.getCardRetrievability(state, at ?? now());
}

/* pl.schedule.apply_rating: advance one card's schedule and record the review. */
export function applyRating(db, card, rating, attemptId) {
  const state = FsrsCard.fromDict(card.fsrs_state_json);
  const updated = SCHEDULER.reviewCard(state, rating, now());

  card.fsrs_state_json = updated.toDict();
  card.due_at = updated.due;
  card.reps += 1;
  if (rating === Rating.Again) card.lapses += 1;
  // `stability` is null throughout the Learning state.
  card.stability_max = Math.max(card.stability_max || 0.0, updated.stability || 0.0);
  db.update("card", card.id, {
    fsrs_state_json: card.fsrs_state_json,
    due_at: card.due_at,
    reps: card.reps,
    lapses: card.lapses,
    stability_max: card.stability_max,
  });

  db.insert("review", { attempt_id: attemptId, card_id: card.id, rating });
}

/* pl.schedule.ratings_for: the routing entry, with dictation's stricter
 * reading of a spelling slip. */
export function ratingsFor(item, errorClass) {
  const ratings = ROUTING.get(errorClass) ?? new Map();
  if (errorClass === ErrorClass.ORTHOGRAPHY && STRICT_ORTHOGRAPHY.has(item.exercise_type)) {
    return new Map([...ratings.keys()].map((population) => [population, Rating.Again]));
  }
  return ratings;
}

/* pl.schedule.ONE_REVIEW_PER_DAY and EARLY_REVIEW_COOLDOWN_DAYS */
export const ONE_REVIEW_PER_DAY = true;
export const EARLY_REVIEW_COOLDOWN_DAYS = 3;

/* pl.schedule.cards_advanced_since: ids of cards whose schedule has moved. */
export function cardsAdvancedSince(db, userId, since) {
  return new Set(
    db.scalars(
      "SELECT review.card_id FROM review JOIN attempt ON attempt.id = review.attempt_id " +
        "WHERE attempt.user_id = ? AND attempt.created_at >= ?",
      [userId, toDb(since)]
    )
  );
}

/* pl.schedule.card_advanced_since: has this card's schedule moved? */
export function cardAdvancedSince(db, card, since) {
  return (
    db.scalar(
      "SELECT review.id FROM review JOIN attempt ON attempt.id = review.attempt_id " +
        "WHERE review.card_id = ? AND attempt.created_at >= ? LIMIT 1",
      [card.id, toDb(since)]
    ) !== null
  );
}


/* pl.schedule.apply_diagnosis: route one diagnosis to the cards it scores.
 * Returns the ratings applied, by population. */
export function applyDiagnosis(db, userId, item, diagnosis, attemptId, dayStart) {
  const cards = cardsForItem(db, userId, item);
  const ratings = ratingsFor(item, diagnosis.errorClass);
  const applied = new Map();

  for (const [population, card] of cards) {
    const rating = ratings.get(population);
    if (rating === undefined) continue; // unscored: this card's schedule is untouched
    if (ONE_REVIEW_PER_DAY && cardAdvancedSince(db, card, dayStart)) continue;
    const at = now();
    if (card.due_at > at && cardAdvancedSince(db, card, at - EARLY_REVIEW_COOLDOWN_DAYS * DAY)) {
      continue;
    }
    applyRating(db, card, rating, attemptId);
    applied.set(population, rating);
  }

  if (diagnosis.errorClass !== ErrorClass.CORRECT) {
    db.insert("error_event", {
      attempt_id: attemptId,
      node_id: item.node_id,
      error_class: diagnosis.errorClass,
      slot_index: 0,
      expected: item.expected_answer,
    });
    queueForPromotion(db, item, diagnosis.errorClass, db.get("attempt", attemptId).submitted);
  }
  return applied;
}

/* pl.schedule.PROMOTION_CANDIDATES */
export const PROMOTION_CANDIDATES = new Set([ErrorClass.WORD_ORDER]);

/* pl.schedule.queue_for_promotion: keep a failed answer that may be good Polish,
 * for the owner to judge, once per item and normalised. On a phone it stays in
 * `item_variant` until the next content update replaces that table. */
export function queueForPromotion(db, item, errorClass, answer) {
  if (!PROMOTION_CANDIDATES.has(errorClass)) return;
  const tokens = tokenise(answer);
  if (!tokens.length || !tokens.every((token) => morph.isKnown(token))) return;
  const normalised = tokens.join(" ");
  const already = db.scalar(
    "SELECT item_variant.id FROM item_variant WHERE item_variant.item_id = ? AND item_variant.accepted_answer = ?",
    [item.id, normalised]
  );
  if (already === null) {
    db.insert("item_variant", { item_id: item.id, accepted_answer: normalised, source: "queued" });
  }
}
