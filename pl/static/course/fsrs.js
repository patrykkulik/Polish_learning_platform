/* The part of `fsrs` 6.3.2 (py-fsrs) that pl.schedule uses, ported from
 * fsrs/scheduler.py and fsrs/card.py: the Scheduler's review_card and
 * get_card_retrievability, Card with to_dict and from_dict, Rating and State.
 * Not ported: parameter validation, reschedule_card, review logs and the
 * optimizer.
 *
 * Ported rather than taken from ts-fsrs, which schedules differently: it counts
 * elapsed time in UTC calendar days where fsrs counts whole 24-hour periods,
 * and it rounds stability, difficulty and retrievability where fsrs rounds only
 * intervals. The parity tests hold this module to fsrs 6.3.2.
 *
 * Each formula keeps the library's order of operations, so results agree with
 * the Python to the last bit or two: `math.e ** x` stays `Math.E ** x` rather
 * than `Math.exp(x)`. `round()` is Python's, halves to the even neighbour.
 * Datetimes and durations are clock.js microseconds.
 *
 * MIT License
 *
 * Copyright (c) 2022 Open Spaced Repetition
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in all
 * copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 * SOFTWARE.
 */

import { DAY, MINUTE, days, fromisoformat, isoformat } from "./clock.js";

/* fsrs.Rating */
export const Rating = Object.freeze({ Again: 1, Hard: 2, Good: 3, Easy: 4 });

/* fsrs.State */
export const State = Object.freeze({ Learning: 1, Review: 2, Relearning: 3 });

const RECALLED = new Set([Rating.Hard, Rating.Good, Rating.Easy]);

/* fsrs.scheduler.DEFAULT_PARAMETERS and the constants the port uses. */
const FSRS_DEFAULT_DECAY = 0.1542;
export const DEFAULT_PARAMETERS = Object.freeze([
  0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835,
  0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, FSRS_DEFAULT_DECAY,
]);
const STABILITY_MIN = 0.001;
const MIN_DIFFICULTY = 1.0;
const MAX_DIFFICULTY = 10.0;
const FUZZ_RANGES = [
  { start: 2.5, end: 7.0, factor: 0.15 },
  { start: 7.0, end: 20.0, factor: 0.1 },
  { start: 20.0, end: Infinity, factor: 0.05 },
];

/* Python's `round(x)`: to an integer, with halves to the even neighbour. */
export function roundHalfEven(x) {
  const floor = Math.floor(x);
  const fraction = x - floor;
  if (fraction > 0.5) return floor + 1;
  if (fraction < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/* fsrs.Card. `cardId` is the library's epoch milliseconds at creation, read
 * from the real clock; nothing in the course reads it. */
export class Card {
  constructor({
    cardId = null,
    state = State.Learning,
    step = null,
    stability = null,
    difficulty = null,
    due = null,
    lastReview = null,
  } = {}) {
    this.cardId = cardId ?? Date.now();
    this.state = state;
    this.step = state === State.Learning && step === null ? 0 : step;
    this.stability = stability;
    this.difficulty = difficulty;
    this.due = due ?? Date.now() * 1000;
    this.lastReview = lastReview;
  }

  /* Card.to_dict */
  toDict() {
    return {
      card_id: this.cardId,
      state: this.state,
      step: this.step,
      stability: this.stability,
      difficulty: this.difficulty,
      due: isoformat(this.due),
      last_review: this.lastReview !== null ? isoformat(this.lastReview) : null,
    };
  }

  /* Card.from_dict */
  static fromDict(source) {
    const state = Number(source.state);
    if (!Object.values(State).includes(state)) throw new Error(`${source.state} is not a valid State`);
    return new Card({
      cardId: Math.trunc(Number(source.card_id)),
      state,
      step: source.step,
      stability: source.stability ? Number(source.stability) : null,
      difficulty: source.difficulty ? Number(source.difficulty) : null,
      due: fromisoformat(source.due),
      lastReview: source.last_review ? fromisoformat(source.last_review) : null,
    });
  }
}

/* fsrs.Scheduler */
export class Scheduler {
  constructor({
    parameters = DEFAULT_PARAMETERS,
    desiredRetention = 0.9,
    learningSteps = [1 * MINUTE, 10 * MINUTE],
    relearningSteps = [10 * MINUTE],
    maximumInterval = 36500,
    enableFuzzing = true,
    random = Math.random,
  } = {}) {
    this.parameters = [...parameters];
    this.desiredRetention = desiredRetention;
    this.learningSteps = [...learningSteps];
    this.relearningSteps = [...relearningSteps];
    this.maximumInterval = maximumInterval;
    this.enableFuzzing = enableFuzzing;
    this.random = random;
    this.DECAY = -this.parameters[20];
    this.FACTOR = 0.9 ** (1 / this.DECAY) - 1;
  }

  /* Scheduler.get_card_retrievability */
  getCardRetrievability(card, currentDatetime = null) {
    if (card.lastReview === null || card.stability === null) return 0;
    const current = currentDatetime ?? Date.now() * 1000;
    const elapsedDays = Math.max(0, days(current - card.lastReview));
    return (1 + (this.FACTOR * elapsedDays) / card.stability) ** this.DECAY;
  }

  /* Scheduler.review_card, returning the card without a review log. */
  reviewCard(original, rating, reviewDatetime = null) {
    const card = Object.assign(Object.create(Card.prototype), original);
    const reviewed = reviewDatetime ?? Date.now() * 1000;
    const daysSinceLastReview = card.lastReview !== null ? days(reviewed - card.lastReview) : null;
    let nextInterval;

    switch (card.state) {
      case State.Learning:
        if (card.stability === null || card.difficulty === null) {
          card.stability = this._initialStability(rating);
          card.difficulty = this._initialDifficulty(rating, true);
        } else if (daysSinceLastReview !== null && daysSinceLastReview < 1) {
          card.stability = this._shortTermStability(card.stability, rating);
          card.difficulty = this._nextDifficulty(card.difficulty, rating);
        } else {
          card.stability = this._nextStability(
            card.difficulty,
            card.stability,
            this.getCardRetrievability(card, reviewed),
            rating
          );
          card.difficulty = this._nextDifficulty(card.difficulty, rating);
        }

        // The first clause covers a card scheduled with more learning steps
        // than this scheduler has.
        if (
          this.learningSteps.length === 0 ||
          (card.step >= this.learningSteps.length && RECALLED.has(rating))
        ) {
          card.state = State.Review;
          card.step = null;
          nextInterval = this._nextInterval(card.stability) * DAY;
        } else {
          switch (rating) {
            case Rating.Again:
              card.step = 0;
              nextInterval = this.learningSteps[card.step];
              break;
            case Rating.Hard:
              // The step stays the same.
              if (card.step === 0 && this.learningSteps.length === 1) {
                nextInterval = roundHalfEven(this.learningSteps[0] * 1.5);
              } else if (card.step === 0 && this.learningSteps.length >= 2) {
                nextInterval = roundHalfEven((this.learningSteps[0] + this.learningSteps[1]) / 2.0);
              } else {
                nextInterval = this.learningSteps[card.step];
              }
              break;
            case Rating.Good:
              if (card.step + 1 === this.learningSteps.length) {
                card.state = State.Review;
                card.step = null;
                nextInterval = this._nextInterval(card.stability) * DAY;
              } else {
                card.step += 1;
                nextInterval = this.learningSteps[card.step];
              }
              break;
            case Rating.Easy:
              card.state = State.Review;
              card.step = null;
              nextInterval = this._nextInterval(card.stability) * DAY;
              break;
            default:
              throw new Error(`Unknown rating: ${rating}`);
          }
        }
        break;

      case State.Review:
        if (daysSinceLastReview !== null && daysSinceLastReview < 1) {
          card.stability = this._shortTermStability(card.stability, rating);
        } else {
          card.stability = this._nextStability(
            card.difficulty,
            card.stability,
            this.getCardRetrievability(card, reviewed),
            rating
          );
        }
        card.difficulty = this._nextDifficulty(card.difficulty, rating);

        switch (rating) {
          case Rating.Again:
            if (this.relearningSteps.length === 0) {
              nextInterval = this._nextInterval(card.stability) * DAY;
            } else {
              card.state = State.Relearning;
              card.step = 0;
              nextInterval = this.relearningSteps[card.step];
            }
            break;
          case Rating.Hard:
          case Rating.Good:
          case Rating.Easy:
            nextInterval = this._nextInterval(card.stability) * DAY;
            break;
          default:
            throw new Error(`Unknown rating: ${rating}`);
        }
        break;

      case State.Relearning:
        if (daysSinceLastReview !== null && daysSinceLastReview < 1) {
          card.stability = this._shortTermStability(card.stability, rating);
          card.difficulty = this._nextDifficulty(card.difficulty, rating);
        } else {
          card.stability = this._nextStability(
            card.difficulty,
            card.stability,
            this.getCardRetrievability(card, reviewed),
            rating
          );
          card.difficulty = this._nextDifficulty(card.difficulty, rating);
        }

        if (
          this.relearningSteps.length === 0 ||
          (card.step >= this.relearningSteps.length && RECALLED.has(rating))
        ) {
          card.state = State.Review;
          card.step = null;
          nextInterval = this._nextInterval(card.stability) * DAY;
        } else {
          switch (rating) {
            case Rating.Again:
              card.step = 0;
              nextInterval = this.relearningSteps[card.step];
              break;
            case Rating.Hard:
              if (card.step === 0 && this.relearningSteps.length === 1) {
                nextInterval = roundHalfEven(this.relearningSteps[0] * 1.5);
              } else if (card.step === 0 && this.relearningSteps.length >= 2) {
                nextInterval = roundHalfEven((this.relearningSteps[0] + this.relearningSteps[1]) / 2.0);
              } else {
                nextInterval = this.relearningSteps[card.step];
              }
              break;
            case Rating.Good:
              if (card.step + 1 === this.relearningSteps.length) {
                card.state = State.Review;
                card.step = null;
                nextInterval = this._nextInterval(card.stability) * DAY;
              } else {
                card.step += 1;
                nextInterval = this.relearningSteps[card.step];
              }
              break;
            case Rating.Easy:
              card.state = State.Review;
              card.step = null;
              nextInterval = this._nextInterval(card.stability) * DAY;
              break;
            default:
              throw new Error(`Unknown rating: ${rating}`);
          }
        }
        break;

      default:
        throw new Error(`Unknown card state: ${card.state}`);
    }

    if (this.enableFuzzing && card.state === State.Review) {
      nextInterval = this._getFuzzedInterval(nextInterval);
    }

    card.due = reviewed + nextInterval;
    card.lastReview = reviewed;
    return card;
  }

  /* Scheduler._clamp_difficulty */
  _clampDifficulty(difficulty) {
    return Math.min(Math.max(difficulty, MIN_DIFFICULTY), MAX_DIFFICULTY);
  }

  /* Scheduler._clamp_stability */
  _clampStability(stability) {
    return Math.max(stability, STABILITY_MIN);
  }

  /* Scheduler._initial_stability */
  _initialStability(rating) {
    return this._clampStability(this.parameters[rating - 1]);
  }

  /* Scheduler._initial_difficulty */
  _initialDifficulty(rating, clamp) {
    const difficulty = this.parameters[4] - Math.E ** (this.parameters[5] * (rating - 1)) + 1;
    return clamp ? this._clampDifficulty(difficulty) : difficulty;
  }

  /* Scheduler._next_interval: whole days, at least one, at most the maximum. */
  _nextInterval(stability) {
    const interval = (stability / this.FACTOR) * (this.desiredRetention ** (1 / this.DECAY) - 1);
    return Math.min(Math.max(roundHalfEven(interval), 1), this.maximumInterval);
  }

  /* Scheduler._short_term_stability */
  _shortTermStability(stability, rating) {
    let increase =
      Math.E ** (this.parameters[17] * (rating - 3 + this.parameters[18])) *
      stability ** -this.parameters[19];
    if (RECALLED.has(rating)) increase = Math.max(increase, 1.0);
    return this._clampStability(stability * increase);
  }

  /* Scheduler._next_difficulty, with its _linear_damping and _mean_reversion. */
  _nextDifficulty(difficulty, rating) {
    const arg1 = this._initialDifficulty(Rating.Easy, false);
    const delta = -(this.parameters[6] * (rating - 3));
    const arg2 = difficulty + ((10.0 - difficulty) * delta) / 9.0;
    const next = this.parameters[7] * arg1 + (1 - this.parameters[7]) * arg2;
    return this._clampDifficulty(next);
  }

  /* Scheduler._next_stability */
  _nextStability(difficulty, stability, retrievability, rating) {
    let next;
    if (rating === Rating.Again) {
      next = this._nextForgetStability(difficulty, stability, retrievability);
    } else if (RECALLED.has(rating)) {
      next = this._nextRecallStability(difficulty, stability, retrievability, rating);
    } else {
      throw new Error(`Unknown rating: ${rating}`);
    }
    return this._clampStability(next);
  }

  /* Scheduler._next_forget_stability */
  _nextForgetStability(difficulty, stability, retrievability) {
    const p = this.parameters;
    const longTerm =
      p[11] *
      difficulty ** -p[12] *
      ((stability + 1) ** p[13] - 1) *
      Math.E ** ((1 - retrievability) * p[14]);
    const shortTerm = stability / Math.E ** (p[17] * p[18]);
    return Math.min(longTerm, shortTerm);
  }

  /* Scheduler._next_recall_stability */
  _nextRecallStability(difficulty, stability, retrievability, rating) {
    const p = this.parameters;
    const hardPenalty = rating === Rating.Hard ? p[15] : 1;
    const easyBonus = rating === Rating.Easy ? p[16] : 1;
    return (
      stability *
      (1 +
        Math.E ** p[8] *
          (11 - difficulty) *
          stability ** -p[9] *
          (Math.E ** ((1 - retrievability) * p[10]) - 1) *
          hardPenalty *
          easyBonus)
    );
  }

  /* Scheduler._get_fuzzed_interval: a whole number of days within the library's
   * range around the interval, drawn from `this.random`. */
  _getFuzzedInterval(interval) {
    const intervalDays = days(interval);
    if (intervalDays < 2.5) return interval;

    let delta = 1.0;
    for (const range of FUZZ_RANGES) {
      delta += range.factor * Math.max(Math.min(intervalDays, range.end) - range.start, 0.0);
    }
    let minIvl = roundHalfEven(intervalDays - delta);
    let maxIvl = roundHalfEven(intervalDays + delta);
    minIvl = Math.max(2, minIvl);
    maxIvl = Math.min(maxIvl, this.maximumInterval);
    minIvl = Math.min(minIvl, maxIvl);

    const fuzzed = this.random() * (maxIvl - minIvl + 1) + minIvl;
    return Math.min(roundHalfEven(fuzzed), this.maximumInterval) * DAY;
  }
}
